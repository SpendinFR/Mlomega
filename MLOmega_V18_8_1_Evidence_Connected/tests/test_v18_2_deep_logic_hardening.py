from __future__ import annotations

import json
from pathlib import Path


def _configure(monkeypatch, tmp_path):
    root = tmp_path / "mlomega"
    monkeypatch.setenv("MLOMEGA_HOME", str(root))
    monkeypatch.setenv("MLOMEGA_DB", str(root / "memory.db"))
    monkeypatch.setenv("MLOMEGA_RAW", str(root / "raw"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")
    monkeypatch.delenv("MLOMEGA_V18_DECOMPOSED_LIVE", raising=False)
    monkeypatch.delenv("MLOMEGA_V18_STRICT_LLM_CONTRACTS", raising=False)
    monkeypatch.delenv("MLOMEGA_V18_ALLOW_INCOMPLETE_CONTEXT_INFERENCE", raising=False)
    return root


def _ref() -> dict:
    return {
        "source_table": "brainlive_turn_buffer",
        "source_id": "turn-deep-1",
        "occurred_at": "2026-06-21T10:00:00+00:00",
        "retrievable": True,
        "truncated": False,
    }


def _context(live_session_id: str) -> dict:
    return {
        "context": {
            "session": {"person_id": "me"},
            "episode": {
                "summary": "Episode local : vérification de livraison.",
                "episode_start_at": "2026-06-21T10:00:00+00:00",
                "episode_end_at": "2026-06-21T10:00:01+00:00",
            },
            "context_manifest": {
                "scope": {
                    "person_id": "me",
                    "live_session_id": live_session_id,
                    "as_of": "2026-06-21T10:00:02+00:00",
                },
                "items": [_ref()],
                "omitted_refs": [],
                "excluded_future_refs": [],
                "incomplete": False,
                "requested_budget_chars": 12000,
            },
        }
    }


def _hot_output(*, decision: str = "queue", message: str = "Vérifier la livraison.") -> dict:
    evidence = [{"source_table": "brainlive_turn_buffer", "source_id": "turn-deep-1"}]
    return {
        "world_state": {
            "where_am_i": "bureau",
            "what_is_happening": "audit",
            "probable_activity": "vérification",
            "active_mode": "focused",
            "confidence": 0.8,
            "evidence": evidence,
            "counter_evidence": [],
        },
        "horizons": {
            "H0": {"summary": "", "confidence": 0.2, "evidence": [], "counter_evidence": [], "intervention_candidates": []},
            "H1": {"summary": "Vérifier la queue.", "confidence": 0.8, "evidence": evidence, "counter_evidence": [], "intervention_candidates": []},
            "H2": {"summary": "", "confidence": 0.2, "evidence": [], "counter_evidence": [], "intervention_candidates": []},
        },
        "active_predictions": [],
        "proactive_decision": {
            "decision": decision,
            "horizon": "H1",
            "message": message if decision in {"queue", "speak_now"} else "",
            "expected_gain": 0.75,
            "intrusion_cost": 0.2,
            "confidence": 0.8,
            "evidence": evidence if decision in {"queue", "speak_now"} else [],
            "counter_evidence": [],
        },
        "notes_for_brain2": [],
        "uncertainties": [],
    }


def _session():
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite.db import connect

    live_session_id = start_live_session(person_id="me", title="v18.3 deep hardening")["live_session_id"]
    # Every hot claim must cite a real, owner/session-scoped record.  This is
    # deliberately part of the test fixture now that V18.3 enforces semantic
    # evidence resolution, not only JSON shape.
    with connect() as con:
        con.execute(
            """INSERT INTO brainlive_turn_buffer(
                 live_turn_id,live_session_id,conversation_id,timestamp_start,timestamp_end,
                 speaker_label,speaker_person_id,speaker_confidence,text_partial,text_final,
                 asr_confidence,is_final,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("turn-deep-1", live_session_id, None, "2026-06-21T10:00:00+00:00", None,
             "me", "me", 1.0, None, "référence récupérable", 1.0, 1, "{}", "2026-06-21T10:00:00+00:00"),
        )
        con.commit()
    return live_session_id


def test_image_retry_reuses_one_immutable_occurrence(monkeypatch, tmp_path):
    root = _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.brainlive_sensor_fusion_v15_4 import ingest_image_sensor
    from mlomega_audio_elite.db import connect

    live_session_id = _session()
    image = root / "raw" / "frame.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"image-bytes-for-retry")
    args = dict(
        use_vlm=False,
        source_event_id="phone-image-deep-1",
        source_occurred_at="2026-06-21T10:00:00+00:00",
        source_device="phone",
    )
    first = ingest_image_sensor(live_session_id, image, **args)
    second = ingest_image_sensor(live_session_id, image, **args)
    assert first["frame"]["frame_id"] == second["frame"]["frame_id"]
    assert second["frame"]["reused"] is True
    with connect() as con:
        counts = {
            table: con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in ("raw_assets", "vision_frames", "source_items", "brainlive_sensor_events", "v18_vision_occurrence_map")
        }
    assert counts == {
        "raw_assets": 1,
        "vision_frames": 1,
        "source_items": 1,
        "brainlive_sensor_events": 1,
        "v18_vision_occurrence_map": 1,
    }


def test_hot_queue_is_the_bridge_consumed_queue_and_is_idempotent(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_hotloop_v15_6 as hot
    from mlomega_audio_elite.db import connect

    live_session_id = _session()

    class FakeClient:
        def require_json(self, *_args, **_kwargs):
            return _hot_output()

    monkeypatch.setattr(hot, "OllamaJsonClient", FakeClient)
    kwargs = dict(
        fused={"fused_id": "fused-deep-queue", "person_id": "me", "summary": {}},
        hot_context=_context(live_session_id),
        route={"route_id": "route-deep-queue", "triggered_horizons": ["H1"], "router": {}},
        timeout=1.0,
    )
    first = hot.run_unified_hot_prediction(live_session_id, **kwargs)
    second = hot.run_unified_hot_prediction(live_session_id, **kwargs)
    assert first["status"] == "ok"
    assert first["delivery_ids"]
    assert second["status"] == "deferred"
    with connect() as con:
        queues = [dict(row) for row in con.execute("SELECT delivery_id,horizon,delivery_status,message FROM brainlive_intervention_delivery_queue")]
        links = [dict(row) for row in con.execute("SELECT decision_run_id,delivery_id FROM v18_hot_delivery_links")]
        logs = [dict(row) for row in con.execute("SELECT delivery_status FROM brainlive_hot_intervention_log")]
    assert len(queues) == len(links) == len(logs) == 1
    assert queues[0]["horizon"] == "H1"
    assert queues[0]["delivery_status"] == "queued"
    assert queues[0]["delivery_id"] == first["delivery_ids"][0]


def test_truncated_hot_output_is_repaired_from_same_immutable_capsule(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_hotloop_v15_6 as hot
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.llm import LLMTruncatedOutputError

    live_session_id = _session()

    class FlakyClient:
        calls = 0

        def require_json(self, *_args, **_kwargs):
            type(self).calls += 1
            if type(self).calls == 1:
                raise LLMTruncatedOutputError("LLM output truncated", raw='{"world_state":')
            return _hot_output(decision="observe")

    monkeypatch.setattr(hot, "OllamaJsonClient", FlakyClient)
    kwargs = dict(
        fused={"fused_id": "fused-deep-truncated", "person_id": "me", "summary": {}},
        hot_context=_context(live_session_id),
        route={"route_id": "route-deep-truncated", "triggered_horizons": ["H1"], "router": {}},
        timeout=1.0,
    )
    first = hot.run_unified_hot_prediction(live_session_id, **kwargs)
    assert first["status"] == "repair_requested"
    with connect() as con:
        before = dict(con.execute("SELECT decision_run_id,capsule_id,state,raw_output_text,repair_count FROM v18_llm_decision_runs").fetchone())
    assert before["state"] == "repair_requested"
    assert before["repair_count"] == 1
    assert before["raw_output_text"] == '{"world_state":'
    drained = hot.drain_due_hot_llm_decisions(live_session_id=live_session_id)
    assert len(drained) == 1 and drained[0]["status"] == "ok"
    with connect() as con:
        after = dict(con.execute("SELECT capsule_id,state,attempt_count FROM v18_llm_decision_runs").fetchone())
        attempts = [dict(row) for row in con.execute("SELECT attempt_no,phase,state,raw_output_text FROM v18_llm_decision_attempts ORDER BY attempt_no")]
    assert after["capsule_id"] == before["capsule_id"]
    assert after["state"] == "succeeded"
    assert after["attempt_count"] == 2
    assert [(row["attempt_no"], row["phase"], row["state"]) for row in attempts] == [
        (1, "initial", "retryable_error"),
        (2, "repair", "succeeded"),
    ]
    assert attempts[0]["raw_output_text"] == '{"world_state":'


def test_unannounced_evidence_is_not_normalized_into_a_success(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_hotloop_v15_6 as hot
    from mlomega_audio_elite.db import connect

    live_session_id = _session()
    invalid = _hot_output()
    invalid["world_state"]["evidence"] = [{"source_table": "turns", "source_id": "invented"}]

    class BadEvidenceClient:
        def require_json(self, *_args, **_kwargs):
            return invalid

    monkeypatch.setattr(hot, "OllamaJsonClient", BadEvidenceClient)
    result = hot.run_unified_hot_prediction(
        live_session_id,
        fused={"fused_id": "fused-deep-evidence", "person_id": "me", "summary": {}},
        hot_context=_context(live_session_id),
        route={"route_id": "route-deep-evidence", "triggered_horizons": ["H1"], "router": {}},
        timeout=1.0,
    )
    assert result["status"] == "repair_requested"
    with connect() as con:
        row = dict(con.execute("SELECT state,error_kind,raw_output_text FROM v18_llm_decision_runs").fetchone())
        queue_count = con.execute("SELECT COUNT(*) AS c FROM brainlive_intervention_delivery_queue").fetchone()["c"]
    assert row["state"] == "repair_requested"
    assert row["error_kind"] == "invalid_contract"
    assert "invented" in (row["raw_output_text"] or "")
    assert queue_count == 0


def test_release_audit_refuses_disabled_runtime_safety(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MLOMEGA_V18_DECOMPOSED_LIVE", "false")
    monkeypatch.setenv("MLOMEGA_V18_STRICT_LLM_CONTRACTS", "false")
    monkeypatch.setenv("MLOMEGA_V18_ALLOW_INCOMPLETE_CONTEXT_INFERENCE", "true")
    from mlomega_audio_elite.v18_release_audit import audit_v18_release

    report = audit_v18_release(persist=False)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["status"] == "fail"
    assert {
        "decomposed_live_disabled",
        "strict_llm_contracts_disabled",
        "incomplete_context_inference_enabled",
    } <= codes


def test_local_capsule_window_and_retrieval_stay_within_announced_refs(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    from mlomega_audio_elite.v18_context import _episode_window, retrieve_context_references
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.brainlive_v15 import start_live_session

    turns = [
        {"turn_id": f"t{idx}", "start_at": f"2026-06-21T10:00:{idx:02d}+00:00", "text": f"tour {idx}"}
        for idx in range(9)
    ]
    before, episode, episode_meta = _episode_window(turns, as_of="2026-06-21T10:00:09+00:00")
    assert [x["turn_id"] for x in before + episode] == ["t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"]
    assert len(episode) <= 12
    assert episode_meta["before_turns"] == 2
    live_session_id = start_live_session(person_id="me")["live_session_id"]
    with connect() as con:
        con.execute(
            """INSERT INTO brainlive_turn_buffer(
                 live_turn_id,live_session_id,conversation_id,timestamp_start,timestamp_end,
                 speaker_label,speaker_person_id,speaker_confidence,text_partial,text_final,
                 asr_confidence,is_final,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("turn-deep-1", live_session_id, None, "2026-06-21T10:00:00+00:00", None,
             "me", "me", 1.0, None, "référence récupérable", 1.0, 1, "{}", "2026-06-21T10:00:00+00:00"),
        )
        con.commit()
    manifest = {
        "scope": {"person_id": "me", "live_session_id": live_session_id, "as_of": "2026-06-21T10:00:02+00:00"},
        "items": [_ref()],
    }
    found = retrieve_context_references(person_id="me", live_session_id=live_session_id, manifest=manifest, refs=[{"source_table": "brainlive_turn_buffer", "source_id": "turn-deep-1"}])
    assert len(found) == 1
    import pytest
    with pytest.raises(ValueError, match="not announced"):
        retrieve_context_references(person_id="me", live_session_id=live_session_id, manifest=manifest, refs=[{"source_table": "turns", "source_id": "not-announced"}])


def test_daemon_signal_claim_prevents_second_consumer(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_daemon_v15_3 as daemon
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.utils import json_dumps

    live_session_id = _session()
    daemon.ensure_daemon_schema()
    signal = {
        "signal_id": "daemon-deep-1",
        "live_session_id": live_session_id,
        "signal_type": "transcript",
        "source_path": None,
        "source_sha256": None,
        "payload_json": json_dumps({"items": [{"text": "bonjour"}]}),
        "status": "queued",
        "created_at": "2026-06-21T10:00:00+00:00",
    }
    with connect() as con:
        con.execute(
            """INSERT INTO brainlive_signal_events(
                 signal_id,live_session_id,signal_type,source_path,source_sha256,payload_json,status,consumed_at,result_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (signal["signal_id"], live_session_id, signal["signal_type"], None, None, signal["payload_json"], "queued", None, "{}", signal["created_at"]),
        )
        con.commit()
    calls = []

    def fake_consume(*_args, **_kwargs):
        calls.append("called")
        return {"type": "transcript", "cycles": []}

    monkeypatch.setattr(daemon, "_consume_signal", fake_consume)
    first = daemon._consume_daemon_signal_durable(person_id="me", live_session_id=live_session_id, signal=signal, use_llm=False, use_vlm=False)
    second = daemon._consume_daemon_signal_durable(person_id="me", live_session_id=live_session_id, signal=signal, use_llm=False, use_vlm=False)
    assert first and first["work_state"] == "completed"
    assert second and second["status"] == "reconciled"
    assert calls == ["called"]
    with connect() as con:
        state = con.execute("SELECT status FROM brainlive_signal_events WHERE signal_id=?", (signal["signal_id"],)).fetchone()["status"]
        lease = con.execute("SELECT state,attempt_count FROM v18_work_leases WHERE work_type='brainlive:daemon_signal'").fetchone()
    assert state == "consumed"
    assert lease["state"] == "completed"
    assert lease["attempt_count"] == 1
