from __future__ import annotations

"""V14.4 automatic latent-outcome -> prediction-verification bridge.

This module does not replace v13-verify. It automates the path that was still
manual: when the latent outcome resolver finds that a new conversation resolves
an older prediction, the strict V13 calibration engine is invoked and the result
is materialized into prediction_results, model_revisions and v13_replay_events.

No regex / keyword verdicts are used here. The bridge only selects structured
latent_outcome_links and delegates semantic verification to the existing strict
Qwen calibration function.
"""

from typing import Any

from .db import connect, init_db, upsert
from .utils import json_dumps, now_iso, stable_id

V14_4_VERSION = "14.4.0-auto-verification-bridge-final"

AUTO_PILOT_COVERAGE = [
    {
        "command_name": "v13-verify",
        "manual_purpose": "Verify an old prediction against a later real observation.",
        "automation_status": "automatic_when_latent_outcome_resolves_prediction",
        "automatic_trigger": "flow-watch -> discover_latent_outcomes_from_conversation -> v14.4 auto verification bridge",
        "notes": "Manual command remains available for explicit corrections. Automatic bridge calls strict V13 Qwen calibration and materializes prediction_results/model_revisions/v13_replay_events.",
    },
    {
        "command_name": "v13-discover-outcomes",
        "manual_purpose": "Search a new conversation for outcomes that resolve old intentions/predictions/choices/commitments.",
        "automation_status": "already_automatic_in_flow_watch",
        "automatic_trigger": "flow-watch after each ingested conversation",
        "notes": "Manual command remains available for reprocessing a conversation.",
    },
    {
        "command_name": "v14-consolidate",
        "manual_purpose": "Build periodic hour/day/week/month/all_time self snapshots.",
        "automation_status": "automatic_for_hour_day_week_month",
        "automatic_trigger": "V14.3 scheduler after flow-watch ingestion when period is due",
        "notes": "Manual command remains useful for all_time, backfills and forced periods.",
    },
    {
        "command_name": "export-self-model",
        "manual_purpose": "Create readable Markdown/JSON self-model exports.",
        "automation_status": "automatic_after_successful_periodic_consolidation",
        "automatic_trigger": "V14.3 scheduler export_after=True",
        "notes": "Manual command remains available for custom formats/output dirs.",
    },
    {
        "command_name": "sync-vectors",
        "manual_purpose": "Push memory chunks to Qdrant/LanceDB.",
        "automation_status": "automatic_incremental_after_ingest",
        "automatic_trigger": "ingest -> sync_vectors(conversation_id=conversation_id)",
        "notes": "Manual command remains available for full rebuilds or repairs.",
    },
    {
        "command_name": "v14-5-run",
        "manual_purpose": "Build people identity hypotheses and personal open-loop solution trackers for a conversation.",
        "automation_status": "automatic_after_each_ingested_conversation",
        "automatic_trigger": "flow-watch after V14 Pattern Mirror; identity remains pending until user confirmation",
        "notes": "Creates pending identity/relation hypotheses and active desires/questions/solution candidates. It does not auto-confirm names.",
    },

    {
        "command_name": "v14-6-run",
        "manual_purpose": "Build other-person state models, emotional couplings, micro-interaction aftereffects and interpersonal loops for a conversation.",
        "automation_status": "automatic_after_each_ingested_conversation",
        "automatic_trigger": "flow-watch after V14.5 people/open-loop pass and before V14.3 scheduler/export",
        "notes": "Creates hypotheses with evidence/counter-evidence. It does not diagnose, read minds or confirm identities.",
    },
    {
        "command_name": "v14-proactive-run",
        "manual_purpose": "Create timing-aware proactive interventions from loops, aftereffects, forecasts, open questions and decision risks.",
        "automation_status": "automatic_after_each_ingested_conversation",
        "automatic_trigger": "flow-watch after V14.6 interpersonal mirror and before V14.3 scheduler/export",
        "notes": "Creates an intervention queue and export file. Phone/desktop notification delivery is left to the host bridge; feedback remains explicit.",
    },
    {
        "command_name": "voice-pending/name-voice",
        "manual_purpose": "Resolve unknown voices into real people.",
        "automation_status": "intentionally_manual",
        "automatic_trigger": "none",
        "notes": "The system must not invent a real identity for an unknown voice. It can surface pending voices, but the user must name them.",
    },
    {
        "command_name": "setup-me/enroll-voice",
        "manual_purpose": "Declare the user's own voice and known voices.",
        "automation_status": "intentionally_manual_initialization",
        "automatic_trigger": "none",
        "notes": "Identity enrollment is a trust boundary and should not be guessed.",
    },
    {
        "command_name": "memory-revise",
        "manual_purpose": "Human correction of wrong memories.",
        "automation_status": "intentionally_manual",
        "automatic_trigger": "none",
        "notes": "Automatic model revisions exist, but direct human corrections should remain explicit.",
    },
]


