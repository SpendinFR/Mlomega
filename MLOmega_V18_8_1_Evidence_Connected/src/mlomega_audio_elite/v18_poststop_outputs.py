"""V18 safe post-stop outputs: deep vision as addenda, silent life as candidates.

The V16 paths used to turn a failed heavy VLM call into a plausible-looking
fallback and append it as a pseudo-dialogue turn.  Silent life could then
promote a one-off inferred activity to an observed life event.  V18 keeps raw
facts immutable and stores derived visual/silent material as scoped addenda or
candidates.  Promotion is a separate, evidence-checked lifecycle operation.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .db import connect, insert_only, upsert, write_transaction
from .governance_v18 import (
    Scope,
    canonical_time,
    projection_is_active,
    record_artifact_version,
    set_projection_active,
    strict_many,
)
from .utils import json_dumps, now_iso, stable_id

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brain2_context_addenda_v18(
  addendum_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  bundle_id TEXT,
  live_session_id TEXT,
  event_time TEXT NOT NULL,
  evidence_role TEXT NOT NULL,
  text TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(person_id, source_table, source_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_context_addenda_scope
  ON brain2_context_addenda_v18(person_id, conversation_id, status, event_time);
CREATE TABLE IF NOT EXISTS v18_deep_vision_attempts(
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  deep_observation_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v18_silent_promotion_queue(
  queue_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_independent_evidence',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(candidate_id, person_id)
);
"""


def _ensure(con) -> None:
    con.executescript(SCHEMA)


