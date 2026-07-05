from __future__ import annotations

"""Fake XR device simulator for the V19 live transport.

Two roles live here:

* :class:`FakeXrDevice` -- an in-memory async iterator of
  ``(frame_dict, FrameEnvelope)`` used by unit tests and the offline bench. It
  does *not* touch the network. Kept stable for the existing transport tests.

* :class:`FakeXrWebrtcClient` -- a *real* aiortc client that (a) reads an MP4
  (via PyAV/OpenCV) or synthesizes a moving pattern, (b) reads a pose JSONL (or
  synthesizes a simple trajectory), (c) streams H.264 video to the gateway over
  WebRTC on localhost, and (d) sends the matching :class:`FrameEnvelope` metadata
  over a reliable DataChannel just before each frame.

CLI options: ``--mp4``, ``--pose-jsonl``, ``--fps``, ``--frames``, ``--loss``
(simulated frame drop probability), ``--rotate90`` (capture-only mode),
``--offer-url`` (gateway signaling endpoint).
"""

import argparse
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np

import sys

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from packages.contracts.python.models import FrameEnvelope, Pose

try:
    import cv2

    CV2_AVAILABLE = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    CV2_AVAILABLE = False

try:
    import av  # noqa: F401
    from aiortc import (
        RTCPeerConnection,
        RTCSessionDescription,
        VideoStreamTrack,
    )
    from aiortc.mediastreams import MediaStreamError
    from av import VideoFrame

    AIORTC_AVAILABLE = True
except Exception:  # pragma: no cover
    VideoStreamTrack = object  # type: ignore[assignment,misc]
    RTCPeerConnection = None  # type: ignore[assignment]
    RTCSessionDescription = None  # type: ignore[assignment]
    MediaStreamError = Exception  # type: ignore[assignment]
    VideoFrame = None  # type: ignore[assignment]
    AIORTC_AVAILABLE = False


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeXrDevice:
    """In-memory frame/envelope generator (no network). Stable test surface."""

    def __init__(
        self,
        *,
        session_id: str = "sim-session",
        fps: float = 30.0,
        frames: int = 90,
        rotation: int = 0,
        source: str = "fake_xr_device",
    ) -> None:
        self.session_id = session_id
        self.fps = fps
        self.frames = frames
        self.rotation = rotation
        self.source = source

    async def stream(self) -> AsyncIterator[tuple[dict[str, Any], FrameEnvelope]]:
        delay = 1.0 / self.fps if self.fps > 0 else 0
        for idx in range(self.frames):
            envelope = FrameEnvelope(
                session_id=self.session_id,
                frame_id=f"{self.session_id}-frame-{idx:06d}",
                capture_monotonic_ns=time.monotonic_ns(),
                captured_at_utc=_utc(),
                pose=Pose(position=[0.0, 0.0, 0.0], rotation=[0.0, 0.0, 0.0, 1.0]),
                rotation=self.rotation,
                source=self.source,
            )
            yield {"width": 1280, "height": 720, "format": "bgr24", "index": idx}, envelope
            if delay:
                await asyncio.sleep(delay)


def write_pose_jsonl(path: Path, envelopes: list[FrameEnvelope]) -> None:
    path.write_text("\n".join(e.model_dump_json() for e in envelopes) + "\n", encoding="utf-8")


