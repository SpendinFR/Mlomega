from __future__ import annotations

import json
from pathlib import Path


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_RAW", str(root / "raw"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_LLM_MIN_INTERVAL_S", "12")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_LLM_AUDIO_WINDOW_S", "45")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_LLM_MAX_WINDOW_S", "90")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_IMAGE_FORCE_AFTER_S", "90")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_IMAGE_MIN_VLM_INTERVAL_S", "20")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_MAX_BUNDLE_MINUTES", "25")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_VISUAL_SPLIT_MIN_SEPARATION_S", "45")
    return root


def _session(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    return start_live_session(person_id="me", title="v18.8 adaptive test")["live_session_id"]


def test_live_llm_is_debounced_and_unchanged_gps_does_not_retrigger(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    import mlomega_audio_elite.v18_8_live_policy as policy

    clock = [1000.0]
    monkeypatch.setattr(policy.time, "time", lambda: clock[0])
    gps_home = {"label": "Maison", "lat": 48.85661, "lon": 2.35221}

    first = policy.plan_live_dispatch(live_session_id=sid, audio_content=True, gps=gps_home)
    assert first["should_dispatch_llm"] is True
    policy.mark_live_dispatch(live_session_id=sid, plan=first, status="ok")

    clock[0] = 1003.0
    early_audio = policy.plan_live_dispatch(live_session_id=sid, audio_content=True, gps=gps_home)
    assert early_audio["should_dispatch_llm"] is False
    assert early_audio["reason"]["gps_changed"] is False

    # A parked gps/current.json is allowed to refresh lightweight context, but
    # cannot manufacture another Qwen call by itself.
    clock[0] = 1016.0
    unchanged_gps = policy.plan_live_dispatch(live_session_id=sid, gps=gps_home, cadence_due=True)
    assert unchanged_gps["should_dispatch_llm"] is False
    assert unchanged_gps["should_refresh_context_only"] is True

    clock[0] = 1048.0
    audio_window = policy.plan_live_dispatch(live_session_id=sid, audio_content=True, gps=gps_home)
    assert audio_window["should_dispatch_llm"] is True
    assert "audio_window" in audio_window["reason"]["pending_signals"]
    policy.mark_live_dispatch(live_session_id=sid, plan=audio_window, status="ok")

    clock[0] = 1062.0
    moved = policy.plan_live_dispatch(
        live_session_id=sid,
        gps={"label": "Café", "lat": 48.8578, "lon": 2.3620},
    )
    assert moved["should_dispatch_llm"] is True
    assert moved["reason"]["gps_changed"] is True


def test_identical_frames_are_captured_but_queue_one_live_vlm_job(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_8_live_policy import plan_image_capture, enqueue_image_work, pending_image_count

    image_a = tmp_path / "same-a.jpg"
    image_b = tmp_path / "same-b.jpg"
    image_c = tmp_path / "changed.jpg"
    image_a.write_bytes(b"identical-fallback-image-content")
    image_b.write_bytes(b"identical-fallback-image-content")
    image_c.write_bytes(b"different-fallback-image-content")

    first = plan_image_capture(live_session_id=sid, path=image_a, descriptor={"event_id": "img-a"})
    assert first["analyze_live_vlm"] is True
    queued = enqueue_image_work(live_session_id=sid, person_id="me", descriptor={"event_id": "img-a", "occurred_at": "2026-06-23T10:00:00Z"}, path=image_a)
    assert queued["status"] == "queued"

    same = plan_image_capture(live_session_id=sid, path=image_b, descriptor={"event_id": "img-b"})
    assert same["analyze_live_vlm"] is False
    assert same["reason"] == "near_duplicate_live_vlm_skipped"
    assert pending_image_count(live_session_id=sid) == 1

    changed = plan_image_capture(live_session_id=sid, path=image_c, descriptor={"event_id": "img-c"})
    assert changed["analyze_live_vlm"] is True
    assert changed["reason"] == "visual_delta"


def test_image_scheduler_uses_silence_slot_without_competing_with_audio(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_8_live_policy import enqueue_image_work, plan_image_worker_dispatch

    image = tmp_path / "frame.jpg"
    image.write_bytes(b"frame")
    enqueue_image_work(
        live_session_id=sid,
        person_id="me",
        descriptor={"event_id": "img-slot", "occurred_at": "2026-06-23T10:00:00Z"},
        path=image,
    )
    busy = plan_image_worker_dispatch(live_session_id=sid, audio_pending=4, silence_seen=False)
    assert busy["run"] is False
    assert busy["reason"] == "audio_priority_wait"

    silence = plan_image_worker_dispatch(live_session_id=sid, audio_pending=4, silence_seen=True)
    assert silence["run"] is True
    assert silence["reason"] == "silence_slot"


def _raw_row(*, sid: str, raw_id: str, time: str, source_table: str, source_id: str, payload: dict, row_kind: str = "vision_scene") -> dict:
    return {
        "raw_id": raw_id,
        "person_id": "me",
        "package_date": "2026-06-23",
        "live_session_id": sid,
        "source_table": source_table,
        "source_id": source_id,
        "event_time": time,
        "modality": "vision",
        "row_kind": row_kind,
        "speaker_label": None,
        "speaker_person_id": None,
        "text": None,
        "summary": "vision",
        "payload_json": json.dumps(payload),
        "linked_event_id": None,
        "linked_forecast_id": None,
        "linked_candidate_id": None,
        "frame_id": None,
        "conversation_id": None,
        "evidence_role": "vision_description",
        "created_at": time,
    }


def test_activity_change_splits_same_place_and_duration_cap_prevents_day_bundle(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import assemble_event_bundles

    game = {
        "possible_user_activities_json": json.dumps([{"activity": "jouer à un jeu", "confidence": 0.9}]),
        "objects_json": json.dumps([{"label": "écran"}]),
        "visible_text_json": json.dumps([{"text": "level one"}]),
        "people_count": 1,
    }
    cook = {
        "possible_user_activities_json": json.dumps([{"activity": "cuisiner", "confidence": 0.91}]),
        "objects_json": json.dumps([{"label": "casserole"}]),
        "visible_text_json": json.dumps([]),
        "people_count": 1,
    }
    same_place_activity_change = [
        _raw_row(sid=sid, raw_id="r1", time="2026-06-23T10:00:00Z", source_table="vision_scene_observations", source_id="v1", payload=game),
        _raw_row(sid=sid, raw_id="r2", time="2026-06-23T10:01:00Z", source_table="vision_scene_observations", source_id="v2", payload=cook),
    ]
    result = assemble_event_bundles(person_id="me", package_date="2026-06-23", raw_timeline=same_place_activity_change, live_session_id=sid)
    assert result["bundles_created"] == 2

    same_activity_long = [
        _raw_row(sid=sid, raw_id="r3", time="2026-06-23T11:00:00Z", source_table="vision_scene_observations", source_id="v3", payload=game),
        _raw_row(sid=sid, raw_id="r4", time="2026-06-23T11:26:00Z", source_table="vision_scene_observations", source_id="v4", payload=game),
    ]
    capped = assemble_event_bundles(person_id="me", package_date="2026-06-23", raw_timeline=same_activity_long, live_session_id=sid)
    assert capped["bundles_created"] == 2


def test_delivery_feedback_and_outcome_are_linked_into_brain2_raw_timeline(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_delivery import enqueue_delivery
    from mlomega_audio_elite.v18_8_live_policy import record_delivery_feedback, materialize_intervention_outcome_observation
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import collect_live_raw_timeline
    from mlomega_audio_elite.utils import now_iso

    delivery = enqueue_delivery(
        live_session_id=sid,
        source_key="test-activity-change",
        candidate={
            "candidate_id": "candidate-1",
            "decision": "queue",
            "message": "Tu voulais faire une pause ?",
            "priority": 0.8,
        },
    )
    assert delivery["status"] == "queued"
    feedback = record_delivery_feedback(
        delivery_id=delivery["delivery_id"],
        feedback_type="acted",
        feedback_source="phone",
        note="pause faite",
        observed_at=now_iso(),
    )
    assert feedback["delivery_id"] == delivery["delivery_id"]
    outcome = materialize_intervention_outcome_observation(
        delivery_id=delivery["delivery_id"],
        outcome_status="feedback_explicit",
        did_help=True,
        observed_later_summary="pause faite",
        observed_at=now_iso(),
    )
    assert outcome["delivery_id"] == delivery["delivery_id"]

    # package_date is a LOCAL calendar day (_period_bounds converts local ->
    # UTC); deriving it from the UTC clock shifted it by one day between
    # midnight and UTC-offset hours, making this test fail at night (G0 fix).
    from datetime import datetime

    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    raw = collect_live_raw_timeline(person_id="me", package_date=day, live_session_id=sid)
    tables = {row["source_table"] for row in raw["timeline"]}
    assert "brainlive_intervention_delivery_queue" in tables
    assert "brainlive_intervention_feedback_events_v188" in tables
    assert "brainlive_intervention_outcomes_v188" in tables


def test_bridge_feedback_file_has_durable_source_descriptor(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_service_v15_5 import _source_descriptor

    feedback = tmp_path / "feedback.json"
    feedback.write_text(json.dumps({
        "delivery_id": "delivery-42",
        "feedback_type": "seen",
        "feedback_id": "phone-seen-42",
        "feedback_source": "phone_bridge",
        "source_device": "android_phone",
        "observed_at": "2026-06-23T12:00:00Z",
    }), encoding="utf-8")
    desc = _source_descriptor(feedback, kind="feedback")
    assert desc["source_event_id"] == "phone-seen-42"
    assert desc["source_device"] == "android_phone"
    assert desc["occurred_at"] == "2026-06-23T12:00:00Z"
