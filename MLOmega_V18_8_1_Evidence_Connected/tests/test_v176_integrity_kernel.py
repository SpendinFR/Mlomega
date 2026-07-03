from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from mlomega_audio_elite.brainlive_v15 import ensure_brainlive_schema, start_live_session
from mlomega_audio_elite.db import connect
from mlomega_audio_elite.integrity_v176 import (
    ContractValidationError,
    LifecycleError,
    create_forecast,
    parse_iso_utc,
    record_forecast_outcome,
    validate_brainlive_output,
)


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")


def _forecast_payload(**overrides):
    payload = {
        "horizon": "H0",
        "forecast_type": "action",
        "predicted_need": None,
        "predicted_action": "open the notebook",
        "predicted_words": None,
        "predicted_emotion": None,
        "predicted_risk": None,
        "predicted_opportunity": None,
        "if_intervene_future": None,
        "if_silent_future": None,
        "expected_gain": 0.4,
        "probability": 0.55,
        "confidence": 0.91,
        "evidence": [{"turn_id": "t1"}],
        "counter_evidence": [],
    }
    payload.update(overrides)
    return payload


def test_forecast_lifecycle_closes_and_keeps_probability_separate(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    ensure_brainlive_schema()
    session = start_live_session(person_id="me", title="test")
    with connect() as con:
        forecast = create_forecast(
            con,
            live_session_id=session["live_session_id"],
            person_id="me",
            event_id=None,
            run_id="run-1",
            payload=_forecast_payload(),
            occurred_at="2026-01-01T10:00:00+00:00",
            source="test",
        )
        con.commit()
    assert forecast["probability"] == 0.55
    assert forecast["epistemic_confidence"] == 0.91
    assert parse_iso_utc(forecast["due_at"]) - parse_iso_utc(forecast["occurred_at"]) == timedelta(seconds=10)

    with connect() as con:
        result = record_forecast_outcome(
            con,
            forecast_id=forecast["forecast_id"],
            person_id="me",
            observed_after="2026-01-01T10:00:11+00:00",
            was_prediction_correct=True,
            match_score=0.8,
            outcome_window="test",
            actor="test",
        )
        con.commit()
        row = con.execute("SELECT lifecycle_state,status,canonical_outcome_id FROM brainlive_short_horizon_forecasts WHERE forecast_id=?", (forecast["forecast_id"],)).fetchone()
    assert result["outcome_id"] == row["canonical_outcome_id"]
    assert row["lifecycle_state"] == "evaluated_correct"
    assert row["status"] == "evaluated_correct"


def test_orphan_and_duplicate_outcomes_are_blocked(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    ensure_brainlive_schema()
    session = start_live_session(person_id="me")
    with connect() as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """INSERT INTO brainlive_prediction_outcomes(outcome_id,live_session_id,forecast_id,candidate_id,person_id,observed_after,outcome_window,was_prediction_correct,match_score,user_feedback,lesson_json,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("orphan", session["live_session_id"], "missing", None, "me", "2026-01-01T10:00:00+00:00", "manual", 1, 1.0, None, "{}", "[]", "2026-01-01T10:00:00+00:00"),
            )
        forecast = create_forecast(
            con, live_session_id=session["live_session_id"], person_id="me", event_id=None, run_id="run", payload=_forecast_payload(), occurred_at="2026-01-01T10:00:00+00:00", source="test"
        )
        record_forecast_outcome(
            con, forecast_id=forecast["forecast_id"], person_id="me", observed_after="2026-01-01T10:00:11+00:00", was_prediction_correct=False, match_score=0.1, outcome_window="test", actor="test"
        )
        with pytest.raises(LifecycleError):
            record_forecast_outcome(
                con, forecast_id=forecast["forecast_id"], person_id="me", observed_after="2026-01-01T10:00:12+00:00", was_prediction_correct=True, match_score=0.9, outcome_window="test", actor="test"
            )
        con.commit()


def test_invalid_contract_is_rejected_not_normalized(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    valid = {
        "world_state": {"where_am_i": "desk", "what_is_happening": "work", "active_mode": "work", "probable_activity": [], "active_emotional_state": None, "confidence": 0.5, "evidence": [], "counter_evidence": []},
        "events": [], "need_predictions": [], "affordances": [], "forecasts": [], "life_hypotheses": [], "interventions": [], "watch_next": [], "notes_for_brain2": [],
    }
    assert validate_brainlive_output(valid)["world_state"]["active_mode"] == "work"
    invalid = dict(valid)
    invalid["world_state"] = dict(valid["world_state"], confidence=float("nan"))
    with pytest.raises(ContractValidationError):
        validate_brainlive_output(invalid)
    invalid2 = dict(valid)
    invalid2["unexpected"] = True
    with pytest.raises(ContractValidationError):
        validate_brainlive_output(invalid2)


def test_identical_input_is_idempotent_per_run_but_not_hash_only(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    ensure_brainlive_schema()
    session = start_live_session(person_id="me")
    with connect() as con:
        first = create_forecast(con, live_session_id=session["live_session_id"], person_id="me", event_id=None, run_id="same", payload=_forecast_payload(), occurred_at="2026-01-01T10:00:00+00:00", source="test")
        second = create_forecast(con, live_session_id=session["live_session_id"], person_id="me", event_id=None, run_id="same", payload=_forecast_payload(), occurred_at="2026-01-01T10:00:00+00:00", source="test")
        later = create_forecast(con, live_session_id=session["live_session_id"], person_id="me", event_id=None, run_id="other", payload=_forecast_payload(), occurred_at="2026-01-01T10:00:01+00:00", source="test")
        con.commit()
    assert first["forecast_id"] == second["forecast_id"]
    assert later["forecast_id"] != first["forecast_id"]


def test_event_envelope_dedupes_same_occurrence_not_same_hash(monkeypatch, tmp_path):
    from mlomega_audio_elite.integrity_v176 import record_event_envelope

    _configure(monkeypatch, tmp_path)
    same = record_event_envelope(
        modality="audio", source_device="phone-a", person_id="me",
        occurred_at="2026-01-01T10:00:00+00:00", source_event_id="event-7",
        source_sha256="same-content", payload={"duration_s": 4},
    )
    retry = record_event_envelope(
        modality="audio", source_device="phone-a", person_id="me",
        occurred_at="2026-01-01T10:00:00+00:00", source_event_id="event-7",
        source_sha256="same-content", payload={"duration_s": 4},
    )
    later_occurrence = record_event_envelope(
        modality="audio", source_device="phone-a", person_id="me",
        occurred_at="2026-01-01T10:00:04+00:00", source_event_id="event-8",
        source_sha256="same-content", payload={"duration_s": 4},
    )
    assert same["created"] is True
    assert retry["created"] is False
    assert retry["event_id"] == same["event_id"]
    assert later_occurrence["event_id"] != same["event_id"]


def test_invalid_brainlive_llm_output_is_quarantined(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_v15 as brainlive

    class InvalidClient:
        def require_json(self, *args, **kwargs):
            return {"world_state": {"active_mode": "not_allowed"}}

    monkeypatch.setattr(brainlive, "OllamaJsonClient", InvalidClient)
    session = brainlive.start_live_session(person_id="me")
    result = brainlive.run_brainlive(session["live_session_id"], use_llm=True)
    assert result["status"] == "quarantined_invalid_llm_output"
    assert result["counts"]["forecasts"] == 0
    with connect() as con:
        q = con.execute("SELECT category FROM data_quarantine_v176").fetchall()
    assert any(row["category"] == "invalid_llm_contract" for row in q)


def test_observations_are_globally_sorted_by_event_time(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_longitudinal_v15_1 import _collect_observations_after

    ensure_brainlive_schema()
    session = start_live_session(person_id="me")
    sid = session["live_session_id"]
    with connect() as con:
        con.execute(
            """INSERT INTO brainlive_turn_buffer(live_turn_id,live_session_id,conversation_id,timestamp_start,timestamp_end,speaker_label,speaker_person_id,speaker_confidence,text_partial,text_final,asr_confidence,is_final,metadata_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("turn-3", sid, None, "2026-01-01T00:00:03+00:00", None, "me", "me", .9, None, "third", .9, 1, "{}", "2026-01-01T01:00:00+00:00"),
        )
        con.execute(
            """INSERT INTO brainlive_world_states(world_state_id,live_session_id,person_id,state_time,where_am_i,who_is_active_json,what_is_happening,probable_activity_json,active_emotional_state,active_mode,audio_context_json,visual_context_json,evidence_json,counter_evidence_json,confidence,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("state-1", sid, "me", "2026-01-01T00:00:01+00:00", None, "[]", "first", "[]", None, "work", "{}", "{}", "[]", "[]", .5, "2026-01-01T01:00:01+00:00"),
        )
        con.execute(
            """INSERT INTO vision_frames(frame_id,source_asset_id,conversation_id,live_session_id,captured_at,image_path,image_sha256,width,height,device_source,capture_mode,metadata_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("frame-2", None, None, sid, "2026-01-01T00:00:02+00:00", "/tmp/test.jpg", "image-hash", None, None, "test", "test", "{}", "2026-01-01T01:00:02+00:00"),
        )
        con.execute(
            """INSERT INTO vision_scene_observations(observation_id,frame_id,live_session_id,conversation_id,model,scene_summary,location_hint,people_count,spatial_context,social_context_hint,visible_text_json,objects_json,risks_json,affordances_json,possible_user_activities_json,personal_relevance_json,confidence,raw_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("vision-2", "frame-2", sid, None, "test", "second", None, 0, None, None, "[]", "[]", "[]", "[]", "[]", "[]", .5, "{}", "2026-01-01T01:00:02+00:00"),
        )
        con.commit()
        rows = _collect_observations_after(
            con, live_session_id=sid, since="2026-01-01T00:00:00+00:00", until="2026-01-01T00:00:10+00:00"
        )
    assert [row["source_table"] for row in rows] == ["brainlive_world_states", "vision_scene_observations", "brainlive_turn_buffer"]


def test_v176_database_triggers_reject_illegal_lifecycle_and_predated_outcome(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    ensure_brainlive_schema()
    session = start_live_session(person_id="me")
    with connect() as con:
        forecast = create_forecast(
            con,
            live_session_id=session["live_session_id"],
            person_id="me",
            event_id=None,
            run_id="run",
            payload=_forecast_payload(),
            occurred_at="2026-01-01T10:00:00+00:00",
            source="test",
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """UPDATE brainlive_short_horizon_forecasts
                   SET lifecycle_state='evaluated_correct', status='evaluated_correct', canonical_outcome_id=NULL
                   WHERE forecast_id=?""",
                (forecast["forecast_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """INSERT INTO brainlive_prediction_outcomes(
                    outcome_id,live_session_id,forecast_id,candidate_id,person_id,observed_after,
                    outcome_window,was_prediction_correct,match_score,user_feedback,lesson_json,evidence_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "predated",
                    session["live_session_id"],
                    forecast["forecast_id"],
                    None,
                    "me",
                    "2026-01-01T09:59:59+00:00",
                    "manual",
                    1,
                    1.0,
                    None,
                    "{}",
                    "[]",
                    "2026-01-01T10:00:01+00:00",
                ),
            )


def test_legacy_open_forecast_with_outcome_is_reconciled_and_duplicates_quarantined(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.integrity_v176 import ensure_integrity_schema

    ensure_brainlive_schema()
    session = start_live_session(person_id="me")
    with connect() as con:
        # Simulate the historic V17.4 state before the V17.6 trigger existed.
        for trigger in (
            "trg_bl_outcome_requires_forecast_v176",
            "trg_bl_outcome_record_transition_v176",
            "trg_bl_outcome_closes_forecast_v176",
            "trg_bl_forecast_validate_insert_v176",
            "trg_bl_forecast_validate_update_v176",
        ):
            con.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        con.execute(
            """INSERT INTO brainlive_short_horizon_forecasts(
                forecast_id,live_session_id,event_id,person_id,horizon,forecast_type,
                predicted_need,predicted_action,predicted_words,predicted_emotion,predicted_risk,predicted_opportunity,
                if_intervene_future,if_silent_future,expected_gain,probability,confidence,evidence_json,counter_evidence_json,
                status,created_at,updated_at,occurred_at,lifecycle_state,schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy-fc", session["live_session_id"], None, "me", "H1", "action",
                None, "legacy action", None, None, None, None,
                None, None, .2, .7, .8, "[]", "[]",
                "open", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00+00:00",
                "2026-01-01T10:00:00+00:00", "open", "legacy_pre_17_6",
            ),
        )
        for outcome_id, correct in (("legacy-o1", 1), ("legacy-o2", 0)):
            con.execute(
                """INSERT INTO brainlive_prediction_outcomes(
                    outcome_id,live_session_id,forecast_id,candidate_id,person_id,observed_after,
                    outcome_window,was_prediction_correct,match_score,user_feedback,lesson_json,evidence_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outcome_id, session["live_session_id"], "legacy-fc", None, "me",
                    "2026-01-01T10:00:11+00:00", "manual", correct, .8, None, "{}", "[]",
                    "2026-01-01T10:00:11+00:00" if outcome_id == "legacy-o1" else "2026-01-01T10:00:12+00:00",
                ),
            )
        con.commit()
    ensure_integrity_schema()
    with connect() as con:
        row = con.execute(
            "SELECT lifecycle_state,status,canonical_outcome_id FROM brainlive_short_horizon_forecasts WHERE forecast_id='legacy-fc'"
        ).fetchone()
        quarantine = con.execute(
            "SELECT category FROM data_quarantine_v176 WHERE source_id='legacy-o2'"
        ).fetchone()
    assert dict(row) == {
        "lifecycle_state": "evaluated_correct",
        "status": "evaluated_correct",
        "canonical_outcome_id": "legacy-o1",
    }
    assert quarantine["category"] == "legacy_duplicate_forecast_outcome"


def test_legacy_open_forecasts_are_not_reinjected_as_live_active(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.integrity_v176 import active_forecast_sql

    ensure_brainlive_schema()
    session = start_live_session(person_id="me")
    with connect() as con:
        valid = create_forecast(
            con,
            live_session_id=session["live_session_id"],
            person_id="me",
            event_id=None,
            run_id="run",
            payload=_forecast_payload(),
            occurred_at="2026-01-01T10:00:00+00:00",
            source="test",
        )
        con.execute("DROP TRIGGER IF EXISTS trg_bl_forecast_validate_insert_v176")
        con.execute(
            """INSERT INTO brainlive_short_horizon_forecasts(
                forecast_id,live_session_id,event_id,person_id,horizon,forecast_type,predicted_action,
                expected_gain,probability,confidence,evidence_json,counter_evidence_json,status,created_at,updated_at,
                occurred_at,lifecycle_state,schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy-open", session["live_session_id"], None, "me", "H1", "action", "old",
                .2, .5, .5, "[]", "[]", "open", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00+00:00",
                "2026-01-01T10:00:00+00:00", "open", "legacy_pre_17_6",
            ),
        )
        rows = con.execute(
            f"SELECT forecast_id FROM brainlive_short_horizon_forecasts f WHERE {active_forecast_sql('f')} ORDER BY forecast_id"
        ).fetchall()
    assert [r["forecast_id"] for r in rows] == [valid["forecast_id"]]


def test_valid_brainlive_output_persists_through_canonical_forecast_writer(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MLOMEGA_V18_DECOMPOSED_LIVE", "false")
    import mlomega_audio_elite.brainlive_v15 as brainlive

    class ValidClient:
        def require_json(self, *args, **kwargs):
            return {
                "world_state": {
                    "where_am_i": "desk",
                    "what_is_happening": "writing tests",
                    "active_mode": "work",
                    "probable_activity": ["testing"],
                    "active_emotional_state": None,
                    "confidence": 0.6,
                    "evidence": [],
                    "counter_evidence": [],
                },
                "events": [],
                "need_predictions": [],
                "affordances": [],
                "forecasts": [_forecast_payload()],
                "life_hypotheses": [],
                "interventions": [],
                "watch_next": [],
                "notes_for_brain2": [],
            }

    monkeypatch.setattr(brainlive, "OllamaJsonClient", ValidClient)
    session = brainlive.start_live_session(person_id="me")
    result = brainlive.run_brainlive(session["live_session_id"], use_llm=True)
    assert result["status"] == "ok"
    assert result["counts"]["forecasts"] == 1
    with connect() as con:
        row = con.execute(
            """SELECT probability,epistemic_confidence,lifecycle_state,due_at,schema_version
               FROM brainlive_short_horizon_forecasts"""
        ).fetchone()
    assert row["probability"] == 0.55
    assert row["epistemic_confidence"] == 0.91
    assert row["lifecycle_state"] == "open"
    assert row["due_at"]
    assert row["schema_version"] == "17.6.0"
