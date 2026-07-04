"""E27 ByteTrack tracker: stable ids, crossing, short-occlusion recovery."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.vision

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tracking = _load("v19_tracking", "services/live-pc/tracking.py")
ByteTracker = tracking.ByteTracker
Detection = tracking.Detection


def _box(cx, cy, w=40, h=40):
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def test_ids_stable_over_time():
    tr = ByteTracker()
    ids = set()
    for i in range(20):
        dets = [Detection(box=_box(100 + i * 5, 100), score=0.9, kind="object")]
        tracks = tr.update(dets)
        assert len(tracks) == 1
        ids.add(tracks[0].track_id)
    assert len(ids) == 1  # a single moving object keeps one id


def test_two_objects_crossing_keep_ids():
    tr = ByteTracker()
    # Two objects approaching, crossing, separating on the x axis at fixed y gap.
    id_a = id_b = None
    for i in range(30):
        ax = 60 + i * 6
        bx = 300 - i * 6
        dets = [
            Detection(box=_box(ax, 100), score=0.9, kind="object", label="A"),
            Detection(box=_box(bx, 160), score=0.9, kind="object", label="B"),
        ]
        tracks = tr.update(dets)
        by_label = {t.label: t.track_id for t in tracks if t.label}
        if i == 0:
            id_a, id_b = by_label.get("A"), by_label.get("B")
        if i == 29:
            assert by_label.get("A") == id_a
            assert by_label.get("B") == id_b
    assert id_a and id_b and id_a != id_b


def test_short_occlusion_same_id():
    tr = ByteTracker(max_age=30)
    # Establish a track.
    for i in range(5):
        tracks = tr.update([Detection(box=_box(150 + i * 4, 120), score=0.9)])
    track_id = tracks[0].track_id
    # Occlusion: no detections for a few frames -> predict_only interpolates.
    for _ in range(4):
        tr.predict_only()
    # Reappears near the predicted position: low-confidence recovery keeps id.
    tracks = tr.update([Detection(box=_box(150 + 8 * 4, 120), score=0.3)])
    assert any(t.track_id == track_id for t in tracks), [t.track_id for t in tracks]


def test_interpolation_advances_position():
    tr = ByteTracker()
    tr.update([Detection(box=_box(100, 100), score=0.9)])
    tr.update([Detection(box=_box(120, 100), score=0.9)])  # +20 vx
    before = tr.tracks[0].box
    tr.predict_only()
    after = tr.tracks[0].box
    # Constant-velocity Kalman should push cx forward, not stay put.
    assert after[0] > before[0]
