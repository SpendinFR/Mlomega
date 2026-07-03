from __future__ import annotations

"""V18.4 one-command end-of-day orchestration.

This module deliberately keeps V13-V17 as the substantive engines.  It adds a
single durable day-level coordinator that runs *after* the session-scoped
post-stop flow has retained its outputs:

    post-stop session -> V17 day longitudinal -> V15.12 coordination
    -> V15.13 Life Model -> V15.9 live-ready projection -> cleanup gate

The coordinator is idempotent per person/day.  A raw-media purge is never
performed here; callers (for example the Phone Bridge) must first receive a
positive ``cleanup`` result and may then delete only their own raw files.
"""

from dataclasses import dataclass
from typing import Any, Callable

from .db import connect, init_db, upsert, write_transaction
from .config import get_settings
from .runtime_v18_7 import acquire_execution_lease, classify_failure, heartbeat_execution_lease, record_phase_event
from .governance_v18 import (
    Scope,
    StageGateError,
    assert_cleanup_eligible,
    begin_or_resume_run,
    ensure_v18_schema,
    finish_stage,
    record_output_manifest,
    start_stage,
    strict_one,
    update_run,
    mark_run_retryable,
    record_recovery_state,
    recovery_state,
    recover_stale_stages,
)
from .utils import json_dumps, json_loads, now_iso, stable_id

VERSION = "18.7.1-resumable-close-day"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS v18_close_day_runs(
  close_day_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  live_session_id TEXT,
  service_run_id TEXT,
  post_stop_run_id TEXT,
  status TEXT NOT NULL,
  cleanup_eligible INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_eligible IN (0,1)),
  result_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(person_id, package_date)
);
CREATE INDEX IF NOT EXISTS idx_v18_close_day_person_date
  ON v18_close_day_runs(person_id, package_date, updated_at);
