"""Handoff-mandated Lot 2 memory tests (the four named cases).

- test_prediction_auto_verified_by_observation
- test_life_model_update_is_incremental
- test_life_model_entry_weakens_without_confirmation
- test_self_schema_conditional_pattern_has_evidence
"""
import pytest

pytestmark = pytest.mark.memory


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))
    return db_path


def _seed_observed_cases(db_path, person_id):
    """Two chronologically ordered observed cases for strict calibration labels."""
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import ensure_longitudinal_case_schema
    from mlomega_audio_elite.db import connect, write_transaction, upsert
    from mlomega_audio_elite.utils import now_iso

    import os
    old = os.environ.get("MLOMEGA_DB")
    os.environ["MLOMEGA_DB"] = str(db_path)
    try:
        ensure_longitudinal_case_schema()
    finally:
        if old is not None:
            os.environ["MLOMEGA_DB"] = old
    now = now_iso()
    with connect(db_path) as con, write_transaction(con):
        for cid, observed_at in (("case-older", "2026-05-01T09:00:00+00:00"), ("case-newer", "2026-05-10T09:00:00+00:00")):
            upsert(con, "brain2_observed_cases_v17", {
                "observed_case_id": cid, "person_id": person_id, "case_type": "routine", "case_key": "cafe",
                "title": "cafe morning", "context_summary": "cafe", "observed_at": observed_at,
                "created_at": now, "updated_at": now,
            }, "observed_case_id")


