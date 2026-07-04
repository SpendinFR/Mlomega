"""E29 acceptance: the 16 live scenario chains + phone_only end-to-end.

The scenario pack proves each live chain against the REAL pipeline
(visionrt/audiort/worldbrain/spatial/scene_adapter); memory/LLM depth is
covered by the final close-day test (E30, user decision).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.scenarios


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


runner = load("run_scenarios_v19", "scripts/run_scenarios_v19.py")
profile_mod = load("v19_profile", "services/live-pc/profile.py")
delivery_adapter = load("delivery_adapter_e29", "services/live-pc/delivery_adapter.py")


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "e29.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))
    return db_path


def test_all_16_scenarios_pass_in_process(tmp_path, monkeypatch):
    """The whole manifest runs against the real pipeline and every chain holds."""
    _env(tmp_path, monkeypatch)
    manifest = runner.load_manifest()
    names = [s["name"] for s in manifest["scenarios"]]
    assert len(names) == 16, f"manifest must cover the 16 handoff scenarios, got {len(names)}: {names}"

    report = runner.run()
    failures = [r for r in report["results"] if r["pass"] is False]
    assert not failures, "scenario failures: " + json.dumps(failures, ensure_ascii=False, default=str)
    assert report["passed"] == report["total"] == 16, report


def test_profile_loader_validates_and_falls_back(tmp_path):
    p = tmp_path / "user_profile.yaml"
    p.write_text("display: phone_only\ncapture: phone_camera\nllm: banana\n", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        profile = profile_mod.load_user_profile(p)
    assert profile["display"] == "phone_only"
    assert profile["llm"] == "ollama_local"  # invalid value fell back safely
    assert profile_mod.renderer_route(profile) == "websocket"
    assert profile_mod.renderer_route({"display": "xreal_one_pro"}) == "datachannel"


def test_phone_only_end_to_end(tmp_path, monkeypatch):
    """Profile phone_only -> BrainLive queue -> WebSocket viewer -> receipt persisted.

    Exercises the exact companion-web contract: the phone viewer receives the
    UIIntent JSON pushed by the delivery adapter and echoes a `displayed`
    UIReceipt, which must land in `brainlive_intervention_feedback_events_v188`.
    """
    _env(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from mlomega_audio_elite.brainlive_v15 import ensure_brainlive_schema
    from mlomega_audio_elite.db import connect, init_db
    from mlomega_audio_elite.v18_delivery import enqueue_delivery, ensure_delivery_schema

    # 1. phone_only profile routes rendering through the WebSocket viewer.
    prof_path = tmp_path / "user_profile.yaml"
    prof_path.write_text("display: phone_only\ncapture: phone_camera\n", encoding="utf-8")
    profile = profile_mod.load_user_profile(prof_path)
    assert profile_mod.renderer_route(profile) == "websocket"

    # 2. A live suggestion enters the V18.8 H1 queue (same primitive E28 uses).
    init_db()
    ensure_brainlive_schema()
    ensure_delivery_schema()
    # Same session-bootstrap primitive the E28 scene adapter uses, so
    # enqueue_delivery can resolve the owner.
    from mlomega_audio_elite.v19_visual_context import publish_visual_context

    publish_visual_context(person_id="me", live_session_id="sess-e29", world_state=None, observations=None)
    queued = enqueue_delivery(
        live_session_id="sess-e29",
        source_key="scene:sess-e29:phone_only_e2e",
        candidate={"message": "Ton téléphone est sur la table basse", "decision": "notify", "priority": 0.8},
    )
    assert queued["status"] == "queued", queued

    # 3. Viewer (companion-web contract) connects, receives, acknowledges.
    adapter = delivery_adapter.DeliveryAdapter(renderer=delivery_adapter.WebSocketRendererHub())
    app = delivery_adapter.create_app(adapter)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        # The endpoint dispatches queued deliveries on connect — the phone
        # viewer receives the UIIntent JSON immediately.
        pushed = json.loads(ws.receive_text())
        assert pushed["delivery_id"] == queued["delivery_id"]
        assert "téléphone" in json.dumps(pushed, ensure_ascii=False)

        ws.send_text(json.dumps({
            "ui_intent_id": pushed["ui_intent_id"],
            "delivery_id": pushed["delivery_id"],
            "event": "displayed",
            "observed_at": "2026-07-04T10:00:05+00:00",
            "source": "companion_web_phone",
        }))
        # Receiving is handled server-side; give the endpoint one round-trip.
        ws.send_text(json.dumps({
            "ui_intent_id": pushed["ui_intent_id"],
            "delivery_id": pushed["delivery_id"],
            "event": "seen",
            "observed_at": "2026-07-04T10:00:07+00:00",
            "source": "companion_web_phone",
        }))

    # 4. The receipts landed in the V18.8 feedback table (memory learns).
    with connect() as con:
        rows = [
            dict(r) for r in con.execute(
                "SELECT feedback_type FROM brainlive_intervention_feedback_events_v188 WHERE delivery_id=?",
                (queued["delivery_id"],),
            ).fetchall()
        ]
    kinds = {r["feedback_type"] for r in rows}
    assert "delivered" in kinds, rows
    assert "displayed" in kinds, rows
