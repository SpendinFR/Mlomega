from __future__ import annotations

"""V19 transport bench.

Two modes:

* ``--sim`` (default): in-memory ``IterableIngress`` queue bench (no transport).
  Cheap sanity check of the queue=1 / drop path.
* ``--webrtc``: real aiortc loopback bench. Streams 720p H.264 over localhost
  from the fake XR device to :class:`AiortcIngress`, and reports:
    - decode/convert P50/P95 (BGR conversion of the H.264-decoded PyAV frame),
    - recv availability P50/P95 (decode + jitter buffer + convert, inter-frame),
    - end-to-end capture->available P50/P95 (sender monotonic capture ->
      frame ready in the queue), measured with a shared monotonic clock (valid
      because sender and receiver share one process/clock on localhost).
"""

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simulators.fake_xr_device import FakeXrDevice  # noqa: E402


def load_gateway():
    path = ROOT / "services" / "live-pc" / "gateway.py"
    spec = importlib.util.spec_from_file_location("v19_gateway_bench", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_fake():
    path = ROOT / "simulators" / "fake_xr_device.py"
    spec = importlib.util.spec_from_file_location("v19_fake_bench", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


async def main_async(frames: int = 120) -> dict[str, object]:
    gateway = load_gateway()
    q = gateway.LatestFrameQueue()
    ingress = gateway.IterableIngress(FakeXrDevice(frames=frames, fps=0).stream())
    samples_ms: list[float] = []
    async for frame, envelope in ingress:
        start = time.perf_counter_ns()
        q.put_latest((frame, envelope))
        samples_ms.append((time.perf_counter_ns() - start) / 1_000_000.0)
    stats = q.stats()
    return {
        "mode": "simulated_python_ingress_queue_only",
        "frames": frames,
        "p50_ms": round(statistics.median(samples_ms), 6),
        "p95_ms": round(percentile(samples_ms, 95), 6),
        "dropped_frames": stats["dropped_frames"],
        "queue_size": stats["queue_size"],
        "limitations": "In-memory queue path only; no aiortc H.264 decode. Use --webrtc for the real decode bench.",
    }


async def webrtc_async(frames: int = 300, fps: float = 30.0, port: int = 8795) -> dict[str, object]:
    gateway = load_gateway()
    fake = load_fake()
    if not (gateway.AIORTC_AVAILABLE and fake.AIORTC_AVAILABLE):
        return {"mode": "webrtc_local_loopback", "error": "aiortc/av not installed"}

    ingress = gateway.AiortcIngress(
        host="127.0.0.1", port=port, session_id="bench-session", max_frames=frames
    )
    await ingress.start()
    q = gateway.LatestFrameQueue()
    e2e_ms: list[float] = []

    async def consume():
        async for frame_bgr, env in ingress:
            now = time.monotonic_ns()
            e2e_ms.append((now - env.capture_monotonic_ns) / 1_000_000.0)
            q.put_latest((frame_bgr, env))

    consumer = asyncio.create_task(consume())
    # 720p synthetic frames (no MP4 needed to hit 720p at any frame count).
    client = fake.FakeXrWebrtcClient(
        offer_url=ingress.offer_url,
        session_id="bench-session",
        fps=fps,
        frames=frames,
        loss=0.0,
        mp4=None,
        pose_jsonl=None,
    )
    result = await client.run()
    try:
        await asyncio.wait_for(consumer, timeout=15)
    except asyncio.TimeoutError:
        consumer.cancel()
    decode = ingress.bench.summary()
    recv = ingress.recv_bench.summary()
    await ingress.close()
    stats = q.stats()
    return {
        "mode": "webrtc_local_loopback",
        "config": {"resolution": "1280x720", "codec": "H.264 (aiortc/PyAV, CPU)", "fps": fps, "transport": "localhost"},
        "frames_sent": result["frames_sent"],
        "frames_received": stats["received_frames"],
        "decode_convert_p50_ms": round(float(decode["p50_ms"]), 4),
        "decode_convert_p95_ms": round(float(decode["p95_ms"]), 4),
        "recv_available_p50_ms": round(float(recv["p50_ms"]), 4),
        "recv_available_p95_ms": round(float(recv["p95_ms"]), 4),
        "e2e_capture_to_available_p50_ms": round(percentile(e2e_ms, 50), 4),
        "e2e_capture_to_available_p95_ms": round(percentile(e2e_ms, 95), 4),
        "dropped_frames": stats["dropped_frames"],
        "queue_size": stats["queue_size"],
        "matcher": ingress.matcher.stats(),
        "note": "P95 decode threshold <33ms must be re-measured on real LAN hardware; localhost has no network jitter.",
    }


def load_visionrt():
    import importlib.util

    # tracking must be importable by name before visionrt loads it.
    for name, rel in (("v19_tracking", "services/live-pc/tracking.py"),
                      ("v19_visionrt", "services/live-pc/visionrt.py")):
        path = ROOT / rel
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules["v19_visionrt"]


def vision_bench(frames: int = 200) -> dict[str, object]:
    """Real detector/tracker bench on this machine (E27 §5).

    Feeds synthetic 720p frames through the full VisionRT frame path and reports
    the real ``vision_infer_ms`` P50/P95 plus effective detector/tracker fps.
    """
    visionrt = load_visionrt()
    model = ROOT / "models" / "yolox_nano.onnx"
    if not model.exists():
        return {"mode": "vision", "error": "models/yolox_nano.onnx missing (run fetch_models_v19.py)"}
    import numpy as np

    det = visionrt.YoloxDetector(str(model))
    vr = visionrt.VisionRT(detector=det, session_id="bench")
    vr.keyframes.change_threshold = 2.0  # disable keyframe writes for the bench

    class _Env:
        def __init__(self, i):
            self.frame_id = f"bench-{i}"
            self.captured_at_utc = None

    rng = np.random.default_rng(0)
    base = (rng.uniform(0, 255, (720, 1280, 3))).astype(np.uint8)
    t_feed = 1.0 / 30.0  # 30 fps decoded feed
    t = 0.0
    wall0 = time.perf_counter()
    for i in range(frames):
        # Moving scene: shift horizontally so the detector stays busy.
        frame = np.roll(base, (i * 12) % 1280, axis=1)
        vr.process_frame(frame, _Env(i), now=t)
        t += t_feed
    wall = time.perf_counter() - wall0
    snap = vr.metrics.snapshot()
    detector_fps = snap["detector_frames"] / wall if wall else 0.0
    tracker_fps = snap["tracker_frames"] / wall if wall else 0.0
    return {
        "mode": "vision",
        "device": "cuda" if det.on_gpu else "cpu",
        "providers": det.providers,
        "model": model.name,
        "frames_fed": frames,
        "vision_infer_ms_p50": round(float(snap["vision_infer_ms_p50"]), 3),
        "vision_infer_ms_p95": round(float(snap["vision_infer_ms_p95"]), 3),
        "detector_frames": snap["detector_frames"],
        "tracker_frames": snap["tracker_frames"],
        "scene_deltas": snap["scene_delta_rate"],
        "wall_s": round(wall, 3),
        "effective_detector_fps": round(detector_fps, 2),
        "effective_tracker_fps": round(tracker_fps, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webrtc", action="store_true", help="run real aiortc loopback bench")
    parser.add_argument("--sim", action="store_true", help="run in-memory queue bench (default)")
    parser.add_argument("--vision", action="store_true", help="run real detector/tracker bench")
    parser.add_argument("--frames", type=int, default=None)
    parser.add_argument("--fps", type=float, default=30.0)
    args = parser.parse_args()

    if args.vision:
        frames = args.frames if args.frames is not None else 200
        print(json.dumps(vision_bench(frames=frames), indent=2))
    elif args.webrtc:
        frames = args.frames if args.frames is not None else 300
        print(json.dumps(asyncio.run(webrtc_async(frames=frames, fps=args.fps)), indent=2))
    else:
        frames = args.frames if args.frames is not None else 120
        print(json.dumps(asyncio.run(main_async(frames=frames)), indent=2))


if __name__ == "__main__":
    main()
