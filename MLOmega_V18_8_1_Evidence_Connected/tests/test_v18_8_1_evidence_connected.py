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
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_MAX_BUNDLE_MINUTES", "25")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_BUNDLE_DHASH_SPLIT_BITS", "14")
    monkeypatch.setenv("MLOMEGA_BRAINLIVE_PIXEL_SPLIT_MIN_SEPARATION_S", "90")
    return root


def _session(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    return start_live_session(person_id="me", title="v18.8.1 evidence test")["live_session_id"]


def _raw_row(*, sid: str, raw_id: str, time: str, source_table: str, source_id: str, payload: dict, modality: str = "vision", row_kind: str = "vision_frame") -> dict:
    return {
        "raw_id": raw_id,
        "person_id": "me",
        "package_date": "2026-06-23",
        "live_session_id": sid,
        "source_table": source_table,
        "source_id": source_id,
        "event_time": time,
        "modality": modality,
        "row_kind": row_kind,
        "speaker_label": None,
        "speaker_person_id": None,
        "text": None,
        "summary": source_table,
        "payload_json": json.dumps(payload),
        "linked_event_id": None,
        "linked_forecast_id": None,
        "linked_candidate_id": None,
        "frame_id": payload.get("vision_evidence", payload).get("frame_id") if isinstance(payload.get("vision_evidence", payload), dict) else None,
        "conversation_id": None,
        "evidence_role": "raw_visual_frame",
        "created_at": time,
    }


def test_silence_boundary_only_fires_once_after_observed_speech(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    import mlomega_audio_elite.v18_8_live_policy as policy

    clock = [1000.0]
    monkeypatch.setattr(policy.time, "time", lambda: clock[0])
    spoken = policy.plan_live_dispatch(live_session_id=sid, audio_content=True, audio_observed=True)
    assert spoken["should_dispatch_llm"] is True
    policy.mark_live_dispatch(live_session_id=sid, plan=spoken, status="ok")

    clock[0] = 1013.0
    first_silence = policy.plan_live_dispatch(
        live_session_id=sid, audio_content=False, silence_boundary=True, audio_observed=True,
    )
    assert first_silence["reason"]["silence_transition"] is True
    assert first_silence["should_dispatch_llm"] is True
    policy.mark_live_dispatch(live_session_id=sid, plan=first_silence, status="ok")

    clock[0] = 1026.0
    repeated_silence = policy.plan_live_dispatch(
        live_session_id=sid, audio_content=False, silence_boundary=True, audio_observed=True,
    )
    assert repeated_silence["reason"]["silence_transition"] is False
    assert repeated_silence["should_dispatch_llm"] is False


def test_raw_frame_path_survives_bundle_and_is_a_deep_keyframe(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import assemble_event_bundles
    from mlomega_audio_elite.brainlive_offline_deep_vision_v16_1 import select_keyframes_for_bundle

    image = tmp_path / "evidence.jpg"
    image.write_bytes(b"raw-pixel-evidence")
    row = _raw_row(
        sid=sid, raw_id="raw-frame", time="2026-06-23T10:00:00+00:00",
        source_table="vision_frames", source_id="frame-1",
        payload={"vision_evidence": {
            "frame_id": "frame-1", "image_path": str(image), "image_sha256": "abc",
            "metadata": {"v188_image_signature_kind": "dhash64", "v188_image_signature": "0000000000000000"},
        }},
    )
    assembled = assemble_event_bundles(person_id="me", package_date="2026-06-23", raw_timeline=[row], live_session_id=sid)
    bundle = assembled["bundles"][0]
    timeline = json.loads(bundle["vision_timeline_json"])
    assert timeline[0]["image_path"] == str(image)
    selected = select_keyframes_for_bundle(bundle)
    assert len(selected) == 1
    assert selected[0]["image_path"] == str(image)


def test_missing_frame_is_not_silent_deep_vision_success(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import assemble_event_bundles
    from mlomega_audio_elite.brainlive_offline_deep_vision_v16_1 import run_offline_deep_vision_for_bundles

    missing = tmp_path / "does-not-exist.jpg"
    row = _raw_row(
        sid=sid, raw_id="raw-missing", time="2026-06-23T11:00:00+00:00",
        source_table="vision_frames", source_id="frame-missing",
        payload={"vision_evidence": {
            "frame_id": "frame-missing", "image_path": str(missing),
            "metadata": {"v188_image_signature_kind": "dhash64", "v188_image_signature": "ffffffffffffffff"},
        }},
    )
    assemble_event_bundles(person_id="me", package_date="2026-06-23", raw_timeline=[row], live_session_id=sid)
    result = run_offline_deep_vision_for_bundles(
        person_id="me", package_date="2026-06-23", live_session_id=sid, use_vlm=False,
    )
    assert result["status"] == "blocked"
    assert result["visual_evidence_failures"][0]["error_code"] == "blocked_visual_evidence_unavailable"


def test_pixel_change_and_place_change_split_without_live_vlm(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import assemble_event_bundles

    raw = [
        _raw_row(
            sid=sid, raw_id="r1", time="2026-06-23T12:00:00+00:00", source_table="vision_frames", source_id="f1",
            payload={"vision_evidence": {"frame_id": "f1", "image_path": "/missing/1.jpg", "metadata": {"v188_image_signature_kind": "dhash64", "v188_image_signature": "0000000000000000"}}},
        ),
        _raw_row(
            sid=sid, raw_id="r2", time="2026-06-23T12:02:00+00:00", source_table="vision_frames", source_id="f2",
            payload={"vision_evidence": {"frame_id": "f2", "image_path": "/missing/2.jpg", "metadata": {"v188_image_signature_kind": "dhash64", "v188_image_signature": "ffffffffffffffff"}}},
        ),
        _raw_row(
            sid=sid, raw_id="r3", time="2026-06-23T12:04:00+00:00", source_table="brainlive_world_states", source_id="w1",
            payload={"context_evidence": {"where_am_i": "Maison"}}, modality="world_state", row_kind="world_state",
        ),
        _raw_row(
            sid=sid, raw_id="r4", time="2026-06-23T12:05:00+00:00", source_table="brainlive_world_states", source_id="w2",
            payload={"context_evidence": {"where_am_i": "Café"}}, modality="world_state", row_kind="world_state",
        ),
    ]
    result = assemble_event_bundles(person_id="me", package_date="2026-06-23", raw_timeline=raw, live_session_id=sid)
    # f1→f2 has a large dHash delta; Maison→Café splits separately.
    assert result["bundles_created"] == 3


def test_real_captured_frame_is_collected_to_bundle_and_deep_selection(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_sensor_fusion_v15_4 import ingest_image_sensor
    from mlomega_audio_elite.v18_8_live_policy import annotate_captured_frame, plan_image_capture
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import collect_live_raw_timeline, assemble_event_bundles
    from mlomega_audio_elite.brainlive_offline_deep_vision_v16_1 import select_keyframes_for_bundle

    image = tmp_path / "captured.jpg"
    image.write_bytes(b"captured-frame-bytes")
    captured_at = "2026-06-23T13:00:00Z"
    ingested = ingest_image_sensor(
        sid, image, use_vlm=False, source_event_id="phone-img-13", source_occurred_at=captured_at, source_device="android_phone",
    )
    policy = plan_image_capture(live_session_id=sid, path=image, descriptor={"event_id": "phone-img-13"})
    annotate_captured_frame(frame_id=ingested["frame"]["frame_id"], policy=policy)

    raw = collect_live_raw_timeline(person_id="me", package_date="2026-06-23", live_session_id=sid)
    assert any(r["source_table"] == "vision_frames" for r in raw["timeline"])
    assembled = assemble_event_bundles(person_id="me", package_date="2026-06-23", raw_timeline=raw["timeline"], live_session_id=sid)
    bundle = assembled["bundles"][0]
    frames = select_keyframes_for_bundle(bundle)
    assert any(f["image_path"] == str(image.resolve()) for f in frames)


def test_deep_visual_pixels_become_scoped_brain2_context(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_sensor_fusion_v15_4 import ingest_image_sensor
    from mlomega_audio_elite.v18_8_live_policy import annotate_captured_frame, plan_image_capture
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import run_brainlive_event_assembly
    import mlomega_audio_elite.brainlive_offline_deep_vision_v16_1 as deep_module
    from mlomega_audio_elite.v18_brain2_context import conversation_context_addenda
    from mlomega_audio_elite.db import connect

    image = tmp_path / "deep-evidence.jpg"
    image.write_bytes(b"deep-evidence-bytes")
    event_id = "phone-img-deep"
    captured = ingest_image_sensor(
        sid, image, use_vlm=False, source_event_id=event_id,
        source_occurred_at="2026-06-23T14:00:00Z", source_device="android_phone",
    )
    annotate_captured_frame(
        frame_id=captured["frame"]["frame_id"],
        policy=plan_image_capture(live_session_id=sid, path=image, descriptor={"event_id": event_id}),
    )
    assembly = run_brainlive_event_assembly(
        person_id="me", package_date="2026-06-23", live_session_id=sid, export_to_brain2=True,
    )
    assert assembly["exports"] == 1

    monkeypatch.setattr(deep_module, "_deep_vlm_json", lambda *args, **kwargs: {
        "scene_summary_detailed": "Écran de jeu visible.",
        "observed_activity": "computer_work",
        "activity_confidence": 0.88,
        "objects": ["écran"],
        "exact_visual_evidence": ["interface de jeu visible"],
        "uncertainty": [],
    })
    result = deep_module.run_offline_deep_vision_for_bundles(
        person_id="me", package_date="2026-06-23", live_session_id=sid, use_vlm=True,
    )
    assert result["status"] == "ok"
    assert result["analyzed_keyframes"] == 1
    assert result["context_addenda_created"] == 1

    from mlomega_audio_elite.db import connect
    with connect() as con:
        export = con.execute(
            """SELECT e.conversation_id
                 FROM brainlive_brain2_event_exports_v1514 e
                 JOIN brainlive_event_bundles_v1514 b ON b.bundle_id=e.bundle_id
                 WHERE b.live_session_id=?""", (sid,)
        ).fetchone()
        assert export
        context = conversation_context_addenda(con, conversation_id=export["conversation_id"], person_id="me")
    assert len(context["entries"]) == 1
    assert "Écran de jeu visible" in context["entries"][0]["text"]
