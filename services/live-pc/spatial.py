from __future__ import annotations

"""Spatial — the V19.A ``SpatialMapProvider`` (guide §10.3).

V19.A is the realistic first tier: **pose (XREAL) + tracks + keyframes + session
directions**. It is enough for outlines, last-seen cards and *cautious* arrows —
nothing more. No SLAM, no relocalisation (those are V19.B/C, isolated later).

``PoseKeyframeMap`` builds session *zones* by clustering pose positions, records
the last-seen pose per entity, and answers bearings (relative direction from the
current pose to an entity's last-seen pose). The single absolute rule (§10.3,
handoff §5): **a bearing is returned only when the measured ``map_quality`` clears
the configured threshold — otherwise ``None``**. A false arrow is worse than no
arrow.

``map_quality`` is *measured*, not asserted: it combines pose density (how many
distinct poses we have), freshness (how recently we saw a pose) and coherence
(how tight the pose cloud is — a wildly scattered cloud is low-trust).
"""

import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


# --------------------------------------------------------------------------- iface
class SpatialMapProvider(Protocol):
    """Interface every spatial tier (V19.A → V19.D) implements."""

    def observe_pose(self, frame_id: str, pose: Mapping[str, Any], *, now: float | None = None) -> None: ...

    def note_entity(self, entity_id: str, frame_id: str, pose: Mapping[str, Any] | None = None) -> None: ...

    def map_quality(self) -> float: ...

    def active_zone(self) -> str | None: ...

    def bearing_to(self, entity_id: str) -> dict[str, Any] | None: ...


# --------------------------------------------------------------------------- config
@dataclass
class SpatialConfig:
    min_map_quality_for_bearing: float = 0.35
    min_poses_for_quality: int = 6          # below this, density penalises quality
    density_saturation: int = 30            # poses that saturate the density term
    freshness_horizon_s: float = 10.0       # pose older than this → freshness decays
    coherence_scale: float = 2.0            # spread (in pose units) that halves coherence
    zone_cluster_radius: float = 1.0        # positions within this radius share a zone


# --------------------------------------------------------------------------- helpers
def _position(pose: Mapping[str, Any] | None) -> tuple[float, float, float] | None:
    if not pose:
        return None
    pos = pose.get("position") if isinstance(pose, Mapping) else None
    if not pos or len(pos) < 3:
        return None
    try:
        return (float(pos[0]), float(pos[1]), float(pos[2]))
    except (TypeError, ValueError):
        return None


def _yaw_from_rotation(pose: Mapping[str, Any] | None) -> float | None:
    """Extract yaw (heading around the up axis) from a quaternion [x,y,z,w]."""
    if not pose:
        return None
    rot = pose.get("rotation") if isinstance(pose, Mapping) else None
    if not rot or len(rot) < 4:
        return None
    try:
        x, y, z, w = (float(v) for v in rot[:4])
    except (TypeError, ValueError):
        return None
    # yaw around Y (up): standard quaternion→euler for the up axis.
    siny_cosp = 2.0 * (w * y + x * z)
    cosy_cosp = 1.0 - 2.0 * (y * y + x * x)
    return math.atan2(siny_cosp, cosy_cosp)


# --------------------------------------------------------------------------- impl
@dataclass
class _PoseSample:
    frame_id: str
    position: tuple[float, float, float]
    yaw: float | None
    at: float


