from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .db import connect, init_db, upsert, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id

DEFAULT_WEAKENING_DAYS = 30

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


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _set_status(con, *, entry_id: str, status: str, now: str, note: Mapping[str, Any]) -> None:
    row = con.execute(
        "SELECT revision_history_json FROM life_model_entries_v19 WHERE entry_id=?", (entry_id,)
    ).fetchone()
    if not row:
        return
    history = json_loads(row["revision_history_json"], [])
    if not isinstance(history, list):
        history = []
    history.append({"at": now, "operation": status, "note": dict(note)})
    con.execute(
        "UPDATE life_model_entries_v19 SET status=?, revision_history_json=?, updated_at=? WHERE entry_id=?",
        (status, json_dumps(history), now, entry_id),
    )


def weaken_stale_entries(
    person_id: str,
    *,
    as_of: str | None = None,
    stale_days: int = DEFAULT_WEAKENING_DAYS,
    db_path=None,
) -> list[str]:
    """Transition ``active`` entries not re-confirmed for ``stale_days`` to ``weakening``.

    An entry is never deleted silently: it merely decays in status so the
    projection/prediction layers stop treating it as fully current, while its
    revision history is preserved for audit.
    """
    ensure_life_model_store(db_path)
    now = now_iso()
    ref_dt = _parse_dt(as_of) or _parse_dt(now) or datetime.now(timezone.utc)
    cutoff = ref_dt - timedelta(days=max(0, int(stale_days)))
    weakened: list[str] = []
    with connect(db_path) as con, write_transaction(con):
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT entry_id, last_confirmed, first_observed, created_at FROM life_model_entries_v19 "
                "WHERE person_id=? AND status='active'",
                (person_id,),
            ).fetchall()
        ]
        for row in rows:
            last = _parse_dt(row.get("last_confirmed")) or _parse_dt(row.get("first_observed")) or _parse_dt(row.get("created_at"))
            if last is not None and last < cutoff:
                _set_status(
                    con,
                    entry_id=row["entry_id"],
                    status="weakening",
                    now=now,
                    note={"reason": "not_reconfirmed", "stale_days": int(stale_days), "last_confirmed": row.get("last_confirmed")},
                )
                weakened.append(row["entry_id"])
    return weakened


def mark_contradicted(
    person_id: str,
    entry_id: str,
    *,
    contradicting_ref: Mapping[str, Any],
    db_path=None,
) -> bool:
    """Move an entry to ``contradicted`` with a reference to the contradicting delta."""
    ensure_life_model_store(db_path)
    now = now_iso()
    with connect(db_path) as con, write_transaction(con):
        exists = con.execute(
            "SELECT 1 FROM life_model_entries_v19 WHERE entry_id=? AND person_id=?", (entry_id, person_id)
        ).fetchone()
        if not exists:
            return False
        _set_status(
            con,
            entry_id=entry_id,
            status="contradicted",
            now=now,
            note={"reason": "contradicted", "contradicting_ref": dict(contradicting_ref)},
        )
    return True


