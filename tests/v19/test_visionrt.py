"""E27 VisionRT: real YOLOX detection, adaptive cadence, keyframes, SceneDelta."""

from __future__ import annotations

import importlib.util
import sys
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


_load("v19_tracking_rt", "services/live-pc/tracking.py")
visionrt = _load("v19_visionrt", "services/live-pc/visionrt.py")

cv2 = pytest.importorskip("cv2")

from packages.contracts.python.models import FrameEnvelope, Pose, SceneDelta  # noqa: E402


def _envelope(fid: str) -> FrameEnvelope:
    from datetime import datetime, timezone

    return FrameEnvelope(
        session_id="visionrt-test",
        frame_id=fid,
        capture_monotonic_ns=1,
        captured_at_utc=datetime.now(timezone.utc).isoformat(),
        pose=Pose(position=[0, 0, 0], rotation=[0, 0, 0, 1]),
        source="test",
    )


skip_no_model = pytest.mark.skipif(not MODEL.exists(), reason="yolox model not fetched")


@skip_no_model
def test_detector_finds_person_on_reference_image():
    det = visionrt.YoloxDetector(str(MODEL))
    assert FIXTURE.exists(), "reference fixture missing"
    img = cv2.imread(str(FIXTURE))
    assert img is not None
    dets = det.detect(img)
    labels = {d.label for d in dets if d.score > 0.4}
    assert "person" in labels, labels
    assert det.last_infer_ms > 0


@skip_no_model
def test_scene_delta_valid_and_bound_to_frame():
    det = visionrt.YoloxDetector(str(MODEL))
    vr = visionrt.VisionRT(detector=det, session_id="visionrt-test")
    img = cv2.imread(str(FIXTURE))
    delta = vr.process_frame(img, _envelope("frame-001"), now=100.0)
    assert delta is not None
    # Validates against the real SceneDelta contract.
    sd = SceneDelta.model_validate(delta)
    assert sd.source_frame_id == "frame-001"
    assert sd.session_id == "visionrt-test"
    assert sd.expires_at is not None
    assert any(e.get("kind") == "object" for e in sd.entities)


def test_adaptive_cadence_static_vs_motion():
    cad = visionrt.AdaptiveCadence(fps_min=5, fps_max=15, motion_low=0.015, motion_high=0.06)
    # static scene -> floor fps
    assert cad.target_fps(0.0) == pytest.approx(5.0)
    # strong motion -> max fps
    assert cad.target_fps(0.2) == pytest.approx(15.0)
    # focus demand overrides to max
    assert cad.target_fps(0.0, focus_active=True) == pytest.approx(15.0)
    # mid motion is between the bounds
    mid = cad.target_fps(0.0375)
    assert 5.0 < mid < 15.0


@skip_no_model
def test_detector_runs_more_often_under_motion():
    det = visionrt.YoloxDetector(str(MODEL))
    img = cv2.imread(str(FIXTURE))
    h, w = img.shape[:2]

    def run(scene_fn) -> int:
        vr = visionrt.VisionRT(detector=det, session_id="s")
        vr.keyframes.change_threshold = 2.0  # disable keyframe writes here
        t = 0.0
        for i in range(30):
            frame = scene_fn(i)
            vr.process_frame(frame, _envelope(f"f{i}"), now=t)
            t += 1.0 / 30.0  # 30 fps feed
        return vr.metrics.detector_frames

    static = run(lambda i: img.copy())
    moving = run(lambda i: np.roll(img, i * 25, axis=1))
    assert moving > static, (moving, static)


@skip_no_model
def test_keyframe_recorded_via_v19_keyframes(tmp_path, monkeypatch):
    monkeypatch.setenv("MLOMEGA_DB", str(tmp_path / "kf.db"))
    monkeypatch.setenv("MLOMEGA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MLOMEGA_RAW", str(tmp_path / "raw"))
    db_path = tmp_path / "kf.db"

    from mlomega_audio_elite.brainlive_v15 import start_live_session
    from mlomega_audio_elite.db import connect

    session = start_live_session(person_id="me", title="kf")
    lsid = session["live_session_id"]

    sink = visionrt.default_keyframe_sink(person_id="me", live_session_id=lsid, db_path=db_path)
    det = visionrt.YoloxDetector(str(MODEL))
    vr = visionrt.VisionRT(detector=det, keyframe_sink=sink, session_id="s")
    vr.keyframes.change_threshold = 0.0  # force keyframe on first frame
    vr.keyframes.min_interval_s = 0.0
    img = cv2.imread(str(FIXTURE))
    vr.process_frame(img, _envelope("kf-frame-1"), now=100.0)
    assert vr.metrics.keyframes_recorded >= 1

    with connect(db_path) as con:
        rows = con.execute(
            "SELECT capture_mode FROM vision_frames WHERE live_session_id=?", (lsid,)
        ).fetchall()
    assert rows, "no vision_frames row written"
    assert rows[0][0] == "xr_keyframe"
