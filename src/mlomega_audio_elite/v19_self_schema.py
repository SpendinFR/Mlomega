from __future__ import annotations

from typing import Any

from .db import connect, init_db, upsert, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v19_life_model_store import ensure_life_model_store
from .v19_outcome_watcher import ensure_outcome_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS self_schema_v19 (
  schema_entry_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  entry_type TEXT NOT NULL,
  statement TEXT NOT NULL,
  occurrence_rate REAL,
  evidence_refs_json TEXT DEFAULT '[]',
  source_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_schema_v19_owner_type ON self_schema_v19(person_id,entry_type);
"""


def ensure_self_schema(db_path=None) -> None:
    init_db(db_path)
    ensure_life_model_store(db_path)
    ensure_outcome_schema(db_path)
    with connect(db_path) as con, write_transaction(con):
        con.executescript(SCHEMA)


def _refs_from_json(raw: Any) -> list[dict[str, Any]]:
    refs = json_loads(raw, []) if isinstance(raw, str) else raw
    return [dict(x) for x in refs if isinstance(x, dict)] if isinstance(refs, list) else []


def rebuild_self_schema(*, person_id: str, db_path=None) -> dict[str, Any]:
    """Project self-schema only from durable model/pattern/outcome sources."""
    ensure_self_schema(db_path)
    now = now_iso()
    entries: list[str] = []
    with connect(db_path) as con, write_transaction(con):
        life_entries = [dict(r) for r in con.execute("SELECT * FROM life_model_entries_v19 WHERE person_id=? AND status IN ('active','confirmed','weakening')", (person_id,)).fetchall()]
        for entry in life_entries:
            entry_type = "veut" if entry["dimension"] in {"goals", "envies", "projects"} else ("aime" if entry["dimension"] in {"values", "preferences"} else "a_fait")
            sid = stable_id("schema", person_id, "life", entry["entry_id"])
            upsert(
                con,
                "self_schema_v19",
                {
                    "schema_entry_id": sid,
                    "person_id": person_id,
                    "entry_type": entry_type,
                    "statement": entry["statement"],
                    "occurrence_rate": entry.get("confidence"),
                    "evidence_refs_json": entry.get("evidence_refs_json") or "[]",
                    "source_json": json_dumps({"source_table": "life_model_entries_v19", "source_id": entry["entry_id"], "status": entry["status"]}),
                    "created_at": now,
                    "updated_at": now,
                },
                "schema_entry_id",
            )
            entries.append(sid)
        patterns = [dict(r) for r in con.execute("SELECT * FROM confirmed_patterns WHERE person_id=? AND validity_status IN ('active','confirmed')", (person_id,)).fetchall()]
        for pattern in patterns:
            sid = stable_id("schema", person_id, "pattern", pattern["confirmed_pattern_id"])
            total = int(pattern.get("evidence_count") or 0) + int(pattern.get("counterexample_count") or 0)
            occurrence = (int(pattern.get("evidence_count") or 0) / total) if total else pattern.get("confidence")
            refs = [{"source_table": "confirmed_patterns", "source_id": pattern["confirmed_pattern_id"]}]
            upsert(con, "self_schema_v19", {"schema_entry_id": sid, "person_id": person_id, "entry_type": "conditionnel", "statement": pattern.get("description") or pattern.get("title") or pattern.get("pattern_key"), "occurrence_rate": occurrence, "evidence_refs_json": json_dumps(refs), "source_json": json_dumps({"source_table": "confirmed_patterns", "source_id": pattern["confirmed_pattern_id"], "usual_outcome": pattern.get("usual_outcome")}), "created_at": now, "updated_at": now}, "schema_entry_id")
            entries.append(sid)
        causal = [dict(r) for r in con.execute("SELECT * FROM causal_edges WHERE truth_status IN ('active','confirmed','hypothesis') ORDER BY updated_at DESC LIMIT 50").fetchall()]
        for edge in causal:
            sid = stable_id("schema", person_id, "causal", edge["causal_edge_id"])
            refs = [{"source_table": "causal_edges", "source_id": edge["causal_edge_id"]}]
            statement = f"{edge['from_table']}:{edge['from_id']} -> {edge['to_table']}:{edge['to_id']}"
            upsert(con, "self_schema_v19", {"schema_entry_id": sid, "person_id": person_id, "entry_type": "causal", "statement": statement, "occurrence_rate": edge.get("confidence"), "evidence_refs_json": json_dumps(refs), "source_json": json_dumps({"source_table": "causal_edges", "source_id": edge["causal_edge_id"], "strength": edge.get("strength")}), "created_at": now, "updated_at": now}, "schema_entry_id")
            entries.append(sid)
        outcomes = [dict(r) for r in con.execute("SELECT * FROM prediction_outcomes_v19 WHERE person_id=?", (person_id,)).fetchall()]
        for outcome in outcomes:
            if outcome["status"] not in {"verified", "refuted"}:
                continue
            sid = stable_id("schema", person_id, "outcome", outcome["outcome_id"])
            refs = _refs_from_json(outcome.get("evidence_refs_json")) or [{"source_table": "prediction_outcomes_v19", "source_id": outcome["outcome_id"]}]
            upsert(con, "self_schema_v19", {"schema_entry_id": sid, "person_id": person_id, "entry_type": "conditionnel", "statement": f"prediction {outcome['prediction_id']} {outcome['status']}", "occurrence_rate": 1.0 if outcome["status"] == "verified" else 0.0, "evidence_refs_json": json_dumps(refs), "source_json": json_dumps({"source_table": "prediction_outcomes_v19", "source_id": outcome["outcome_id"]}), "created_at": now, "updated_at": now}, "schema_entry_id")
            entries.append(sid)
    return {"status": "completed", "schema_entry_ids": entries, "count": len(entries)}


def get_self_schema(*, person_id: str, db_path=None, limit: int = 20) -> list[dict[str, Any]]:
    ensure_self_schema(db_path)
    with connect(db_path) as con:
        return [dict(r) for r in con.execute("SELECT * FROM self_schema_v19 WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)).fetchall()]
