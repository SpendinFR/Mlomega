"""V19 nightly visual consolidation (Lot 2, E15).

Consolidates the day's ``visual_events_v19`` purely from V19 tables (no
WorldBrain yet — that arrives in Lot 3). What it does, honestly and only from
observed rows:

1. Aggregate the day's events into a **last-seen per entity** map.
2. Detect **object moves**: an entity seen at a place different from its most
   recent previous last-seen emits an inferred ``object_moved`` event
   (``truth_level='inferred'``) that references both observations.
3. Detect **spatial routines**: the same (entity, place, time-slot) observed
   >=3 times upserts a ``brain2_spatial_routine_models`` row.
4. Write a ``scene_session_summaries_v19`` row summarising the day.

All writes stay owner-scoped and append-only where the target is immutable.
"""
from __future__ import annotations

from typing import Any

from .db import connect, insert_only, upsert, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v19_visual_store import ensure_v19_visual_schema, store_scene_summary

SPATIAL_ROUTINE_MIN_OCCURRENCES = 3


def _entity_key(event: dict[str, Any]) -> str | None:
    entity = json_loads(event.get("entity_json"), {}) or {}
    if not isinstance(entity, dict):
        return None
    key = entity.get("entity_id") or entity.get("label") or entity.get("kind")
    return str(key).strip().lower() if key else None


def _place_key(event: dict[str, Any]) -> str | None:
    place = json_loads(event.get("place_json"), {}) or {}
    if isinstance(place, dict):
        key = place.get("place_id") or place.get("label") or place.get("name")
        if key:
            return str(key).strip().lower()
    obs = json_loads(event.get("observation_json"), {}) or {}
    if isinstance(obs, dict) and obs.get("place"):
        return str(obs["place"]).strip().lower()
    return None


