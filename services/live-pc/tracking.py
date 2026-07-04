from __future__ import annotations

"""Self-contained ByteTrack tracker (handoff §3.6, "tracker every frame").

A dependency-light implementation of the ByteTrack association strategy:

* a simple constant-velocity Kalman filter per track (state = cx, cy, area,
  aspect + their velocities), enough to *interpolate* a track's position on the
  many frames where the detector did not run;
* two-pass IoU association: high-confidence detections first, then a second pass
  over the surviving tracks against low-confidence detections (the core
  ByteTrack idea — low-score boxes recover tracks through short occlusions
  instead of spawning ID switches);
* stable, short ``track_id`` strings, with ``age`` (frames since birth) and
  ``visibility`` (fraction of recent frames the track was matched) so downstream
  SceneDelta can flag remembered/last-seen tracks honestly (handoff §17.2).

No external tracking package. NumPy only. Pure/deterministic → unit-testable.

Frame model: the tracker is fed on *every* decoded frame. On frames with fresh
detections call :meth:`update`; on frames between detector passes call
:meth:`predict_only` to advance the Kalman state (the "interpolation between two
detection passes" of §3.6).
"""

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

BBox = tuple[float, float, float, float]  # x1, y1, x2, y2


def iou(a: BBox, b: BBox) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _to_xyah(box: BBox) -> np.ndarray:
    x1, y1, x2, y2 = box
    w = max(1e-6, x2 - x1)
    h = max(1e-6, y2 - y1)
    return np.array([x1 + w / 2.0, y1 + h / 2.0, w / h, h], dtype=float)


def _to_xyxy(xyah: np.ndarray) -> BBox:
    cx, cy, a, h = xyah
    w = a * h
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


class _Kalman:
    """Constant-velocity Kalman on (cx, cy, aspect, h). 8-dim state."""

    def __init__(self, measurement: np.ndarray) -> None:
        self.x = np.concatenate([measurement, np.zeros(4)])  # pos + velocity
        self.P = np.eye(8) * 10.0
        self._q = 1.0     # process noise
        self._r = 1.0     # measurement noise

    def _F(self) -> np.ndarray:
        F = np.eye(8)
        for i in range(4):
            F[i, i + 4] = 1.0
        return F

    def predict(self) -> np.ndarray:
        F = self._F()
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + np.eye(8) * self._q
        return self.x[:4]

    def update(self, measurement: np.ndarray) -> None:
        H = np.zeros((4, 8))
        H[:4, :4] = np.eye(4)
        S = H @ self.P @ H.T + np.eye(4) * self._r
        K = self.P @ H.T @ np.linalg.inv(S)
        y = measurement - H @ self.x
        self.x = self.x + K @ y
        self.P = (np.eye(8) - K @ H) @ self.P

    @property
    def box(self) -> BBox:
        return _to_xyxy(self.x[:4])


@dataclass
class Track:
    track_id: str
    kind: str
    kalman: _Kalman
    score: float
    age: int = 0                 # frames since birth
    hits: int = 0                # matched detections
    time_since_update: int = 0   # frames since last matched
    label: str | None = None
    _recent: list[int] = field(default_factory=list)  # 1 matched / 0 missed, window

    @property
    def box(self) -> BBox:
        return self.kalman.box

    @property
    def visibility(self) -> float:
        if not self._recent:
            return 1.0
        return sum(self._recent) / len(self._recent)

    def _tick(self, matched: bool) -> None:
        self._recent.append(1 if matched else 0)
        if len(self._recent) > 30:
            self._recent.pop(0)


@dataclass
class Detection:
    box: BBox
    score: float
    kind: str = "object"
    label: str | None = None


