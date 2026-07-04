"""E27 integration: fake_xr_device -> LivePipeline -> SceneDelta + focus reply.

Runs entirely on the PC side (no Android/hardware):

  1. the fake XR device streams frames + FrameEnvelope over the unified
     token-gated ``POST /webrtc/offer`` signaling into ``AiortcIngress``;
  2. ``LivePipeline`` drives ``VisionRT`` on the decoded frames and pushes
     ``scene_delta`` messages back over the DataChannel; the client asserts they
     carry a coherent ``source_frame_id`` matching a frame it sent;
  3. a ``what_is`` focus request is handled with Ollama OFF -> the real degraded
     VLM path (``status: vlm_unavailable`` / honest inferred truth level).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.vision

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "models" / "yolox_nano.onnx"
FIXTURE = ROOT / "tests" / "v19" / "fixtures" / "people.jpg"

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
live_pipeline = _load("v19_live_pipeline", "services/live-pc/live_pipeline.py")

cv2 = pytest.importorskip("cv2")
aiortc_missing = not (gateway.AIORTC_AVAILABLE and fake.AIORTC_AVAILABLE)
skip = pytest.mark.skipif(
    aiortc_missing or not MODEL.exists(), reason="aiortc/av or yolox model missing"
)

from packages.contracts.python.models import FrameEnvelope, Pose, SceneDelta, UIIntent  # noqa: E402


def _envelope(fid: str) -> FrameEnvelope:
    return FrameEnvelope(
        session_id="e27",
        frame_id=fid,
        capture_monotonic_ns=1,
        captured_at_utc=datetime.now(timezone.utc).isoformat(),
        pose=Pose(position=[0, 0, 0], rotation=[0, 0, 0, 1]),
        source="test",
    )


@pytest.mark.skipif(not MODEL.exists(), reason="yolox model missing")
def test_focus_what_is_degrades_when_ollama_absent(monkeypatch):
    """VLM path with Ollama unreachable -> honest degraded UIIntent (no block)."""
    monkeypatch.setenv("MLOMEGA_VLM_MODEL", "moondream")
    pipe = live_pipeline.LivePipeline(session_id="e27", detector_model=str(MODEL))
    # Point the VLM at a dead port so the degraded path is exercised for real.
    pipe.vision.vlm.base_url = "http://127.0.0.1:1"
    pipe.vision.vlm.timeout_s = 0.5
    img = cv2.imread(str(FIXTURE))
    # Crop a blank region so the detector finds nothing -> VLM fallback fires.
    blank = np.full((80, 80, 3), 127, dtype=np.uint8)
    h, w = img.shape[:2]
    img[0:80, 0:80] = blank
    intent = pipe.on_focus_request(
        {"kind": "what_is", "bbox": [0, 0, 80, 80], "track_id": "t1"}, img, _envelope("f1")
    )
    UIIntent.model_validate(intent)
    assert intent["producer"] == "visionrt"
    # Detector found nothing in the blank crop, VLM unreachable -> honest degrade.
    assert intent["content"].get("status") in {"vlm_unavailable", "vlm_busy"}
    assert intent["truth_level"] == "inferred"  # never presented as observation
    assert intent["confidence"] == 0.0


@skip
def test_pipeline_scene_delta_roundtrip_with_source_frame_id():
    httpx = pytest.importorskip("httpx")

    async def run():
        hub = sessionhub.SessionHub()
        ingress = gateway.AiortcIngress(
            host="127.0.0.1", port=8797, session_id="e27-session", max_frames=40
        )
        await ingress.start()

        pipe = live_pipeline.LivePipeline(
            session_id="e27-session", ingress=ingress, detector_model=str(MODEL)
        )
        # Force keyframes off so this test stays about SceneDelta.
        pipe.vision.keyframes.change_threshold = 2.0

        app = sessionhub_http.create_app(hub, ingress=ingress, enable_signaling=True)

        sent_frame_ids: list[str] = []

        async def drive():
            async for frame_bgr, env in ingress:
                sent_frame_ids.append(env.frame_id)
                pipe.on_video_frame(frame_bgr, env)

        driver = asyncio.create_task(drive())

        from aiortc import RTCPeerConnection, RTCSessionDescription

        pc = RTCPeerConnection()
        channel = pc.createDataChannel("contracts", ordered=True)
        pending: list = []
        scene_deltas: list = []

        def _emit(env) -> None:
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
            payload = json.loads(message)
            if payload.get("type") == "scene_delta":
                scene_deltas.append(payload)

        track = fake._FakeCaptureTrack(
            session_id="e27-session",
            fps=30,
            frames=40,
            rotation=0,
            loss=0.0,
            source="fake_xr_device",
            mp4=None,
            poses=None,
            on_envelope=_emit,
        )
        pc.addTrack(track)
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sh.test") as http:
            creds = (await http.post("/session/create", json={"device_id": "s25-e27"})).json()
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
            for _ in range(300):
                if len(scene_deltas) >= 2:
                    break
                await asyncio.sleep(0.02)
            while track._idx < track.total:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            await pc.close()

        try:
            await asyncio.wait_for(driver, timeout=6)
        except asyncio.TimeoutError:
            driver.cancel()
        await ingress.close()
        return scene_deltas, sent_frame_ids, pipe.metrics()

    scene_deltas, sent_frame_ids, metrics = asyncio.run(run())

    assert scene_deltas, "no SceneDelta pushed to the device"
    assert sent_frame_ids
    for sd_msg in scene_deltas:
        sd = SceneDelta.model_validate({k: v for k, v in sd_msg.items() if k != "type"})
        # source_frame_id must be one the device actually sent (coherent binding).
        assert sd.source_frame_id in set(sent_frame_ids), sd.source_frame_id
    assert metrics["detector_frames"] >= 1
    assert metrics["tracker_frames"] >= metrics["detector_frames"]
