from __future__ import annotations

"""LivePipeline — wires the gateway to VisionRT and AudioRT (E27 orchestration).

Video: the gateway (``AiortcIngress`` / ``LatestFrameQueue``, queue=1) feeds
:class:`VisionRT.process_frame` on every decoded frame. SceneDeltas and focus
UIIntents are pushed to the device over the DataChannel (``ingress.send_ui_intent``)
and mirrored to a callback for the future WorldBrain (E28).

Audio: raw audio chunks (from an aiortc AudioStreamTrack, or the test feeder)
flow into :class:`AudioRT.push_audio`; subtitle UIIntents go directly over the
same DataChannel (reflex path §3.2 — never through the BrainLive queue).

Degraded control: a :class:`DegradedStateMachine` turns GpuArbiter/heartbeat
signals into an action level, applied to VisionRT (detector floor / pause
changes / refuse VLM) and surfaced to the StatusBar. The tracker and subtitles
are never touched (handoff §3.6).

Metrics: :meth:`metrics` merges VisionRT + AudioRT + queue counters, exposed by
``create_metrics_app`` at ``/metrics``.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load sibling service modules robustly (package or importlib test loading).
_tracking = _load_sibling("v19_tracking", "tracking.py")
visionrt = _load_sibling("v19_visionrt", "visionrt.py")
audiort = _load_sibling("v19_audiort", "audiort.py")
degraded = _load_sibling("v19_degraded", "degraded.py")


def load_profile(profile_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(profile_path) if profile_path else _ROOT / "configs" / "profiles" / "rtx3070.yaml"
    if not path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


class LivePipeline:
    def __init__(
        self,
        *,
        session_id: str = "live",
        ingress: Any = None,
        arbiter: Any = None,
        profile_path: Path | str | None = None,
        detector_model: str | Path | None = None,
        keyframe_sink: Callable[[np.ndarray, Any], Any] | None = None,
        on_scene_delta: Callable[[dict[str, Any]], Any] | None = None,
        enable_detector: bool = True,
    ) -> None:
        self.session_id = session_id
        self.ingress = ingress
        self.arbiter = arbiter
        self.profile = load_profile(profile_path)
        vcfg = self.profile.get("vision", {}) if isinstance(self.profile, dict) else {}
        acfg = self.profile.get("audio", {}) if isinstance(self.profile, dict) else {}

        detector = None
        if enable_detector:
            model = detector_model or (vcfg.get("detector", {}) or {}).get("model_path") or (
                _ROOT / "models" / "yolox_nano.onnx"
            )
            try:
                if Path(model).exists():
                    dcfg = vcfg.get("detector", {}) or {}
                    detector = visionrt.YoloxDetector(
                        model,
                        input_size=int(dcfg.get("input_size", 416)),
                        score_threshold=float(dcfg.get("score_threshold", 0.30)),
                        nms_threshold=float(dcfg.get("nms_threshold", 0.45)),
                    )
            except Exception:
                detector = None
        self.detector_available = detector is not None

        cadence = visionrt.AdaptiveCadence(
            fps_min=float(vcfg.get("detector_fps_min", 5)),
            fps_max=float(vcfg.get("detector_fps_max", 15)),
            motion_low=float(vcfg.get("motion_low", 0.015)),
            motion_high=float(vcfg.get("motion_high", 0.06)),
        )
        keyframes = visionrt.KeyframeSelector(
            change_threshold=float(vcfg.get("keyframe_change_threshold", 0.35)),
            min_interval_s=float(vcfg.get("keyframe_min_interval_s", 3.0)),
        )
        self._external_scene_cb = on_scene_delta
        self.vision = visionrt.VisionRT(
            detector=detector,
            cadence=cadence,
            keyframes=keyframes,
            arbiter=arbiter,
            session_id=session_id,
            on_scene_delta=self._on_scene_delta,
            on_ui_intent=self._push_intent,
            keyframe_sink=keyframe_sink,
        )
        self.audio = audiort.AudioRT(
            session_id=session_id,
            target_language=str((acfg.get("asr", {}) or {}).get("target_language", "fr")),
            arbiter=arbiter,
            on_intent=self._push_intent,
        )
        self.degraded = degraded.DegradedStateMachine()
        self._status_cb: Callable[[dict[str, Any]], Any] | None = None
        self._last_action = degraded.ACTION_NOMINAL

    # ------------------------------------------------------------- push helpers
    def set_status_sink(self, cb: Callable[[dict[str, Any]], Any]) -> None:
        self._status_cb = cb

    def _push_intent(self, intent: dict[str, Any]) -> None:
        if self.ingress is not None and hasattr(self.ingress, "send_ui_intent"):
            try:
                self.ingress.send_ui_intent(json.dumps(intent))
            except Exception:
                pass

    def _on_scene_delta(self, delta: dict[str, Any]) -> None:
        # Push to device (DataChannel) and mirror to WorldBrain (E28) callback.
        if self.ingress is not None and hasattr(self.ingress, "send_ui_intent"):
            try:
                self.ingress.send_ui_intent(json.dumps({"type": "scene_delta", **delta}))
            except Exception:
                pass
        if self._external_scene_cb is not None:
            try:
                self._external_scene_cb(delta)
            except Exception:
                pass

    # ------------------------------------------------------------------ degraded
    def update_degraded(self, signals: Any) -> dict[str, Any]:
        state = self.degraded.evaluate(signals)
        if state.action_level != self._last_action:
            self._last_action = state.action_level
            self.vision.apply_action_level(state.action_level)
            if self._status_cb is not None:
                try:
                    self._status_cb(state.event())
                except Exception:
                    pass
        return state.event()

    # ------------------------------------------------------------------- feeders
    def on_video_frame(self, frame_bgr: np.ndarray, envelope: Any, *, focus_active: bool = False) -> dict[str, Any] | None:
        return self.vision.process_frame(frame_bgr, envelope, focus_active=focus_active)

    def on_audio_chunk(self, samples: np.ndarray, src_rate: int) -> list[dict[str, Any]]:
        return self.audio.push_audio(samples, src_rate)

    def on_focus_request(self, request: dict[str, Any], frame_bgr: np.ndarray, envelope: Any) -> dict[str, Any]:
        return self.vision.handle_focus(request, frame_bgr, envelope)

    async def run_video(self, *, limit: int | None = None) -> dict[str, Any]:
        """Consume the ingress and drive VisionRT until it stops."""
        assert self.ingress is not None, "ingress required for run_video"
        count = 0
        async for frame_bgr, envelope in self.ingress:
            self.on_video_frame(frame_bgr, envelope)
            count += 1
            if limit is not None and count >= limit:
                break
        return self.metrics()

    # ------------------------------------------------------------------- metrics
    def metrics(self) -> dict[str, Any]:
        m: dict[str, Any] = {"session_id": self.session_id, "action_level": self._last_action}
        m.update(self.vision.metrics.snapshot())
        m.update({f"audio_{k}": v for k, v in self.audio.metrics.snapshot().items()})
        if self.ingress is not None and hasattr(self.ingress, "matcher"):
            try:
                m["envelope_match"] = self.ingress.matcher.stats()
            except Exception:
                pass
        return m


def create_metrics_app(pipeline: LivePipeline):
    """A tiny FastAPI app exposing GET /metrics for the pipeline (E27 §3)."""
    from fastapi import FastAPI

    app = FastAPI(title="MLOmega V19 LivePipeline metrics")

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        return pipeline.metrics()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "detector": pipeline.detector_available,
            "asr": pipeline.audio.transcriber.available,
        }

    return app