class ByteTracker:
    """Two-pass IoU/Kalman tracker (ByteTrack).

    Parameters mirror the reference defaults but are overridable for tests.
    """

    def __init__(
        self,
        *,
        high_thresh: float = 0.5,
        low_thresh: float = 0.1,
        match_thresh: float = 0.2,
        max_age: int = 30,
        min_hits: int = 1,
    ) -> None:
        self.high_thresh = high_thresh
        self.low_thresh = low_thresh
        self.match_thresh = match_thresh
        self.max_age = max_age
        self.min_hits = min_hits
        self.tracks: list[Track] = []
        self._next_id = 0
        self.frame_index = 0

    # -------------------------------------------------------------- helpers
    def _new_id(self) -> str:
        self._next_id += 1
        return f"t{self._next_id}"

    def _associate(
        self, tracks: list[Track], dets: list[Detection]
    ) -> tuple[list[tuple[int, int]], list[int], list[int]]:
        """Greedy IoU matching. Returns (matches, unmatched_tracks, unmatched_dets)."""
        if not tracks or not dets:
            return [], list(range(len(tracks))), list(range(len(dets)))
        cost = np.zeros((len(tracks), len(dets)))
        for ti, t in enumerate(tracks):
            for di, d in enumerate(dets):
                cost[ti, di] = iou(t.box, d.box)
        matches: list[tuple[int, int]] = []
        used_t: set[int] = set()
        used_d: set[int] = set()
        # Greedy over descending IoU (Hungarian is overkill for these counts).
        pairs = sorted(
            ((cost[ti, di], ti, di) for ti in range(len(tracks)) for di in range(len(dets))),
            key=lambda p: p[0],
            reverse=True,
        )
        for score, ti, di in pairs:
            if score < self.match_thresh:
                break
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            matches.append((ti, di))
        un_t = [ti for ti in range(len(tracks)) if ti not in used_t]
        un_d = [di for di in range(len(dets)) if di not in used_d]
        return matches, un_t, un_d

    # ------------------------------------------------------------- stepping
    def predict_only(self) -> list[Track]:
        """Advance every track's Kalman state one frame (no detections).

        Used on frames between detector passes: the tracker "interpolates the
        positions between two detections" (§3.6). Tracks age but are not culled
        here (culling happens in :meth:`update`).
        """
        self.frame_index += 1
        for t in self.tracks:
            t.kalman.predict()
            t.age += 1
            t.time_since_update += 1
        return self.active_tracks()

    def update(self, detections: Sequence[Detection]) -> list[Track]:
        """Run a full detection frame through the two-pass association."""
        self.frame_index += 1
        for t in self.tracks:
            t.kalman.predict()
            t.age += 1

        dets = list(detections)
        high = [d for d in dets if d.score >= self.high_thresh]
        low = [d for d in dets if self.low_thresh <= d.score < self.high_thresh]

        # Pass 1: high-confidence detections vs all tracks.
        matches, un_t, un_d_high = self._associate(self.tracks, high)
        for ti, di in matches:
            self._apply_match(self.tracks[ti], high[di])

        # Pass 2: remaining tracks vs low-confidence detections (occlusion
        # recovery — the ByteTrack twist). Unmatched low dets are discarded, not
        # promoted to new tracks.
        remaining = [self.tracks[ti] for ti in un_t]
        m2, un_t2_rel, _ = self._associate(remaining, low)
        matched_remaining: set[int] = set()
        for ti_rel, di in m2:
            self._apply_match(remaining[ti_rel], low[di])
            matched_remaining.add(ti_rel)
        for idx, t in enumerate(remaining):
            if idx not in matched_remaining:
                t.time_since_update += 1
                t._tick(False)

        # New tracks from unmatched high-confidence detections only.
        for di in un_d_high:
            self._spawn(high[di])

        # Cull dead tracks.
        self.tracks = [t for t in self.tracks if t.time_since_update <= self.max_age]
        return self.active_tracks()

    def _apply_match(self, track: Track, det: Detection) -> None:
        track.kalman.update(_to_xyah(det.box))
        track.score = det.score
        track.hits += 1
        track.time_since_update = 0
        if det.label:
            track.label = det.label
        track.kind = det.kind
        track._tick(True)

    def _spawn(self, det: Detection) -> None:
        track = Track(
            track_id=self._new_id(),
            kind=det.kind,
            kalman=_Kalman(_to_xyah(det.box)),
            score=det.score,
            label=det.label,
        )
        track.hits = 1
        track._tick(True)
        self.tracks.append(track)

    def active_tracks(self) -> list[Track]:
        """Tracks confirmed and currently visible enough to surface."""
        return [
            t
            for t in self.tracks
            if t.hits >= self.min_hits and t.time_since_update <= self.max_age
        ]
