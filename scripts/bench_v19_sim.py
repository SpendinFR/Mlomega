from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib.util
from simulators.fake_xr_device import FakeXrDevice


def load_gateway():
    path = ROOT / "services" / "live-pc" / "gateway.py"
    spec = importlib.util.spec_from_file_location("v19_gateway_bench", path)
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
    ingress = gateway.AiortcIngress(FakeXrDevice(frames=frames, fps=0).stream())
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
        "limitations": "No XREAL/S25 camera, aiortc H.264 decode, GPU, or real LAN hardware in the Codex container.",
    }


def main() -> None:
    print(json.dumps(asyncio.run(main_async()), indent=2))


if __name__ == "__main__":
    main()