"""


@dataclass(frozen=True)
class _Context:
    person_id: str
    package_date: str
    live_session_id: str | None
    service_run_id: str | None


def ensure_close_day_schema() -> None:
    init_db()
    # A direct ``brainlive-close-day`` may be run after a manual stop, without
    # the long-lived service import having previously created its state tables.
    # Install that schema before resolving the last run; this is additive and
    # keeps the command safe on a newly initialized V18 database.
    from .brainlive_service_v15_5 import ensure_service_schema
    ensure_service_schema()
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)


def _one(con: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return strict_one(con, sql, params, purpose="close-day query")


def _status_ok(result: Any, *, stage_name: str) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    allowed = {
        "post_stop": {"completed"},
        "longitudinal": {"ok", "completed"},
        "coordination": {"ok", "completed"},
        # V15.13 returns ``llm_patch_ready`` for a patch and bootstrap embeds
        # the V15.10 status in ``bootstrap``.
        "life_model": {"llm_patch_ready", "ok", "completed", "active"},
        "live_ready": {"active", "ok", "completed", "llm_ready"},
    }
    if stage_name == "life_model" and result.get("mode") == "bootstrap_v15_10":
        bootstrap = result.get("bootstrap") or {}
        return isinstance(bootstrap, dict) and str(bootstrap.get("status") or "").lower() in {"llm_ready", "ok", "completed", "active"}
    return status in allowed.get(stage_name, {"ok", "completed"})


def _stage_identifier(name: str, result: dict[str, Any]) -> str:
    keys = {
        "post_stop": ("run_id",),
        "longitudinal": ("run_id",),
        "coordination": ("run_id",),
        "life_model": ("patch_run_id",),
        "live_ready": ("export_id",),
    }
    for key in keys.get(name, ()):
        if result.get(key):
            return f"{name}:{result[key]}"
    if name == "life_model":
        bootstrap = result.get("bootstrap") or {}
        if isinstance(bootstrap, dict) and bootstrap.get("export_id"):
            return f"life_model:{bootstrap['export_id']}"
    raise StageGateError(f"close-day {name} returned no durable identifier")


def _package_day(value: str | None) -> str:
    from .brainlive_poststop_deep_flow_v15_15 import _package_day as post_stop_day
    return post_stop_day(value)


def _resolve_context(
    *,
    person_id: str,
    package_date: str,
    live_session_id: str | None,
    service_run_id: str | None,
) -> _Context:
    """Resolve a single session/run without guessing an owner.

    A hard PC shutdown leaves a historical ``running`` row.  Convert only a
    stale heartbeat into explicit ``orphaned`` before enforcing the active-run
    gate, so a resume can safely continue post-stop without pretending the
    service ended normally.
    """
    from .brainlive_service_v15_5 import recover_stale_brainlive_service_runs
    recover_stale_brainlive_service_runs()
    with connect() as con:
        service: dict[str, Any] | None = None
        if service_run_id:
            service = _one(
                con,
                "SELECT * FROM brainlive_service_runs WHERE service_run_id=? AND person_id=?",
                (service_run_id, person_id),
            )
            if not service:
                raise StageGateError("close-day service run is missing or belongs to another owner")
        elif live_session_id:
            service = _one(
                con,
                """SELECT * FROM brainlive_service_runs
                   WHERE live_session_id=? AND person_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (live_session_id, person_id),
            )
        else:
            service = _one(
                con,
                """SELECT * FROM brainlive_service_runs
                   WHERE person_id=?
                   ORDER BY COALESCE(stopped_at, started_at) DESC LIMIT 1""",
                (person_id,),
            )
        resolved_session = live_session_id or (str(service.get("live_session_id")) if service and service.get("live_session_id") else None)
        resolved_run = service_run_id or (str(service.get("service_run_id")) if service and service.get("service_run_id") else None)
        active = [
            dict(r)
            for r in con.execute(
                """SELECT service_run_id,live_session_id,status FROM brainlive_service_runs
                   WHERE person_id=? AND status IN ('running','stop_requested')""",
                (person_id,),
            ).fetchall()
        ]
    if active:
        raise StageGateError(
            "close-day blocked: an active BrainLive service still exists; request stop first "
            f"({', '.join(str(r.get('service_run_id')) for r in active)})"
        )
    # Only the resolved session may gate this close-day.  A historical orphan
    # from another already-closed day must not block today's independent run.
    unresolved_sql = """SELECT service_run_id,status FROM brainlive_service_runs
        WHERE person_id=? AND status IN ('stopped_pending_ingest','orphaned','drain_recovery')"""
    unresolved_params: list[Any] = [person_id]
    if resolved_session:
        unresolved_sql += " AND live_session_id=?"
        unresolved_params.append(resolved_session)
    elif resolved_run:
        unresolved_sql += " AND service_run_id=?"
        unresolved_params.append(resolved_run)
    unresolved = [dict(r) for r in con.execute(unresolved_sql, tuple(unresolved_params)).fetchall()]
    if unresolved:
        raise StageGateError(
            "close-day blocked: raw inbox acknowledgement is incomplete after an interrupted service; "
            "run brainlive-resume-inbox-drain first "
            f"({', '.join(str(r.get('service_run_id')) for r in unresolved)})"
        )
    return _Context(person_id=person_id, package_date=package_date, live_session_id=resolved_session, service_run_id=resolved_run)


def _load_existing_close_day(person_id: str, package_date: str) -> dict[str, Any] | None:
    with connect() as con:
        row = _one(
            con,
            "SELECT * FROM v18_close_day_runs WHERE person_id=? AND package_date=?",
            (person_id, package_date),
        )
    return row


def _save_close_day(
    *,
    close_day_id: str,
    ctx: _Context,
    status: str,
    post_stop_run_id: str | None,
    cleanup_eligible: bool,
    result: dict[str, Any],
    error_text: str | None = None,
) -> None:
    """Upsert by logical person/day, not only by a physical run id.

    This also repairs legacy V18.5 failed rows: a new canonical V18.7 run can
    replace the row's primary id without violating ``UNIQUE(person_id,date)``.
    """
    now = now_iso()
    with connect() as con, write_transaction(con):
        previous = _one(con, "SELECT created_at FROM v18_close_day_runs WHERE person_id=? AND package_date=?", (ctx.person_id, ctx.package_date)) or {}
        con.execute(
            """INSERT INTO v18_close_day_runs(
                 close_day_id,person_id,package_date,live_session_id,service_run_id,post_stop_run_id,
                 status,cleanup_eligible,result_json,error_text,created_at,updated_at,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(person_id,package_date) DO UPDATE SET
                 close_day_id=excluded.close_day_id, live_session_id=excluded.live_session_id,
                 service_run_id=excluded.service_run_id, post_stop_run_id=excluded.post_stop_run_id,
                 status=excluded.status, cleanup_eligible=excluded.cleanup_eligible,
                 result_json=excluded.result_json,error_text=excluded.error_text,
                 updated_at=excluded.updated_at, completed_at=excluded.completed_at""",
            (close_day_id, ctx.person_id, ctx.package_date, ctx.live_session_id, ctx.service_run_id, post_stop_run_id,
             status, 1 if cleanup_eligible else 0, json_dumps(result), error_text,
             previous.get("created_at") or now, now, now if status == "completed" else None),
        )


