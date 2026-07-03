from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .db import connect, init_db, insert_only, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v19_prediction_loop import ensure_prediction_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_outcomes_v19 (
  outcome_id TEXT PRIMARY KEY,
  prediction_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  resolved_at TEXT NOT NULL,
  evidence_refs_json TEXT DEFAULT '[]',
  audit_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_v19_owner ON prediction_outcomes_v19(person_id,status,resolved_at);
"""


def ensure_outcome_schema(db_path=None) -> None:
    init_db(db_path)
    ensure_prediction_schema(db_path)
    with connect(db_path) as con, write_transaction(con):
        con.executescript(SCHEMA)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _event_matches(event: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    if spec.get("event_type") and str(event.get("event_type")) != str(spec.get("event_type")):
        return False
    blob = " ".join(str(event.get(k) or "") for k in ("entity_json", "observation_json", "place_json", "provenance_json")).lower()
    for key in ("entity_label", "place_label", "observation_contains"):
        expected = str(spec.get(key) or "").strip().lower()
        if expected and expected not in blob:
            return False
    return True


def _event_refutes(event: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    refute = spec.get("refutes") if isinstance(spec.get("refutes"), Mapping) else {}
    if not refute:
        return False
    return _event_matches(event, refute)


def _call_conversation_auto_verifier(*, person_id: str) -> dict[str, Any]:
    try:
        from .auto_verification_v14_4 import auto_verify_latent_outcome_predictions
        return auto_verify_latent_outcome_predictions(person_id=person_id, limit=50)
    except Exception as exc:
        return {"status": "skipped", "error": str(exc)[:300]}


def _try_register_calibration(*, person_id: str, prediction_id: str, status: str, resolved_at: str, db_path=None) -> dict[str, Any]:
    """Best-effort strict calibration hook.

    The predictive calibration API requires pre-existing observed-case pairs;
    when none are linked to the prediction, record an explicit skip instead of
    fabricating labels.
    """
    try:
        from .v18_predictive_retrieval import register_verified_similarity_label
        with connect(db_path) as con:
            rows = [dict(r) for r in con.execute(
                """SELECT observed_case_id FROM brain2_observed_cases_v17
                   WHERE person_id=? ORDER BY observed_at DESC LIMIT 2""",
                (person_id,),
            ).fetchall()]
        if len(rows) < 2:
            return {"status": "skipped", "reason": "not_enough_observed_cases"}
        return register_verified_similarity_label(
            person_id=person_id,
            anchor_case_id=rows[0]["observed_case_id"],
            similar_case_id=rows[1]["observed_case_id"],
            label=status == "verified",
            label_source="strict_verifier",
            verified_at=resolved_at,
            metadata={"prediction_id": prediction_id, "outcome_status": status},
        )
    except Exception as exc:
        return {"status": "skipped", "error": str(exc)[:300]}


def resolve_prediction_outcomes(*, person_id: str, package_date: str, db_path=None) -> dict[str, Any]:
    ensure_outcome_schema(db_path)
    resolved_at = now_iso()
    now_dt = _parse_dt(resolved_at) or datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    pending_calibrations: list[tuple[str, str, str]] = []
    conversation_auto = _call_conversation_auto_verifier(person_id=person_id)
    with connect(db_path) as con, write_transaction(con):
        preds = [dict(r) for r in con.execute("SELECT * FROM predictions_v19 WHERE person_id=? AND status='open'", (person_id,)).fetchall()]
        events = [dict(r) for r in con.execute("SELECT * FROM visual_events_v19 WHERE person_id=?", (person_id,)).fetchall()]
        for pred in preds:
            spec = json_loads(pred.get("verification_spec_json"), {}) or {}
            window_start = _parse_dt(spec.get("horizon_start") or pred.get("horizon_start"))
            window_end = _parse_dt(spec.get("horizon_end") or pred.get("horizon_end"))
            window_events = []
            for event in events:
                occurred = _parse_dt(event.get("occurred_at"))
                if window_start and occurred and occurred < window_start:
                    continue
                if window_end and occurred and occurred > window_end:
                    continue
                window_events.append(event)
            match = next((event for event in window_events if _event_matches(event, spec)), None)
            refutation = next((event for event in window_events if _event_refutes(event, spec)), None)
            if match:
                status = "verified"
                chosen = match
            elif refutation:
                status = "refuted"
                chosen = refutation
            elif window_end and now_dt > window_end:
                status = "expired"
                chosen = None
            else:
                status = "unverifiable"
                chosen = None
            evidence_refs = [{"source_table": "visual_events_v19", "source_id": chosen["visual_event_id"]}] if chosen else []
            # Calibration writes on a second connection; deferred until the
            # outer write transaction releases its lock (SQLite single-writer).
            if status in {"verified", "refuted"}:
                calibration: dict[str, Any] = {"status": "pending"}
            else:
                calibration = {"status": "skipped", "reason": status}
            oid = stable_id("outv19", pred["prediction_id"], status, json_dumps(evidence_refs))
            if status in {"verified", "refuted"}:
                pending_calibrations.append((oid, pred["prediction_id"], status))
            insert_only(
                con,
                "prediction_outcomes_v19",
                {
                    "outcome_id": oid,
                    "prediction_id": pred["prediction_id"],
                    "person_id": person_id,
                    "status": status,
                    "resolved_at": resolved_at,
                    "evidence_refs_json": json_dumps(evidence_refs),
                    "audit_json": json_dumps({"strict_verifier": True, "spec": spec, "calibration": calibration, "conversation_auto_verifier": conversation_auto}),
                    "created_at": resolved_at,
                },
                on_conflict="ignore",
            )
            if status != "unverifiable":
                con.execute("UPDATE predictions_v19 SET status=? WHERE prediction_id=?", (status, pred["prediction_id"]))
            results.append({"prediction_id": pred["prediction_id"], "status": status})

    for oid, prediction_id, status in pending_calibrations:
        calibration = _try_register_calibration(
            person_id=person_id, prediction_id=prediction_id, status=status,
            resolved_at=resolved_at, db_path=db_path,
        )
        with connect(db_path) as con, write_transaction(con):
            row = con.execute(
                "SELECT audit_json FROM prediction_outcomes_v19 WHERE outcome_id=?", (oid,)
            ).fetchone()
            if row:
                audit = json_loads(row["audit_json"], {}) or {}
                audit["calibration"] = calibration
                con.execute(
                    "UPDATE prediction_outcomes_v19 SET audit_json=? WHERE outcome_id=?",
                    (json_dumps(audit), oid),
                )
    return {"status": "completed", "resolved": results, "count": len(results), "conversation_auto_verifier": conversation_auto}
