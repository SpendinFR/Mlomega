from __future__ import annotations

import json
import os
import wave
import pytest
from pathlib import Path


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_RAW", str(root / "raw"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")
    monkeypatch.setenv("MLOMEGA_ENABLE_WHISPERX", "true")
    monkeypatch.setenv("MLOMEGA_ENABLE_PYANNOTE", "true")
    return root


def _wav(path: Path, seconds: float = 0.3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(16000 * seconds)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * frames)


def _bundle_with_two_audio_events(monkeypatch, tmp_path):
    root = _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite.brainlive_event_assembler_v15_14 import run_brainlive_event_assembly
    from mlomega_audio_elite.brainlive_sensor_fusion_v15_4 import ensure_sensor_fusion_schema
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.utils import json_dumps

    sid = start_live_session(person_id="me", title="deep audio test")["live_session_id"]
    ensure_sensor_fusion_schema()
    a = root / "raw" / "chunk-a.wav"
    b = root / "raw" / "chunk-b.wav"
    _wav(a)
    _wav(b)
    events = [
        ("audio-event-a", a, "2026-06-21T10:00:00Z", "2026-06-21T10:00:00.300000Z"),
        ("audio-event-b", b, "2026-06-21T10:00:01Z", "2026-06-21T10:00:01.300000Z"),
    ]
    with connect() as con:
        for eid, path, start, end in events:
            payload = {
                "source_event_id": f"phone:{eid}",
                "absolute_start": start,
                "absolute_end": end,
                "segment": {"start": 0.0, "end": 0.3},
                "raw_audio_path": str(path),
                "chunk_path": str(path),
                "speaker": {"label": "SPEAKER_00", "person_id": "me"},
            }
            con.execute(
                """INSERT INTO brainlive_sensor_events(event_id,live_session_id,person_id,event_time,modality,event_type,source_path,source_sha256,confidence,payload_json,model_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, sid, "me", start, "audio", "speech_segment", str(path), None, 0.9, json_dumps(payload), "ok", start),
            )
        con.execute(
            """INSERT INTO brainlive_turn_buffer(live_turn_id,live_session_id,conversation_id,timestamp_start,timestamp_end,speaker_label,speaker_person_id,speaker_confidence,text_partial,text_final,asr_confidence,is_final,metadata_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("live-turn-a", sid, None, "2026-06-21T10:00:00Z", "2026-06-21T10:00:00.300000Z", "me", "me", 0.9, None, "live rapide imprécis", 0.9, 1, "{}", "2026-06-21T10:00:00Z"),
        )
        con.commit()
    assembled = run_brainlive_event_assembly(person_id="me", package_date="2026-06-21", live_session_id=sid)
    assert assembled["bundles"] == 1
    with connect() as con:
        bundle_id = con.execute("SELECT bundle_id FROM brainlive_event_bundles_v1514 WHERE live_session_id=?", (sid,)).fetchone()[0]
    return root, sid, bundle_id


def test_deep_audio_refines_bundle_export_without_flow_once(monkeypatch, tmp_path):
    _root, sid, bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    import mlomega_audio_elite.audio_pipeline as pipeline
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import run_offline_deep_audio_for_bundles
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.voice_identity import ensure_speaker

    calls = {"n": 0}

    def fake_transcribe(path, *, language="fr", speaker_map=None, runtime=None):
        assert Path(path).exists()
        calls["n"] += 1
        # This profile is created by the offline resolution itself. It has no
        # enrollment embedding, so it must not alter the next input digest and
        # create a duplicate deep-audio revision on retry.
        ensure_speaker("UNKNOWN_VOICE_001", display_name="UNKNOWN_VOICE_001", is_user=False)
        return {
            "metadata": {
                "speaker_map": {"SPEAKER_00": "me", "SPEAKER_01": "UNKNOWN_VOICE_001"},
                "voice_identity": {"status": "resolved", "details": [
                    {"speaker_label": "SPEAKER_00", "person_id": "me", "decision": "known_person_match", "known_score": 0.93, "duration_s": 0.24},
                    {"speaker_label": "SPEAKER_01", "person_id": "UNKNOWN_VOICE_001", "decision": "unknown_cluster", "known_score": 0.0, "duration_s": 0.24},
                ]},
                "pipeline": {"transcriber": "whisperx", "diarization": True},
            },
            "turns": [
                {"speaker": "SPEAKER_00", "person_id": "me", "start": 0.0, "end": 0.24, "text": "bonjour version profonde", "words": [{"word": "bonjour", "start": 0.0, "end": 0.1}]},
                {"speaker": "SPEAKER_01", "person_id": "UNKNOWN_VOICE_001", "start": 1.01, "end": 1.25, "text": "réponse précise", "words": []},
            ],
        }

    monkeypatch.setattr(pipeline, "transcribe_with_whisperx", fake_transcribe)
    result = run_offline_deep_audio_for_bundles(person_id="me", package_date="2026-06-21", live_session_id=sid)
    assert result["status"] == "ok"
    assert result["bundles_refined"] == 1
    assert result["artifacts"][0]["bundle_id"] == bundle_id

    with connect() as con:
        exports = [dict(row) for row in con.execute("SELECT conversation_id,export_status FROM brainlive_brain2_event_exports_v1514 WHERE bundle_id=? ORDER BY created_at", (bundle_id,)).fetchall()]
        assert len(exports) == 2
        deep_export = next(row for row in exports if row["export_status"] == "exported")
        old_export = next(row for row in exports if row["export_status"] == "superseded")
        assert deep_export["conversation_id"] != old_export["conversation_id"]
        conv = dict(con.execute("SELECT channel,raw_json FROM conversations WHERE conversation_id=?", (deep_export["conversation_id"],)).fetchone())
        assert conv["channel"] == "brainlive_event_bundle_deep_audio_v185"
        rows = [dict(row) for row in con.execute("SELECT text,metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (deep_export["conversation_id"],)).fetchall()]
        assert any(row["text"] == "bonjour version profonde" for row in rows)
        deep_meta = next(json.loads(row["metadata_json"]) for row in rows if row["text"] == "bonjour version profonde")
        assert set(deep_meta["source"]["source_event_ids"]) == {"audio-event-a"}
        second_meta = next(json.loads(row["metadata_json"]) for row in rows if row["text"] == "réponse précise")
        assert set(second_meta["source"]["source_event_ids"]) == {"audio-event-b"}
        artifact = dict(con.execute("SELECT status,stitched_audio_path,refined_conversation_id,speaker_reconciliation_json,time_map_json FROM brainlive_deep_audio_artifacts_v185").fetchone())
        assert artifact["status"] == "completed"
        assert Path(artifact["stitched_audio_path"]).exists()
        assert artifact["refined_conversation_id"] == deep_export["conversation_id"]
        assert json.loads(artifact["speaker_reconciliation_json"])["engine"]["speechbrain_enabled"] is True
        assert len([x for x in json.loads(artifact["time_map_json"]) if x["kind"] == "audio_piece"]) == 2

    repeated = run_offline_deep_audio_for_bundles(person_id="me", package_date="2026-06-21", live_session_id=sid)
    assert repeated["status"] == "ok"
    assert repeated["artifacts"][0]["status"] == "resumed"
    assert calls["n"] == 1


def test_audio_bearing_bundle_with_missing_raw_source_fails_closed(monkeypatch, tmp_path):
    _root, sid, _bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import DeepAudioError, run_offline_deep_audio_for_bundles
    from mlomega_audio_elite.db import connect

    with connect() as con:
        path = con.execute("SELECT source_path FROM brainlive_sensor_events WHERE modality='audio' LIMIT 1").fetchone()[0]
    Path(path).unlink()
    try:
        run_offline_deep_audio_for_bundles(person_id="me", package_date="2026-06-21", live_session_id=sid)
    except DeepAudioError as exc:
        assert "deep audio failed" in str(exc)
    else:
        raise AssertionError("missing raw audio must not be silently treated as success")


def test_post_stop_runs_deep_audio_before_brain2(monkeypatch, tmp_path):
    _root, sid, _bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    import mlomega_audio_elite.audio_pipeline as pipeline
    import mlomega_audio_elite.brain2_flow_v13_3 as brain2
    import mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 as post
    from mlomega_audio_elite.db import connect

    monkeypatch.setattr(
        pipeline,
        "transcribe_with_whisperx",
        lambda *_a, **_kw: {
            "metadata": {
                "speaker_map": {"SPEAKER_00": "me"},
                "voice_identity": {"status": "resolved", "details": [{"speaker_label": "SPEAKER_00", "person_id": "me", "decision": "known_person_match", "known_score": 0.93, "duration_s": 0.25}]},
                "pipeline": {"transcriber": "whisperx", "diarization": True},
            },
            "turns": [{"speaker": "SPEAKER_00", "person_id": "me", "start": 0.0, "end": 0.25, "text": "deep avant Brain2", "words": []}],
        },
    )
    seen: list[str] = []
    monkeypatch.setattr(
        brain2,
        "run_brain2_deep_stack_for_conversation",
        lambda conversation_id, **_kw: seen.append(conversation_id) or {"status": "ok", "run_id": f"brain2:{conversation_id}"},
    )
    monkeypatch.setattr(post, "_sync_secondary_memory_for_conversation", lambda *_a, **_kw: {"status": "ok"})

    result = post.run_brainlive_post_stop_deep_flow(
        person_id="me",
        live_session_id=sid,
        package_date="2026-06-21",
        run_deep_vision=False,
        run_silent_life=False,
        run_v15=False,
        use_llm=True,
    )
    assert result["status"] == "completed"
    assert result["v18_deep_audio"]["status"] == "ok"
    assert len(seen) == 1
    with connect() as con:
        channel = con.execute("SELECT channel FROM conversations WHERE conversation_id=?", (seen[0],)).fetchone()[0]
        stages = {row[0]: row[1] for row in con.execute("SELECT stage_name,status FROM v18_pipeline_stages WHERE run_id=?", (result["run_id"],)).fetchall()}
    assert channel == "brainlive_event_bundle_deep_audio_v185"
    assert stages["deep_audio"] == "completed"
    assert stages["brain2"] == "completed"


def test_deep_audio_rejects_missing_offline_speech_reco_provenance(monkeypatch, tmp_path):
    _root, sid, _bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    import mlomega_audio_elite.audio_pipeline as pipeline
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import DeepAudioError, run_offline_deep_audio_for_bundles

    monkeypatch.setattr(
        pipeline,
        "transcribe_with_whisperx",
        lambda *_a, **_kw: {
            "metadata": {"speaker_map": {"SPEAKER_00": "me"}, "pipeline": {"transcriber": "whisperx", "diarization": True}},
            "turns": [{"speaker": "SPEAKER_00", "person_id": "me", "start": 0.0, "end": 0.2, "text": "sans preuve de reco", "words": []}],
        },
    )
    try:
        run_offline_deep_audio_for_bundles(person_id="me", package_date="2026-06-21", live_session_id=sid)
    except DeepAudioError as exc:
        assert "deep audio failed" in str(exc)
    else:
        raise AssertionError("offline speaker recognition provenance must be required")


def test_audio_refinement_ignores_non_audio_bundle_sources(monkeypatch, tmp_path):
    _root, sid, bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    import mlomega_audio_elite.audio_pipeline as pipeline
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import run_offline_deep_audio_for_bundles
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.utils import json_dumps, json_loads

    with connect() as con:
        bundle = con.execute("SELECT raw_timeline_json FROM brainlive_event_bundles_v1514 WHERE bundle_id=?", (bundle_id,)).fetchone()
        raw = json_loads(bundle[0], [])
        raw.append({"source_table": "brainlive_sensor_events", "source_id": "image-event-1", "modality": "image", "row_kind": "vision_frame"})
        con.execute(
            """INSERT INTO brainlive_sensor_events(event_id,live_session_id,person_id,event_time,modality,event_type,source_path,source_sha256,confidence,payload_json,model_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("image-event-1", sid, "me", "2026-06-21T10:00:00.500000Z", "image", "vision_frame", None, None, 0.8, json_dumps({"source_event_id": "phone:image-1"}), "ok", "2026-06-21T10:00:00.500000Z"),
        )
        con.execute("UPDATE brainlive_event_bundles_v1514 SET raw_timeline_json=? WHERE bundle_id=?", (json_dumps(raw), bundle_id))
        con.commit()

    monkeypatch.setattr(
        pipeline,
        "transcribe_with_whisperx",
        lambda *_a, **_kw: {
            "metadata": {
                "speaker_map": {"SPEAKER_00": "me"},
                "voice_identity": {"status": "resolved", "details": [{"speaker_label": "SPEAKER_00", "person_id": "me", "decision": "known_person_match", "known_score": 0.92, "duration_s": 0.4}]},
                "pipeline": {"transcriber": "whisperx", "diarization": True},
            },
            "turns": [{"speaker": "SPEAKER_00", "person_id": "me", "start": 0.0, "end": 0.25, "text": "audio seulement", "words": []}],
        },
    )
    result = run_offline_deep_audio_for_bundles(person_id="me", package_date="2026-06-21", live_session_id=sid)
    assert result["status"] == "ok"
    assert result["bundles_refined"] == 1


def test_poststop_skip_deep_audio_keeps_cleanup_blocked(monkeypatch, tmp_path):
    _root, sid, _bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_poststop_deep_flow_v15_15 as post
    import mlomega_audio_elite.brain2_flow_v13_3 as brain2
    from mlomega_audio_elite.governance_v18 import StageGateError

    monkeypatch.setattr(
        brain2,
        "run_brain2_deep_stack_for_conversation",
        lambda conversation_id, **_kw: {"status": "ok", "run_id": f"brain2:{conversation_id}"},
    )
    monkeypatch.setattr(post, "_sync_secondary_memory_for_conversation", lambda *_a, **_kw: {"status": "ok"})
    result = post.run_brainlive_post_stop_deep_flow(
        person_id="me", live_session_id=sid, package_date="2026-06-21",
        run_deep_audio=False, run_deep_vision=False, run_silent_life=False,
        run_v15=False, use_llm=True,
    )
    assert result["status"] == "blocked"
    assert result["v18_deep_audio"]["status"] == "skipped_requires_retention"
    with pytest.raises(StageGateError):
        post.post_stop_cleanup_eligible(run_id=result["run_id"], person_id="me")


def test_release_audit_rejects_unresolved_deep_audio_error(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import ensure_deep_audio_schema
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.utils import now_iso
    from mlomega_audio_elite.v18_release_audit import audit_v18_release

    ensure_deep_audio_schema()
    stamp = now_iso()
    with connect() as con:
        con.execute(
            """INSERT INTO brainlive_deep_audio_artifacts_v185(
                 artifact_id,person_id,package_date,run_id,bundle_id,source_digest,source_manifest_json,
                 processing_profile_json,speaker_reconciliation_json,time_map_json,tape_duration_seconds,
                 stitched_audio_path,stitched_audio_sha256,transcript_json,transcript_sha256,
                 refined_conversation_id,superseded_conversation_ids_json,status,error_text,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("deep-audit-error", "me", "2026-06-21", "run-error", "bundle-error", "digest-error", "[]", "{}", "{}", "[]", None,
             None, None, "{}", None, None, "[]", "error", "whisperx timeout", stamp, stamp),
        )
        con.commit()
    report = audit_v18_release(strict=True, persist=False)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["status"] == "fail"
    assert "deep_audio_refinement_error" in codes


def test_brain2_uses_refined_audio_once_and_receives_deep_vision_as_context(monkeypatch, tmp_path):
    """The active Brain2 revision has deep audio, not a duplicate live transcript.

    Deep VLM evidence remains a labelled addendum, and the V13.3 consumers get
    it through their bounded context envelope rather than via a fake dialogue
    turn.
    """
    _root, sid, bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    import mlomega_audio_elite.audio_pipeline as pipeline
    import mlomega_audio_elite.brain2_flow_v13_3 as brain2
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import run_offline_deep_audio_for_bundles
    from mlomega_audio_elite.brainlive_offline_deep_vision_v16_1 import ensure_deep_vision_schema
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.utils import json_dumps, now_iso, stable_id
    from mlomega_audio_elite.v18_brain2_context import conversation_context_addenda

    monkeypatch.setattr(
        pipeline,
        "transcribe_with_whisperx",
        lambda *_a, **_kw: {
            "metadata": {
                "speaker_map": {"SPEAKER_00": "me"},
                "voice_identity": {"status": "resolved", "details": [
                    {"speaker_label": "SPEAKER_00", "person_id": "me", "decision": "known_person_match", "known_score": 0.96, "duration_s": 0.3},
                ]},
                "pipeline": {"transcriber": "whisperx", "diarization": True},
            },
            "turns": [{"speaker": "SPEAKER_00", "person_id": "me", "start": 0.0, "end": 0.3, "text": "transcription offline exacte", "words": []}],
        },
    )
    assert run_offline_deep_audio_for_bundles(person_id="me", package_date="2026-06-21", live_session_id=sid)["status"] == "ok"

    ensure_deep_vision_schema()
    with connect() as con:
        active = dict(con.execute(
            "SELECT conversation_id FROM brainlive_brain2_event_exports_v1514 WHERE bundle_id=? AND export_status='exported'",
            (bundle_id,),
        ).fetchone())
        superseded = dict(con.execute(
            "SELECT conversation_id FROM brainlive_brain2_event_exports_v1514 WHERE bundle_id=? AND export_status='superseded'",
            (bundle_id,),
        ).fetchone())
        active_text = [row[0] for row in con.execute("SELECT text FROM turns WHERE conversation_id=? ORDER BY idx", (active["conversation_id"],))]
        assert "transcription offline exacte" in active_text
        assert "live rapide imprécis" not in active_text
        addendum_id = stable_id("test_deep_vision_addendum", active["conversation_id"])
        con.execute(
            """INSERT INTO brain2_context_addenda_v18(
                 addendum_id,person_id,conversation_id,source_table,source_id,bundle_id,live_session_id,
                 event_time,evidence_role,text,metadata_json,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (addendum_id, "me", active["conversation_id"], "brainlive_deep_vision_observations_v161", "vision-test-1", bundle_id, sid,
             "2026-06-21T10:00:00.150000Z", "system_visual_observation", "[CONTEXT_VISION_DEEP] écran de travail visible", json_dumps({"frame_id": "frame-1"}), "active", now_iso(), now_iso()),
        )
        con.commit()
        envelope = conversation_context_addenda(con, conversation_id=active["conversation_id"], person_id="me")
        assert [item["source_id"] for item in envelope["entries"]] == ["vision-test-1"]
        assert all("CONTEXT_VISION_DEEP" not in text for text in active_text)
        assert superseded["conversation_id"] != active["conversation_id"]

    captured: dict[str, object] = {}
    def fake_llm(_system, payload, _schema):
        captured.update(payload)
        return {"subtopics": [], "missing_context": [], "confidence": 0.8}
    monkeypatch.setattr(brain2, "_llm_json", fake_llm)
    brain2.build_subtopic_segments(active["conversation_id"])
    assert captured["turns"]
    assert all(turn["text"] != "live rapide imprécis" for turn in captured["turns"])
    assert captured["context_addenda"]["entries"][0]["source_id"] == "vision-test-1"
    assert captured["context_addenda"]["entries"][0]["evidence_role"] == "system_visual_observation"


def test_refined_export_is_the_only_active_global_brain2_source(monkeypatch, tmp_path):
    """Global V17/V18 readers must not count live and offline text twice."""
    _root, sid, bundle_id = _bundle_with_two_audio_events(monkeypatch, tmp_path)
    import mlomega_audio_elite.audio_pipeline as pipeline
    from mlomega_audio_elite.brainlive_offline_deep_audio_v18_5 import run_offline_deep_audio_for_bundles
    from mlomega_audio_elite.brain2_longitudinal_cases_v17 import build_observed_cases_for_period
    from mlomega_audio_elite.db import connect

    monkeypatch.setattr(
        pipeline,
        "transcribe_with_whisperx",
        lambda *_a, **_kw: {
            "metadata": {
                "speaker_map": {"SPEAKER_00": "me"},
                "voice_identity": {"status": "resolved", "details": [
                    {"speaker_label": "SPEAKER_00", "person_id": "me", "decision": "known_person_match", "known_score": 0.95, "duration_s": 0.3},
                ]},
                "pipeline": {"transcriber": "whisperx", "diarization": True},
            },
            "turns": [{"speaker": "SPEAKER_00", "person_id": "me", "start": 0.0, "end": 0.3, "text": "seule version offline", "words": []}],
        },
    )
    result = run_offline_deep_audio_for_bundles(person_id="me", package_date="2026-06-21", live_session_id=sid)
    assert result["status"] == "ok"
    with connect() as con:
        rows = [dict(row) for row in con.execute(
            """SELECT e.conversation_id,e.export_status,COALESCE(cs.active,0) AS scope_active
               FROM brainlive_brain2_event_exports_v1514 e
               LEFT JOIN v18_conversation_scopes cs ON cs.conversation_id=e.conversation_id AND cs.person_id=e.person_id
               WHERE e.bundle_id=? ORDER BY e.created_at""",
            (bundle_id,),
        )]
    assert len(rows) == 2
    active = next(row for row in rows if row["export_status"] == "exported")
    old = next(row for row in rows if row["export_status"] == "superseded")
    assert int(active["scope_active"]) == 1
    assert int(old["scope_active"]) == 0

    # The public V17 command is V18-overridden; with no episodes it simply
    # reports the conversations it would process.  It must select the refined
    # export exactly once, never the live export as a second source.
    period = build_observed_cases_for_period(
        person_id="me",
        period_start="2026-06-21T00:00:00Z",
        period_end="2026-06-22T00:00:00Z",
    )
    assert period["conversation_ids"] == [active["conversation_id"]]

    # The global V14 mirror is another historical Brain2 reader.  It must use
    # the same active-revision resolver instead of scanning all turns.
    import mlomega_audio_elite.pattern_mirror_v14 as mirror
    with connect() as con:
        payload = mirror._bundle(con, conversation_id=None, person_id="me", limit=20)
    assert [row["conversation_id"] for row in payload["conversations"]] == [active["conversation_id"]]
    assert all(row["text"] != "live rapide imprécis" for row in payload["turns"])
