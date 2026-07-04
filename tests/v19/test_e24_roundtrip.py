"""E24 end criterion (PC-executable cross-validation roundtrip).

Proves the full mobile-transport loop against the gateway, over the unified
token-gated ``POST /webrtc/offer`` signaling:

  1. fake XR device streams frames + FrameEnvelope; ``frame_id`` and ``pose``
     arrive intact in the gateway (AiortcIngress).
  2. the gateway sends a UIIntent back over the DataChannel referencing a
     ``target_track_id``; the device receives it and echoes a UIReceipt carrying
     the same ``target_track_id`` / ``delivery_id`` / ``ui_intent_id``.
  3. the returned UIReceipt is routed to ``DeliveryAdapter.record_receipt`` ->
     ``record_delivery_feedback`` and lands in
     ``brainlive_intervention_feedback_events_v188``.

This is the ``brainlive_xr_delivery_receipt`` end-to-end path, run entirely on
the PC side (no Android/hardware). Marked ``transport``; skipped without aiortc.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.transport

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gateway = _load("gateway", "services/live-pc/gateway.py")
fake = _load("fake_xr_device", "simulators/fake_xr_device.py")
sessionhub = _load("sessionhub", "services/live-pc/sessionhub.py")
sessionhub_http = _load("sessionhub_http", "services/live-pc/sessionhub_http.py")
delivery_adapter = _load("delivery_adapter", "services/live-pc/delivery_adapter.py")

aiortc_missing = not (gateway.AIORTC_AVAILABLE and fake.AIORTC_AVAILABLE)
skip_no_aiortc = pytest.mark.skipif(aiortc_missing, reason="aiortc/av not installed")

from packages.contracts.python.models import UIIntent, UIReceipt  # noqa: E402


@skip_no_aiortc
def test_e24_roundtrip_frame_uiintent_uireceipt(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")

    # Isolated V18.8 DB so the receipt lands in a clean feedback table.
    db_path = tmp_path / "e24.db"
    monkeypatch.setenv("MLOMEGA_DB", str(db_path))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    monkeypatch.setenv("MLOMEGA_ENABLE_OLLAMA", "false")

    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite.db import connect
    from mlomega_audio_elite.v18_delivery import enqueue_delivery

    # A real queued BrainLive delivery gives us an authentic delivery_id.
    session = start_live_session(person_id="me", title="E24 roundtrip")
    queued = enqueue_delivery(
        live_session_id=session["live_session_id"],
        source_key="v19-e24:roundtrip",
        candidate={
            "candidate_id": "v19-e24-roundtrip",
            "message": "E24 roundtrip card",
            "action_type": "notify",
            "decision": "queue",
            "priority": 0.9,
        },
    )
    delivery_id = queued["delivery_id"]
    assert delivery_id

    target_track_id = "track-e24-42"
    ui_intent = UIIntent(
        ui_intent_id="ui-e24-roundtrip",
        producer="brainlive",
        target_track_id=target_track_id,
        component="context_card",
        anchor={"type": "panel", "position": "side"},
        content={"message": "E24 roundtrip card"},
        truth_level="inferred",
        confidence=1.0,
        priority=0.9,
        ttl_ms=15000,
        delivery_id=delivery_id,
    )

    async def run():
        hub = sessionhub.SessionHub()
        ingress = gateway.AiortcIngress(
            host="127.0.0.1", port=8796, session_id="sim-session", max_frames=20
        )
        await ingress.start()

        # Wire receipts from the DataChannel to V18.8 feedback persistence.
        adapter = delivery_adapter.DeliveryAdapter()

        def _on_receipt(text: str) -> None:
            adapter.record_receipt(UIReceipt.model_validate_json(text))

        ingress.on_receipt = _on_receipt

        app = sessionhub_http.create_app(hub, ingress=ingress, enable_signaling=True)

        received: list = []

        async def consume():
            async for frame_bgr, env in ingress:
                received.append((frame_bgr, env))

        consumer = asyncio.create_task(consume())

        # --- device side: a raw aiortc peer we fully control ---
        from aiortc import RTCPeerConnection, RTCSessionDescription

        pc = RTCPeerConnection()
        channel = pc.createDataChannel("contracts", ordered=True)
        pending: list = []
        got_intent: dict = {}
        receipt_sent = asyncio.Event()

        def _emit_envelope(env) -> None:
            if channel.readyState == "open":
                channel.send(env.model_dump_json())
            else:
                pending.append(env)

        @channel.on("open")
        def _flush() -> None:
            for env in pending:
                channel.send(env.model_dump_json())
            pending.clear()

        @channel.on("message")
        def _on_downlink(message) -> None:
            # The gateway pushes a UIIntent; the device echoes a UIReceipt that
            # references the SAME target_track_id (proves attachment) + delivery_id.
            payload = json.loads(message)
            if "target_track_id" not in payload:
                return
            got_intent.update(payload)
            receipt = UIReceipt(
                ui_intent_id=payload["ui_intent_id"],
                delivery_id=payload.get("delivery_id"),
                event="displayed",
                observed_at=datetime.now(timezone.utc).isoformat(),
                local_track_state={"target_track_id": payload["target_track_id"]},
                source="fake_xr_device",
            )
            channel.send(receipt.model_dump_json())
            receipt_sent.set()

        track = fake._FakeCaptureTrack(
            session_id="sim-session",
            fps=30,
            frames=20,
            rotation=0,
            loss=0.0,
            source="fake_xr_device",
            mp4=None,
            poses=None,
            on_envelope=_emit_envelope,
        )
        pc.addTrack(track)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://sessionhub.test"
        ) as http:
            creds = (
                await http.post("/session/create", json={"device_id": "s25-e24"})
            ).json()
            resp = await http.post(
                "/webrtc/offer",
                json={
                    "sdp": pc.localDescription.sdp,
                    "type": pc.localDescription.type,
                    "session_id": creds["session_id"],
                    "token": creds["token"],
                },
            )
            assert resp.status_code == 200
            answer = resp.json()
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )

            # Wait for a few frames to confirm the uplink is live, then push the
            # UIIntent downlink and wait for the echoed receipt.
            for _ in range(200):
                if len(received) >= 3:
                    break
                await asyncio.sleep(0.02)
            assert len(received) >= 3

            sent = ingress.send_ui_intent(ui_intent.model_dump_json())
            assert sent >= 1

            try:
                await asyncio.wait_for(receipt_sent.wait(), timeout=4)
            finally:
                # let the tail frames flush
                while track._idx < track.total:
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.3)
                await pc.close()

        try:
            await asyncio.wait_for(consumer, timeout=6)
        except asyncio.TimeoutError:
            consumer.cancel()
        await ingress.close()
        return received, got_intent

    received, got_intent = asyncio.run(run())

    # (1) frame_id + pose intact in the gateway
    assert len(received) >= 15
    _, first_env = received[0]
    assert first_env.frame_id.startswith("sim-session-frame-")
    assert len(first_env.pose.position) == 3

    # (2) UIIntent came back with the correct target_track_id
    assert got_intent.get("target_track_id") == target_track_id
    assert got_intent.get("delivery_id") == delivery_id

    # (3) UIReceipt reached record_delivery_feedback (V18.8 feedback table)
    with connect() as con:
        con.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in con.execute(
                """SELECT feedback_type, feedback_source FROM
                   brainlive_intervention_feedback_events_v188 WHERE delivery_id=?""",
                (delivery_id,),
            ).fetchall()
        ]
    assert any(r["feedback_type"] == "displayed" for r in rows), rows
    assert any(r["feedback_source"] == "xr_adapter" for r in rows)
