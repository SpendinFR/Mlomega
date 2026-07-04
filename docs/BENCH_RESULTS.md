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

## E27 — VisionRT + AudioRT (mesures réelles, machine cible RTX 3070)

### Vision — détecteur YOLOX-nano ONNX + tracker ByteTrack

Commande : `python scripts/bench_v19_sim.py --vision --frames 300` (ou `BENCH_V19.ps1 -Vision`). Scène 720p synthétique en mouvement, cadence adaptative active.

```json
{
  "mode": "vision", "device": "cpu", "providers": ["CPUExecutionProvider"],
  "model": "yolox_nano.onnx", "frames_fed": 300,
  "vision_infer_ms_p50": 9.87, "vision_infer_ms_p95": 10.48,
  "detector_frames": 106, "tracker_frames": 300, "scene_deltas": 106,
  "wall_s": 2.13, "effective_detector_fps": 49.8, "effective_tracker_fps": 140.8
}
```

Interprétation :

- **`vision_infer_ms` réel : P50 9,9 ms / P95 10,5 ms** (YOLOX-nano 416, ONNX Runtime **CPU** sur cette machine). Très en dessous du budget par frame (33 ms à 30 fps) : une seule inférence détecteur tient largement dans un intervalle de frame, marge pour l'OCR/VLM ciblés.
- **Cadence adaptative vérifiée** : le détecteur n'a tourné que sur 106/300 frames (le reste interpolé par le tracker), piloté par le score de mouvement inter-frames — c'est le contrat §3.6 (« pas chaque frame dans le détecteur »).
- Le tracker tourne sur les 300 frames (interpolation Kalman entre passes détecteur), débit brut ~141 fps — jamais le goulot.
- **ONNX Runtime GPU** : le code sélectionne `CUDAExecutionProvider` automatiquement s'il est disponible. Sur cette machine, `onnxruntime` installé est la build CPU (les tests V18 en dépendent) ; une tentative d'`onnxruntime-gpu` en venv isolé a chargé le provider CUDA mais est retombée en CPU faute de cuDNN 9 apparié (friction packaging Windows connue). Le budget CPU étant déjà tenu, le détecteur reste en CPU ici ; le chemin GPU est prêt sans changement de code dès que l'environnement fournit les DLL CUDA/cuDNN.

### Audio — faster-whisper small int8 + Argos Translate

faster-whisper tourne réellement sur la **RTX 3070** (`device=cuda`, via CTranslate2 CUDA déjà présent) :

| Fixture | Langue détectée | ASR (segment complet) | Traduction |
|---|---|---|---|
| speech_en.wav (« The quick brown fox… ») | en | ~380 ms | — (cible fr, LID variable sur voix SAPI) |
| speech_fr.wav (« Bonjour, je m'appelle Marie… ») | fr | ~200 ms | fr→en Argos : « Hello, my name is Marie and I live in Paris near the river. » |

Interprétation :

- ASR par segment VAD ~200-380 ms sur GPU pour whisper `small` → bien sous le budget sous-titre partiel < 1 s (§3.2). Le partiel est émis dès la transcription, le final ajoute la traduction.
- **Traduction locale sans LLM** (Argos / CTranslate2, MIT) vérifiée fr→en de bout en bout. Paires en↔fr installées ; zh→fr absent de l'index Argos (noté au manifest, dégradation honnête `no_pack`).
- Chemin sous-titres = réflexe direct DataChannel (`producer=ultralive`), jamais la queue BrainLive.
