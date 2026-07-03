"""Lifecycle bridge for V14 trajectory forecasts and watch queues.

V14 stored a textual status only.  V18 treats the old rows as source records and
keeps a separate, owner-scoped lifecycle ledger that is the sole authority for
live/coordination/Life Model selection.  Missing or ambiguous deadlines fail
closed as ``indeterminate`` rather than becoming perpetual live context.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .db import connect, init_db
from .utils import json_dumps, now_iso


LIFECYCLE_TABLE = "v18_legacy_forecast_lifecycle"
_SOURCE_SPECS = {
    "v14_trajectory_forecasts": {"id": "forecast_id", "horizon": "time_horizon", "kind": "trajectory"},
    "v14_forecast_watch_queue": {"id": "watch_id", "horizon": "due_horizon", "kind": "watch"},
}
_ACTIVE_STATES = {"open"}
_TERMINAL_STATES = {"evaluated_correct", "evaluated_incorrect", "expired", "indeterminate", "cancelled"}

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS v18_legacy_forecast_lifecycle(
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_status TEXT,
  due_at TEXT,
  expires_at TEXT,
  lifecycle_state TEXT NOT NULL,
  evaluated_at TEXT,
  outcome_json TEXT DEFAULT '{}',
  source_snapshot_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(source_table, source_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_legacy_forecast_lifecycle_active
  ON v18_legacy_forecast_lifecycle(person_id, lifecycle_state, due_at);
CREATE INDEX IF NOT EXISTS idx_v18_legacy_forecast_lifecycle_source
  ON v18_legacy_forecast_lifecycle(source_table, source_id, person_id);
"""