class PoseKeyframeMap:
    """V19.A spatial provider built purely from pose keyframes."""

    def __init__(self, config: SpatialConfig | None = None) -> None:
        self.config = config or SpatialConfig()
        self._poses: list[_PoseSample] = []
        self._entity_last_pose: dict[str, _PoseSample] = {}
        self._last_pose: _PoseSample | None = None

    # ------------------------------------------------------------------ observe
    def observe_pose(self, frame_id: str, pose: Mapping[str, Any], *, now: float | None = None) -> None:
        pos = _position(pose)
        if pos is None:
            return
        now = time.monotonic() if now is None else now
        sample = _PoseSample(frame_id=frame_id, position=pos, yaw=_yaw_from_rotation(pose), at=now)
        self._poses.append(sample)
        self._last_pose = sample

    def note_entity(self, entity_id: str, frame_id: str, pose: Mapping[str, Any] | None = None) -> None:
        """Record where an entity was last seen (defaults to the current pose)."""
        if pose is not None:
            pos = _position(pose)
            if pos is not None:
                self._entity_last_pose[entity_id] = _PoseSample(frame_id, pos, _yaw_from_rotation(pose), time.monotonic())
                return
        if self._last_pose is not None:
            self._entity_last_pose[entity_id] = self._last_pose

    # ------------------------------------------------------------------ quality
    def map_quality(self, *, now: float | None = None) -> float:
        """Measured 0..1 quality: density × freshness × coherence."""
        if not self._poses:
            return 0.0
        now = time.monotonic() if now is None else now
        cfg = self.config

        n = len(self._poses)
        density = min(1.0, n / max(1, cfg.density_saturation))
        if n < cfg.min_poses_for_quality:
            density *= n / max(1, cfg.min_poses_for_quality)

        last = self._last_pose.at if self._last_pose else now
        age = max(0.0, now - last)
        freshness = math.exp(-age / max(1e-6, cfg.freshness_horizon_s))

        coherence = self._coherence()
        return max(0.0, min(1.0, density * freshness * coherence))

    def _coherence(self) -> float:
        pts = [p.position for p in self._poses]
        if len(pts) < 2:
            return 1.0
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        cz = sum(p[2] for p in pts) / len(pts)
        spread = sum(math.dist(p, (cx, cy, cz)) for p in pts) / len(pts)
        # Tight cloud → ~1.0 ; very scattered → decays toward 0.
        return 1.0 / (1.0 + spread / max(1e-6, self.config.coherence_scale))

    # ------------------------------------------------------------------ zones
    def zones(self) -> list[dict[str, Any]]:
        """Cluster pose positions into zones (simple radius clustering)."""
        clusters: list[dict[str, Any]] = []
        r = self.config.zone_cluster_radius
        for p in self._poses:
            placed = False
            for c in clusters:
                if math.dist(p.position, c["center"]) <= r:
                    c["members"].append(p.position)
                    n = len(c["members"])
                    c["center"] = tuple(sum(m[i] for m in c["members"]) / n for i in range(3))
                    placed = True
                    break
            if not placed:
                clusters.append({"center": p.position, "members": [p.position]})
        for i, c in enumerate(clusters):
            c["zone_id"] = f"zone-{i}"
            c["weight"] = len(c["members"])
        return clusters

    def active_zone(self) -> str | None:
        if self._last_pose is None:
            return None
        zs = self.zones()
        if not zs:
            return None
        nearest = min(zs, key=lambda c: math.dist(self._last_pose.position, c["center"]))
        return nearest["zone_id"]

    # ------------------------------------------------------------------ bearing
    def bearing_to(self, entity_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Relative bearing to an entity's last-seen pose.

        Returns ``None`` when map quality is below threshold (§10.3) — never a
        false arrow — or when we have no pose for the entity/current view.
        """
        mq = self.map_quality(now=now)
        if mq < self.config.min_map_quality_for_bearing:
            return None
        if self._last_pose is None:
            return None
        target = self._entity_last_pose.get(entity_id)
        if target is None:
            return None

        cur = self._last_pose.position
        dx = target.position[0] - cur[0]
        dz = target.position[2] - cur[2]
        distance = math.hypot(dx, dz)
        if distance < 1e-6:
            return {"entity_id": entity_id, "bearing_deg": 0.0, "distance": 0.0, "map_quality": round(mq, 3)}

        world_bearing = math.atan2(dx, -dz)  # 0 = forward (-Z), +right
        heading = self._last_pose.yaw or 0.0
        relative = world_bearing - heading
        # normalise to (-pi, pi]
        relative = (relative + math.pi) % (2 * math.pi) - math.pi
        return {
            "entity_id": entity_id,
            "bearing_deg": round(math.degrees(relative), 1),
            "distance": round(distance, 2),
            "map_quality": round(mq, 3),
        }


# --------------------------------------------------------------------------- search
def answer_find(
    *,
    entity_id: str | None,
    entity: Mapping[str, Any] | None,
    spatial: SpatialMapProvider,
    session_id: str,
    visible: bool,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Build the UIIntent reply for a FocusSearch ``find`` (E27 handoff §9.3).

    * visible → the outline is already handled by VisionRT/UltraLive; return a
      lightweight ``visible`` marker so the caller can defer to the outline.
    * not visible → a *last-seen card* with age, plus a bearing **only if the map
      qualifies** (otherwise ``bearing=None`` and the card is honest about it).
    """
    from datetime import datetime, timezone

    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    if visible:
        return {
            "component": "object_outline",
            "content": {"kind": "find", "state": "visible", "entity_id": entity_id},
            "truth_level": "observed",
            "bearing": None,
        }

    bearing = spatial.bearing_to(entity_id) if entity_id else None
    ent = dict(entity or {})
    card = {
        "component": "context_card",
        "content": {
            "kind": "find",
            "state": "last_seen",
            "entity_id": entity_id,
            "label": ent.get("label"),
            "age_seconds": ent.get("age_seconds"),
            "last_seen": ent.get("last_seen"),
            "place_hint": ent.get("place_hint"),
        },
        "truth_level": "remembered",
        "bearing": bearing,  # None unless the map qualifies — never a false arrow
    }
    if bearing is not None:
        card["component"] = "offscreen_arrow"
    return card