def ensure_v14_4_schema() -> None:
    # Also ensure the V13.3 latent outcome tables exist, because V14.4
    # consumes latent_outcome_links as its structured input.
    from .brain2_flow_v13_3 import ensure_brain2_flow_schema
    ensure_brain2_flow_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_4_auto_verify_runs(
                run_id TEXT PRIMARY KEY,
                trigger_conversation_id TEXT,
                person_id TEXT,
                status TEXT NOT NULL,
                inspected_links INTEGER NOT NULL DEFAULT 0,
                verified_predictions INTEGER NOT NULL DEFAULT 0,
                skipped_links INTEGER NOT NULL DEFAULT 0,
                error_text TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_4_auto_verify_links(
                bridge_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                latent_link_id TEXT NOT NULL,
                prediction_id TEXT,
                result_id TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v144_links_latent ON v14_4_auto_verify_links(latent_link_id, status);
            CREATE INDEX IF NOT EXISTS idx_v144_links_prediction ON v14_4_auto_verify_links(prediction_id, status);
            CREATE TABLE IF NOT EXISTS v14_4_autopilot_coverage(
                coverage_id TEXT PRIMARY KEY,
                command_name TEXT NOT NULL,
                manual_purpose TEXT,
                automation_status TEXT NOT NULL,
                automatic_trigger TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        now = now_iso()
        for row in AUTO_PILOT_COVERAGE:
            cid = stable_id("v144coverage", row["command_name"])
            upsert(con, "v14_4_autopilot_coverage", {**row, "coverage_id": cid, "updated_at": now}, "coverage_id")
        con.commit()


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _candidate_links(con, *, conversation_id: str | None, person_id: str | None, limit: int, min_confidence: float) -> list[dict[str, Any]]:
    params: list[Any] = [min_confidence]
    where = ["lol.source_table='predictions'", "lol.confidence>=?", "p.prediction_id IS NOT NULL"]
    if conversation_id:
        where.append("lol.new_conversation_id=?")
        params.append(conversation_id)
    if person_id:
        where.append("p.person_id=?")
        params.append(person_id)
    params.append(limit)
    sql = f"""
        SELECT
            lol.*,
            p.person_id AS prediction_person_id,
            p.prediction_target,
            p.predicted_value,
            p.status AS prediction_status
        FROM latent_outcome_links lol
        JOIN predictions p ON p.prediction_id = lol.source_id
        WHERE {' AND '.join(where)}
        ORDER BY lol.created_at DESC
        LIMIT ?
    """
    return [dict(r) for r in con.execute(sql, params)]


def auto_verify_latent_outcome_predictions(
    *,
    conversation_id: str | None = None,
    person_id: str | None = None,
    limit: int = 50,
    min_confidence: float = 0.55,
    skip_already_verified: bool = True,
) -> dict[str, Any]:
    """Materialize prediction verification from latent outcome links.

    Selection is structured only: it consumes latent_outcome_links produced by
    Qwen's latent outcome resolver. Semantic correctness is delegated to the
    existing strict V13 calibration function, not to local rules.
    """
    ensure_v14_4_schema()
    from .brain2_strict_v13_2 import verify_strict_v13_prediction

    now = now_iso()
    run_id = stable_id("v144autoverify", conversation_id or "all", person_id or "any", now)
    verified = 0
    skipped = 0
    inspected = 0
    errors: list[dict[str, Any]] = []
    bridge_rows: list[dict[str, Any]] = []

    with connect() as con:
        person_id = person_id or _default_user(con)
        candidates = _candidate_links(con, conversation_id=conversation_id, person_id=person_id, limit=limit, min_confidence=min_confidence)

    for link in candidates:
        inspected += 1
        latent_link_id = str(link.get("link_id") or "")
        prediction_id = str(link.get("source_id") or "")
        bridge_id = stable_id("v144bridge", latent_link_id, prediction_id)
        try:
            with connect() as con:
                existing_bridge = con.execute("SELECT * FROM v14_4_auto_verify_links WHERE latent_link_id=? AND status='verified' LIMIT 1", (latent_link_id,)).fetchone()
                existing_result = con.execute("SELECT * FROM prediction_results WHERE prediction_id=? LIMIT 1", (prediction_id,)).fetchone() if skip_already_verified else None
                if existing_bridge:
                    skipped += 1
                    upsert(con, "v14_4_auto_verify_links", {
                        "bridge_id": stable_id("v144bridge_skip", run_id, latent_link_id),
                        "run_id": run_id,
                        "latent_link_id": latent_link_id,
                        "prediction_id": prediction_id,
                        "result_id": existing_bridge["result_id"],
                        "status": "skipped",
                        "reason": "latent_link_already_auto_verified",
                        "created_at": now_iso(),
                    }, "bridge_id")
                    con.commit()
                    continue
                if existing_result:
                    skipped += 1
                    upsert(con, "v14_4_auto_verify_links", {
                        "bridge_id": stable_id("v144bridge_skip", run_id, latent_link_id),
                        "run_id": run_id,
                        "latent_link_id": latent_link_id,
                        "prediction_id": prediction_id,
                        "result_id": existing_result["result_id"],
                        "status": "skipped",
                        "reason": "prediction_already_has_result",
                        "created_at": now_iso(),
                    }, "bridge_id")
                    con.commit()
                    continue
            observed = json_dumps({
                "source": "latent_outcome_link",
                "latent_link_id": latent_link_id,
                "new_conversation_id": link.get("new_conversation_id"),
                "evidence_turn_id": link.get("evidence_turn_id"),
                "evidence_text": link.get("evidence_text"),
                "outcome_type": link.get("outcome_type"),
                "outcome_summary": link.get("outcome_summary"),
                "status_update": link.get("status_update"),
                "latent_confidence": link.get("confidence"),
            })
            result = verify_strict_v13_prediction(
                prediction_id,
                observed,
                note="auto_v14_4_from_latent_outcome_link",
            )
            result_id = result.get("result_id") if isinstance(result, dict) else None
            status = "verified" if result_id else "error"
            reason = None if result_id else json_dumps(result)
            with connect() as con:
                upsert(con, "v14_4_auto_verify_links", {
                    "bridge_id": bridge_id,
                    "run_id": run_id,
                    "latent_link_id": latent_link_id,
                    "prediction_id": prediction_id,
                    "result_id": result_id,
                    "status": status,
                    "reason": reason,
                    "created_at": now_iso(),
                }, "bridge_id")
                con.commit()
            if result_id:
                verified += 1
            else:
                errors.append({"latent_link_id": latent_link_id, "prediction_id": prediction_id, "result": result})
            bridge_rows.append({"latent_link_id": latent_link_id, "prediction_id": prediction_id, "result_id": result_id, "status": status})
        except Exception as exc:
            skipped += 1
            err = str(exc)[:1000]
            errors.append({"latent_link_id": latent_link_id, "prediction_id": prediction_id, "error": err})
            with connect() as con:
                upsert(con, "v14_4_auto_verify_links", {
                    "bridge_id": bridge_id,
                    "run_id": run_id,
                    "latent_link_id": latent_link_id,
                    "prediction_id": prediction_id,
                    "result_id": None,
                    "status": "error",
                    "reason": err,
                    "created_at": now_iso(),
                }, "bridge_id")
                con.commit()

    status = "ok" if not errors else "partial"
    payload = {"bridges": bridge_rows, "errors": errors, "min_confidence": min_confidence, "skip_already_verified": skip_already_verified}
    with connect() as con:
        upsert(con, "v14_4_auto_verify_runs", {
            "run_id": run_id,
            "trigger_conversation_id": conversation_id,
            "person_id": person_id,
            "status": status,
            "inspected_links": inspected,
            "verified_predictions": verified,
            "skipped_links": skipped,
            "error_text": json_dumps(errors)[:2000] if errors else None,
            "created_at": now,
            "payload_json": json_dumps(payload),
        }, "run_id")
        con.commit()
    return {
        "version": V14_4_VERSION,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "person_id": person_id,
        "status": status,
        "inspected_links": inspected,
        "verified_predictions": verified,
        "skipped_links": skipped,
        "errors": errors,
        "bridges": bridge_rows,
    }


def autopilot_coverage() -> dict[str, Any]:
    ensure_v14_4_schema()
    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM v14_4_autopilot_coverage ORDER BY command_name")]
        latest_runs = [dict(r) for r in con.execute("SELECT * FROM v14_4_auto_verify_runs ORDER BY created_at DESC LIMIT 20")]
    return {"version": V14_4_VERSION, "coverage": rows, "latest_auto_verify_runs": latest_runs}


def audit_v14_4(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_4_schema()
    expected = ["v14_4_auto_verify_runs", "v14_4_auto_verify_links", "v14_4_autopilot_coverage"]
    with connect() as con:
        existing = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in expected if t not in existing]
        coverage_count = con.execute("SELECT COUNT(*) c FROM v14_4_autopilot_coverage").fetchone()["c"] if not missing else 0
    return {
        "version": V14_4_VERSION,
        "goal": "latent outcomes automatically verify old predictions; manual commands remain available but flow-watch is autonomous",
        "ok": not missing and coverage_count >= 7,
        "missing_tables": missing,
        "coverage_count": coverage_count,
        "no_regex_policy": "V14.4 selects structured latent outcome links and delegates semantic verification to strict Qwen calibration; no regex verdicts.",
    }