def run_life_model_v19_stage(
    *,
    person_id: str,
    package_date: str,
    stale_days: int = DEFAULT_WEAKENING_DAYS,
    db_path=None,
) -> dict[str, Any]:
    """Durable Life-Model V19 close-day stage (incremental deltas only).

    Collects the day's new facts — confirmed visual events, resolved prediction
    outcomes and confirmed patterns — and applies incremental deltas:
    - a matching visual/place fact ``confirms`` the corresponding routine entry
      (bumps ``last_confirmed``, appends history, never regenerates the model);
    - a ``refuted`` prediction outcome ``contradicts`` its source entry;
    - active entries not re-confirmed for ``stale_days`` decay to ``weakening``.

    This stage never regenerates the whole model and never deletes entries.
    """
    from .v19_visual_store import ensure_v19_visual_schema

    ensure_life_model_store(db_path)
    ensure_v19_visual_schema(db_path)
    day_start = f"{package_date}T00:00:00+00:00"
    day_end = f"{package_date}T23:59:59+00:00"
    confirmed: list[str] = []
    contradicted: list[str] = []

    with connect(db_path) as con:
        events = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM visual_events_v19 WHERE person_id=? AND occurred_at BETWEEN ? AND ?",
                (person_id, day_start, day_end),
            ).fetchall()
        ]
        entries = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM life_model_entries_v19 WHERE person_id=? AND status IN ('active','confirmed','weakening')",
                (person_id,),
            ).fetchall()
        ]
        try:
            refuted = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM prediction_outcomes_v19 WHERE person_id=? AND status='refuted' "
                    "AND resolved_at BETWEEN ? AND ?",
                    (person_id, day_start, day_end),
                ).fetchall()
            ]
        except Exception:
            refuted = []
        try:
            predictions = {
                r["prediction_id"]: dict(r)
                for r in con.execute("SELECT * FROM predictions_v19 WHERE person_id=?", (person_id,)).fetchall()
            }
        except Exception:
            predictions = {}

    # Confirm entries whose verification spec matched a day event.
    event_blobs = [
        (
            str(e.get("event_type") or "").lower(),
            " ".join(str(e.get(k) or "") for k in ("entity_json", "observation_json", "place_json")).lower(),
            e,
        )
        for e in events
    ]
    for entry in entries:
        spec = json_loads(entry.get("verification_spec_json"), {}) or {}
        if not isinstance(spec, dict) or not spec:
            continue
        want_type = str(spec.get("event_type") or "").lower()
        want_labels = [str(spec.get(k) or "").strip().lower() for k in ("entity_label", "place_label", "observation_contains")]
        want_labels = [w for w in want_labels if w]
        match = None
        for etype, blob, ev in event_blobs:
            if want_type and etype != want_type:
                continue
            if any(w not in blob for w in want_labels):
                continue
            match = ev
            break
        if match is not None:
            apply_life_model_delta(
                person_id,
                {
                    "entry_id": entry["entry_id"],
                    "dimension": entry["dimension"],
                    "temporal_axis": entry["temporal_axis"],
                    "statement": entry["statement"],
                    "operation": "confirm",
                    "status": "active" if entry["status"] == "weakening" else entry["status"],
                    "confidence": entry.get("confidence"),
                    "evidence_refs": [{"source_table": "visual_events_v19", "source_id": match["visual_event_id"]}],
                    "verification_spec": spec,
                    "last_confirmed": match.get("occurred_at"),
                },
                db_path=db_path,
            )
            confirmed.append(entry["entry_id"])

    # Contradict entries whose emitted prediction was refuted today.
    entries_by_pred_source: dict[str, str] = {}
    for entry in entries:
        entries_by_pred_source[entry["statement"]] = entry["entry_id"]
    for outcome in refuted:
        pred = predictions.get(outcome.get("prediction_id"), {})
        source_entry_id = entries_by_pred_source.get(str(pred.get("statement") or ""))
        if source_entry_id and source_entry_id not in contradicted:
            mark_contradicted(
                person_id,
                source_entry_id,
                contradicting_ref={"source_table": "prediction_outcomes_v19", "source_id": outcome["outcome_id"]},
                db_path=db_path,
            )
            contradicted.append(source_entry_id)

    weakened = weaken_stale_entries(person_id, as_of=day_end, stale_days=stale_days, db_path=db_path)

    total = 0
    with connect(db_path) as con:
        total = con.execute("SELECT COUNT(*) FROM life_model_entries_v19 WHERE person_id=?", (person_id,)).fetchone()[0]

    return {
        "status": "completed",
        "stage": "life_model_v19",
        "package_date": package_date,
        "confirmed": confirmed,
        "contradicted": contradicted,
        "weakened": weakened,
        "count": total,
    }
