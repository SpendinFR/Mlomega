from __future__ import annotations

from typing import Any, Mapping

from .db import connect, init_db, upsert, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS life_model_entries_v19 (
  entry_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  dimension TEXT NOT NULL,
  temporal_axis TEXT NOT NULL,
  statement TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL,
  evidence_refs_json TEXT DEFAULT '[]',
  verification_spec_json TEXT DEFAULT '{}',
  prediction_template_json TEXT DEFAULT '{}',
  first_observed TEXT,
  last_confirmed TEXT,
  revision_history_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_model_entries_v19_owner ON life_model_entries_v19(person_id, dimension, status);
"""


def ensure_life_model_store(db_path=None) -> None:
    init_db(db_path)
    with connect(db_path) as con, write_transaction(con):
        con.executescript(SCHEMA)
        for col in ("verification_spec_json", "prediction_template_json"):
            try:
                con.execute(f"ALTER TABLE life_model_entries_v19 ADD COLUMN {col} TEXT DEFAULT '{{}}'")
            except Exception:
                pass


def apply_life_model_delta(person_id: str, delta: Mapping[str, Any], *, db_path=None) -> str:
    """Apply a typed incremental V19 life-model delta.

    The caller must provide the statement and evidence-bearing delta.  This
    helper preserves revision history and never regenerates the whole model.
    """
    ensure_life_model_store(db_path)
    now = now_iso()
    stmt = str(delta.get("statement") or "").strip()
    if not stmt:
        raise ValueError("life_model_v19 delta requires statement")
    operation = str(delta.get("operation") or "upsert")
    eid = str(delta.get("entry_id") or stable_id("lifev19", person_id, delta.get("dimension"), delta.get("temporal_axis"), stmt))
    with connect(db_path) as con, write_transaction(con):
        existing = con.execute("SELECT revision_history_json, created_at, first_observed FROM life_model_entries_v19 WHERE entry_id=?", (eid,)).fetchone()
        history = json_loads(existing["revision_history_json"], []) if existing else []
        if not isinstance(history, list):
            history = []
        history.append({"at": now, "operation": operation, "delta": dict(delta)})
        upsert(
            con,
            "life_model_entries_v19",
            {
                "entry_id": eid,
                "person_id": person_id,
                "dimension": str(delta.get("dimension") or "unspecified"),
                "temporal_axis": str(delta.get("temporal_axis") or "present"),
                "statement": stmt,
                "confidence": max(0.0, min(1.0, float(delta.get("confidence") or 0.5))),
                "status": str(delta.get("status") or "active"),
                "evidence_refs_json": json_dumps(delta.get("evidence_refs") or []),
                "verification_spec_json": json_dumps(delta.get("verification_spec") or {}),
                "prediction_template_json": json_dumps(delta.get("prediction_template") or {}),
                "first_observed": delta.get("first_observed") or (existing["first_observed"] if existing else now),
                "last_confirmed": delta.get("last_confirmed") or now,
                "revision_history_json": json_dumps(history),
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            },
            "entry_id",
        )
    return eid
