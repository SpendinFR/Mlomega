from __future__ import annotations

"""VisionRT — the PC live-vision pipeline (handoff §3.6).

One frame source (the gateway's latest-frame queue), many consumers at their own
cadence — never "every frame through everything":

* :class:`YoloxDetector` — YOLOX-nano ONNX (Apache-2.0), ONNX Runtime on GPU if a
  CUDA provider is present else CPU. Run at an *adaptive* 5-15 fps driven by an
  inter-frame motion score + focus demand (bounds from the profile).
* :class:`tracking.ByteTracker` — runs on every decoded frame, interpolating
  between detector passes so track ids stay stable and short.
* :class:`OcrRoi` — rapidocr_onnxruntime on a crop only, on demand.
* :class:`VlmCrop` — one-job-at-a-time Ollama VLM on a crop, preemptible, honest
  degraded reply when Ollama is unreachable.
* :class:`KeyframeSelector` — scene-change score → records keyframes via the
  existing ``v19_keyframes.register_xr_keyframe`` (the E14 bridge to the night
  chain).
* :class:`VisionRT` — orchestrates the above, emits :class:`SceneDelta` bound to
  ``source_frame_id`` and handles focus (``what_is``/``find``/``ocr``) requests
  → :class:`UIIntent` replies with the correct truth level (§17.2).

Admission of GPU work goes through :class:`GpuArbiter` (``detector``/``ocr``/
``vlm`` classes). Nothing here blocks the pipeline: a denied/absent model
degrades honestly.
"""

import base64
import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

# COCO-80 class names (YOLOX output order).
COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)


def _load_tracking_module():
    """Load the sibling tracking.py once, robustly (package or importlib tests).

    Registers the module in ``sys.modules`` *before* exec so its dataclasses
    (which reference ``cls.__module__``) process correctly.
    """
    import sys as _sys

    for name in ("v19_tracking", "v19_tracking_rt"):
        if name in _sys.modules:
            return _sys.modules[name]
    import importlib.util as _iu

    name = "v19_tracking_rt"
    spec = _iu.spec_from_file_location(name, Path(__file__).with_name("tracking.py"))
    assert spec and spec.loader
    mod = _iu.module_from_spec(spec)
    _sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- detector
@dataclass
class Detection:
    box: tuple[float, float, float, float]  # x1,y1,x2,y2 in source pixels
    score: float
    class_id: int
    label: str


def _nms(boxes: np.ndarray, scores: np.ndarray, thresh: float) -> list[int]:
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][ovr <= thresh]
    return keep


