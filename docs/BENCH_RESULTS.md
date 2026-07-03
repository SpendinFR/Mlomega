# BENCH_RESULTS — MLOmega V19

## Lot 1 — simulated ingress checkpoint (Codex container, 2026-07-03)

Command executed:

```bash
python scripts/bench_v19_sim.py
```

Result:

```json
{
  "mode": "simulated_python_ingress_queue_only",
  "frames": 120,
  "p50_ms": 0.00126,
  "p95_ms": 0.002977,
  "dropped_frames": 119,
  "queue_size": 1,
  "limitations": "No XREAL/S25 camera, aiortc H.264 decode, GPU, or real LAN hardware in the Codex container."
}
```

Interpretation:

- This is a simulator-only Python ingress/queue bench of the V19 `LatestFrameQueue` path, not a hardware or media-decoder benchmark.
- It validates the Lot 1 queue=1/drop-old-frames behavior in simulation: 120 input frames leave one latest frame in the queue and account for 119 dropped stale frames.
- No real XREAL/S25, LAN, aiortc H.264 decode, PyAV/FFmpeg decode, GPU, VRAM, or camera latency is claimed from this container run.
- The mandatory real 720p30 decode P50/P95 bench remains a later hardware gate and must be captured on the target machine/device before any hardware performance claim.
