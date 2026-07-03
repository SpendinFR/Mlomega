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
