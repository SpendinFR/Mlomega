"""Synthetic 30-day life generator for the V19 deep-memory tests.

Seeded and reproducible. Produces a plausible month of lived data purely
through the public V19 ingest/close-day surface:

- recurring **people** with short conversation transcripts;
- **places** with realistic transitions (home -> work -> cafe/gym -> home);
- **objects that move** (phone left behind, keys relocated);
- **routines with variability** (~80% adherence + occasional exceptions);
- a few **rare events** (parcel delivered, unplanned encounter);
- daily prediction emission + observational auto-resolution, yielding at least
  one ``verified`` and one ``refuted`` prediction and one conditional pattern.

The generator is deterministic given ``seed``; the assertions the tests rely on
hold for the default seed.
"""
from __future__ import annotations

import random
from typing import Any

from mlomega_audio_elite.db import connect
from mlomega_audio_elite.v19_life_model_store import apply_life_model_delta, run_life_model_v19_stage
from mlomega_audio_elite.v19_outcome_watcher import resolve_prediction_outcomes
from mlomega_audio_elite.v19_prediction_loop import emit_daily_predictions
from mlomega_audio_elite.v19_self_schema import rebuild_self_schema
from mlomega_audio_elite.v19_visual_consolidation import run_visual_consolidation
from mlomega_audio_elite.v19_visual_store import store_visual_event

PEOPLE = ["alex", "morgan", "sam"]
PLACES = ["home", "work", "cafe", "gym"]
CONVERSATION_SNIPPETS = {
    "alex": "on se voit au café demain matin ?",
    "morgan": "réunion projet cet après-midi au travail",
    "sam": "tu viens à la salle ce soir ?",
}


def _iso(day: int, hour: int, minute: int = 0) -> str:
    # June 2026 has 30 days; keep everything inside the month.
    d = min(max(day, 1), 30)
    return f"2026-06-{d:02d}T{hour:02d}:{minute:02d}:00+00:00"


def seed_synthetic_life_model(*, person_id: str, db_path=None) -> None:
    """Seed the durable life-model entries whose specs drive daily predictions."""
    # Morning cafe routine (verifiable, refuted by a gym visit in the window).
    apply_life_model_delta(
        person_id,
        {
            "dimension": "routines",
            "temporal_axis": "future_short",
            "statement": "routine déduite des preuves: visite café matinale attendue",
            "confidence": 0.78,
            "status": "active",
            "evidence_refs": [],
            "verification_spec": {
                "event_type": "visit",
                "place_label": "cafe",
                "sources": ["visual_events_v19"],
                "horizon_start_hour": 6,
                "horizon_end_hour": 11,
                "refutes": {"event_type": "visit", "place_label": "gym"},
            },
        },
        db_path=db_path,
    )
    # Phone visible during the day (verifiable object presence).
    apply_life_model_delta(
        person_id,
        {
            "dimension": "routines",
            "temporal_axis": "future_short",
            "statement": "routine déduite des preuves: téléphone visible attendu",
            "confidence": 0.66,
            "status": "active",
            "evidence_refs": [],
            "verification_spec": {
                "event_type": "object_seen",
                "entity_label": "phone",
                "sources": ["visual_events_v19"],
            },
        },
        db_path=db_path,
    )


def _visit(person_id: str, day: int, hour: int, place: str, session: str, db_path=None) -> None:
    store_visual_event(
        {
            "memory_owner_id": person_id,
            "live_session_id": session,
            "event_type": "visit",
            "occurred_at": _iso(day, hour),
            "entity": {"label": place, "kind": "place"},
            "place": {"label": place},
            "truth_level": "observed",
            "confidence": 0.9,
            "evidence": [{"frame_id": f"f-{place}-{day}-{hour}", "sha256": f"{place}{day}{hour}", "kind": "keyframe"}],
            "provenance": {"models": ["synthetic_life"]},
        },
        db_path=db_path,
    )


def _object_seen(person_id: str, day: int, hour: int, label: str, place: str, session: str, db_path=None) -> None:
    store_visual_event(
        {
            "memory_owner_id": person_id,
            "live_session_id": session,
            "event_type": "object_seen",
            "occurred_at": _iso(day, hour),
            "entity": {"label": label, "kind": "object"},
            "place": {"label": place},
            "observation": {"place": place},
            "truth_level": "observed",
            "confidence": 0.88,
            "evidence": [{"frame_id": f"o-{label}-{day}-{hour}", "sha256": f"{label}{day}{hour}", "kind": "keyframe"}],
            "provenance": {"models": ["synthetic_life"]},
        },
        db_path=db_path,
    )