def _dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _parse(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _horizon_due_at(created_at: str | None, horizon: Any) -> str | None:
    base = _parse(created_at)
    if base is None:
        return None
    raw = str(horizon or "").strip()
    explicit = _parse(raw)
    if explicit is not None:
        return _iso(explicit)
    key = raw.lower().replace("-", "_").replace(" ", "_")
    offsets = {
        "h0": timedelta(seconds=10),
        "h1": timedelta(minutes=5),
        "h2": timedelta(hours=2),
        "next_message": timedelta(minutes=10),
        "next_turn": timedelta(minutes=10),
        "immediate": timedelta(minutes=10),
        "today": timedelta(days=1),
        "day": timedelta(days=1),
        "this_week": timedelta(days=7),
        "week": timedelta(days=7),
        "month": timedelta(days=30),
        "long_term": timedelta(days=90),
        "long": timedelta(days=90),
    }
    delta = offsets.get(key)
    return _iso(base + delta) if delta is not None else None


def _expires_at(due_at: str | None) -> str | None:
    due = _parse(due_at)
    # A V14 item that reached due must be evaluated within a bounded grace
    # period. It can never stay active indefinitely after the expected window.
    return _iso(due + timedelta(days=7)) if due is not None else None


def _state_from_source(*, source_status: str | None, due_at: str | None, expires_at: str | None, now: datetime) -> str:
    status = str(source_status or "").strip().lower()
    if status in {"evaluated_correct", "correct", "verified", "confirmed", "completed"}:
        return "evaluated_correct"
    if status in {"evaluated_incorrect", "incorrect", "wrong", "contradicted"}:
        return "evaluated_incorrect"
    if status in {"expired", "stale"}:
        return "expired"
    if status in {"indeterminate", "unknown", "cancelled", "canceled", "closed", "dismissed"}:
        return "indeterminate" if status not in {"cancelled", "canceled"} else "cancelled"
    due = _parse(due_at)
    expires = _parse(expires_at)
    if due is None:
        return "indeterminate"
    if expires is not None and now >= expires:
        return "expired"
    if now >= due:
        return "due"
    return "open"


def ensure_legacy_forecast_lifecycle_schema(con=None) -> None:
    """Create the V18 ledger without importing/initialising V14 modules."""
    if con is not None:
        con.executescript(SCHEMA)
        return
    init_db()
    with connect() as own:
        own.executescript(SCHEMA)
        own.commit()


def reconcile_legacy_forecasts(*, person_id: str | None = None, con=None, now: str | None = None) -> dict[str, int]:
    """Derive lifecycle state from legacy source rows deterministically.

    This is idempotent.  It does not pretend an unknown time horizon has a
    deadline: those items are marked indeterminate and excluded from live use.
    """
    owns_connection = con is None
    if owns_connection:
        init_db()
        con = connect()
    assert con is not None
    try:
        ensure_legacy_forecast_lifecycle_schema(con)
        current = _parse(now or now_iso()) or datetime.now(timezone.utc)
        counts = {"seen": 0, "open": 0, "due": 0, "expired": 0, "indeterminate": 0, "terminal": 0}
        for table, spec in _SOURCE_SPECS.items():
            if not _table_exists(con, table):
                continue
            params: list[Any] = []
            sql = f"SELECT * FROM {table}"
            if person_id:
                sql += " WHERE person_id=?"
                params.append(person_id)
            for row in _dicts(con.execute(sql, tuple(params)).fetchall()):
                source_id = str(row.get(spec["id"]) or "")
                owner = str(row.get("person_id") or "")
                if not source_id or not owner:
                    continue
                due_at = _horizon_due_at(row.get("created_at"), row.get(spec["horizon"]))
                expires_at = _expires_at(due_at)
                state = _state_from_source(
                    source_status=row.get("status"), due_at=due_at, expires_at=expires_at, now=current
                )
                evaluated_at = row.get("verified_at") if state.startswith("evaluated") else None
                updated = now_iso()
                con.execute(
                    """INSERT INTO v18_legacy_forecast_lifecycle(
                           source_table,source_id,person_id,source_kind,source_status,due_at,expires_at,
                           lifecycle_state,evaluated_at,outcome_json,source_snapshot_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_table,source_id) DO UPDATE SET
                         person_id=excluded.person_id, source_kind=excluded.source_kind,
                         source_status=excluded.source_status, due_at=excluded.due_at,
                         expires_at=excluded.expires_at, lifecycle_state=excluded.lifecycle_state,
                         evaluated_at=COALESCE(v18_legacy_forecast_lifecycle.evaluated_at, excluded.evaluated_at),
                         source_snapshot_json=excluded.source_snapshot_json, updated_at=excluded.updated_at""",
                    (
                        table, source_id, owner, spec["kind"], row.get("status"), due_at, expires_at,
                        state, evaluated_at, "{}", json_dumps(row), row.get("created_at") or updated, updated,
                    ),
                )
                counts["seen"] += 1
                if state in counts:
                    counts[state] += 1
                elif state in _TERMINAL_STATES:
                    counts["terminal"] += 1
        if owns_connection:
            con.commit()
        return counts
    finally:
        if owns_connection:
            con.close()


def active_legacy_forecasts(con, person_id: str, source_table: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return only owner-scoped, future-due lifecycle rows for a V14 table."""
    if source_table not in _SOURCE_SPECS:
        raise ValueError(f"unsupported legacy forecast table: {source_table}")
    reconcile_legacy_forecasts(person_id=person_id, con=con)
    if not _table_exists(con, source_table):
        return []
    spec = _SOURCE_SPECS[source_table]
    now = now_iso()
    sql = f"""
        SELECT s.*, l.due_at AS v18_due_at, l.expires_at AS v18_expires_at,
               l.lifecycle_state AS v18_lifecycle_state, l.updated_at AS v18_lifecycle_updated_at
        FROM {source_table} s
        JOIN {LIFECYCLE_TABLE} l
          ON l.source_table=? AND l.source_id=s.{spec['id']} AND l.person_id=s.person_id
        WHERE s.person_id=? AND l.lifecycle_state='open' AND l.due_at IS NOT NULL AND l.due_at>?
        ORDER BY l.due_at ASC, s.confidence DESC, s.created_at DESC
        LIMIT ?
    """
    return _dicts(con.execute(sql, (source_table, person_id, now, int(limit))).fetchall())


def record_legacy_forecast_outcome(
    *,
    source_table: str,
    source_id: str,
    person_id: str,
    correct: bool | None,
    evidence: Any = None,
) -> dict[str, Any]:
    """Close a V14 lifecycle row with an explicit owner-scoped outcome."""
    if source_table not in _SOURCE_SPECS:
        raise ValueError(f"unsupported legacy forecast table: {source_table}")
    if not person_id:
        raise ValueError("person_id is required")
    ensure_legacy_forecast_lifecycle_schema()
    with connect() as con:
        reconcile_legacy_forecasts(person_id=person_id, con=con)
        row = con.execute(
            "SELECT * FROM v18_legacy_forecast_lifecycle WHERE source_table=? AND source_id=? AND person_id=?",
            (source_table, source_id, person_id),
        ).fetchone()
        if not row:
            raise ValueError("legacy forecast is unknown or not owned by this person")
        state = "indeterminate" if correct is None else ("evaluated_correct" if correct else "evaluated_incorrect")
        evaluated = now_iso()
        con.execute(
            "UPDATE v18_legacy_forecast_lifecycle SET lifecycle_state=?, evaluated_at=?, outcome_json=?, updated_at=? WHERE source_table=? AND source_id=? AND person_id=?",
            (state, evaluated, json_dumps({"correct": correct, "evidence": evidence}), evaluated, source_table, source_id, person_id),
        )
        # Retain the legacy textual field only as a compatibility display value;
        # live selection always reads the V18 ledger above.
        spec = _SOURCE_SPECS[source_table]
        con.execute(
            f"UPDATE {source_table} SET status=? WHERE {spec['id']}=? AND person_id=?",
            (state, source_id, person_id),
        )
        con.commit()
    return {"source_table": source_table, "source_id": source_id, "person_id": person_id, "lifecycle_state": state, "evaluated_at": evaluated}


def legacy_forecast_audit(*, person_id: str | None = None) -> dict[str, Any]:
    ensure_legacy_forecast_lifecycle_schema()
    with connect() as con:
        summary = reconcile_legacy_forecasts(person_id=person_id, con=con)
        params: list[Any] = []
        sql = "SELECT source_table,lifecycle_state,COUNT(*) AS n FROM v18_legacy_forecast_lifecycle"
        if person_id:
            sql += " WHERE person_id=?"
            params.append(person_id)
        sql += " GROUP BY source_table,lifecycle_state ORDER BY source_table,lifecycle_state"
        rows = _dicts(con.execute(sql, tuple(params)).fetchall())
    return {"contract": "V14 forecasts are selectable only through the V18 lifecycle ledger", "reconciled": summary, "states": rows}
