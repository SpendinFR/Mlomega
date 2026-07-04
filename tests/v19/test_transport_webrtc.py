"""Real WebRTC transport integration tests (aiortc loopback).

Marked ``transport`` and skipped when aiortc is unavailable. Exercises the full
local loop: :class:`FakeXrWebrtcClient` streams N frames -> :class:`AiortcIngress`
receives/decodes them -> assert frame_id/pose association, queue=1 bound, drops.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.transport

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gateway = _load("gateway", "services/live-pc/gateway.py")
fake = _load("fake_xr_device", "simulators/fake_xr_device.py")
sessionhub = _load("sessionhub", "services/live-pc/sessionhub.py")
sessionhub_http = _load("sessionhub_http", "services/live-pc/sessionhub_http.py")

aiortc_missing = not (gateway.AIORTC_AVAILABLE and fake.AIORTC_AVAILABLE)
skip_no_aiortc = pytest.mark.skipif(aiortc_missing, reason="aiortc/av not installed")

SCENARIO_MP4 = ROOT / "simulators" / "scenarios" / "test_scene.mp4"
SCENARIO_POSE = ROOT / "simulators" / "scenarios" / "test_scene_pose.jsonl"


async def _run_loop(*, frames: int, loss: float, port: int, use_scenario: bool):
    ingress = gateway.AiortcIngress(
        host="127.0.0.1", port=port, session_id="sim-session", max_frames=frames
    )
    await ingress.start()
    queue = gateway.LatestFrameQueue()
    received: list = []

    async def consume():
        async for frame_bgr, env in ingress:
            queue.put_latest((frame_bgr, env))
            received.append((frame_bgr, env))

    consumer = asyncio.create_task(consume())

    mp4 = SCENARIO_MP4 if (use_scenario and SCENARIO_MP4.exists()) else None
    pose = SCENARIO_POSE if (use_scenario and SCENARIO_POSE.exists()) else None
    client = fake.FakeXrWebrtcClient(
        offer_url=ingress.offer_url,
        session_id="sim-session",
        fps=30,
        frames=frames,
        loss=loss,
        mp4=mp4,
        pose_jsonl=pose,
    )
    client_result = await client.run()
    try:
        await asyncio.wait_for(consumer, timeout=6)
    except asyncio.TimeoutError:
        consumer.cancel()
    await ingress.close()
    return ingress, queue, received, client_result


@skip_no_aiortc
def test_webrtc_loop_delivers_frames_with_pose():
    async def run():
        ingress, queue, received, result = await _run_loop(
            frames=20, loss=0.0, port=8792, use_scenario=True
        )
        # Most frames should arrive (tail frame may be lost during teardown).
        assert len(received) >= 15
        first_bgr, first_env = received[0]
        # Real decoded frame: numpy BGR image of the scenario resolution.
        assert first_bgr.ndim == 3 and first_bgr.shape[2] == 3
        # Envelope carries a valid frame_id and pose from the sender.
        assert first_env.frame_id.startswith("sim-session-frame-")
        assert len(first_env.pose.position) == 3
        # Every association is accounted for (matched or nearest-timestamp).
        m = ingress.matcher.stats()
        assert m["matched"] + m["fallback_nearest"] >= len(received)
        assert m["unmatched"] == 0
        # Real decode happened: bench recorded samples.
        assert ingress.bench.summary()["count"] >= 15

    asyncio.run(run())


@skip_no_aiortc
def test_webrtc_queue_bounded_and_drops_counted():
    async def run():
        ingress, queue, received, result = await _run_loop(
            frames=20, loss=0.0, port=8793, use_scenario=False
        )
        stats = queue.stats()
        # queue never exceeds one slot.
        assert stats["queue_size"] <= 1
        # We consumed faster than nothing; received == put; drops = received - 1
        # (or received when the last was consumed). Drops are counted honestly.
        assert stats["received_frames"] == len(received)
        assert stats["dropped_frames"] == max(0, stats["received_frames"] - stats["queue_size"])

    asyncio.run(run())


@skip_no_aiortc
def test_webrtc_metadata_loss_tolerated():
    async def run():
        ingress, queue, received, result = await _run_loop(
            frames=20, loss=0.5, port=8794, use_scenario=False
        )
        # Half the envelopes are dropped on the wire; frames still arrive, either
        # with a nearest-match envelope or a synthesized placeholder. Every
        # received frame is categorized (matched / nearest / unmatched-placeholder).
        assert len(received) >= 10
        m = ingress.matcher.stats()
        assert m["matched"] + m["fallback_nearest"] + m["unmatched"] == len(received)
        # Loss actually happened: fewer envelopes were sent than frames.
        assert result["envelopes_sent"] < result["frames_sent"]
        # Frames whose envelope was lost fall back to a placeholder envelope.
        assert m["unmatched"] >= 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# E24: unified signaling. fake_xr_device negotiates through the SessionHub HTTP
# server's POST /webrtc/offer (token-gated) instead of the ingress' own /offer,
# and frames arrive with intact FrameEnvelope. The FastAPI app + shared ingress
# run in one asyncio loop; the offer is driven through the real ASGI stack so
# the token check and JSON contract are exercised end to end.
# ---------------------------------------------------------------------------
@skip_no_aiortc
def test_webrtc_offer_through_sessionhub_http_delivers_frames():
    httpx = pytest.importorskip("httpx")

    async def run():
        hub = sessionhub.SessionHub()
        ingress = gateway.AiortcIngress(
            host="127.0.0.1", port=8795, session_id="sim-session", max_frames=20
        )
        await ingress.start()
        app = sessionhub_http.create_app(hub, ingress=ingress, enable_signaling=True)

        received: list = []

        async def consume():
            async for frame_bgr, env in ingress:
                received.append((frame_bgr, env))

        consumer = asyncio.create_task(consume())

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://sessionhub.test"
        ) as http:
            # 1) create a session -> ephemeral token
            r = await http.post("/session/create", json={"device_id": "s25-int"})
            assert r.status_code == 200
            creds = r.json()

            # 2) unauthenticated offer is refused
            bad = await http.post(
                "/webrtc/offer",
                json={"sdp": "x", "type": "offer", "session_id": creds["session_id"], "token": "no"},
            )
            assert bad.status_code == 401

            # 3) drive a real aiortc offer through /webrtc/offer with the token
            from aiortc import RTCPeerConnection, RTCSessionDescription

            pc = RTCPeerConnection()
            channel = pc.createDataChannel("envelopes", ordered=True)
            pending: list = []

            track = fake._FakeCaptureTrack(
                session_id="sim-session",
                fps=30,
                frames=20,
                rotation=0,
                loss=0.0,
                source="fake_xr_device",
                mp4=None,
                poses=None,
                on_envelope=lambda env: (
                    channel.send(env.model_dump_json())
                    if channel.readyState == "open"
                    else pending.append(env)
                ),
            )

            @channel.on("open")
            def _flush() -> None:
                for env in pending:
                    channel.send(env.model_dump_json())
                pending.clear()

            pc.addTrack(track)
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
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
            assert answer["type"] == "answer"
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )

            # wait for frames then tear down
            while track._idx < track.total:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.4)
            await pc.close()

        try:
            await asyncio.wait_for(consumer, timeout=6)
        except asyncio.TimeoutError:
            consumer.cancel()
        await ingress.close()

        # frames arrived through the token-gated unified signaling, with envelopes
        assert len(received) >= 15
        _, first_env = received[0]
        assert first_env.frame_id.startswith("sim-session-frame-")
        assert len(first_env.pose.position) == 3
        m = ingress.matcher.stats()
        assert m["unmatched"] == 0

    asyncio.run(run())