def _run_stage(*, run_id: str, name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run or resume a day-level stage without discarding a prior checkpoint."""
    with connect() as con, write_transaction(con):
        existing = _one(con, "SELECT status,result_json FROM v18_pipeline_stages WHERE run_id=? AND stage_name=?", (run_id, name))
        if existing and str(existing.get("status")) == "completed":
            cached = json_loads(existing.get("result_json"), {}) or {}
            if isinstance(cached, dict):
                return {**cached, "resumed_stage": True}
            raise StageGateError(f"close-day {name} has an invalid cached result")
        start_stage(con, run_id=run_id, stage_name=name, required=True)
    record_recovery_state(run_id=run_id, state="running", stage_name=name)
    try:
        result = fn()
        if not _status_ok(result, stage_name=name):
            # Preserve retryable status from the post-stop result instead of
            # flattening it into a generic StageGateError.
            st = str((result or {}).get("status") or "blocked").lower()
            from .runtime_v18_7 import RuntimePolicyError
            raise RuntimePolicyError(f"close-day {name} returned {st}", code=f"{name}_{st}", retryable=st in {"retryable_error", "retryable", "pending_retry"})
    except Exception as exc:
        failure = classify_failure(exc)
        with connect() as con, write_transaction(con):
            finish_stage(con, run_id=run_id, stage_name=name, result={"status": "error", "error_code": failure.code}, status="failed", error_text=str(exc)[:2000])
        if failure.retryable:
            cfg = get_settings()
            delay = int(cfg.poststop_retry_backoff_seconds[-1]) if cfg.poststop_retry_backoff_seconds else 0
            mark_run_retryable(run_id=run_id, stage_name=name, error_code=failure.code, error_text=str(exc), retry_after_seconds=delay)
        else:
            record_recovery_state(run_id=run_id, state="blocked", stage_name=name, error_code=failure.code, error_text=str(exc))
        raise
    with connect() as con, write_transaction(con):
        finish_stage(con, run_id=run_id, stage_name=name, result=result, status="completed")
    return result


def _find_completed_post_stop(ctx: _Context) -> dict[str, Any] | None:
    with connect() as con:
        where = ["person_id=?", "package_date=?", "status='completed'"]
        params: list[Any] = [ctx.person_id, ctx.package_date]
        if ctx.live_session_id:
            where.append("live_session_id=?")
            params.append(ctx.live_session_id)
        row = _one(
            con,
            f"SELECT * FROM brainlive_post_stop_deep_flow_runs_v1515 WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 1",
            tuple(params),
        )
    if not row:
        return None
    return {
        "run_id": row.get("run_id"),
        "status": row.get("status"),
        "package_date": row.get("package_date"),
        "resumed_existing_post_stop": True,
    }


def close_brainlive_day(
    *,
    person_id: str,
    live_session_id: str | None = None,
    service_run_id: str | None = None,
    package_date: str | None = None,
    use_llm: bool = True,
    force: bool = False,
    post_stop_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize or resume one logical close-day run.

    A retryable stage failure never becomes a terminal day.  The next call keeps
    the same run id, returns completed checkpoints, and resumes at the first
    failed stage.  ``force`` only bypasses a safety backoff; it does not erase
    or duplicate completed work.
    """
    if not person_id:
        raise StageGateError("close-day requires explicit person_id")
    cfg = get_settings()
    ensure_close_day_schema()
    day = _package_day(package_date)
    existing = _load_existing_close_day(person_id, day)
    if existing and str(existing.get("status")) == "completed":
        cached = json_loads(existing.get("result_json"), {}) or {}
        return {**(cached if isinstance(cached, dict) else {}), "resumed_close_day": True}

    ctx = _resolve_context(person_id=person_id, package_date=day, live_session_id=live_session_id, service_run_id=service_run_id)
    scope = Scope(person_id=person_id, mode="maintenance")
    manifest = {
        "release": VERSION, "package_date": day, "person_id": person_id,
        "use_llm": bool(use_llm), "ollama_model": cfg.ollama_model,
    }
    run_id, resumed = begin_or_resume_run(
        pipeline_name="brainlive_close_day", scope=scope, input_manifest=manifest,
        idempotency_key=f"close_day_v18_7:{person_id}:{day}", force_resume=bool(force),
    )
    execution_lease = acquire_execution_lease(run_id=run_id, purpose="brainlive_close_day")
    if not execution_lease.acquired:
        return {
            "version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day,
            "status": "in_progress", "resumed": resumed,
            "lease_owner_pid": execution_lease.owner_pid, "lease_owner_host": execution_lease.owner_host,
            "cleanup": {"eligible": False},
        }
    try:
        recover_stale_stages(
            run_id=run_id,
            stale_after_seconds=0 if (force or execution_lease.reclaimed) else cfg.stage_stale_after_s,
            reason="close_day_resume_v18_7",
        )
    except Exception:
        execution_lease.release()
        raise
    _save_close_day(
        close_day_id=run_id, ctx=ctx, status="running", post_stop_run_id=(post_stop_result or {}).get("run_id") if isinstance(post_stop_result, dict) else None,
        cleanup_eligible=False, result={"version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day, "status": "running", "resumed": resumed},
    )
    result: dict[str, Any] = {
        "version": VERSION, "run_id": run_id, "person_id": person_id, "package_date": day,
        "live_session_id": ctx.live_session_id, "service_run_id": ctx.service_run_id, "resumed": resumed,
        "status": "blocked", "stages": {}, "cleanup": {"eligible": False},
    }
    post_stop_run_id: str | None = None
    try:
        # The day-level lease is refreshed before each heavy stage.  If the PC
        # dies, its PID disappears and the next RESUME can reclaim this exact run.
        heartbeat_execution_lease(execution_lease)
        def do_post_stop() -> dict[str, Any]:
            candidate = post_stop_result if isinstance(post_stop_result, dict) else _find_completed_post_stop(ctx)
            if not candidate:
                from .brainlive_poststop_deep_flow_v15_15 import run_brainlive_post_stop_deep_flow
                candidate = run_brainlive_post_stop_deep_flow(
                    person_id=person_id, live_session_id=ctx.live_session_id, service_run_id=ctx.service_run_id,
                    package_date=day, force=force, use_llm=use_llm,
                )
            if str(candidate.get("status")) != "completed":
                return dict(candidate)
            return dict(candidate)

        heartbeat_execution_lease(execution_lease)
        post = _run_stage(run_id=run_id, name="post_stop", fn=do_post_stop)
        result["stages"]["post_stop"] = post
        post_stop_run_id = str(post.get("run_id") or "") or None
        if not post_stop_run_id:
            raise StageGateError("post-stop completed without a durable run id")
        from .brainlive_poststop_deep_flow_v15_15 import post_stop_cleanup_eligible
        post_gate = post_stop_cleanup_eligible(run_id=post_stop_run_id, person_id=person_id)
        result["post_stop_cleanup_gate"] = post_gate
        if not bool((post_gate or {}).get("eligible")):
            raise StageGateError("close-day blocked: the session post-stop cleanup gate is not eligible")

        def do_longitudinal() -> dict[str, Any]:
            from .brain2_longitudinal_cases_v17 import run_longitudinal_consolidation
            return run_longitudinal_consolidation(person_id=person_id, period="day", run_date=day, use_llm=use_llm, run_periodic_mirror_layer=False, force_cases=False)
        heartbeat_execution_lease(execution_lease)
        longitudinal = _run_stage(run_id=run_id, name="longitudinal", fn=do_longitudinal)
        result["stages"]["longitudinal"] = longitudinal

        def do_coordination() -> dict[str, Any]:
            from .brainlive_brain2_coordination_v15_12 import run_brainlive_brain2_coordination
            return run_brainlive_brain2_coordination(person_id=person_id, package_date=day, use_llm=use_llm, timeout=cfg.poststop_llm_timeout_s)
        heartbeat_execution_lease(execution_lease)
        coordination = _run_stage(run_id=run_id, name="coordination", fn=do_coordination)
        result["stages"]["coordination"] = coordination

        def do_life_model() -> dict[str, Any]:
            from .brain2_life_model_updater_v15_13 import run_brain2_life_model_update
            from .brain2_longitudinal_cases_v17 import period_bounds
            start_at, end_at, _ = period_bounds("day", run_date=day)
            return run_brain2_life_model_update(person_id, period_start=start_at, period_end=end_at, use_llm=use_llm, timeout=cfg.poststop_llm_timeout_s, limit=120)
        heartbeat_execution_lease(execution_lease)
        life = _run_stage(run_id=run_id, name="life_model", fn=do_life_model)
        result["stages"]["life_model"] = life

        def do_live_ready() -> dict[str, Any]:
            from .brainlive_personal_model_v15_9 import build_brain2_live_personal_model
            return build_brain2_live_personal_model(person_id=person_id, use_llm=use_llm, timeout=cfg.poststop_llm_timeout_s, limit=80)
        heartbeat_execution_lease(execution_lease)
        live_ready = _run_stage(run_id=run_id, name="live_ready", fn=do_live_ready)
        result["stages"]["live_ready"] = live_ready

        expected = [_stage_identifier("post_stop", post), _stage_identifier("longitudinal", longitudinal), _stage_identifier("coordination", coordination), _stage_identifier("life_model", life), _stage_identifier("live_ready", live_ready)]
        output = record_output_manifest(run_id=run_id, person_id=person_id, expected=expected, observed=list(expected), reason="close_day_retention_and_live_ready_check_v18_7")
        update_run(run_id, status="completed")
        cleanup = assert_cleanup_eligible(run_id=run_id, person_id=person_id, required_stages=["post_stop", "longitudinal", "coordination", "life_model", "live_ready"])
        result.update(status="completed", output_manifest=output, cleanup={**cleanup, "post_stop_run_id": post_stop_run_id})
        _save_close_day(close_day_id=run_id, ctx=ctx, status="completed", post_stop_run_id=post_stop_run_id, cleanup_eligible=True, result=result)
    except Exception as exc:
        error = str(exc)[:2000]
        failure = classify_failure(exc)
        if failure.retryable:
            status = "retryable_error"
            delay = int(cfg.poststop_retry_backoff_seconds[-1]) if cfg.poststop_retry_backoff_seconds else 0
            mark_run_retryable(run_id=run_id, stage_name="close_day", error_code=failure.code, error_text=error, retry_after_seconds=delay)
        else:
            # Preserve the canonical run and every completed checkpoint.  A
            # configuration/evidence block is not a reason to create a new
            # day or rerun deep audio after the operator fixes the condition.
            # Normal invocation stays blocked; explicit RESUME --force is the
            # controlled re-entry point enforced by assert_run_resumable().
            status = "blocked"
            try:
                record_recovery_state(run_id=run_id, state="blocked", stage_name="close_day", error_code=failure.code, error_text=error)
            except Exception:
                pass
        result.update(status=status, error=error, cleanup={"eligible": False, "reason": f"close_day_{status}"})
        _save_close_day(close_day_id=run_id, ctx=ctx, status=status, post_stop_run_id=post_stop_run_id, cleanup_eligible=False, result=result, error_text=error)
        record_phase_event("close_day_failed", run_id=run_id, error_code=failure.code, retryable=failure.retryable)
    finally:
        execution_lease.release()
    return result

def close_day_status(*, person_id: str, package_date: str | None = None) -> dict[str, Any]:
    ensure_close_day_schema()
    day = _package_day(package_date)
    row = _load_existing_close_day(person_id, day)
    if not row:
        return {"version": VERSION, "person_id": person_id, "package_date": day, "status": "missing"}
    result = json_loads(row.get("result_json"), {}) or {}
    return {
        "version": VERSION,
        "person_id": person_id,
        "package_date": day,
        "close_day_id": row.get("close_day_id"),
        "status": row.get("status"),
        "cleanup_eligible": bool(row.get("cleanup_eligible")),
        "live_session_id": row.get("live_session_id"),
        "service_run_id": row.get("service_run_id"),
        "post_stop_run_id": row.get("post_stop_run_id"),
        "error": row.get("error_text"),
        "recovery": recovery_state(run_id=str(row.get("close_day_id"))) if row.get("close_day_id") else None,
        "result": result,
    }
