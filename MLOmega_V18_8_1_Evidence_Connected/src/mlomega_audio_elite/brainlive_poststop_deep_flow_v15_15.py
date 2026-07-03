from __future__ import annotations

"""V18-gated post-stop orchestration.

The old post-stop flow allowed a partial assembly/deep stack to keep flowing into
longitudinal learning and then the Life Model.  This module is intentionally a
barrier: every materialisation stage has a durable run/stage record and no
later stage can consume a failed, incomplete or cross-session input.
"""

from typing import Any, Callable

from .db import connect, init_db, insert_only, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id
from .config import get_settings
from .runtime_v18_7 import (
    RuntimePolicyError, acquire_execution_lease, classify_failure, gpu_phase,
    heartbeat_execution_lease, phase, record_phase_event, release_live_model_caches,
)
from .governance_v18 import (
    Scope,
    StageGateError,
    assert_live_session_owner,
    begin_or_resume_run,
    ensure_v18_schema,
    finish_stage,
    start_stage,
    recover_stale_stages,
    record_output_manifest,
    assert_cleanup_eligible,
    strict_many,
    strict_one,
    update_run,
    mark_run_retryable,
    record_recovery_state,
)

VERSION = "18.7.1-resumable-post-stop"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_post_stop_deep_flow_runs_v1515(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  live_session_id TEXT,
  service_run_id TEXT,
  assembly_run_id TEXT,
  exported_conversations_json TEXT DEFAULT '[]',
  brain2_processed_conversations_json TEXT DEFAULT '[]',
  v15_result_json TEXT DEFAULT '{}',
  v18_deep_audio_result_json TEXT DEFAULT '{}',
  v16_deep_vision_result_json TEXT DEFAULT '{}',
  v16_silent_life_result_json TEXT DEFAULT '{}',
  v17_longitudinal_result_json TEXT DEFAULT '{}',
  status TEXT NOT NULL,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_post_stop_conversation_runs_v1515(
  row_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  run_id TEXT NOT NULL,
  bundle_id TEXT,
  conversation_id TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_blpost1515_person_date ON brainlive_post_stop_deep_flow_runs_v1515(person_id, package_date, created_at);
CREATE INDEX IF NOT EXISTS idx_blpost1515_conv ON brainlive_post_stop_conversation_runs_v1515(conversation_id, status);
"""


def ensure_post_stop_deep_flow_schema() -> None:
    init_db()
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)
        # Old databases may predate additive result columns.
        cols = {str(r[1]) for r in con.execute("PRAGMA table_info(brainlive_post_stop_deep_flow_runs_v1515)").fetchall()}
        for name in ("v18_deep_audio_result_json", "v16_deep_vision_result_json", "v16_silent_life_result_json", "v17_longitudinal_result_json"):
            if name not in cols:
                con.execute(f"ALTER TABLE brainlive_post_stop_deep_flow_runs_v1515 ADD COLUMN {name} TEXT DEFAULT '{{}}'")


def _rows(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return strict_many(con, sql, params, purpose="post-stop query")


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return strict_one(con, sql, params, purpose="post-stop query")


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _package_day(package_date: str | None) -> str:
    from .brainlive_event_assembler_v15_14 import _period_bounds
    return _period_bounds(package_date)[0]


def _exported_bundle_conversations(person_id: str, package_date: str, *, live_session_id: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    with connect() as con:
        if not (_table_exists(con, "brainlive_event_bundles_v1514") and _table_exists(con, "brainlive_brain2_event_exports_v1514")):
            return []
        params: list[Any] = [person_id, package_date]
        where = "b.person_id=? AND b.package_date=? AND b.status IN ('assembled','active')"
        if live_session_id:
            where += " AND b.live_session_id=?"
            params.append(live_session_id)
        params.append(limit)
        return _rows(con, f"""
            SELECT b.bundle_id,b.live_session_id,b.package_date,b.start_time,b.end_time,
                   b.title,b.brain2_conversation_id,e.conversation_id,e.export_id,e.export_status
            FROM brainlive_event_bundles_v1514 b
            JOIN brainlive_brain2_event_exports_v1514 e ON e.bundle_id=b.bundle_id AND e.person_id=b.person_id
            WHERE {where} AND e.export_status IN ('active','ok','exported')
            ORDER BY b.start_time,b.bundle_id LIMIT ?
        """, tuple(params))


def _conversation_digest(con, conversation_id: str) -> str:
    conv = _one(con, "SELECT conversation_id,raw_json,started_at,ended_at FROM conversations WHERE conversation_id=?", (conversation_id,))
    if not conv:
        raise StageGateError(f"missing exported conversation {conversation_id}")
    turns = _rows(con, "SELECT turn_id,idx,text,metadata_json FROM turns WHERE conversation_id=? ORDER BY idx,turn_id", (conversation_id,))
    return stable_id("convdigest_v18", conversation_id, json_dumps(conv), json_dumps(turns))


def _conversation_already_done(con, conversation_id: str) -> bool:
    digest = _conversation_digest(con, conversation_id)
    rows = _rows(con, "SELECT result_json FROM brainlive_post_stop_conversation_runs_v1515 WHERE conversation_id=? AND status='ok' ORDER BY created_at DESC LIMIT 5", (conversation_id,))
    return any(json_loads(r.get("result_json"), {}).get("conversation_digest") == digest for r in rows)


class SecondaryMemorySyncError(RuntimeError):
    pass


def _sync_secondary_memory_for_conversation(conversation_id: str, *, person_id: str) -> dict[str, Any]:
    """Sync only after a canonical conversation is complete.

    A sync failure is explicit.  It is never converted into an apparently
    successful Life Model input.  The concrete sync workers perform their own
    idempotent source-version/retraction handling in V18.
    """
    out: dict[str, Any] = {"conversation_id": conversation_id, "person_id": person_id, "steps": []}
    failures: list[dict[str, str]] = []
    try:
        from .sync_jobs import schedule_post_ingest_sync
        with connect() as con, write_transaction(con):
            jobs = schedule_post_ingest_sync(con, conversation_id=conversation_id, person_id=person_id)
        out["sync_jobs"] = jobs; out["steps"].append("schedule")
    except Exception as exc:
        failures.append({"step": "schedule", "error": str(exc)[:1000]})
    try:
        from .vector_sync import sync_vectors
        out["vector_sync"] = sync_vectors(conversation_id=conversation_id, person_id=person_id); out["steps"].append("vectors")
    except Exception as exc:
        failures.append({"step": "vectors", "error": str(exc)[:1000]})
    # V18.7 Core deliberately does not initialize or run Graphiti/Mem0.
    # Keep an explicit audit entry rather than silently scheduling legacy
    # projections or making their absence a post-stop failure.
    from .config import get_settings
    cfg = get_settings()
    core_v187 = str(cfg.deployment_profile or "").upper().startswith(("CORE_BRAINLIVE_V18_7", "CORE_BRAINLIVE_V18_8"))
    graph_enabled = str(cfg.graph_backend or "").lower() not in {"", "disabled", "none", "off"}
    if not core_v187 and (graph_enabled or cfg.mem0_enabled):
        try:
            from .external_memory import sync_external_all
            out["external_memory"] = sync_external_all(conversation_id, person_id=person_id); out["steps"].append("external")
        except Exception as exc:
            failures.append({"step": "external", "error": str(exc)[:1000]})
    else:
        out["external_memory"] = {"status": "not_configured_core_profile", "backends": []}
    if failures:
        out.update(status="error", failures=failures)
        raise SecondaryMemorySyncError(json_dumps(out))
    out["status"] = "ok"
    return out


def _legacy_run_row(*, run_id: str, person_id: str, day: str, live_session_id: str | None, service_run_id: str | None,
                    assembly: dict[str, Any] | None, exported: list[dict[str, Any]], processed: list[dict[str, Any]],
                    v15: dict[str, Any] | None, deep_audio: dict[str, Any] | None, deep: dict[str, Any] | None, silent: dict[str, Any] | None,
                    longitudinal: dict[str, Any] | None, status: str, error: str | None, created_at: str) -> None:
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO brainlive_post_stop_deep_flow_runs_v1515(
                 run_id,person_id,package_date,live_session_id,service_run_id,assembly_run_id,
                 exported_conversations_json,brain2_processed_conversations_json,v15_result_json,
                 v18_deep_audio_result_json,v16_deep_vision_result_json,v16_silent_life_result_json,v17_longitudinal_result_json,
                 status,error_text,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET exported_conversations_json=excluded.exported_conversations_json,
                 brain2_processed_conversations_json=excluded.brain2_processed_conversations_json,
                 v15_result_json=excluded.v15_result_json,v18_deep_audio_result_json=excluded.v18_deep_audio_result_json,
                 v16_deep_vision_result_json=excluded.v16_deep_vision_result_json,
                 v16_silent_life_result_json=excluded.v16_silent_life_result_json,
                 v17_longitudinal_result_json=excluded.v17_longitudinal_result_json,status=excluded.status,
                 error_text=excluded.error_text,updated_at=excluded.updated_at""",
            (run_id,person_id,day,live_session_id,service_run_id,(assembly or {}).get("run_id"),
             json_dumps(exported),json_dumps(processed),json_dumps(v15 or {}),json_dumps(deep_audio or {}),json_dumps(deep or {}),
             json_dumps(silent or {}),json_dumps(longitudinal or {}),status,error,created_at,now_iso()),
        )


def _poststop_stage_success(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return str(result.get("status") or "ok").lower() in {"ok", "completed", "skipped", "skipped_llm_disabled", "deferred_session_scope"}


def _stage_failure_from_result(name: str, result: Any) -> RuntimePolicyError:
    status = str((result or {}).get("status") or "blocked").lower() if isinstance(result, dict) else "blocked"
    retryable = status in {"retryable_error", "retryable", "pending_retry"}
    return RuntimePolicyError(
        f"{name} returned {status}",
        code=f"{name}_{'retryable' if retryable else 'blocked'}",
        retryable=retryable,
    )


def run_brainlive_post_stop_deep_flow(
    *,
    person_id: str | None = None,
    live_session_id: str | None = None,
    service_run_id: str | None = None,
    package_date: str | None = None,
    limit_per_table: int = 5000,
    gap_minutes: int = 20,
    force: bool = False,
    run_brain2: bool = True,
    run_v15: bool = True,
    use_llm: bool = True,
    run_silent_life: bool = True,
    silent_life_timeout: float | None = None,
    run_deep_audio: bool = True,
    deep_audio_language: str = "fr",
    deep_audio_max_bundle_seconds: float | None = None,
    run_deep_vision: bool = True,
    deep_vision_model: str | None = None,
    deep_vision_timeout_per_image: float | None = None,
    deep_vision_max_keyframes_per_bundle: int = 12,
) -> dict[str, Any]:
    """Execute or resume the canonical post-stop run.

    Every material phase is checkpointed.  A timeout, temporary Ollama outage,
    SQLite lock or power loss leaves the same logical run in ``retryable_error``
    and never permits cleanup.  Calling this function again resumes completed
    stages verbatim and retries only the failed unit.
    """
    if not person_id:
        raise StageGateError("V18.7 post-stop requires explicit person_id")
    cfg = get_settings()
    ensure_post_stop_deep_flow_schema()
    day = _package_day(package_date)
    deep_audio_max_bundle_seconds = float(deep_audio_max_bundle_seconds or cfg.deep_audio_bundle_max_seconds)
    deep_vision_timeout_per_image = float(deep_vision_timeout_per_image or cfg.poststop_vlm_timeout_s)
    silent_life_timeout = float(silent_life_timeout or cfg.poststop_llm_timeout_s)
    scope = Scope(person_id=person_id, live_session_id=live_session_id, mode="post_stop")
    if live_session_id:
        with connect() as con:
            assert_live_session_owner(con, live_session_id=live_session_id, person_id=person_id)
    input_manifest = {
        "release": VERSION,
        "day": day,
        # service_run_id identifies the particular supervising process; it is
        # intentionally not a semantic input. A crash/restart must resume the
        # same day/session rather than invalidating all completed artefacts.
        "limits": {"per_table": int(limit_per_table), "gap_minutes": int(gap_minutes)},
        "deep_audio": {"enabled": bool(run_deep_audio), "language": deep_audio_language, "max_seconds": deep_audio_max_bundle_seconds,
                       "model": cfg.whisperx_model, "device": cfg.whisperx_device, "compute": cfg.whisperx_compute_type},
        "deep_vision": {"enabled": bool(run_deep_vision), "model": deep_vision_model or None},
        "brain2": {"enabled": bool(run_brain2), "use_llm": bool(use_llm), "model": cfg.ollama_model},
    }
    idempotency_key = stable_id("poststop_v18_session", person_id, live_session_id or "day", day)
    run_id, resumed = begin_or_resume_run(
        pipeline_name="brainlive_post_stop", scope=scope, input_manifest=input_manifest,
        idempotency_key=idempotency_key, force_resume=bool(force),
        profile_invalidation_map={
            "release": ("*",),
            "day": ("*",),
            "limits": ("assembly", "deep_audio", "deep_vision", "silent_life", "brain2", "longitudinal", "life_model"),
            "deep_audio": ("deep_audio", "deep_vision", "silent_life", "brain2", "longitudinal", "life_model"),
            "deep_vision": ("deep_vision", "silent_life", "brain2", "longitudinal", "life_model"),
            "brain2": ("brain2", "longitudinal", "life_model"),
        },
    )
    execution_lease = acquire_execution_lease(run_id=run_id, purpose="brainlive_post_stop")
    if not execution_lease.acquired:
        return {
            "version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day,
            "live_session_id": live_session_id, "service_run_id": service_run_id,
            "status": "in_progress", "resumed": resumed,
            "lease_owner_pid": execution_lease.owner_pid, "lease_owner_host": execution_lease.owner_host,
        }
    # A retained lease whose owner PID is gone is a proven crash boundary.  It is
    # safe to recover its running stage immediately; otherwise preserve the
    # configured stale grace period for legacy/non-leased executions.
    try:
        recovery = recover_stale_stages(
            run_id=run_id,
            stale_after_seconds=0 if (force or execution_lease.reclaimed) else cfg.stage_stale_after_s,
            reason="post_stop_resume_v18_7",
        )
    except Exception:
        execution_lease.release()
        raise
    # Never steal a fresh running stage from a legacy execution that did not own
    # an execution lease.  A current V18.7 worker is protected by the lease
    # above and returned before this point.
    with connect() as con:
        fresh_running = _rows(
            con,
            "SELECT stage_name,started_at FROM v18_pipeline_stages WHERE run_id=? AND status='running'",
            (run_id,),
        )
    if fresh_running:
        execution_lease.release()
        return {
            "version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day,
            "live_session_id": live_session_id, "service_run_id": service_run_id,
            "status": "in_progress", "resumed": resumed, "recovery": recovery,
            "running_stages": [str(row["stage_name"]) for row in fresh_running],
        }
    with connect() as con:
        existing_run = _one(con, "SELECT started_at FROM v18_pipeline_runs WHERE run_id=?", (run_id,))
    created_at = str((existing_run or {}).get("started_at") or now_iso())
    assembly: dict[str, Any] | None = None
    deep_audio: dict[str, Any] | None = {"status": "skipped"}
    deep: dict[str, Any] | None = {"status": "skipped"}
    silent: dict[str, Any] | None = {"status": "skipped"}
    longitudinal: dict[str, Any] | None = {"status": "skipped"}
    life: dict[str, Any] | None = {"status": "skipped"}
    exported: list[dict[str, Any]] = []
    processed: list[dict[str, Any]] = []
    error: str | None = None
    status = "blocked"

    def stage(name: str, fn: Callable[[], dict[str, Any]], *, required: bool = True) -> dict[str, Any]:
        """Run one durable stage or return its verified checkpoint."""
        with connect() as con, write_transaction(con):
            existing = _one(con, "SELECT status,result_json FROM v18_pipeline_stages WHERE run_id=? AND stage_name=?", (run_id, name))
            if existing and existing.get("status") == "completed":
                cached = json_loads(existing.get("result_json"), {}) or {}
                if not isinstance(cached, dict):
                    raise StageGateError(f"completed {name} checkpoint is malformed")
                return {**cached, "resumed_stage": True}
            start_stage(con, run_id=run_id, stage_name=name, required=required, input_payload=input_manifest)
        record_recovery_state(run_id=run_id, state="running", stage_name=name)
        heartbeat_execution_lease(execution_lease)
        record_phase_event("post_stop_stage_started", run_id=run_id, stage=name)
        try:
            # Every post-stop stage, including silent-life and Brain2-adjacent
            # operations, receives the long local-Ollama timeout floor and the
            # common retry/keep-alive policy.  Heavy GPU stages still establish
            # their own nested gpu_phase for explicit VRAM release.
            with phase(f"post_stop_{name}"):
                result = fn()
            if not _poststop_stage_success(result):
                raise _stage_failure_from_result(name, result)
        except Exception as exc:
            failure = classify_failure(exc)
            with connect() as con, write_transaction(con):
                finish_stage(
                    con, run_id=run_id, stage_name=name,
                    result={"status": "error", "error_code": failure.code},
                    status="retryable_error" if failure.retryable else "blocked",
                    error_text=str(exc)[:2000],
                )
            if failure.retryable:
                delay = int(cfg.poststop_retry_backoff_seconds[-1]) if cfg.poststop_retry_backoff_seconds else 0
                mark_run_retryable(run_id=run_id, stage_name=name, error_code=failure.code, error_text=str(exc), retry_after_seconds=delay)
            else:
                record_recovery_state(run_id=run_id, state="blocked", stage_name=name, error_code=failure.code, error_text=str(exc))
            record_phase_event("post_stop_stage_failed", run_id=run_id, stage=name, error_code=failure.code, retryable=failure.retryable)
            raise
        with connect() as con, write_transaction(con):
            finish_stage(con, run_id=run_id, stage_name=name, result=result, status="completed")
        record_recovery_state(run_id=run_id, state="running", stage_name=name)
        heartbeat_execution_lease(execution_lease)
        record_phase_event("post_stop_stage_completed", run_id=run_id, stage=name)
        return result

    try:
        from .brainlive_event_assembler_v15_14 import run_brainlive_event_assembly
        assembly = stage("assembly", lambda: run_brainlive_event_assembly(
            person_id=person_id, package_date=day, export_to_brain2=True, limit_per_table=limit_per_table,
            gap_minutes=gap_minutes, live_session_id=live_session_id,
        ))
        if bool(assembly.get("incomplete")):
            raise StageGateError(f"assembly is incomplete: {assembly.get('incomplete_reasons') or 'unknown reason'}")
        exported = _exported_bundle_conversations(person_id, day, live_session_id=live_session_id)
        if int(assembly.get("bundles", 0)) != len(exported):
            raise StageGateError("assembly/export cardinality mismatch")
        if int(assembly.get("raw_rows", 0) or 0) > 0 and not exported:
            raise StageGateError("assembly has raw evidence but no retained Brain2 export")

        if run_deep_audio:
            # The live service and deep audio run in the same process after
            # stop. Release real-time-only models before loading WhisperX
            # large-v3 so an 8–12 GB GPU does not carry both stacks.
            release_live_model_caches()
            from .brainlive_offline_deep_audio_v18_5 import run_offline_deep_audio_for_bundles
            deep_audio = stage("deep_audio", lambda: run_offline_deep_audio_for_bundles(
                person_id=person_id, package_date=day, live_session_id=live_session_id,
                language=deep_audio_language, max_bundle_audio_seconds=deep_audio_max_bundle_seconds,
            ))
            exported = _exported_bundle_conversations(person_id, day, live_session_id=live_session_id)
            if int(assembly.get("bundles", 0)) != len(exported):
                raise StageGateError("deep-audio export cardinality mismatch")
        else:
            from .brainlive_offline_deep_audio_v18_5 import bundles_require_deep_audio
            required_audio = bundles_require_deep_audio(person_id=person_id, package_date=day, live_session_id=live_session_id)
            deep_audio = {"status": "skipped_requires_retention" if required_audio else "skipped_no_audio", "cleanup_blocked": bool(required_audio)}
            with connect() as con, write_transaction(con):
                start_stage(con, run_id=run_id, stage_name="deep_audio", required=bool(required_audio))
                finish_stage(con, run_id=run_id, stage_name="deep_audio", result=deep_audio, status="skipped")
            if required_audio:
                raise RuntimePolicyError("deep audio disabled while raw audio exists", code="blocked_deep_audio_disabled", retryable=False)

        if run_deep_vision:
            from .brainlive_offline_deep_vision_v16_1 import run_offline_deep_vision_for_bundles
            # The vision worker itself checkpoints each image. It returns a
            # retryable status only after all images have had their bounded
            # transport retries; this avoids abandoning a complete bundle on
            # the first cold Ollama request.
            deep = stage("deep_vision", lambda: run_offline_deep_vision_for_bundles(
                person_id=person_id, package_date=day, live_session_id=live_session_id,
                model=deep_vision_model, timeout_per_image=deep_vision_timeout_per_image,
                max_keyframes_per_bundle=deep_vision_max_keyframes_per_bundle,
                append_to_brain2=True, fail_on_vlm_error=False, use_vlm=use_llm,
            ))
        else:
            with connect() as con, write_transaction(con):
                start_stage(con, run_id=run_id, stage_name="deep_vision", required=False)
                finish_stage(con, run_id=run_id, stage_name="deep_vision", result=deep, status="skipped")

        if run_silent_life:
            from .brainlive_silent_life_v16_0 import mine_silent_nonverbal_life_events
            silent = stage("silent_life", lambda: mine_silent_nonverbal_life_events(
                person_id=person_id, package_date=day, live_session_id=live_session_id,
                use_llm=use_llm, timeout=silent_life_timeout,
            ))
        else:
            with connect() as con, write_transaction(con):
                start_stage(con, run_id=run_id, stage_name="silent_life", required=False)
                finish_stage(con, run_id=run_id, stage_name="silent_life", result=silent, status="skipped")

        if run_brain2:
            from .brain2_flow_v13_3 import run_brain2_deep_stack_for_conversation
            # Recover per-conversation checkpoints first.  A PC shutdown during
            # conversation 3 must not repeat completed conversations 1 and 2.
            with connect() as con:
                rows = _rows(con, "SELECT conversation_id,bundle_id,status FROM brainlive_post_stop_conversation_runs_v1515 WHERE run_id=?", (run_id,))
            prior = {str(row.get("conversation_id")): dict(row) for row in rows if str(row.get("status")) in {"ok", "skipped_already_ok"}}
            processed = [
                {"conversation_id": cid, "bundle_id": row.get("bundle_id"), "status": str(row.get("status")), "resumed_unit": True}
                for cid, row in prior.items()
            ]
            with connect() as con, write_transaction(con):
                existing_stage = _one(con, "SELECT status,result_json FROM v18_pipeline_stages WHERE run_id=? AND stage_name='brain2'", (run_id,))
                if existing_stage and existing_stage.get("status") == "completed":
                    cached = json_loads(existing_stage.get("result_json"), {}) or {}
                    cached_processed = cached.get("processed") if isinstance(cached, dict) else None
                    if not isinstance(cached_processed, list):
                        raise StageGateError("completed Brain2 checkpoint has no processed manifest")
                    processed = [dict(item) for item in cached_processed if isinstance(item, dict)]
                    brain2_complete = True
                else:
                    start_stage(con, run_id=run_id, stage_name="brain2", required=True, input_payload=exported)
                    brain2_complete = False
            if not brain2_complete:
                record_recovery_state(run_id=run_id, state="running", stage_name="brain2")
                for item in exported:
                    cid = str(item.get("conversation_id") or "")
                    if not cid:
                        raise StageGateError(f"bundle {item.get('bundle_id')} lacks an exported conversation")
                    if cid in prior:
                        continue
                    with connect() as con:
                        skipped = (not force and _conversation_already_done(con, cid))
                    try:
                        if skipped:
                            unit = {"conversation_id": cid, "bundle_id": item.get("bundle_id"), "status": "skipped_already_ok"}
                        else:
                            result = run_brain2_deep_stack_for_conversation(
                                cid, person_id=person_id, trigger_type="brainlive_event_bundle_v18_7",
                                run_v13=True, run_v15_after=False, run_periodic_export=False, use_llm=use_llm,
                                checkpoint_run_id=run_id,
                            )
                            if str(result.get("status")) not in {"ok", "completed", "skipped_llm_disabled"}:
                                raise _stage_failure_from_result("brain2_unit", result)
                            if result.get("status") != "skipped_llm_disabled":
                                result["secondary_memory_sync"] = _sync_secondary_memory_for_conversation(cid, person_id=person_id)
                            with connect() as con:
                                result["conversation_digest"] = _conversation_digest(con, cid)
                            unit = {"conversation_id": cid, "bundle_id": item.get("bundle_id"), "status": "ok" if result.get("status") != "skipped_llm_disabled" else "skipped_llm_disabled"}
                        processed.append(unit)
                        with connect() as con, write_transaction(con):
                            con.execute(
                                """INSERT INTO brainlive_post_stop_conversation_runs_v1515(row_id,person_id,package_date,run_id,bundle_id,conversation_id,status,result_json,error_text,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                                   ON CONFLICT(run_id,conversation_id) DO UPDATE SET status=excluded.status,result_json=excluded.result_json,error_text=excluded.error_text,updated_at=excluded.updated_at""",
                                (stable_id("postconv_v18_7", run_id, cid), person_id, day, run_id, item.get("bundle_id"), cid, unit["status"], json_dumps(result if not skipped else unit), None, now_iso(), now_iso()),
                            )
                    except Exception as exc:
                        failure = classify_failure(exc)
                        with connect() as con, write_transaction(con):
                            con.execute(
                                """INSERT INTO brainlive_post_stop_conversation_runs_v1515(row_id,person_id,package_date,run_id,bundle_id,conversation_id,status,result_json,error_text,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                                   ON CONFLICT(run_id,conversation_id) DO UPDATE SET status=excluded.status,error_text=excluded.error_text,updated_at=excluded.updated_at""",
                                (stable_id("postconv_v18_7", run_id, cid), person_id, day, run_id, item.get("bundle_id"), cid, "retryable_error" if failure.retryable else "blocked", "{}", str(exc)[:2000], now_iso(), now_iso()),
                            )
                            # Crucial for immediate resume: leave the outer stage
                            # retryable/failed rather than "running", otherwise a
                            # power loss or timeout would make the next RESUME wait
                            # for the stale-stage lease even though every completed
                            # conversation row is already checkpointed.
                            finish_stage(
                                con, run_id=run_id, stage_name="brain2",
                                result={"processed": processed, "failed_conversation_id": cid, "error_code": failure.code},
                                status="retryable_error" if failure.retryable else "blocked", error_text=str(exc)[:2000],
                            )
                        if failure.retryable:
                            delay = int(cfg.poststop_retry_backoff_seconds[-1]) if cfg.poststop_retry_backoff_seconds else 0
                            mark_run_retryable(run_id=run_id, stage_name="brain2", error_code=failure.code, error_text=str(exc), retry_after_seconds=delay)
                        else:
                            record_recovery_state(run_id=run_id, state="blocked", stage_name="brain2", error_code=failure.code, error_text=str(exc))
                        raise
                with connect() as con, write_transaction(con):
                    finish_stage(con, run_id=run_id, stage_name="brain2", result={"processed": processed}, status="completed")
        else:
            with connect() as con, write_transaction(con):
                start_stage(con, run_id=run_id, stage_name="brain2", required=False)
                finish_stage(con, run_id=run_id, stage_name="brain2", result={"status": "skipped"}, status="skipped")

        # Day-level longitudinal and Life Model remain in close-day for a
        # session-scoped run.  This preserves session isolation.
        longitudinal = {"status": "deferred_session_scope" if live_session_id else "skipped"}
        life = {"status": "deferred_session_scope" if live_session_id else "skipped"}
        for name, value in (("longitudinal", longitudinal), ("life_model", life)):
            with connect() as con, write_transaction(con):
                existing = _one(con, "SELECT status FROM v18_pipeline_stages WHERE run_id=? AND stage_name=?", (run_id, name))
                if not existing or existing.get("status") != "completed":
                    start_stage(con, run_id=run_id, stage_name=name, required=False)
                    finish_stage(con, run_id=run_id, stage_name=name, result=value, status="skipped")

        expected = [str(item.get("conversation_id") or "") for item in exported if item.get("conversation_id")]
        # Deep capture without Brain2 inference is intentionally not a
        # production-complete post-stop result. The refined raw evidence stays
        # durable, but cleanup is forbidden until Brain2 is completed.
        accepted_brain2_statuses = {"ok", "skipped_already_ok"}
        observed = [str(item.get("conversation_id") or "") for item in processed if item.get("status") in accepted_brain2_statuses]
        output_manifest = record_output_manifest(run_id=run_id, person_id=person_id, expected=expected, observed=observed, reason="post_stop_retention_check_v18_7")
        if not output_manifest["complete"]:
            raise StageGateError("post-stop retained output manifest is incomplete")
        status = "completed"
        update_run(run_id, status="completed")
    except Exception as exc:
        error = str(exc)[:2000]
        failure = classify_failure(exc)
        if failure.retryable:
            status = "retryable_error"
            # ``stage`` has already written the precise recovery boundary where
            # possible.  This fallback covers errors raised between stages.
            mark_run_retryable(run_id=run_id, stage_name="post_stop", error_code=failure.code, error_text=error,
                               retry_after_seconds=int(cfg.poststop_retry_backoff_seconds[-1]) if cfg.poststop_retry_backoff_seconds else 0)
        else:
            # Preserve the canonical run and every completed checkpoint.  A
            # configuration/evidence block is not a reason to create a new
            # day or rerun deep audio after the operator fixes the condition.
            # Normal invocation stays blocked; explicit RESUME --force is the
            # controlled re-entry point enforced by assert_run_resumable().
            status = "blocked"
            try:
                record_recovery_state(run_id=run_id, state="blocked", stage_name="post_stop", error_code=failure.code, error_text=error)
            except Exception:
                pass
        record_phase_event("post_stop_failed", run_id=run_id, error_code=failure.code, retryable=failure.retryable)
    finally:
        try:
            expected = [str(item.get("conversation_id") or "") for item in exported if item.get("conversation_id")]
            accepted_brain2_statuses = {"ok", "skipped_already_ok"}
            observed = [str(item.get("conversation_id") or "") for item in processed if item.get("status") in accepted_brain2_statuses]
            if status != "completed":
                record_output_manifest(run_id=run_id, person_id=person_id, expected=expected, observed=observed, reason=f"post_stop_{status}_v18_7")
        except Exception:
            pass
        _legacy_run_row(
            run_id=run_id, person_id=person_id, day=day, live_session_id=live_session_id, service_run_id=service_run_id,
            assembly=assembly, exported=exported, processed=processed, v15=life, deep_audio=deep_audio, deep=deep,
            silent=silent, longitudinal=longitudinal, status=status, error=error, created_at=created_at,
        )
        execution_lease.release()

    return {
        "version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day,
        "live_session_id": live_session_id, "service_run_id": service_run_id, "assembly": assembly,
        "v18_deep_audio": deep_audio, "v16_deep_vision": deep, "v16_silent_life": silent,
        "v17_longitudinal": longitudinal, "exported_conversations": len(exported),
        "brain2_processed": processed, "v15": life, "status": status, "error": error,
        "resumed": resumed, "recovery": recovery,
    }

def post_stop_cleanup_eligible(*, run_id: str, person_id: str) -> dict[str, Any]:
    """Return cleanup permission only after the post-stop retention gate passes."""
    with connect() as con:
        rows = _rows(
            con,
            "SELECT stage_name FROM v18_pipeline_stages WHERE run_id=? AND required=1 ORDER BY stage_name",
            (run_id,),
        )
    required = [str(row["stage_name"]) for row in rows]
    if not required:
        raise StageGateError("cleanup blocked: post-stop run has no required stage record")
    return assert_cleanup_eligible(run_id=run_id, person_id=person_id, required_stages=required)


def post_stop_deep_flow_audit(person_id: str = "me", *, package_date: str | None = None) -> dict[str, Any]:
    ensure_post_stop_deep_flow_schema()
    day = _package_day(package_date)
    with connect() as con:
        runs = _rows(con, "SELECT * FROM brainlive_post_stop_deep_flow_runs_v1515 WHERE person_id=? AND package_date=? ORDER BY created_at DESC LIMIT 5", (person_id, day))
        convs = _rows(con, "SELECT status,COUNT(*) AS n FROM brainlive_post_stop_conversation_runs_v1515 WHERE person_id=? AND package_date=? GROUP BY status", (person_id, day))
        stages = _rows(con, "SELECT status,COUNT(*) AS n FROM v18_pipeline_stages s JOIN v18_pipeline_runs r ON r.run_id=s.run_id WHERE r.person_id=? AND r.pipeline_name='brainlive_post_stop' GROUP BY status", (person_id,))
    return {"version": VERSION,"person_id": person_id,"package_date": day,"latest_runs": runs,
            "conversation_status_counts": {r["status"]: int(r["n"]) for r in convs},
            "stage_status_counts": {r["status"]: int(r["n"]) for r in stages},
            "verdict": "ready" if runs and runs[0].get("status") == "completed" else "attention"}
