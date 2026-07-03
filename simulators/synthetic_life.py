from __future__ import annotations

from typing import Any

from mlomega_audio_elite.db import connect
from mlomega_audio_elite.v19_life_model_store import apply_life_model_delta
from mlomega_audio_elite.v19_outcome_watcher import resolve_prediction_outcomes
from mlomega_audio_elite.v19_prediction_loop import emit_daily_predictions
from mlomega_audio_elite.v19_self_schema import rebuild_self_schema
from mlomega_audio_elite.v19_visual_store import store_visual_event


def seed_synthetic_life_model(*, person_id: str, db_path=None) -> None:
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
                "refutes": {"event_type": "visit", "place_label": "gym"},
            },
        },
        db_path=db_path,
    )
    apply_life_model_delta(
        person_id,
        {
            "dimension": "routines",
            "temporal_axis": "future_short",
            "statement": "routine déduite des preuves: téléphone visible attendu",
            "confidence": 0.66,
            "status": "active",
            "evidence_refs": [],
            "verification_spec": {"event_type": "object_seen", "entity_label": "phone", "sources": ["visual_events_v19"]},
        },
        db_path=db_path,
    )


def run_synthetic_life(*, person_id: str = "me", days: int = 30, db_path=None) -> dict[str, Any]:
    seed_synthetic_life_model(person_id=person_id, db_path=db_path)
    for d in range(1, days + 1):
        date = f"2026-06-{d:02d}" if d <= 30 else "2026-07-01"
        emit_daily_predictions(person_id=person_id, package_date=date, db_path=db_path)
        if d % 2 == 0:
            store_visual_event({"memory_owner_id": person_id, "live_session_id": f"s-{d}", "event_type": "visit", "occurred_at": date + "T08:00:00+00:00", "entity": {"label": "cafe"}, "place": {"label": "cafe"}, "truth_level": "observed", "confidence": 0.9, "evidence": [{"frame_id": f"f-{d}", "sha256": str(d), "kind": "keyframe"}]}, db_path=db_path)
        else:
            store_visual_event({"memory_owner_id": person_id, "live_session_id": f"s-{d}", "event_type": "visit", "occurred_at": date + "T18:00:00+00:00", "entity": {"label": "gym"}, "place": {"label": "gym"}, "truth_level": "observed", "confidence": 0.9, "evidence": [{"frame_id": f"f-{d}", "sha256": str(d), "kind": "keyframe"}]}, db_path=db_path)
        if d % 5 == 0:
            store_visual_event({"memory_owner_id": person_id, "live_session_id": f"s-{d}", "event_type": "object_seen", "occurred_at": date + "T12:00:00+00:00", "entity": {"label": "phone"}, "truth_level": "observed", "confidence": 0.9, "evidence": [{"frame_id": f"p-{d}", "sha256": "p" + str(d), "kind": "keyframe"}]}, db_path=db_path)
        resolve_prediction_outcomes(person_id=person_id, package_date=date, db_path=db_path)
    rebuild_self_schema(person_id=person_id, db_path=db_path)
    with connect(db_path) as con:
        verified = con.execute("SELECT COUNT(*) FROM prediction_outcomes_v19 WHERE person_id=? AND status='verified'", (person_id,)).fetchone()[0]
        refuted = con.execute("SELECT COUNT(*) FROM prediction_outcomes_v19 WHERE person_id=? AND status='refuted'", (person_id,)).fetchone()[0]
        cond = con.execute("SELECT COUNT(*) FROM self_schema_v19 WHERE person_id=? AND entry_type='conditionnel'", (person_id,)).fetchone()[0]
    return {"status": "completed", "days": days, "routine_detected": verified > 0, "verified": verified, "refuted": refuted, "conditional_patterns": cond}