def test_prediction_auto_verified_by_observation(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    person_id = "person-verify"

    from mlomega_audio_elite.v19_life_model_store import apply_life_model_delta
    from mlomega_audio_elite.v19_prediction_loop import emit_daily_predictions
    from mlomega_audio_elite.v19_outcome_watcher import resolve_prediction_outcomes
    from mlomega_audio_elite.v19_visual_store import store_visual_event
    from mlomega_audio_elite.db import connect

    _seed_observed_cases(db_path, person_id)

    # A durable, machine-verifiable life-model entry -> a prediction for the day.
    apply_life_model_delta(person_id, {
        "dimension": "routines", "temporal_axis": "future_short",
        "statement": "café matinal attendu", "confidence": 0.8, "status": "active",
        "verification_spec": {"event_type": "visit", "place_label": "cafe", "sources": ["visual_events_v19"]},
    }, db_path=db_path)

    emit_daily_predictions(person_id=person_id, package_date="2026-06-02", db_path=db_path)

    # The NEXT day the system observes a matching event - no user input at all.
    store_visual_event({
        "memory_owner_id": person_id, "live_session_id": "s1", "event_type": "visit",
        "occurred_at": "2026-06-02T08:00:00+00:00", "entity": {"label": "cafe"}, "place": {"label": "cafe"},
        "truth_level": "observed", "confidence": 0.9,
        "evidence": [{"frame_id": "f1", "sha256": "s", "kind": "keyframe"}],
    }, db_path=db_path)

    res = resolve_prediction_outcomes(person_id=person_id, package_date="2026-06-02", db_path=db_path)
    assert any(r["status"] == "verified" for r in res["resolved"])

    with connect(db_path) as con:
        verified = con.execute("SELECT COUNT(*) FROM prediction_outcomes_v19 WHERE person_id=? AND status='verified'", (person_id,)).fetchone()[0]
        assert verified >= 1
        # Strict verifier calibration label was recorded (no human input).
        labels = con.execute(
            "SELECT COUNT(*) FROM v18_predictive_similarity_labels WHERE person_id=? AND label_source='strict_verifier'",
            (person_id,),
        ).fetchone()[0]
        assert labels >= 1


def test_life_model_update_is_incremental(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    person_id = "person-incr"

    from mlomega_audio_elite.v19_life_model_store import apply_life_model_delta, run_life_model_v19_stage
    from mlomega_audio_elite.v19_visual_store import store_visual_event
    from mlomega_audio_elite.db import connect

    # Two independent entries; only the first is touched by day-2 evidence.
    touched = apply_life_model_delta(person_id, {
        "dimension": "routines", "temporal_axis": "future_short", "statement": "café matinal",
        "confidence": 0.7, "status": "active",
        "verification_spec": {"event_type": "visit", "place_label": "cafe", "sources": ["visual_events_v19"]},
    }, db_path=db_path)
    untouched = apply_life_model_delta(person_id, {
        "dimension": "relations", "temporal_axis": "present", "statement": "aime les longues marches",
        "confidence": 0.6, "status": "active",
        "verification_spec": {"event_type": "walk", "place_label": "park", "sources": ["visual_events_v19"]},
    }, db_path=db_path)

    def snapshot(entry_id):
        with connect(db_path) as con:
            r = dict(con.execute("SELECT updated_at, revision_history_json FROM life_model_entries_v19 WHERE entry_id=?", (entry_id,)).fetchone())
        return r

    before_untouched = snapshot(untouched)
    before_touched = snapshot(touched)

    # Night 1: no new evidence -> nothing confirmed.
    run_life_model_v19_stage(person_id=person_id, package_date="2026-06-01", db_path=db_path)

    # Day 2 evidence matches only the cafe entry.
    store_visual_event({
        "memory_owner_id": person_id, "live_session_id": "s", "event_type": "visit",
        "occurred_at": "2026-06-02T08:00:00+00:00", "entity": {"label": "cafe"}, "place": {"label": "cafe"},
        "truth_level": "observed", "confidence": 0.9,
        "evidence": [{"frame_id": "f", "sha256": "x", "kind": "keyframe"}],
    }, db_path=db_path)
    result = run_life_model_v19_stage(person_id=person_id, package_date="2026-06-02", db_path=db_path)

    # Only the cafe entry is in the delta set.
    assert touched in result["confirmed"]
    assert untouched not in result["confirmed"]

    after_touched = snapshot(touched)
    after_untouched = snapshot(untouched)

    # The touched entry advanced (new revision-history record); the untouched one is byte-identical.
    assert len(after_touched["revision_history_json"]) > len(before_touched["revision_history_json"])
    assert after_untouched["updated_at"] == before_untouched["updated_at"]
    assert after_untouched["revision_history_json"] == before_untouched["revision_history_json"]


def test_life_model_entry_weakens_without_confirmation(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    person_id = "person-weaken"

    from mlomega_audio_elite.v19_life_model_store import apply_life_model_delta, run_life_model_v19_stage
    from mlomega_audio_elite.db import connect

    stale = apply_life_model_delta(person_id, {
        "dimension": "routines", "temporal_axis": "future_short", "statement": "vieille routine",
        "confidence": 0.7, "status": "active",
        "first_observed": "2026-01-01T00:00:00+00:00", "last_confirmed": "2026-01-01T00:00:00+00:00",
    }, db_path=db_path)

    # Package date far past the default 30-day window since last confirmation.
    result = run_life_model_v19_stage(person_id=person_id, package_date="2026-06-15", db_path=db_path)
    assert stale in result["weakened"]

    with connect(db_path) as con:
        row = dict(con.execute("SELECT status FROM life_model_entries_v19 WHERE entry_id=?", (stale,)).fetchone())
    assert row["status"] == "weakening"
    # Never deleted silently.
    with connect(db_path) as con:
        assert con.execute("SELECT COUNT(*) FROM life_model_entries_v19 WHERE entry_id=?", (stale,)).fetchone()[0] == 1


def test_self_schema_conditional_pattern_has_evidence(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    person_id = "person-schema"

    from mlomega_audio_elite.v19_self_schema import rebuild_self_schema, ensure_self_schema
    from mlomega_audio_elite.db import connect, write_transaction, upsert
    from mlomega_audio_elite.utils import now_iso, json_loads

    ensure_self_schema(db_path)
    now = now_iso()
    # A confirmed pattern -> projected as a 'conditionnel' self-schema entry.
    with connect(db_path) as con, write_transaction(con):
        upsert(con, "confirmed_patterns", {
            "confirmed_pattern_id": "cp1", "person_id": person_id, "pattern_type": "conditional",
            "pattern_key": "cafe_then_focus", "title": "café puis concentration",
            "description": "quand il va au café le matin -> journée productive",
            "evidence_count": 8, "counterexample_count": 2, "usual_outcome": "productive",
            "confidence": 0.8, "validity_status": "active", "created_at": now, "updated_at": now,
        }, "confirmed_pattern_id")

    rebuild_self_schema(person_id=person_id, db_path=db_path)

    with connect(db_path) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM self_schema_v19 WHERE person_id=? AND entry_type='conditionnel'", (person_id,)
        ).fetchall()]
    assert rows, "expected at least one conditional pattern in the self schema"
    for row in rows:
        assert row["occurrence_rate"] is not None
        refs = json_loads(row["evidence_refs_json"], [])
        assert isinstance(refs, list) and len(refs) >= 1, f"conditional pattern lacks evidence_refs: {row}"
