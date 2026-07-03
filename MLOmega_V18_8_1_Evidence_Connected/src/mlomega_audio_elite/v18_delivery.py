from __future__ import annotations

"""One H1-owned intervention delivery primitive for all BrainLive paths."""

from typing import Any, Mapping

from .db import connect, upsert, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v18_runtime_hardening import ensure_runtime_hardening_schema


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_intervention_delivery_queue(
  delivery_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  tick_id TEXT,
  candidate_id TEXT,
  horizon TEXT,
  message TEXT,
  action_type TEXT DEFAULT 'notify',
  delivery_status TEXT DEFAULT 'queued',
  priority REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_intervention_delivery_dedupes(
  dedupe_key TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  owner_horizon TEXT NOT NULL,
  candidate_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bld_delivery_session
  ON brainlive_intervention_delivery_queue(live_session_id,delivery_status,created_at);
CREATE INDEX IF NOT EXISTS idx_bld_delivery_dedupes_session
  ON brainlive_intervention_delivery_dedupes(live_session_id,created_at);
"""


def ensure_delivery_schema() -> None:
    ensure_runtime_hardening_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)


def _clamp(value: Any) -> float:
    try:
        numeric = float(value)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, numeric))


def _session_owner(con: Any, live_session_id: str) -> str:
    row = con.execute("SELECT person_id FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown BrainLive live session: {live_session_id}")
    return str(row["person_id"])


def _cooldown_suppressed(con: Any, *, person_id: str, cooldown_key: str | None) -> bool:
    if not cooldown_key:
        return False
    # V14 feedback keeps operational authority. A terminal user action must not
    # be resurrected by V15/V18 just because a later hot prompt repeats it.
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v14_7_intervention_queue'").fetchone()
    if not exists:
        return False
    row = con.execute(
        """SELECT 1 FROM v14_7_intervention_queue
           WHERE person_id=? AND cooldown_key=?
             AND status IN ('dismissed','acted','closed','cancelled','suppressed') LIMIT 1""",
        (person_id, cooldown_key),
    ).fetchone()
    return bool(row)


def enqueue_delivery(
    *,
    live_session_id: str,
    source_key: str,
    candidate: Mapping[str, Any],
    decision_run_id: str | None = None,
    hot_intervention_id: str | None = None,
    tick_id: str | None = None,
    con: Any | None = None,
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Write the one queue consumed by CLI/Phone Bridge, with H1-only ownership.

    ``source_key`` must identify the original signal/decision attempt. It is the
    durable idempotency boundary, so a retry after crash returns the same
    ``delivery_id`` rather than adding a second notification.
    """
    if not source_key or not str(source_key).strip():
        raise ValueError("delivery source_key is required for durable deduplication")
    if not schema_ready:
        ensure_delivery_schema()
    message = str(candidate.get("message") or candidate.get("text") or candidate.get("say") or candidate.get("intervention_message") or "").strip()
    if not message:
        return {"status": "skipped", "reason": "empty_message", "delivery_id": None}
    decision = str(candidate.get("decision") or "queue").lower()
    if decision not in {"queue", "speak_now", "proactive", "notify"}:
        return {"status": "skipped", "reason": f"non_delivery_decision:{decision}", "delivery_id": None}
    action_type = str(candidate.get("action_type") or candidate.get("intervention_type") or "notify")
    cooldown_key = candidate.get("cooldown_key")
    fingerprint = {
        "candidate_id": candidate.get("candidate_id"),
        "message": message,
        "action_type": action_type,
        "cooldown_key": cooldown_key,
        "recommended_timing": candidate.get("recommended_timing"),
    }

    def _write(tx: Any) -> dict[str, Any]:
        person_id = _session_owner(tx, live_session_id)
        if _cooldown_suppressed(tx, person_id=person_id, cooldown_key=str(cooldown_key) if cooldown_key else None):
            return {"status": "suppressed", "reason": "terminal_feedback_cooldown", "delivery_id": None}
        dedupe_key = stable_id("v18_delivery", person_id, live_session_id, source_key, fingerprint)
        row = tx.execute("SELECT delivery_id FROM brainlive_intervention_delivery_dedupes WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if row:
            return {"status": "deduplicated", "delivery_id": str(row["delivery_id"]), "dedupe_key": dedupe_key}
        delivery_id = stable_id("blddeliver", dedupe_key)
        now = now_iso()
        tx.execute(
            """INSERT INTO brainlive_intervention_delivery_dedupes(
                 dedupe_key,delivery_id,live_session_id,owner_horizon,candidate_fingerprint,created_at
               ) VALUES(?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING""",
            (dedupe_key, delivery_id, live_session_id, "H1", json_dumps(fingerprint), now),
        )
        winner = tx.execute("SELECT delivery_id FROM brainlive_intervention_delivery_dedupes WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if not winner:
            raise RuntimeError("delivery dedupe reservation disappeared")
        winner_id = str(winner["delivery_id"])
        if winner_id != delivery_id:
            return {"status": "deduplicated", "delivery_id": winner_id, "dedupe_key": dedupe_key}
        evidence = {
            "candidate": dict(candidate), "v18_dedupe_key": dedupe_key, "source_key": source_key,
            "delivery_owner_horizon": "H1", "decision_run_id": decision_run_id,
            "hot_intervention_id": hot_intervention_id,
        }
        upsert(tx, "brainlive_intervention_delivery_queue", {
            "delivery_id": delivery_id, "live_session_id": live_session_id,
            "tick_id": tick_id, "candidate_id": candidate.get("candidate_id"), "horizon": "H1",
            "message": message, "action_type": action_type, "delivery_status": "queued",
            "priority": _clamp(candidate.get("urgency") or candidate.get("priority") or candidate.get("expected_gain")),
            "evidence_json": json_dumps(evidence), "created_at": now, "delivered_at": None,
        }, "delivery_id")
        if decision_run_id or hot_intervention_id:
            tx.execute(
                """INSERT INTO v18_hot_delivery_links(delivery_link_id,decision_run_id,hot_intervention_id,delivery_id,live_session_id,source_key,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(decision_run_id,hot_intervention_id) DO UPDATE SET delivery_id=excluded.delivery_id,created_at=excluded.created_at""",
                (stable_id("v18deliverylink", decision_run_id or "", hot_intervention_id or "", delivery_id), decision_run_id, hot_intervention_id, delivery_id, live_session_id, source_key, now),
            )
        return {"status": "queued", "delivery_id": delivery_id, "dedupe_key": dedupe_key}

    if con is not None:
        return _write(con)
    with connect() as own, write_transaction(own):
        return _write(own)