def _conversation(person_id: str, day: int, hour: int, who: str, session: str, db_path=None) -> None:
    store_visual_event(
        {
            "memory_owner_id": person_id,
            "live_session_id": session,
            "event_type": "conversation",
            "occurred_at": _iso(day, hour),
            "entity": {"label": who, "kind": "person"},
            "observation": {"transcript": CONVERSATION_SNIPPETS.get(who, "..."), "speaker": who},
            "truth_level": "observed",
            "confidence": 0.85,
            "evidence": [{"frame_id": f"c-{who}-{day}", "sha256": f"c{who}{day}", "kind": "keyframe"}],
            "provenance": {"models": ["synthetic_life"]},
        },
        db_path=db_path,
    )


def run_synthetic_life(*, person_id: str = "me", days: int = 30, seed: int = 1337, db_path=None) -> dict[str, Any]:
    rng = random.Random(seed)
    seed_synthetic_life_model(person_id=person_id, db_path=db_path)

    phone_place = "home"  # phone last-seen location, moves occasionally

    for d in range(1, days + 1):
        date = f"2026-06-{min(d, 30):02d}"
        session = f"s-{d}"

        # Emit the day's predictions from the durable life model.
        emit_daily_predictions(person_id=person_id, package_date=date, db_path=db_path)

        # Morning: ~80% adherence to the cafe routine; the rest go to the gym.
        # Force a handful of deterministic gym mornings so at least one prediction
        # is refuted regardless of RNG drift.
        forced_gym = d in {3, 13, 23}
        if forced_gym or rng.random() > 0.8:
            _visit(person_id, d, 8, "gym", session, db_path=db_path)
        else:
            _visit(person_id, d, 8, "cafe", session, db_path=db_path)

        # Work most weekdays.
        if d % 7 not in {6, 0}:
            _visit(person_id, d, 10, "work", session, db_path=db_path)

        # A recurring person conversation (rotates through the social circle).
        who = PEOPLE[d % len(PEOPLE)]
        _conversation(person_id, d, 12, who, session, db_path=db_path)

        # Phone visible during the day; occasionally moved (left behind at work).
        if d % 4 == 0:
            phone_place = "work"  # object_moved: phone at a new place
        else:
            phone_place = "home"
        _object_seen(person_id, d, 13, "phone", phone_place, session, db_path=db_path)

        # Keys relocated on a couple of days.
        if d in {7, 21}:
            _object_seen(person_id, d, 19, "keys", "cafe", session, db_path=db_path)
        else:
            _object_seen(person_id, d, 19, "keys", "home", session, db_path=db_path)

        # Evening gym on some days (routine with variability).
        if d % 3 == 0:
            _visit(person_id, d, 20, "gym", session, db_path=db_path)

        # Rare events.
        if d == 15:
            store_visual_event(
                {
                    "memory_owner_id": person_id,
                    "live_session_id": session,
                    "event_type": "delivery",
                    "occurred_at": _iso(d, 16),
                    "entity": {"label": "parcel", "kind": "object"},
                    "place": {"label": "home"},
                    "truth_level": "observed",
                    "confidence": 0.8,
                    "evidence": [{"frame_id": f"parcel-{d}", "sha256": f"parcel{d}", "kind": "keyframe"}],
                    "provenance": {"models": ["synthetic_life"]},
                },
                db_path=db_path,
            )
        if d == 24:
            _conversation(person_id, d, 17, "morgan", session, db_path=db_path)  # unplanned encounter

        # Nightly consolidation, durable life-model update and outcome resolution.
        run_visual_consolidation(person_id=person_id, package_date=date, live_session_id=session, db_path=db_path)
        run_life_model_v19_stage(person_id=person_id, package_date=date, db_path=db_path)
        resolve_prediction_outcomes(person_id=person_id, package_date=date, db_path=db_path)

    rebuild_self_schema(person_id=person_id, db_path=db_path)

    with connect(db_path) as con:
        verified = con.execute(
            "SELECT COUNT(*) FROM prediction_outcomes_v19 WHERE person_id=? AND status='verified'", (person_id,)
        ).fetchone()[0]
        refuted = con.execute(
            "SELECT COUNT(*) FROM prediction_outcomes_v19 WHERE person_id=? AND status='refuted'", (person_id,)
        ).fetchone()[0]
        cond = con.execute(
            "SELECT COUNT(*) FROM self_schema_v19 WHERE person_id=? AND entry_type='conditionnel'", (person_id,)
        ).fetchone()[0]
        spatial_routines = con.execute(
            "SELECT COUNT(*) FROM brain2_spatial_routine_models WHERE person_id=?", (person_id,)
        ).fetchone()[0]
        object_moves = con.execute(
            "SELECT COUNT(*) FROM visual_events_v19 WHERE person_id=? AND event_type='object_moved'", (person_id,)
        ).fetchone()[0]

    return {
        "status": "completed",
        "days": days,
        "seed": seed,
        "routine_detected": spatial_routines > 0,
        "verified": verified,
        "refuted": refuted,
        "conditional_patterns": cond,
        "spatial_routines": spatial_routines,
        "object_moves": object_moves,
    }
