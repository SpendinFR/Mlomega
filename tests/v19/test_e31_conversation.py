"""E31 acceptance — live transcripts → V18.8 BrainLive conversational loop.

Three real chains, all against the actual core (no pipeline stubs):

1. **wiring** — a simulated final AudioRT segment goes through the
   ``ConversationBridge`` and lands in the core ``brainlive_turn_buffer`` with the
   shared V19 ``live_session_id`` and correct UTC timestamps.

2. **reactivity** — a test memory is seeded through the core's own primitives
   (a prior turn establishing a subject), a transcript mentioning that subject is
   ingested, the existing hot loop runs and its proactive H1 decision lands in
   ``brainlive_intervention_delivery_queue`` **with evidence**. The LLM is used
   for real if Ollama has a model loaded; otherwise ONLY the external LLM client
   boundary (``OllamaJsonClient.require_json``) is monkeypatched with a valid JSON
   response that echoes a real manifest evidence ref — an external-service border,
   not a pipeline stub (ADR §E31).

3. **end-to-end** — the queued candidate reaches the companion-web WebSocket viewer
   (reusing the E29 ``phone_only`` delivery-adapter pattern).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bridge_mod = load("v19_conversation_bridge_e31", "services/live-pc/conversation_bridge.py")
delivery_adapter = load("delivery_adapter_e31", "services/live-pc/delivery_adapter.py")


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MLOMEGA_DB", str(tmp_path / "e31.db"))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path))
    from mlomega_audio_elite.db import init_db
    from mlomega_audio_elite.brainlive_v15 import ensure_brainlive_schema

    init_db()
    ensure_brainlive_schema()


def _ollama_has_model() -> bool:
    """True only if Ollama is serving AND has at least one model available."""
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("models"))
    except Exception:
        return False


# --------------------------------------------------------------------------- 1
def test_wiring_final_segment_reaches_core_turn_buffer(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from mlomega_audio_elite.db import connect

    bridge = bridge_mod.ConversationBridge(person_id="me", run_hot_cycle=False)
    res = bridge.ingest_segment(
        "On doit rappeler le dentiste demain matin",
        language="fr",
        is_final=True,
        timestamp_start="2026-07-04T10:00:00+00:00",
        timestamp_end="2026-07-04T10:00:03+00:00",
        event_id="audiort-live-1",
    )
    assert res is not None
    session_id = res["live_session_id"]
    assert session_id and res["live_turn_id"]

    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM brainlive_turn_buffer WHERE live_session_id=?", (session_id,)
        ).fetchall()]
    assert len(rows) == 1, rows
    turn = rows[0]
    assert turn["is_final"] == 1
    assert turn["text_final"] == "On doit rappeler le dentiste demain matin"
    from datetime import datetime
    assert datetime.fromisoformat(turn["timestamp_start"]) == datetime.fromisoformat("2026-07-04T10:00:00+00:00")
    assert turn["speaker_label"] == "speaker"  # generic; identity is E32
    assert bridge.metrics["conversation_turns"] == 1

    # A partial (non-final) segment stays on the reflex subtitle path — never
    # enters the buffer.
    assert bridge.ingest_segment("partiel", is_final=False) is None
    with connect() as con:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM brainlive_turn_buffer WHERE live_session_id=?", (session_id,)
        ).fetchone()["n"]
    assert n == 1


# --------------------------------------------------------------------------- helpers for 2/3
def _hot_result_echoing_manifest(user_json: str) -> dict:
    """Build a contract-valid HOT_UNIFIED result whose H1 proactive decision
    references a REAL manifest evidence ref (a seeded turn). This mirrors exactly
    what a compliant local model must emit; the strict manifest/DB validator in
    ``_hot_output_contract`` still runs against it."""
    payload = json.loads(user_json)
    manifest = payload.get("manifest") or {}
    items = [it for it in (manifest.get("items") or []) if isinstance(it, dict)]
    turn_refs = [it for it in items if it.get("source_table") == "brainlive_turn_buffer"]
    ref_src = turn_refs[0] if turn_refs else (items[0] if items else {})
    ev = {"source_table": ref_src.get("source_table"), "source_id": ref_src.get("source_id")}
    empty_h = lambda: {"summary": "", "needs": [], "risks_or_opportunities": [],
                       "intervention_candidates": [], "watch_next": [], "confidence": 0.0,
                       "evidence": [], "counter_evidence": []}
    h1 = {"summary": "Rappel dentiste évoqué", "needs": [], "risks_or_opportunities": [],
          "intervention_candidates": ["rappeler le dentiste"], "watch_next": [],
          "confidence": 0.7, "evidence": [ev], "counter_evidence": []}
    return {
        "world_state": {"where_am_i": None, "what_is_happening": None,
                        "probable_activity": None, "active_mode": "unknown",
                        "confidence": 0.4, "evidence": [], "counter_evidence": []},
        "horizons": {"H0": empty_h(), "H1": h1, "H2": empty_h()},
        "active_predictions": [],
        "proactive_decision": {
            "decision": "queue",
            "horizon": "H1",
            "message": "Tu voulais rappeler le dentiste demain matin",
            "expected_gain": 0.7, "intrusion_cost": 0.2, "confidence": 0.7,
            "evidence": [ev], "counter_evidence": [],
        },
        "notes_for_brain2": "",
        "uncertainties": [],
        "needs_evidence": [],
    }


def _run_reactivity(tmp_path, monkeypatch):
    """Seed memory + ingest a subject-bearing transcript, then run the real H1
    hot decision (Ollama if a model is loaded, else the LLM client border mocked).
    Returns (session_id, delivery_rows)."""
    _env(tmp_path, monkeypatch)
    from datetime import datetime, timedelta, timezone

    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.brainlive_v15 import ingest_live_turn, start_live_session
    from mlomega_audio_elite.brainlive_hotloop_v15_6 import (
        prepare_hot_context,
        run_unified_hot_prediction,
    )

    sess = start_live_session(person_id="me", title="e31 reactivity", mode="live_xr")
    sid = sess["live_session_id"]

    base = datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc)
    # (a) seed the memory: a prior turn establishing the subject (the open loop).
    ingest_live_turn(
        sid, "Il faut vraiment que je rappelle le dentiste pour le rendez-vous",
        speaker_label="me", is_final=True,
        timestamp_start=base.isoformat(), timestamp_end=(base + timedelta(seconds=3)).isoformat(),
        metadata={"event_id": "seed-1", "source": "test_seed"},
    )
    # (b) the live transcript that re-raises the subject.
    t2 = base + timedelta(minutes=1)
    ingest_live_turn(
        sid, "Au fait, le dentiste, c'est bien demain matin ?",
        speaker_label="speaker", is_final=True,
        timestamp_start=t2.isoformat(), timestamp_end=(t2 + timedelta(seconds=3)).isoformat(),
        metadata={"event_id": "live-1", "source": "v19_audiort"},
    )

    hot_ctx = prepare_hot_context(sid, person_id="me", active_people=["me"], force=True)
    route = {"route_id": None, "route_status": "run_h0_h1", "triggered_horizons": ["H1"]}
    fused = {"fused_id": None, "person_id": "me", "confidence": {"overall": 0.5}}

    if _ollama_has_model():
        prediction = run_unified_hot_prediction(sid, fused=fused, hot_context=hot_ctx, route=route)
    else:
        from mlomega_audio_elite import llm as llm_mod

        real = llm_mod.OllamaJsonClient.require_json

        def fake_require_json(self, system, user, *, schema_hint=None, timeout=None, **kw):
            hint = json.dumps(schema_hint or {})
            # Route/fused/identity borders return safe minimal shapes; the hot
            # unified decision returns the manifest-grounded H1 candidate.
            if "world_state" in hint and "proactive_decision" in hint:
                return _hot_result_echoing_manifest(user)
            return {}

        monkeypatch.setattr(llm_mod.OllamaJsonClient, "require_json", fake_require_json, raising=True)
        prediction = run_unified_hot_prediction(sid, fused=fused, hot_context=hot_ctx, route=route)

    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM brainlive_intervention_delivery_queue WHERE live_session_id=?", (sid,)
        ).fetchall()]
    return sid, prediction, rows


# --------------------------------------------------------------------------- 2
def test_reactivity_subject_in_memory_triggers_h1_candidate_with_evidence(tmp_path, monkeypatch):
    sid, prediction, rows = _run_reactivity(tmp_path, monkeypatch)
    assert rows, f"expected an H1 candidate in the delivery queue, got none (prediction={prediction})"
    row = rows[0]
    assert row["delivery_status"] == "queued", row
    assert "dentiste" in (row["message"] or "").lower()
    evidence = json.loads(row["evidence_json"] or "{}")
    refs = evidence.get("evidence_refs") or evidence.get("refs") or evidence
    assert refs, f"H1 candidate must carry evidence, got {evidence}"


# --------------------------------------------------------------------------- 3
def test_end_to_end_candidate_reaches_websocket_viewer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    sid, prediction, rows = _run_reactivity(tmp_path, monkeypatch)
    assert rows, f"no queued candidate to deliver (prediction={prediction})"
    delivery_id = rows[0]["delivery_id"]

    adapter = delivery_adapter.DeliveryAdapter(renderer=delivery_adapter.WebSocketRendererHub())
    app = delivery_adapter.create_app(adapter)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        pushed = json.loads(ws.receive_text())
        assert pushed["delivery_id"] == delivery_id
        assert "dentiste" in json.dumps(pushed, ensure_ascii=False).lower()
        ws.send_text(json.dumps({
            "ui_intent_id": pushed["ui_intent_id"],
            "delivery_id": pushed["delivery_id"],
            "event": "displayed",
            "observed_at": "2026-07-04T09:02:00+00:00",
            "source": "companion_web_phone",
        }))

    from mlomega_audio_elite.db import connect

    with connect() as con:
        kinds = {r["feedback_type"] for r in con.execute(
            "SELECT feedback_type FROM brainlive_intervention_feedback_events_v188 WHERE delivery_id=?",
            (delivery_id,),
        ).fetchall()}
    assert "delivered" in kinds, kinds
    assert "displayed" in kinds, kinds