def _synthetic_frame(idx: int, width: int, height: int) -> np.ndarray:
    """Deterministic moving-pattern BGR frame with a bright roaming square."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    # gradient background so H.264 has real structure to compress
    x = np.linspace(0, 255, width, dtype=np.uint8)
    frame[:, :, 0] = x  # blue ramp
    frame[:, :, 1] = (idx * 3) % 256  # animated green
    # moving square
    box = 80
    cx = int((np.sin(idx / 12.0) * 0.5 + 0.5) * (width - box))
    cy = int((np.cos(idx / 9.0) * 0.5 + 0.5) * (height - box))
    frame[cy : cy + box, cx : cx + box] = (0, 0, 255)
    if CV2_AVAILABLE:
        cv2.putText(
            frame,
            f"f{idx}",
            (20, 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (255, 255, 255),
            3,
        )
    return frame


def _rotate90(frame: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(np.rot90(frame))


def _pose_for(idx: int) -> Pose:
    t = idx / 30.0
    return Pose(
        position=[float(np.sin(t)), 0.0, float(np.cos(t))],
        rotation=[0.0, 0.0, 0.0, 1.0],
    )


def load_pose_jsonl(path: Path) -> list[Pose]:
    poses: list[Pose] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if "pose" in obj:
            poses.append(Pose.model_validate(obj["pose"]))
        else:
            poses.append(Pose.model_validate(obj))
    return poses


def _frames_from_mp4(path: Path, limit: int | None) -> list[np.ndarray]:
    if not CV2_AVAILABLE:
        raise RuntimeError("opencv-python-headless is required to read MP4 files")
    cap = cv2.VideoCapture(str(path))
    frames: list[np.ndarray] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(frame)
            if limit is not None and len(frames) >= limit:
                break
    finally:
        cap.release()
    return frames


class _FakeCaptureTrack(VideoStreamTrack):  # type: ignore[misc]
    """aiortc video track fed by MP4 or synthetic frames.

    For each frame it (1) pushes the matching ``FrameEnvelope`` onto ``channel``
    (when open) and (2) returns the ``VideoFrame`` to aiortc for H.264 encoding.
    Frames can be probabilistically dropped (``loss``) to emulate a lossy link.
    """

    def __init__(
        self,
        *,
        session_id: str,
        fps: float,
        frames: int,
        rotation: int,
        loss: float,
        source: str,
        mp4: Path | None,
        poses: list[Pose] | None,
        width: int = 1280,
        height: int = 720,
        on_envelope: Any = None,
    ) -> None:
        super().__init__()
        self.session_id = session_id
        self.fps = fps if fps > 0 else 30.0
        self.total = frames
        self.rotation = rotation
        self.loss = loss
        self.source = source
        self.poses = poses
        self.width = width
        self.height = height
        self._on_envelope = on_envelope
        self._idx = 0
        self._time_base = Fraction(1, int(self.fps))
        self._mp4_frames: list[np.ndarray] | None = None
        if mp4 is not None:
            self._mp4_frames = _frames_from_mp4(mp4, frames)
            if self._mp4_frames:
                self.height, self.width = self._mp4_frames[0].shape[:2]

    def _bgr_for(self, idx: int) -> np.ndarray:
        if self._mp4_frames:
            frame = self._mp4_frames[idx % len(self._mp4_frames)]
        else:
            frame = _synthetic_frame(idx, self.width, self.height)
        if self.rotation == 90:
            frame = _rotate90(frame)
        return frame

    async def recv(self) -> Any:  # noqa: ANN401
        if self._idx >= self.total:
            raise MediaStreamError
        idx = self._idx
        self._idx += 1

        pose = (
            self.poses[idx] if self.poses and idx < len(self.poses) else _pose_for(idx)
        )
        envelope = FrameEnvelope(
            session_id=self.session_id,
            frame_id=f"{self.session_id}-frame-{idx:06d}",
            capture_monotonic_ns=time.monotonic_ns(),
            captured_at_utc=_utc(),
            pose=pose,
            rotation=self.rotation,
            source=self.source,
        )
        # Send metadata BEFORE the frame so the gateway can associate it.
        if self._on_envelope is not None and random.random() >= self.loss:
            self._on_envelope(envelope)

        bgr = self._bgr_for(idx)
        video_frame = VideoFrame.from_ndarray(bgr, format="bgr24")
        video_frame.pts = idx
        video_frame.time_base = self._time_base
        # Pace the producer to the requested fps.
        await asyncio.sleep(1.0 / self.fps)
        return video_frame


class FakeXrWebrtcClient:
    """Real aiortc client that streams a fake XR feed to the gateway."""

    def __init__(
        self,
        *,
        offer_url: str,
        session_id: str = "sim-session",
        fps: float = 30.0,
        frames: int = 90,
        rotation: int = 0,
        loss: float = 0.0,
        source: str = "fake_xr_device",
        mp4: Path | None = None,
        pose_jsonl: Path | None = None,
        token: str | None = None,
    ) -> None:
        if not AIORTC_AVAILABLE:
            raise RuntimeError("aiortc/av are not installed; FakeXrWebrtcClient is unavailable")
        self.offer_url = offer_url
        self.session_id = session_id
        self.fps = fps
        self.frames = frames
        self.rotation = rotation
        self.loss = loss
        self.source = source
        self.mp4 = mp4
        self.poses = load_pose_jsonl(pose_jsonl) if pose_jsonl else None
        # When ``token`` is set, the client targets the unified E24 signaling
        # endpoint ``POST /webrtc/offer`` and includes ``{session_id, token}`` in
        # the offer body (same surface the Android LiveTransportPlugin uses). When
        # None it POSTs the bare ``{sdp, type}`` the ingress' own /offer expects.
        self.token = token
        self.sent_envelopes = 0

    async def run(self) -> dict[str, Any]:
        import aiohttp

        pc = RTCPeerConnection()
        channel = pc.createDataChannel("envelopes", ordered=True)
        done = asyncio.Event()

        pending: list[FrameEnvelope] = []

        def _on_envelope(env: FrameEnvelope) -> None:
            self.sent_envelopes += 1
            if channel.readyState == "open":
                channel.send(env.model_dump_json())
            else:
                pending.append(env)

        @channel.on("open")
        def _flush() -> None:
            for env in pending:
                channel.send(env.model_dump_json())
            pending.clear()

        track = _FakeCaptureTrack(
            session_id=self.session_id,
            fps=self.fps,
            frames=self.frames,
            rotation=self.rotation,
            loss=self.loss,
            source=self.source,
            mp4=self.mp4,
            poses=self.poses,
            on_envelope=_on_envelope,
        )
        pc.addTrack(track)

        @pc.on("connectionstatechange")
        async def _state() -> None:
            if pc.connectionState in {"failed", "closed", "disconnected"}:
                done.set()

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        offer_body: dict[str, Any] = {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }
        if self.token is not None:
            # Unified /webrtc/offer endpoint requires the session token.
            offer_body["session_id"] = self.session_id
            offer_body["token"] = self.token
        async with aiohttp.ClientSession() as session:
            async with session.post(self.offer_url, json=offer_body) as resp:
                answer = await resp.json()
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )

        # Wait until all frames have been produced (track exhausts).
        while track._idx < track.total:
            await asyncio.sleep(0.02)
        # Give the transport a moment to flush the tail.
        await asyncio.sleep(0.3)
        await pc.close()
        return {"frames_sent": track._idx, "envelopes_sent": self.sent_envelopes}


def resolve_offer_url(
    endpoints: Any,
    *,
    probe: Any = None,
    timeout_s: float = 2.0,
) -> tuple[str | None, dict[str, Any]]:
    """E36 §1: pick the first reachable PC endpoint and return its /webrtc/offer URL.

    ``endpoints`` is the ordered list (config shape accepted by
    ``endpoint_resolver.parse_endpoints``: a list of {name,host,port} or a bare
    host). Returns ``(offer_url_or_None, resolve_result_dict)``. When no endpoint
    answers, ``offer_url`` is None and the result carries ``pc_unreachable`` — the
    caller stays in the device-only reflex mode (honest degrade)."""
    import importlib.util as _iu

    spec = _iu.spec_from_file_location(
        "v19_endpoint_resolver",
        Path(__file__).resolve().parents[1] / "services" / "live-pc" / "endpoint_resolver.py",
    )
    assert spec and spec.loader
    er = _iu.module_from_spec(spec)
    spec.loader.exec_module(er)
    eps = er.parse_endpoints(endpoints)
    resolver = er.EndpointResolver(eps, probe=probe, timeout_s=timeout_s)
    result = resolver.resolve()
    url = result.active.webrtc_offer_url if result.active else None
    return url, result.to_dict()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Fake XR device (WebRTC or in-memory)")
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], default=0)
    parser.add_argument("--rotate90", action="store_true", help="capture-only rotate 90")
    parser.add_argument("--loss", type=float, default=0.0, help="metadata drop probability 0..1")
    parser.add_argument("--mp4", type=Path, help="MP4 to replay (else synthetic)")
    parser.add_argument("--pose-jsonl", type=Path, help="pose JSONL (else synthetic)")
    parser.add_argument("--jsonl", type=Path, help="write generated envelopes to JSONL (in-memory mode)")
    parser.add_argument("--offer-url", type=str, help="gateway /offer or /webrtc/offer URL -> real WebRTC mode")
    parser.add_argument(
        "--endpoints", type=str,
        help="E36: ordered comma-separated host[:port] list (LAN first, then tunnel). "
             "The first reachable /health wins; overrides --offer-url when set.",
    )
    parser.add_argument("--session-id", type=str, default="sim-session")
    parser.add_argument("--token", type=str, help="session token for the unified /webrtc/offer endpoint")
    args = parser.parse_args()

    rotation = 90 if args.rotate90 else args.rotation

    # E36 §1: an ordered endpoint list is resolved (LAN → tunnel) with a health
    # probe; the winning endpoint's /webrtc/offer URL replaces --offer-url.
    if args.endpoints:
        eps = []
        for i, item in enumerate(args.endpoints.split(",")):
            item = item.strip()
            if not item:
                continue
            host, _, port = item.partition(":")
            eps.append({"name": ("lan" if i == 0 else f"endpoint{i + 1}"),
                        "host": host, "port": int(port) if port else 8710})
        url, result = resolve_offer_url(eps)
        print(json.dumps({"resolve": result}))
        if url is None:
            print(json.dumps({"status": "pc_unreachable", "reflex_only": True}))
            return
        args.offer_url = url

    if args.offer_url:
        client = FakeXrWebrtcClient(
            offer_url=args.offer_url,
            session_id=args.session_id,
            fps=args.fps,
            frames=args.frames,
            rotation=rotation,
            loss=args.loss,
            source="fake_xr_device",
            mp4=args.mp4,
            pose_jsonl=args.pose_jsonl,
            token=args.token,
        )
        result = await client.run()
        print(json.dumps(result))
        return

    device = FakeXrDevice(
        session_id=args.session_id, frames=args.frames, fps=args.fps, rotation=rotation
    )
    envelopes: list[FrameEnvelope] = []
    async for _frame, envelope in device.stream():
        envelopes.append(envelope)
        print(json.dumps({"frame_id": envelope.frame_id, "rotation": envelope.rotation}))
    if args.jsonl:
        write_pose_jsonl(args.jsonl, envelopes)


if __name__ == "__main__":
    asyncio.run(_main())
