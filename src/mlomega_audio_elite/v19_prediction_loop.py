from __future__ import annotations

from typing import Any, Mapping

from .db import connect, init_db, insert_only, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v19_life_model_store import ensure_life_model_store
from .v19_visual_store import ensure_v19_visual_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions_v19 (
  prediction_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  emitted_at TEXT NOT NULL,
  horizon_start TEXT,
  horizon_end TEXT,
  statement TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL,
  verification_spec_json TEXT NOT NULL,
  evidence_refs_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_v19_owner_status ON predictions_v19(person_id,status,horizon_end);
"""


def ensure_prediction_schema(db_path=None) -> None:
    init_db(db_path)
    ensure_v19_visual_schema(db_path)
    ensure_life_model_store(db_path)
    with connect(db_path) as con, write_transaction(con):
        con.executescript(SCHEMA)


def _usable_spec(raw: Any) -> dict[str, Any] | None:
    spec = json_loads(raw, {}) if isinstance(raw, str) else (dict(raw) if isinstance(raw, Mapping) else {})
    if not isinstance(spec, dict):
        return None
    sources = spec.get("sources") or spec.get("observation_sources") or []
    if "visual_events_v19" not in {str(x) for x in sources}:
        return None
    if not (spec.get("event_type") or spec.get("entity_label") or spec.get("place_label") or spec.get("observation_contains")):
        return None
    return spec


def _candidate_entries(con: Any, *, person_id: str, limit: int) -> list[dict[str, Any]]:
    rows = [
        dict(r)
        for r in con.execute(
            """SELECT * FROM life_model_entries_v19
               WHERE person_id=? AND status IN ('active','confirmed')
               ORDER BY COALESCE(last_confirmed, updated_at, created_at) DESC
               LIMIT ?""",
            (person_id, limit * 4),
        ).fetchall()
    ]
    out: list[dict[str, Any]] = []
    for row in rows:
        spec = _usable_spec(row.get("verification_spec_json") or row.get("prediction_template_json"))
        if not spec:
            continue
        out.append({**row, "verification_spec": spec})
        if len(out) >= limit:
            break
    return out


def emit_daily_predictions(*, person_id: str, package_date: str, db_path=None, limit: int = 7) -> dict[str, Any]:
    """Emit only predictions grounded in V19 life-model entries.

    No default prediction text or confidence is invented here.  A life-model
    entry must carry an explicit machine-verifiable spec whose sources include
    ``visual_events_v19``; otherwise emission abstains.
    """
    ensure_prediction_schema(db_path)
    now = now_iso()
    ids: list[str] = []
    rejected = 0
    with connect(db_path) as con, write_transaction(con):
        candidates = _candidate_entries(con, person_id=person_id, limit=max(1, int(limit)))
        rejected = con.execute(
            "SELECT COUNT(*) FROM life_model_entries_v19 WHERE person_id=? AND status IN ('active','confirmed')",
            (person_id,),
        ).fetchone()[0] - len(candidates)
        for entry in candidates:
            spec = dict(entry["verification_spec"])
            horizon_start = spec.get("horizon_start") or f"{package_date}T00:00:00+00:00"
            horizon_end = spec.get("horizon_end") or f"{package_date}T23:59:59+00:00"
            confidence = max(0.0, min(1.0, float(entry.get("confidence") or 0.0)))
            pid = stable_id("predv19", person_id, package_date, entry["entry_id"], json_dumps(spec))
            insert_only(
                con,
                "predictions_v19",
                {
                    "prediction_id": pid,
                    "person_id": person_id,
                    "emitted_at": now,
                    "horizon_start": horizon_start,
                    "horizon_end": horizon_end,
                    "statement": str(entry.get("statement") or ""),
                    "confidence": confidence,
                    "status": "open",
                    "verification_spec_json": json_dumps(spec),
                    "evidence_refs_json": entry.get("evidence_refs_json") or "[]",
                    "created_at": now,
                },
                on_conflict="ignore",
            )
            ids.append(pid)
    return {"status": "completed", "prediction_ids": ids, "count": len(ids), "rejected_unverifiable": max(0, rejected)}