class YoloxDetector:
    """YOLOX-nano ONNX detector. GPU (CUDA provider) if available, else CPU."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        input_size: int = 416,
        score_threshold: float = 0.30,
        nms_threshold: float = 0.45,
        providers: Sequence[str] | None = None,
    ) -> None:
        import onnxruntime as ort

        self.model_path = str(model_path)
        self.input_size = int(input_size)
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        if providers is None:
            avail = ort.get_available_providers()
            providers = (
                ["CUDAExecutionProvider", "CPUExecutionProvider"]
                if "CUDAExecutionProvider" in avail
                else ["CPUExecutionProvider"]
            )
        self.session = ort.InferenceSession(self.model_path, providers=list(providers))
        self.providers = self.session.get_providers()
        self.on_gpu = "CUDAExecutionProvider" in self.providers
        self._input_name = self.session.get_inputs()[0].name
        self._grids: np.ndarray | None = None
        self._expanded_strides: np.ndarray | None = None
        self.last_infer_ms = 0.0

    # ---- pre/post ----
    def _preprocess(self, img_bgr: np.ndarray) -> tuple[np.ndarray, float]:
        h, w = img_bgr.shape[:2]
        r = min(self.input_size / h, self.input_size / w)
        import cv2

        resized = cv2.resize(
            img_bgr, (int(w * r), int(h * r)), interpolation=cv2.INTER_LINEAR
        )
        padded = np.ones((self.input_size, self.input_size, 3), dtype=np.uint8) * 114
        padded[: resized.shape[0], : resized.shape[1]] = resized
        # YOLOX ONNX export expects raw BGR, CHW, no /255 normalisation.
        chw = padded.transpose(2, 0, 1)[np.newaxis, :, :, :].astype(np.float32)
        return np.ascontiguousarray(chw), r

    def _build_grids(self) -> tuple[np.ndarray, np.ndarray]:
        if self._grids is not None:
            return self._grids, self._expanded_strides  # type: ignore[return-value]
        strides = [8, 16, 32]
        grids, exp = [], []
        for stride in strides:
            g = self.input_size // stride
            xv, yv = np.meshgrid(np.arange(g), np.arange(g))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(grid)
            exp.append(np.full((1, grid.shape[1], 1), stride))
        self._grids = np.concatenate(grids, 1)
        self._expanded_strides = np.concatenate(exp, 1)
        return self._grids, self._expanded_strides

    def _postprocess(self, output: np.ndarray, ratio: float) -> list[Detection]:
        grids, strides = self._build_grids()
        preds = output.copy()
        preds[..., :2] = (preds[..., :2] + grids) * strides
        preds[..., 2:4] = np.exp(preds[..., 2:4]) * strides
        preds = preds[0]
        boxes = preds[:, :4]
        scores = preds[:, 4:5] * preds[:, 5:]
        # xywh -> xyxy
        xyxy = np.empty_like(boxes)
        xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
        xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
        xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.0
        xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.0
        xyxy /= ratio
        class_ids = scores.argmax(1)
        class_scores = scores.max(1)
        keep_mask = class_scores > self.score_threshold
        xyxy = xyxy[keep_mask]
        class_ids = class_ids[keep_mask]
        class_scores = class_scores[keep_mask]
        if len(xyxy) == 0:
            return []
        keep = _nms(xyxy, class_scores, self.nms_threshold)
        out: list[Detection] = []
        for i in keep:
            cid = int(class_ids[i])
            out.append(
                Detection(
                    box=tuple(float(v) for v in xyxy[i]),  # type: ignore[arg-type]
                    score=float(class_scores[i]),
                    class_id=cid,
                    label=COCO_CLASSES[cid] if cid < len(COCO_CLASSES) else str(cid),
                )
            )
        return out

    def detect(self, img_bgr: np.ndarray) -> list[Detection]:
        blob, ratio = self._preprocess(img_bgr)
        t0 = time.perf_counter()
        output = self.session.run(None, {self._input_name: blob})[0]
        self.last_infer_ms = (time.perf_counter() - t0) * 1000.0
        return self._postprocess(output, ratio)


# ------------------------------------------------------------------ adaptive cadence
@dataclass
class AdaptiveCadence:
    """Motion + focus driven detector fps within [fps_min, fps_max] (§3.6)."""

    fps_min: float = 5.0
    fps_max: float = 15.0
    motion_low: float = 0.015
    motion_high: float = 0.06
    _prev_gray: np.ndarray | None = None

    def motion_score(self, img_bgr: np.ndarray) -> float:
        import cv2

        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (64, 64)).astype(np.float32) / 255.0
        if self._prev_gray is None:
            self._prev_gray = small
            return 0.0
        score = float(np.mean(np.abs(small - self._prev_gray)))
        self._prev_gray = small
        return score

    def target_fps(self, motion: float, focus_active: bool = False) -> float:
        if focus_active:
            return self.fps_max
        if motion >= self.motion_high:
            return self.fps_max
        if motion <= self.motion_low:
            return self.fps_min
        # linear interpolation between the bounds
        t = (motion - self.motion_low) / max(1e-6, self.motion_high - self.motion_low)
        return self.fps_min + t * (self.fps_max - self.fps_min)

    def interval_s(self, motion: float, focus_active: bool = False) -> float:
        return 1.0 / max(1e-6, self.target_fps(motion, focus_active))


# ------------------------------------------------------------------ keyframe selector
@dataclass
class KeyframeSelector:
    """Scene-change score (histogram + motion) → keyframe recording (E14 bridge)."""

    change_threshold: float = 0.35
    min_interval_s: float = 3.0
    _prev_hist: np.ndarray | None = None
    _last_keyframe_t: float = -1e9

    def change_score(self, img_bgr: np.ndarray) -> float:
        import cv2

        hist = cv2.calcHist([img_bgr], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
        cv2.normalize(hist, hist)
        hist = hist.flatten()
        if self._prev_hist is None:
            self._prev_hist = hist
            return 1.0  # first frame is always a keyframe candidate
        # 1 - correlation, clamped 0..1
        corr = float(np.correlate(hist, self._prev_hist)[0] / (
            np.linalg.norm(hist) * np.linalg.norm(self._prev_hist) + 1e-9
        ))
        self._prev_hist = hist
        return max(0.0, min(1.0, 1.0 - corr))

    def should_record(self, img_bgr: np.ndarray, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        score = self.change_score(img_bgr)
        if score < self.change_threshold:
            return False
        if now - self._last_keyframe_t < self.min_interval_s:
            return False
        self._last_keyframe_t = now
        return True


# ------------------------------------------------------------------------------ OCR
class OcrRoi:
    """rapidocr_onnxruntime on a crop only (never full-screen, handoff §3.6)."""

    def __init__(self, max_roi_px: int = 640) -> None:
        self.max_roi_px = max_roi_px
        self._engine: Any | None = None

    def _ensure(self) -> Any:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def read(self, crop_bgr: np.ndarray) -> list[dict[str, Any]]:
        if crop_bgr.size == 0:
            return []
        h, w = crop_bgr.shape[:2]
        if max(h, w) > self.max_roi_px:
            import cv2

            scale = self.max_roi_px / max(h, w)
            crop_bgr = cv2.resize(crop_bgr, (int(w * scale), int(h * scale)))
        result, _ = self._ensure()(crop_bgr)
        out: list[dict[str, Any]] = []
        for entry in result or []:
            box, text, conf = entry
            out.append({"text": text, "confidence": float(conf), "box": box})
        return out


# ------------------------------------------------------------------------------ VLM
class VlmCrop:
    """One-job-at-a-time Ollama VLM on a crop. Honest degraded reply if absent."""

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout_s: float = 8.0,
    ) -> None:
        import os

        self.model = model or os.environ.get("MLOMEGA_VLM_MODEL") or "moondream"
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._busy = False  # semaphore of 1 (single-threaded caller)

    def describe(self, crop_bgr: np.ndarray, prompt: str = "What is in this image? Answer briefly.") -> dict[str, Any]:
        if self._busy:
            return {"status": "vlm_busy", "text": None, "model": self.model}
        import cv2

        ok, buf = cv2.imencode(".jpg", crop_bgr)
        if not ok:
            return {"status": "vlm_encode_error", "text": None, "model": self.model}
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "images": [b64], "stream": False}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        self._busy = True
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
            return {"status": "ok", "text": (data.get("response") or "").strip(), "model": self.model}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Ollama unreachable / timed out: degrade honestly, never block.
            return {"status": "vlm_unavailable", "text": None, "model": self.model}
        finally:
            self._busy = False


# --------------------------------------------------------------------------- VisionRT
@dataclass
class VisionMetrics:
    vision_infer_ms: list[float] = field(default_factory=list)
    ocr_ms: list[float] = field(default_factory=list)
    vlm_queue_depth: int = 0
    scene_delta_count: int = 0
    detector_frames: int = 0
    tracker_frames: int = 0
    keyframes_recorded: int = 0
    drops: int = 0

    def snapshot(self) -> dict[str, Any]:
        def pct(xs: list[float], p: float) -> float:
            if not xs:
                return 0.0
            s = sorted(xs)
            return s[min(len(s) - 1, max(0, round((len(s) - 1) * p)))]

        return {
            "vision_infer_ms_p50": pct(self.vision_infer_ms, 0.5),
            "vision_infer_ms_p95": pct(self.vision_infer_ms, 0.95),
            "ocr_ms_p50": pct(self.ocr_ms, 0.5),
            "vlm_queue_depth": self.vlm_queue_depth,
            "scene_delta_rate": self.scene_delta_count,
            "detector_frames": self.detector_frames,
            "tracker_frames": self.tracker_frames,
            "keyframes_recorded": self.keyframes_recorded,
            "drops": self.drops,
        }


class VisionRT:
    """Vision pipeline orchestrator (see module docstring).

    ``on_scene_delta`` receives every :class:`SceneDelta` (dict) — the gateway
    pushes it over the DataChannel and the future WorldBrain (E28) subscribes to
    the same callback. ``on_ui_intent`` receives focus replies (UIIntent dict).
    ``keyframe_sink`` is called ``(image_bgr, frame_envelope)`` when a keyframe is
    selected; the default records it via ``v19_keyframes``.
    """

    def __init__(
        self,
        *,
        detector: YoloxDetector | None = None,
        tracker: Any = None,
        cadence: AdaptiveCadence | None = None,
        keyframes: KeyframeSelector | None = None,
        ocr: OcrRoi | None = None,
        vlm: VlmCrop | None = None,
        arbiter: Any = None,
        session_id: str = "visionrt",
        scene_ttl_ms: int = 2000,
        on_scene_delta: Callable[[dict[str, Any]], Any] | None = None,
        on_ui_intent: Callable[[dict[str, Any]], Any] | None = None,
        keyframe_sink: Callable[[np.ndarray, Any], Any] | None = None,
    ) -> None:
        # ByteTracker lives beside this module; import robustly whether loaded as
        # a package or via importlib (tests).
        if tracker is None:
            mod = _load_tracking_module()
            self._TrackingDetection = mod.Detection
            tracker = mod.ByteTracker()
        else:
            self._TrackingDetection = None
        self.detector = detector
        self.tracker = tracker
        self.cadence = cadence or AdaptiveCadence()
        self.keyframes = keyframes or KeyframeSelector()
        self.ocr = ocr or OcrRoi()
        self.vlm = vlm or VlmCrop()
        self.arbiter = arbiter
        self.session_id = session_id
        self.scene_ttl_ms = scene_ttl_ms
        self.on_scene_delta = on_scene_delta
        self.on_ui_intent = on_ui_intent
        self.keyframe_sink = keyframe_sink
        self.metrics = VisionMetrics()
        self._prev_track_ids: set[str] = set()
        self._last_detect_t = -1e9
        self._floor_only = False  # degraded: detector clamped to fps_min
        self._changes_paused = False
        self._vlm_refused = False
        self._ui_intent_seq = 0

    # ---------------------------------------------------------- degraded control
    def apply_action_level(self, action_level: str) -> None:
        """Map a degraded.py action level onto vision behaviour (handoff §3.6)."""
        self._floor_only = action_level in {
            "detector_floor", "pause_change_detection", "refuse_vlm", "pc_unavailable",
        }
        self._changes_paused = action_level in {
            "pause_change_detection", "refuse_vlm", "pc_unavailable",
        }
        self._vlm_refused = action_level in {"refuse_vlm", "pc_unavailable"}

    # ------------------------------------------------------------------ per-frame
    def _admit(self, job_class: str) -> bool:
        if self.arbiter is None:
            return True
        try:
            return bool(self.arbiter.request(job_class).get("grant"))
        except Exception:
            return True

    def process_frame(
        self, frame_bgr: np.ndarray, envelope: Any, *, focus_active: bool = False, now: float | None = None
    ) -> dict[str, Any] | None:
        """Feed one decoded frame. Returns a SceneDelta dict when detection ran.

        The tracker runs every frame (interpolating between detector passes); the
        detector runs only when the adaptive interval has elapsed and the arbiter
        grants the ``detector`` class.
        """
        now = time.monotonic() if now is None else now
        self.metrics.tracker_frames += 1
        frame_id = getattr(envelope, "frame_id", None) or "unknown"

        motion = self.cadence.motion_score(frame_bgr)
        interval = self.cadence.interval_s(
            motion, focus_active=focus_active and not self._floor_only
        )
        if self._floor_only:
            interval = 1.0 / max(1e-6, self.cadence.fps_min)

        ran_detector = False
        if self.detector is not None and (now - self._last_detect_t) >= interval and self._admit("detector"):
            dets = self.detector.detect(frame_bgr)
            self.metrics.vision_infer_ms.append(self.detector.last_infer_ms)
            self.metrics.detector_frames += 1
            self._last_detect_t = now
            ran_detector = True
            tdets = [
                self._TrackingDetection(box=d.box, score=d.score, kind="object", label=d.label)
                for d in dets
            ] if self._TrackingDetection else []
            tracks = self.tracker.update(tdets)
        else:
            tracks = self.tracker.predict_only()

        # Keyframe selection (event-driven; paused when change detection paused).
        if not self._changes_paused and self.keyframes.should_record(frame_bgr, now=now):
            self._record_keyframe(frame_bgr, envelope)

        if not ran_detector:
            return None  # SceneDelta emitted only on detection frames

        return self._emit_scene_delta(frame_id, tracks)

    def _emit_scene_delta(self, frame_id: str, tracks: list[Any]) -> dict[str, Any]:
        entities = []
        current_ids: set[str] = set()
        for t in tracks:
            current_ids.add(t.track_id)
            x1, y1, x2, y2 = t.box
            entities.append(
                {
                    "track_id": t.track_id,
                    "kind": t.kind,
                    "label": t.label,
                    "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    "confidence": round(float(t.score), 3),
                    "visibility": round(float(t.visibility), 3),
                    "age": int(t.age),
                }
            )
        appeared = current_ids - self._prev_track_ids
        disappeared = self._prev_track_ids - current_ids
        changes = (
            []
            if self._changes_paused
            else (
                [{"type": "appeared", "track_id": tid} for tid in sorted(appeared)]
                + [{"type": "disappeared", "track_id": tid} for tid in sorted(disappeared)]
            )
        )
        self._prev_track_ids = current_ids
        expires = (_utc_now() + timedelta(milliseconds=self.scene_ttl_ms)).isoformat()
        delta = {
            "session_id": self.session_id,
            "source_frame_id": frame_id,
            "entities": entities,
            "relations": [],
            "changes": changes,
            "map_quality": 0.0,
            "evidence_refs": [f"frame:{frame_id}"],
            "expires_at": expires,
        }
        self.metrics.scene_delta_count += 1
        if self.on_scene_delta:
            try:
                self.on_scene_delta(delta)
            except Exception:
                pass
        return delta

    def _record_keyframe(self, frame_bgr: np.ndarray, envelope: Any) -> None:
        self.metrics.keyframes_recorded += 1
        if self.keyframe_sink is not None:
            try:
                self.keyframe_sink(frame_bgr, envelope)
            except Exception:
                pass

    # -------------------------------------------------------------------- focus
    def _next_ui_id(self) -> str:
        self._ui_intent_seq += 1
        return f"visionrt-{self.session_id}-{self._ui_intent_seq}"

    def handle_focus(
        self,
        request: dict[str, Any],
        frame_bgr: np.ndarray,
        envelope: Any,
    ) -> dict[str, Any]:
        """Handle a FocusSearch/LensWindow request (``what_is``/``find``/``ocr``).

        ``request`` carries ``kind`` (what_is|find|ocr), an optional ``bbox``
        (x1,y1,x2,y2 in source pixels) and an optional ``track_id`` to attach the
        reply to. Returns a UIIntent dict (also pushed to ``on_ui_intent``).
        """
        kind = request.get("kind", "what_is")
        bbox = request.get("bbox")
        track_id = request.get("track_id")
        frame_id = getattr(envelope, "frame_id", None)
        crop = self._crop(frame_bgr, bbox)

        content: dict[str, Any] = {}
        truth_level = "inferred"
        confidence = 0.5

        if kind == "ocr":
            if self._admit("ocr"):
                t0 = time.perf_counter()
                lines = self.ocr.read(crop)
                self.metrics.ocr_ms.append((time.perf_counter() - t0) * 1000.0)
                text = " ".join(l["text"] for l in lines)
                content = {"kind": "ocr", "text": text, "lines": lines}
                truth_level = "observed" if text else "inferred"
                confidence = max((l["confidence"] for l in lines), default=0.0)
            else:
                content = {"kind": "ocr", "text": None, "status": "ocr_refused"}
        elif kind == "find":
            # Localised detection on the crop → best-matching label.
            dets = self.detector.detect(crop) if (self.detector and self._admit("detector")) else []
            target = str(request.get("query", "")).lower()
            hits = [d for d in dets if target in d.label.lower()] if target else dets
            hits.sort(key=lambda d: d.score, reverse=True)
            if hits:
                content = {"kind": "find", "label": hits[0].label, "matches": len(hits)}
                truth_level = "observed"
                confidence = float(hits[0].score)
            else:
                content = {"kind": "find", "label": None, "matches": 0}
        else:  # what_is → detector label first, VLM fallback
            label = None
            conf = 0.0
            if self.detector and self._admit("detector"):
                dets = self.detector.detect(crop)
                dets.sort(key=lambda d: d.score, reverse=True)
                if dets:
                    label, conf = dets[0].label, float(dets[0].score)
            if label is not None:
                content = {"kind": "what_is", "label": label, "source": "detector"}
                truth_level = "observed"
                confidence = conf
            elif not self._vlm_refused and self._admit("vlm"):
                self.metrics.vlm_queue_depth += 1
                vlm = self.vlm.describe(crop)
                self.metrics.vlm_queue_depth = max(0, self.metrics.vlm_queue_depth - 1)
                if vlm["status"] == "ok" and vlm["text"]:
                    content = {"kind": "what_is", "label": vlm["text"], "source": "vlm"}
                    truth_level = "probable"
                    confidence = 0.5
                else:
                    # Honest degraded reply (§17.2): never present as observation.
                    content = {
                        "kind": "what_is",
                        "label": None,
                        "source": "vlm",
                        "status": vlm["status"],
                    }
                    truth_level = "inferred"
                    confidence = 0.0
            else:
                content = {"kind": "what_is", "label": None, "status": "vlm_refused"}
                truth_level = "inferred"
                confidence = 0.0

        intent = {
            "ui_intent_id": self._next_ui_id(),
            "producer": "visionrt",
            "source_frame_id": frame_id,
            "target_track_id": track_id,
            "component": "lens_window" if kind == "ocr" else "context_card",
            "anchor": {"type": "track", "track_id": track_id} if track_id else {"type": "crop", "bbox": bbox},
            "content": content,
            "truth_level": truth_level,
            "confidence": round(float(confidence), 3),
            "priority": 0.6,
            "ttl_ms": 5000,
            "ui_hint": {"focus": kind},
            "evidence_refs": [f"frame:{frame_id}"] if frame_id else [],
        }
        if self.on_ui_intent:
            try:
                self.on_ui_intent(intent)
            except Exception:
                pass
        return intent

    def _crop(self, frame_bgr: np.ndarray, bbox: Any) -> np.ndarray:
        if not bbox:
            return frame_bgr
        h, w = frame_bgr.shape[:2]
        x1, y1, x2, y2 = bbox
        x1 = max(0, int(x1)); y1 = max(0, int(y1))
        x2 = min(w, int(x2)); y2 = min(h, int(y2))
        if x2 <= x1 or y2 <= y1:
            return frame_bgr
        return frame_bgr[y1:y2, x1:x2]


def default_keyframe_sink(person_id: str, live_session_id: str, db_path: Any = None) -> Callable[[np.ndarray, Any], Any]:
    """Build a keyframe sink that writes via v19_keyframes.register_xr_keyframe.

    The image is written to a temp path, then registered (raw_assets +
    insert_only vision_frames, capture_mode='xr_keyframe') — the E14 production
    bridge into the existing night chain.
    """
    import tempfile
    import cv2

    root = Path(__file__).resolve().parents[2]
    import sys

    if str(root / "src") not in sys.path:
        sys.path.insert(0, str(root / "src"))
    from mlomega_audio_elite.v19_keyframes import register_xr_keyframe

    def _sink(frame_bgr: np.ndarray, envelope: Any) -> None:
        frame_id = getattr(envelope, "frame_id", None)
        captured_at = getattr(envelope, "captured_at_utc", None)
        fd = tempfile.NamedTemporaryFile(prefix="kf_", suffix=".jpg", delete=False)
        fd.close()
        cv2.imwrite(fd.name, frame_bgr)
        register_xr_keyframe(
            person_id=person_id,
            live_session_id=live_session_id,
            image_path=fd.name,
            captured_at=captured_at,
            frame_id=frame_id,
            db_path=db_path,
        )

    return _sink
