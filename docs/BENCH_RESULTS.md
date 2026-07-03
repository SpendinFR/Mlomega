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

## Lot 1 — bench WebRTC réel, machine cible (2026-07-04)

Commande : `python scripts/bench_v19_sim.py --webrtc` — boucle locale aiortc complète (fake_xr_device H.264 → AiortcIngress), machine Windows + RTX 3070, décodage CPU PyAV.

```json
{
  "config": {"resolution": "1280x720", "codec": "H.264 (aiortc/PyAV, CPU)", "fps": 30.0, "transport": "localhost"},
  "frames_sent": 300, "frames_received": 299,
  "decode_convert_p50_ms": 0.5875, "decode_convert_p95_ms": 0.8081,
  "recv_available_p50_ms": 31.89, "recv_available_p95_ms": 62.63,
  "e2e_capture_to_available_p50_ms": 78.0, "e2e_capture_to_available_p95_ms": 125.0,
  "dropped_frames": 298, "queue_size": 1,
  "matcher": {"matched": 0, "fallback_nearest": 299, "unmatched": 0}
}
```

Interprétation :

- **Critère de fin E10 tenu** : P95 décodage 0,81 ms ≪ 33 ms à 720p30. La piste GStreamer/nvh264dec (plan B, même interface `VideoIngress`) n'est pas nécessaire à cette résolution ; à re-vérifier si l'on monte en 1080p.
- Le e2e capture→disponible (P50 78 ms / P95 125 ms) inclut l'encodage, le pacing 30 fps et le jitter buffer aiortc en localhost — budget compatible avec le chemin VisionRT (< 3 s) ; le chemin réflexe reste sur l'appareil et n'emprunte jamais ce trajet.
- Limite connue : l'association FrameEnvelope↔frame se fait aujourd'hui par timestamp le plus proche (0 correspondance exacte par frame_id sur 299 frames) — à resserrer côté S25 réel (horodatage RTP commun) au Lot 3.
- Reste à mesurer en conditions réelles : LAN Wi-Fi 6 + S25 (jitter réseau réel), et sessions longues (dérive/refragmentation) — gates matériels du Lot 3.