def _time_slot(occurred_at: str | None) -> str:
    """Coarse time-slot bucket derived from the ISO hour (no external deps)."""
    if not occurred_at:
        return "unknown"
    try:
        hour = int(str(occurred_at)[11:13])
    except Exception:
        return "unknown"
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def run_visual_consolidation(
    *,
    person_id: str,
    package_date: str,
    live_session_id: str | None = None,
    db_path=None,
) -> dict[str, Any]:
    ensure_v19_visual_schema(db_path)
    now = now_iso()
    day_start = f"{package_date}T00:00:00+00:00"
    day_end = f"{package_date}T23:59:59+00:00"

    with connect(db_path) as con:
        q = "SELECT * FROM visual_events_v19 WHERE person_id=? AND occurred_at BETWEEN ? AND ?"
        params: list[Any] = [person_id, day_start, day_end]
        if live_session_id:
            q += " AND live_session_id=?"
            params.append(live_session_id)
        q += " ORDER BY occurred_at ASC, created_at ASC"
        day_events = [dict(r) for r in con.execute(q, tuple(params)).fetchall()]
        # Prior last-seen per entity (before the day) to detect moves at day boundary.
        prior = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM visual_events_v19 WHERE person_id=? AND occurred_at < ? "
                "AND event_type != 'object_moved' ORDER BY occurred_at ASC, created_at ASC",
                (person_id, day_start),
            ).fetchall()
        ]

    # Build last-seen map from prior events.
    last_seen: dict[str, dict[str, Any]] = {}
    for ev in prior:
        ek = _entity_key(ev)
        if ek:
            last_seen[ek] = {"place": _place_key(ev), "event": ev}

    move_events: list[dict[str, Any]] = []
    routine_counts: dict[tuple[str, str, str], dict[str, Any]] = {}

    for ev in day_events:
        if str(ev.get("event_type")) == "object_moved":
            continue
        ek = _entity_key(ev)
        pk = _place_key(ev)
        if ek is None:
            continue
        # Spatial routine accumulation.
        if pk:
            slot = _time_slot(ev.get("occurred_at"))
            rk = (ek, pk, slot)
            bucket = routine_counts.setdefault(rk, {"count": 0, "refs": [], "first": ev.get("occurred_at"), "last": ev.get("occurred_at")})
            bucket["count"] += 1
            bucket["last"] = ev.get("occurred_at")
            if len(bucket["refs"]) < 20:
                bucket["refs"].append({"source_table": "visual_events_v19", "source_id": ev["visual_event_id"]})
        # Move detection vs prior last-seen.
        prev = last_seen.get(ek)
        if prev is not None and prev.get("place") and pk and prev["place"] != pk:
            move_events.append({"entity_key": ek, "from": prev, "to": ev})
        # Update last-seen.
        last_seen[ek] = {"place": pk, "event": ev}

    inferred_ids: list[str] = []
    routine_ids: list[str] = []
    with connect(db_path) as con, write_transaction(con):
        for mv in move_events:
            from_ev = mv["from"]["event"]
            to_ev = mv["to"]
            refs = [
                {"source_table": "visual_events_v19", "source_id": from_ev["visual_event_id"]},
                {"source_table": "visual_events_v19", "source_id": to_ev["visual_event_id"]},
            ]
            move_id = stable_id("v19move", person_id, from_ev["visual_event_id"], to_ev["visual_event_id"])
            wrote = insert_only(
                con,
                "visual_events_v19",
                {
                    "visual_event_id": move_id,
                    "person_id": person_id,
                    "live_session_id": to_ev.get("live_session_id") or (live_session_id or ""),
                    "event_type": "object_moved",
                    "occurred_at": to_ev.get("occurred_at") or now,
                    "entity_json": to_ev.get("entity_json") or "{}",
                    "observation_json": json_dumps({
                        "from_place": mv["from"].get("place"),
                        "to_place": _place_key(to_ev),
                        "from_event_id": from_ev["visual_event_id"],
                        "to_event_id": to_ev["visual_event_id"],
                    }),
                    "place_json": to_ev.get("place_json") or "{}",
                    "truth_level": "inferred",
                    "confidence": 0.6,
                    "evidence_refs_json": json_dumps(refs),
                    "provenance_json": json_dumps({"models": ["v19_visual_consolidation"]}),
                    "asset_id": None,
                    "created_at": now,
                },
                on_conflict="ignore",
            )
            if wrote:
                inferred_ids.append(move_id)

        for (ek, pk, slot), bucket in routine_counts.items():
            if bucket["count"] < SPATIAL_ROUTINE_MIN_OCCURRENCES:
                continue
            rid = stable_id("spatroutine", person_id, ek, pk, slot)
            existing = con.execute(
                "SELECT occurrence_count, first_observed, created_at FROM brain2_spatial_routine_models WHERE routine_id=?",
                (rid,),
            ).fetchone()
            first_observed = existing["first_observed"] if existing and existing["first_observed"] else bucket["first"]
            created_at = existing["created_at"] if existing else now
            upsert(
                con,
                "brain2_spatial_routine_models",
                {
                    "routine_id": rid,
                    "person_id": person_id,
                    "live_session_id": live_session_id,
                    "entity_key": ek,
                    "place_key": pk,
                    "time_slot": slot,
                    "occurrence_count": int(bucket["count"]),
                    "confidence": min(1.0, 0.5 + 0.1 * int(bucket["count"])),
                    "evidence_refs_json": json_dumps(bucket["refs"]),
                    "first_observed": first_observed,
                    "last_observed": bucket["last"],
                    "updated_at": now,
                    "created_at": created_at,
                },
                "routine_id",
            )
            routine_ids.append(rid)

    summary_id = None
    if day_events:
        summary_id = store_scene_summary(
            {
                "memory_owner_id": person_id,
                "live_session_id": live_session_id or day_events[-1]["live_session_id"],
                "summary_start": day_events[0]["occurred_at"],
                "summary_end": day_events[-1]["occurred_at"],
                "summary": {
                    "event_count": len(day_events),
                    "event_types": sorted({r["event_type"] for r in day_events}),
                    "entities_last_seen": {k: v.get("place") for k, v in last_seen.items()},
                    "object_moves": len(inferred_ids),
                    "spatial_routines": len(routine_ids),
                },
                "evidence_refs": [
                    {"source_table": "visual_events_v19", "source_id": r["visual_event_id"]} for r in day_events[:20]
                ],
            },
            db_path=db_path,
        )

    return {
        "status": "completed",
        "stage": "visual_consolidation",
        "summary_id": summary_id,
        "visual_event_count": len(day_events),
        "object_moved_count": len(inferred_ids),
        "spatial_routine_count": len(routine_ids),
        "package_date": package_date,
    }