def _table_exists(con: Any, table: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _package_day(module: Any, package_date: str | None) -> str:
    return module._package_day(package_date)


def install_deep(module: Any) -> dict[str, Any]:
    old_audit = module.deep_vision_audit

    def ensure_deep_vision_schema() -> None:
        # Do not call init recursively under caller transactions.
        module.init_db()
        with connect() as con, write_transaction(con):
            con.executescript(module.SCHEMA)
            _ensure(con)

    def _active_bundles(con: Any, person_id: str, day: str, live_session_id: str | None, limit: int) -> list[dict[str, Any]]:
        where = ["person_id=?", "package_date=?", "status IN ('assembled','active')"]
        params: list[Any] = [person_id, day]
        if live_session_id:
            where.append("live_session_id=?")
            params.append(live_session_id)
        params.append(max(1, int(limit)))
        return strict_many(
            con,
            "SELECT * FROM brainlive_event_bundles_v1514 WHERE " + " AND ".join(where) + " ORDER BY start_time,bundle_id LIMIT ?",
            tuple(params),
            purpose="V18 deep vision active bundle selection",
        )

    def run_offline_deep_vision_for_bundles(
        person_id: str = "me",
        *,
        package_date: str | None = None,
        live_session_id: str | None = None,
        model: str | None = None,
        timeout_per_image: float = 45.0,
        max_keyframes_per_bundle: int = 12,
        transcript_char_threshold: int | None = None,
        limit_bundles: int = 200,
        append_to_brain2: bool = True,
        fail_on_vlm_error: bool = False,
        use_vlm: bool = True,
    ) -> dict[str, Any]:
        """Analyze only active scoped bundles; failed VLM data is quarantined.

        No fallback observation is exported.  A failed image remains evidence
        that the analysis failed, not a substitute fact derived from another
        model.  Successful output is placed in a context addendum, not in the
        immutable dialogue turn stream.
        """
        if not person_id:
            raise ValueError("V18 deep vision requires explicit person_id")
        ensure_deep_vision_schema()
        day = _package_day(module, package_date)
        chosen = model or os.environ.get("MLOMEGA_OFFLINE_VLM_MODEL") or os.environ.get("MLOMEGA_VLM_HEAVY_MODEL") or os.environ.get("MLOMEGA_VLM_MODEL") or module.get_settings().ollama_model
        run_id = stable_id("v18deepvisionrun", person_id, day, live_session_id or "all", chosen, now_iso(), uuid4().hex)
        scanned = selected = analyzed = quarantined = 0
        # These are evidence-integrity failures, not ordinary VLM failures.  A
        # bundle that says it has images but has no readable raw pixels must
        # block the post-stop run: Brain2 cannot honestly treat that as “no
        # vision”.  The source stays intact for repair/resume.
        visual_evidence_failures: list[dict[str, Any]] = []
        terminal_status = "ok"
        run_error: str | None = None
        try:
            with connect() as con:
                bundles = _active_bundles(con, person_id, day, live_session_id, limit_bundles)
            scanned = len(bundles)
            for bundle in bundles:
                if transcript_char_threshold is not None and module._transcript_chars(bundle) > int(transcript_char_threshold):
                    continue
                # `select_keyframes_for_bundle` deliberately excludes missing
                # files.  Inspect the complete candidate set first so a broken
                # evidence bridge never becomes a false successful VLM stage.
                all_candidates = module._keyframe_candidates(bundle)
                frames = module.select_keyframes_for_bundle(bundle, max_keyframes=max_keyframes_per_bundle)
                if all_candidates and not frames:
                    frame = all_candidates[0]
                    image_path = str(frame.get("image_path") or "")
                    obs_id = stable_id("bldeep161", person_id, bundle.get("bundle_id"), frame.get("frame_id") or image_path or "missing", 0, chosen)
                    failure = {
                        "bundle_id": bundle.get("bundle_id"),
                        "frame_id": frame.get("frame_id"),
                        "image_path": image_path,
                        "error_code": "blocked_visual_evidence_unavailable",
                        "error": "bundle contains captured image evidence but no readable keyframe path",
                    }
                    visual_evidence_failures.append(failure)
                    terminal_status = "blocked"
                    run_error = failure["error"]
                    with connect() as con, write_transaction(con):
                        upsert(con, "brainlive_deep_vision_observations_v161", {
                            "deep_observation_id": obs_id, "run_id": run_id, "person_id": person_id, "package_date": day,
                            "bundle_id": bundle.get("bundle_id"), "live_session_id": bundle.get("live_session_id"),
                            "conversation_id": bundle.get("brain2_conversation_id"), "frame_id": frame.get("frame_id"),
                            "image_path": str(Path(image_path).expanduser()) if image_path else "",
                            "frame_time": frame.get("frame_time"), "sample_index": 0,
                            "sample_reason": "blocked_missing_raw_visual_evidence", "model": chosen,
                            "status": "blocked_visual_evidence_unavailable",
                            "scene_summary_detailed": None, "observed_activity": None, "activity_confidence": 0.0,
                            "location_hint": None, "spatial_layout": None, "objects_json": "[]", "affordances_json": "[]",
                            "visible_text_json": "[]", "people_presence_json": "{}", "screens_or_devices_json": "[]",
                            "posture_motion_json": "{}", "work_or_rest_signal_json": "{}", "smoking_pause_signal_json": "{}",
                            "exact_visual_evidence_json": "[]", "uncertainty_json": json_dumps([failure["error_code"]]),
                            "qwen_json": "{}", "latency_ms": 0, "error_text": failure["error"],
                            "created_at": now_iso(), "updated_at": now_iso(),
                        }, "deep_observation_id")
                        con.execute(
                            "INSERT INTO v18_deep_vision_attempts(attempt_id,run_id,deep_observation_id,person_id,bundle_id,outcome,error_text,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (stable_id("v18deepattempt", run_id, obs_id), run_id, obs_id, person_id, str(bundle.get("bundle_id")), "blocked_visual_evidence_unavailable", failure["error"], now_iso()),
                        )
                    set_projection_active(
                        projection_kind="deep_vision", source_table="brainlive_deep_vision_observations_v161", source_id=obs_id,
                        person_id=person_id, active=False, reason="blocked_visual_evidence_unavailable",
                    )
                    continue
                for frame in frames:
                    selected += 1
                    image_path = str(frame.get("image_path") or "")
                    obs_id = stable_id("bldeep161", person_id, bundle.get("bundle_id"), frame.get("frame_id") or image_path, frame.get("sample_index"), chosen)
                    started = time.time()
                    raw: dict[str, Any] = {}
                    status = "ok"
                    error_text: str | None = None
                    try:
                        if not use_vlm:
                            raise RuntimeError("offline_vlm_disabled")
                        raw = module._deep_vlm_json(
                            image_path,
                            model=chosen,
                            timeout=timeout_per_image,
                            personal_context={"bundle_title": bundle.get("title"), "place": module._safe_json(bundle.get("place_json"), {}), "live_summary": frame.get("live_summary")},
                        )
                        norm = module._normalize_observation(raw)
                    except Exception as exc:
                        status = "quarantined_vlm_error" if use_vlm else "quarantined_vlm_disabled"
                        error_text = str(exc)[:1500]
                        norm = {
                            "scene_summary_detailed": None,
                            "observed_activity": None,
                            "activity_confidence": 0.0,
                            "location_hint": None,
                            "spatial_layout": None,
                            "objects": [], "affordances": [], "visible_text": [],
                            "people_presence": {}, "screens_or_devices": [], "posture_motion": {},
                            "work_or_rest_signal": {}, "smoking_pause_signal": {},
                            "exact_visual_evidence": [],
                            "uncertainty": ["deep_vlm_not_available_or_invalid"],
                            "qwen_json": {},
                        }
                        quarantined += 1
                        if fail_on_vlm_error:
                            raise
                    latency_ms = int((time.time() - started) * 1000)
                    row = {
                        "deep_observation_id": obs_id, "run_id": run_id, "person_id": person_id, "package_date": day,
                        "bundle_id": bundle.get("bundle_id"), "live_session_id": bundle.get("live_session_id"),
                        "conversation_id": bundle.get("brain2_conversation_id"), "frame_id": frame.get("frame_id"),
                        "image_path": str(Path(image_path).expanduser()), "frame_time": frame.get("frame_time"),
                        "sample_index": int(frame.get("sample_index") or 0), "sample_reason": frame.get("sample_reason"),
                        "model": chosen, "status": status,
                        "scene_summary_detailed": norm.get("scene_summary_detailed"), "observed_activity": norm.get("observed_activity"),
                        "activity_confidence": norm.get("activity_confidence") or 0.0, "location_hint": norm.get("location_hint"),
                        "spatial_layout": norm.get("spatial_layout"), "objects_json": json_dumps(norm.get("objects") or []),
                        "affordances_json": json_dumps(norm.get("affordances") or []), "visible_text_json": json_dumps(norm.get("visible_text") or []),
                        "people_presence_json": json_dumps(norm.get("people_presence") or {}), "screens_or_devices_json": json_dumps(norm.get("screens_or_devices") or []),
                        "posture_motion_json": json_dumps(norm.get("posture_motion") or {}), "work_or_rest_signal_json": json_dumps(norm.get("work_or_rest_signal") or {}),
                        "smoking_pause_signal_json": json_dumps(norm.get("smoking_pause_signal") or {}),
                        "exact_visual_evidence_json": json_dumps(norm.get("exact_visual_evidence") or []),
                        "uncertainty_json": json_dumps(norm.get("uncertainty") or []), "qwen_json": json_dumps(norm.get("qwen_json") or raw or {}),
                        "latency_ms": latency_ms, "error_text": error_text, "created_at": now_iso(), "updated_at": now_iso(),
                    }
                    with connect() as con, write_transaction(con):
                        upsert(con, "brainlive_deep_vision_observations_v161", row, "deep_observation_id")
                        con.execute(
                            "INSERT INTO v18_deep_vision_attempts(attempt_id,run_id,deep_observation_id,person_id,bundle_id,outcome,error_text,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (stable_id("v18deepattempt", run_id, obs_id), run_id, obs_id, person_id, str(bundle.get("bundle_id")), status, error_text, now_iso()),
                        )
                    if status == "ok":
                        analyzed += 1
                        record_artifact_version(
                            artifact_table="brainlive_deep_vision_observations_v161", artifact_id=obs_id,
                            identity_key=f"deep:{bundle.get('bundle_id')}:{frame.get('frame_id') or image_path}:{chosen}",
                            scope=Scope(person_id=person_id, live_session_id=bundle.get("live_session_id"), mode="post_stop"),
                            source_payload={"bundle_id": bundle.get("bundle_id"), "frame_id": frame.get("frame_id"), "raw": raw},
                            metadata={"run_id": run_id, "model": chosen},
                        )
                    else:
                        set_projection_active(
                            projection_kind="deep_vision", source_table="brainlive_deep_vision_observations_v161", source_id=obs_id,
                            person_id=person_id, active=False, reason=status,
                        )
            # Never let Brain2 continue with a partial visual record after an
            # evidence-integrity block. Ordinary VLM failures remain quarantined
            # as before; missing raw pixels are a stronger, repair-required state.
            appended = append_deep_vision_context_turns_to_brain2(person_id, package_date=day, live_session_id=live_session_id, only_status_ok=True).get("context_addenda_created", 0) if append_to_brain2 and terminal_status == "ok" else 0
        except Exception as exc:
            terminal_status = "error"
            run_error = str(exc)[:2000]
            raise
        finally:
            with connect() as con, write_transaction(con):
                upsert(con, "brainlive_deep_vision_runs_v161", {
                    "run_id": run_id, "person_id": person_id, "package_date": day, "model": chosen,
                    "max_keyframes_per_bundle": int(max_keyframes_per_bundle), "scanned_bundles": scanned,
                    "selected_keyframes": selected, "analyzed_keyframes": analyzed, "appended_brain2_turns": appended if 'appended' in locals() else 0,
                    "status": terminal_status, "error_text": run_error, "created_at": now_iso(), "updated_at": now_iso(),
                }, "run_id")
        return {
            "version": "18.8.1-deep-vision-evidence-connected", "run_id": run_id, "person_id": person_id,
            "package_date": day, "live_session_id": live_session_id, "model": chosen,
            "scanned_bundles": scanned, "selected_keyframes": selected, "analyzed_keyframes": analyzed,
            "quarantined_observations": quarantined, "context_addenda_created": appended if 'appended' in locals() else 0,
            "visual_evidence_failures": visual_evidence_failures,
            "status": terminal_status,
        }

    def append_deep_vision_context_turns_to_brain2(
        person_id: str = "me", *, package_date: str | None = None, live_session_id: str | None = None, only_status_ok: bool = True
    ) -> dict[str, Any]:
        """Expose successful deep-vision output as a versioned context addendum.

        Despite the legacy function name, V18 intentionally does not append a
        pseudo-turn: that would mutate dialogue chronology and contaminate
        speaker/episode logic.  V18 Brain2 context readers load the addendum
        explicitly with its source id and event time.
        """
        if not person_id:
            raise ValueError("V18 deep vision export requires explicit person_id")
        ensure_deep_vision_schema()
        day = _package_day(module, package_date)
        created = 0
        with connect() as con, write_transaction(con):
            _ensure(con)
            where = ["o.person_id=?", "o.package_date=?", "o.status='ok'", "b.person_id=o.person_id", "b.status IN ('assembled','active')", "e.person_id=o.person_id", "e.export_status IN ('exported','active','ok')"]
            params: list[Any] = [person_id, day]
            if live_session_id:
                where.append("o.live_session_id=?")
                params.append(live_session_id)
            rows = strict_many(con, f"""
                SELECT o.*, e.conversation_id AS exported_conversation_id
                FROM brainlive_deep_vision_observations_v161 o
                JOIN brainlive_event_bundles_v1514 b ON b.bundle_id=o.bundle_id
                JOIN brainlive_brain2_event_exports_v1514 e ON e.bundle_id=o.bundle_id
                WHERE {' AND '.join(where)}
                ORDER BY o.frame_time,o.sample_index,o.deep_observation_id
            """, tuple(params), purpose="V18 successful deep vision exports")
            for row in rows:
                sid = str(row["deep_observation_id"])
                if not projection_is_active(con, projection_kind="deep_vision", source_table="brainlive_deep_vision_observations_v161", source_id=sid, person_id=person_id):
                    # A missing projection state defaults to active; an explicit
                    # revocation/tombstone returns False and must never be
                    # re-exported as fresh Brain2 evidence.
                    continue
                conv_id = str(row.get("exported_conversation_id") or row.get("conversation_id") or "")
                if not conv_id:
                    continue
                text = module._deep_turn_text(row)
                addendum_id = stable_id("v18deepaddendum", person_id, conv_id, sid)
                insert_only(con, "brain2_context_addenda_v18", {
                    "addendum_id": addendum_id, "person_id": person_id, "conversation_id": conv_id,
                    "source_table": "brainlive_deep_vision_observations_v161", "source_id": sid,
                    "bundle_id": row.get("bundle_id"), "live_session_id": row.get("live_session_id"),
                    "event_time": canonical_time(row, "frame_time", "created_at") or now_iso(),
                    "evidence_role": "system_visual_observation", "text": text,
                    "metadata_json": json_dumps({"model": row.get("model"), "frame_id": row.get("frame_id"), "image_path": row.get("image_path"), "uncertainty": module._safe_json(row.get("uncertainty_json"), [])}),
                    "status": "active", "created_at": now_iso(), "updated_at": now_iso(),
                }, on_conflict="ignore")
                # Keep the legacy export record for existing dashboards, but it
                # points to the addendum rather than a nonexistent dialogue turn.
                export_id = stable_id("bldeep161export", sid, conv_id)
                upsert(con, "brainlive_deep_vision_brain2_exports_v161", {
                    "export_id": export_id, "deep_observation_id": sid, "bundle_id": row.get("bundle_id"),
                    "conversation_id": conv_id, "turn_id": addendum_id, "status": "context_addendum",
                    "created_at": now_iso(), "updated_at": now_iso(),
                }, "export_id")
                created += 1
        return {"version": "18.8.1-deep-vision-evidence-connected", "person_id": person_id, "package_date": day, "live_session_id": live_session_id, "context_addenda_created": created, "turns_appended": 0}

    def deep_vision_audit(person_id: str = "me", *, package_date: str | None = None) -> dict[str, Any]:
        data = old_audit(person_id, package_date=package_date)
        day = _package_day(module, package_date)
        with connect() as con:
            addenda = con.execute("SELECT COUNT(*) AS n FROM brain2_context_addenda_v18 WHERE person_id=? AND status='active' AND substr(event_time,1,10)=?", (person_id, day)).fetchone()
            bad = con.execute("SELECT COUNT(*) AS n FROM brainlive_deep_vision_brain2_exports_v161 e JOIN brainlive_deep_vision_observations_v161 o ON o.deep_observation_id=e.deep_observation_id WHERE o.person_id=? AND o.package_date=? AND o.status<>'ok' AND e.status IN ('exported','context_addendum')", (person_id, day)).fetchone()
        data.update({"v18_context_addenda": int(addenda["n"] if addenda else 0), "invalid_exports": int(bad["n"] if bad else 0), "verdict": "ready" if not bad or int(bad["n"]) == 0 else "blocked"})
        return data

    return {
        "ensure_deep_vision_schema": ensure_deep_vision_schema,
        "run_offline_deep_vision_for_bundles": run_offline_deep_vision_for_bundles,
        "append_deep_vision_context_turns_to_brain2": append_deep_vision_context_turns_to_brain2,
        "deep_vision_audit": deep_vision_audit,
    }


def install_silent(module: Any) -> dict[str, Any]:
    old_evidence = module._evidence_from_bundle
    old_audit = module.silent_life_audit

    def ensure_silent_life_schema() -> None:
        module.init_db()
        with connect() as con, write_transaction(con):
            con.executescript(module.SCHEMA)
            _ensure(con)

    def _evidence_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
        evidence = old_evidence(bundle)
        # The legacy query included vlm_error/skipped data.  A failure is not
        # visual evidence.  Keep it only as an uncertainty recorded in the
        # candidate's counter-evidence by the caller.
        evidence["deep_vision"] = [x for x in (evidence.get("deep_vision") or []) if str(x.get("status") or "") == "ok"]
        return evidence

    def mine_silent_nonverbal_life_events(
        person_id: str = "me", *, package_date: str | None = None, live_session_id: str | None = None,
        use_llm: bool = True, timeout: float = 120.0, transcript_char_threshold: int = 80,
        export_life_events: bool = False, limit: int = 200,
    ) -> dict[str, Any]:
        """Create scoped candidates only; automatic observed-fact promotion is forbidden."""
        if not person_id:
            raise ValueError("V18 silent life requires explicit person_id")
        ensure_silent_life_schema()
        module._evidence_from_bundle = _evidence_from_bundle
        from .brainlive_event_assembler_v15_14 import _period_bounds
        day = _period_bounds(package_date)[0]
        run_id = stable_id("v18silentrun", person_id, day, live_session_id or "all", now_iso(), uuid4().hex)
        scanned = candidates = queued = 0
        status = "ok"
        error: str | None = None
        try:
            with connect() as con:
                where = ["person_id=?", "package_date=?", "status IN ('assembled','active')"]
                params: list[Any] = [person_id, day]
                if live_session_id:
                    where.append("live_session_id=?")
                    params.append(live_session_id)
                params.append(max(1, int(limit)))
                bundles = strict_many(con, "SELECT * FROM brainlive_event_bundles_v1514 WHERE " + " AND ".join(where) + " ORDER BY start_time,bundle_id LIMIT ?", tuple(params), purpose="V18 silent life bundle selection")
            scanned = len(bundles)
            for bundle in bundles:
                chars = module._transcript_chars(bundle)
                evidence = _evidence_from_bundle(bundle)
                if chars > int(transcript_char_threshold) or not (evidence.get("vision") or evidence.get("deep_vision") or evidence.get("world") or evidence.get("audio")):
                    continue
                fallback = module._fallback_candidate(bundle, evidence)
                raw: dict[str, Any] = module._llm_candidate(bundle, evidence, timeout=timeout) if use_llm else {}
                candidate = module._normalize_candidate(raw, fallback)
                exact = list(candidate.get("exact_evidence") or [])
                # A single inferred visual activity can only become watch-only;
                # no automatic life event is an observed fact.
                if not exact:
                    candidate["memory_action"] = "ignore"
                    candidate["use_policy"] = "watch_only"
                else:
                    candidate["memory_action"] = "watch"
                    candidate["use_policy"] = "routine_candidate" if candidate.get("use_policy") == "routine_candidate" else "watch_only"
                candidate["confidence"] = min(0.70, float(candidate.get("confidence") or 0.0))
                candidate_id = stable_id("blsilent160", person_id, bundle.get("bundle_id"), candidate.get("summary"), candidate.get("inferred_activity_type"))
                state = "candidate" if exact else "quarantined_insufficient_evidence"
                with connect() as con, write_transaction(con):
                    upsert(con, "brainlive_silent_event_candidates_v160", {
                        "candidate_id": candidate_id, "person_id": person_id, "package_date": day, "bundle_id": bundle.get("bundle_id"),
                        "conversation_id": bundle.get("brain2_conversation_id"), "live_session_id": bundle.get("live_session_id"),
                        "start_time": bundle.get("start_time"), "end_time": bundle.get("end_time"), "place_json": json_dumps(evidence.get("place") or {}),
                        "transcript_chars": chars, "vision_evidence_json": json_dumps(evidence.get("vision") or []),
                        "deep_vision_evidence_json": json_dumps(evidence.get("deep_vision") or []), "world_evidence_json": json_dumps(evidence.get("world") or []),
                        "audio_evidence_json": json_dumps(evidence.get("audio") or []), "activity_candidates_json": json_dumps(evidence.get("activity_candidates") or []),
                        "inferred_activity_type": candidate.get("inferred_activity_type"), "title": candidate.get("title"), "summary": candidate.get("summary"),
                        "likely_need_hypothesis": candidate.get("likely_need_hypothesis"), "mood_effect_hypothesis": candidate.get("mood_effect_hypothesis"),
                        "routine_signal_json": json_dumps(candidate.get("routine_signal") or {}), "exact_evidence_json": json_dumps(exact),
                        "counter_evidence_json": json_dumps((candidate.get("counter_evidence") or []) + ["automatic promotion to observed life event disabled in V18"]),
                        "confidence": candidate.get("confidence"), "memory_action": candidate.get("memory_action"), "use_policy": candidate.get("use_policy"),
                        "status": state, "llm_json": json_dumps(candidate.get("llm_json") or {}), "created_life_event_id": None,
                        "created_at": now_iso(), "updated_at": now_iso(),
                    }, "candidate_id")
                    if state == "candidate":
                        insert_only(con, "v18_silent_promotion_queue", {
                            "queue_id": stable_id("v18silentqueue", person_id, candidate_id), "candidate_id": candidate_id, "person_id": person_id,
                            "reason": "requires independent repeated evidence and explicit promotion", "status": "pending_independent_evidence", "created_at": now_iso(), "updated_at": now_iso(),
                        }, on_conflict="ignore")
                        queued += 1
                candidates += 1
                set_projection_active(projection_kind="silent_candidate", source_table="brainlive_silent_event_candidates_v160", source_id=candidate_id, person_id=person_id, active=state == "candidate", reason=state)
        except Exception as exc:
            status = "error"
            error = str(exc)[:2000]
            raise
        finally:
            with connect() as con, write_transaction(con):
                upsert(con, "brainlive_silent_life_mining_runs_v160", {
                    "run_id": run_id, "person_id": person_id, "package_date": day, "scanned_bundles": scanned,
                    "silent_candidates": candidates, "exported_life_events": 0, "status": status, "error_text": error,
                    "created_at": now_iso(), "updated_at": now_iso(),
                }, "run_id")
        return {"version": "18.0.0-silent-life", "run_id": run_id, "person_id": person_id, "package_date": day, "live_session_id": live_session_id, "scanned_bundles": scanned, "silent_candidates": candidates, "promotion_queue_created": queued, "exported_life_events": 0, "export_suppressed": bool(export_life_events), "status": status}

    def silent_life_audit(person_id: str = "me", *, package_date: str | None = None) -> dict[str, Any]:
        data = old_audit(person_id, package_date=package_date)
        with connect() as con:
            observed = con.execute("SELECT COUNT(*) AS n FROM life_events WHERE subject_person_id=? AND event_status='observed_nonverbal'", (person_id,)).fetchone() if _table_exists(con, "life_events") else None
            queue = con.execute("SELECT COUNT(*) AS n FROM v18_silent_promotion_queue WHERE person_id=? AND status='pending_independent_evidence'", (person_id,)).fetchone()
        data.update({"v18_pending_promotions": int(queue["n"] if queue else 0), "legacy_observed_nonverbal_count": int(observed["n"] if observed else 0), "automatic_promotion": "disabled"})
        return data

    return {
        "ensure_silent_life_schema": ensure_silent_life_schema,
        "_evidence_from_bundle": _evidence_from_bundle,
        "mine_silent_nonverbal_life_events": mine_silent_nonverbal_life_events,
        "silent_life_audit": silent_life_audit,
    }
