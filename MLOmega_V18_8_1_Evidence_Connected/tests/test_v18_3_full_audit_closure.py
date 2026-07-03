from __future__ import annotations

import json


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


def _session_with_turn(turn_id: str = "turn-v183") -> str:
    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite.db import connect

    sid = start_live_session(person_id="me", title="V18.3 audit closure")["live_session_id"]
    with connect() as con:
        con.execute(
            """INSERT INTO brainlive_turn_buffer(
                   live_turn_id,live_session_id,conversation_id,timestamp_start,timestamp_end,
                   speaker_label,speaker_person_id,speaker_confidence,text_partial,text_final,
                   asr_confidence,is_final,metadata_json,created_at
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (turn_id, sid, None, "2026-06-21T10:00:00+00:00", None, "me", "me", 1.0,
             None, "preuve réelle de l'épisode", 1.0, 1, "{}", "2026-06-21T10:00:00+00:00"),
        )
        con.commit()
    return sid


def _ref(source_id: str = "turn-v183") -> dict:
    return {
        "source_table": "brainlive_turn_buffer",
        "source_id": source_id,
        "occurred_at": "2026-06-21T10:00:00+00:00",
        "retrievable": True,
        "truncated": False,
        "content_sha256": "test-ref",
    }


def _context(sid: str, refs: list[dict] | None = None, *, incomplete: bool = False) -> dict:
    return {
        "context": {
            "session": {"person_id": "me"},
            "episode": {
                "episode_start_at": "2026-06-21T10:00:00+00:00",
                "episode_end_at": "2026-06-21T10:00:01+00:00",
                "summary": "Épisode local et borné de test.",
                "turn_ids": ["turn-v183"],
            },
            "context_manifest": {
                "scope": {"person_id": "me", "live_session_id": sid, "as_of": "2026-06-21T10:00:02+00:00", "mode": "live"},
                "items": refs if refs is not None else [_ref()],
                "omitted_refs": [],
                "excluded_future_refs": [],
                "incomplete": incomplete,
                "requested_budget_chars": 12_000,
            },
        }
    }


def _output(*, refs: list[dict] | None = None, needs_evidence: list[dict] | None = None, decision: str = "observe") -> dict:
    refs = refs if refs is not None else [_ref()]
    return {
        "world_state": {
            "where_am_i": "bureau", "who_is_active": ["me"], "what_is_happening": "audit",
            "probable_activity": ["vérification"], "active_mode": "focused", "confidence": 0.8,
            "evidence": refs, "counter_evidence": [], "missing_evidence": [],
        },
        "horizons": {
            "H0": {"summary": "", "needs": [], "risks_or_opportunities": [], "intervention_candidates": [], "watch_next": [], "confidence": 0.1, "evidence": [], "counter_evidence": []},
            "H1": {"summary": "Observer le résultat.", "needs": [], "risks_or_opportunities": [], "intervention_candidates": [], "watch_next": [], "confidence": 0.8, "evidence": refs, "counter_evidence": []},
            "H2": {"summary": "", "needs": [], "risks_or_opportunities": [], "intervention_candidates": [], "watch_next": [], "confidence": 0.1, "evidence": [], "counter_evidence": []},
        },
        "active_predictions": [],
        "proactive_decision": {
            "decision": decision, "message": "" if decision in {"observe", "wait"} else "Prends une pause.",
            "horizon": "H1", "expected_gain": 0.0 if decision in {"observe", "wait"} else 0.7,
            "intrusion_cost": 0.1, "confidence": 0.4, "why_now": "", "risk_if_wrong": "",
            "evidence": [] if decision in {"observe", "wait"} else refs, "counter_evidence": [],
        },
        "notes_for_brain2": [], "uncertainties": [], "needs_evidence": needs_evidence,
    }


def test_hot_capsule_hard_budget_is_exact_user_prompt(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MLOMEGA_V18_HOT_CAPSULE_MAX_CHARS", "1800")
    import mlomega_audio_elite.brainlive_hotloop_v15_6 as hot
    from mlomega_audio_elite.db import connect

    sid = _session_with_turn()
    long_refs = [
        {**_ref(), "source_id": f"large-{i}", "text": "X" * 2500, "importance": 0.8}
        for i in range(40)
    ]
    # Keep one actual resolvable ref for the accepted fake answer; the rest are
    # deliberately large and verify that the renderer removes whole content.
    ctx = _context(sid, [_ref(), *long_refs])
    captured: dict[str, object] = {}

    class Client:
        def require_json(self, _system, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["budget"] = kwargs.get("max_output_tokens")
            return _output()

    monkeypatch.setattr(hot, "OllamaJsonClient", Client)
    result = hot.run_unified_hot_prediction(
        sid,
        fused={"fused_id": "fused-budget", "person_id": "me", "summary": {}},
        hot_context=ctx,
        route={"route_id": "route-budget", "triggered_horizons": ["H1"], "router": {}},
        timeout=1.0,
    )
    assert result["status"] == "ok"
    sent = json.loads(str(captured["prompt"]))
    assert len(str(captured["prompt"])) <= int(sent["input_budget_chars"])
    assert sent["rendered_input_chars"] == len(str(captured["prompt"]))
    assert int(captured["budget"]) == sent["output_budget_tokens"]
    assert sent["manifest"]["omitted_ref_count"] > 0
    with connect() as con:
        capsule = json.loads(con.execute("SELECT capsule_json FROM v18_episode_capsules").fetchone()["capsule_json"])
    assert capsule["extra"]["prompt_payload"] == sent


def test_model_evidence_request_creates_successor_without_h0_block(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_hotloop_v15_6 as hot
    from mlomega_audio_elite.db import connect

    sid = _session_with_turn()

    class Client:
        calls = 0
        def require_json(self, *_args, **_kwargs):
            type(self).calls += 1
            return _output(needs_evidence=[_ref()]) if type(self).calls == 1 else _output()

    monkeypatch.setattr(hot, "OllamaJsonClient", Client)
    args = dict(
        fused={"fused_id": "fused-evidence", "person_id": "me", "summary": {}},
        hot_context=_context(sid),
        route={"route_id": "route-evidence", "triggered_horizons": ["H1"], "router": {}},
        timeout=1.0,
    )
    first = hot.run_unified_hot_prediction(sid, **args)
    assert first["status"] == "needs_evidence"
    assert first["successor_decision_run_id"]
    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT decision_run_id,state,capsule_id FROM v18_llm_decision_runs ORDER BY created_at")]
        requests = [dict(r) for r in con.execute("SELECT state FROM v18_llm_evidence_requests")]
    assert len(rows) == 2 and rows[0]["state"] == "terminal_error" and rows[1]["state"] == "pending"
    assert requests and all(r["state"] == "resolved" for r in requests)
    drained = hot.drain_due_hot_llm_decisions(live_session_id=sid)
    assert len(drained) == 1 and drained[0]["status"] == "ok"


def test_existing_but_cross_session_evidence_is_rejected(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_hotloop_v15_6 as hot
    from mlomega_audio_elite.db import connect

    sid = _session_with_turn("turn-own")
    other_sid = _session_with_turn("turn-other")
    assert other_sid != sid

    class Client:
        def require_json(self, *_args, **_kwargs):
            return _output(refs=[_ref("turn-other")])

    monkeypatch.setattr(hot, "OllamaJsonClient", Client)
    result = hot.run_unified_hot_prediction(
        sid,
        fused={"fused_id": "fused-cross", "person_id": "me", "summary": {}},
        hot_context=_context(sid, [_ref("turn-other")]),
        route={"route_id": "route-cross", "triggered_horizons": ["H1"], "router": {}},
        timeout=1.0,
    )
    assert result["status"] == "repair_requested"
    with connect() as con:
        run = dict(con.execute("SELECT state,error_kind FROM v18_llm_decision_runs").fetchone())
    assert run["state"] == "repair_requested" and run["error_kind"] == "invalid_contract"


def test_daemon_hidden_llm_error_is_retryable_not_consumed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_daemon_v15_3 as daemon
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.utils import json_dumps

    sid = _session_with_turn()
    daemon.ensure_daemon_schema()
    signal = {
        "signal_id": "sig-hidden-error", "live_session_id": sid, "signal_type": "audio_chunk",
        "source_path": None, "source_sha256": None,
        "payload_json": json_dumps({"text": "bonjour", "speaker": {}}),
        "created_at": "2026-06-21T10:00:00+00:00",
    }
    with connect() as con:
        con.execute(
            "INSERT INTO brainlive_signal_events(signal_id,live_session_id,signal_type,source_path,source_sha256,payload_json,status,consumed_at,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (signal["signal_id"], sid, signal["signal_type"], None, None, signal["payload_json"], "queued", None, "{}", signal["created_at"]),
        )
        con.commit()

    monkeypatch.setattr(daemon, "live_cycle_all_horizons", lambda *_a, **_k: {"H0": {"status": "ok"}, "H1": {"status": "llm_error", "error": "timeout"}, "H2": {"status": "ok"}})
    result = daemon._consume_daemon_signal_durable(person_id="me", live_session_id=sid, signal=signal, use_llm=True, use_vlm=False)
    assert result and result["work_state"] == "retryable_error"
    with connect() as con:
        inbox = dict(con.execute("SELECT status,consumed_at FROM brainlive_signal_events WHERE signal_id=?", (signal["signal_id"],)).fetchone())
        lease = dict(con.execute("SELECT state FROM v18_work_leases WHERE work_type='brainlive:daemon_signal'").fetchone())
    assert inbox["status"] == "retryable_error" and inbox["consumed_at"] is None
    assert lease["state"] == "retryable_error"


def test_incomplete_source_capsule_refuses_llm_without_success(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    import mlomega_audio_elite.brainlive_hotloop_v15_6 as hot

    sid = _session_with_turn()
    calls: list[bool] = []

    class Client:
        def require_json(self, *_args, **_kwargs):
            calls.append(True)
            return _output()

    monkeypatch.setattr(hot, "OllamaJsonClient", Client)
    result = hot.run_unified_hot_prediction(
        sid,
        fused={"fused_id": "fused-incomplete", "person_id": "me", "summary": {}},
        hot_context=_context(sid, incomplete=True),
        route={"route_id": "route-incomplete", "triggered_horizons": ["H1"], "router": {}},
        timeout=1.0,
    )
    assert result["status"] == "context_incomplete"
    assert calls == []


def test_ollama_output_budget_is_sent_to_provider(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "true")
    from mlomega_audio_elite.llm import OllamaJsonClient
    import mlomega_audio_elite.llm as llm

    seen: dict[str, object] = {}

    class Response:
        def read(self):
            return b'{"response":"{\\"ok\\":true}","done":true,"done_reason":"stop"}'
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    data = OllamaJsonClient().require_json("sys", "{}", schema_hint={"ok": True}, timeout=1.0, max_output_tokens=333)
    assert data == {"ok": True}
    assert seen["payload"]["options"]["num_predict"] == 333


def test_image_retry_after_mid_transaction_failure_leaves_one_occurrence(monkeypatch, tmp_path):
    root = _configure(monkeypatch, tmp_path)
    import pytest
    import mlomega_audio_elite.brainlive_sensor_fusion_v15_4 as sensor
    from mlomega_audio_elite.db import connect

    sid = _session_with_turn()
    image = root / "raw" / "atomic.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"atomic-image")
    original = sensor._record_sensor_event
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash before sensor event commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(sensor, "_record_sensor_event", fail_once)
    args = dict(use_vlm=False, source_event_id="phone-atomic-1", source_occurred_at="2026-06-21T10:00:00+00:00", source_device="phone")
    with pytest.raises(RuntimeError, match="injected crash"):
        sensor.ingest_image_sensor(sid, image, **args)
    with connect() as con:
        assert con.execute("SELECT COUNT(*) AS c FROM vision_frames").fetchone()["c"] == 0
        assert con.execute("SELECT COUNT(*) AS c FROM v18_vision_occurrence_map").fetchone()["c"] == 0
    second = sensor.ingest_image_sensor(sid, image, **args)
    assert second["frame"]["reused"] is False
    third = sensor.ingest_image_sensor(sid, image, **args)
    assert third["frame"]["frame_id"] == second["frame"]["frame_id"]
    with connect() as con:
        assert con.execute("SELECT COUNT(*) AS c FROM vision_frames").fetchone()["c"] == 1
        assert con.execute("SELECT COUNT(*) AS c FROM brainlive_sensor_events").fetchone()["c"] == 1
