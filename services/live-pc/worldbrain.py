from __future__ import annotations

"""WorldBrain — the spatial/relational *present* (guide §10.2).

WorldBrain consumes the :class:`SceneDelta` stream emitted by VisionRT (E27) and
maintains the live, relational picture of *what is here now*:

* :class:`WorldEntity` — a durable ``entity_id`` **promoted** from repeated,
  confirmed tracks. A single weak bbox never becomes an entity (§7.1): promotion
  requires ``promote_min_observations`` confirmed sightings above
  ``promote_min_confidence``.
* :class:`Observation` — one dated, correctable sighting (frame_id, track_id,
  state, model, confidence, evidence).
* :class:`Relation` — subject/predicate/object derived *geometrically* from the
  bboxes of the current frame (``on_top_of``, ``near``, ``holds``).
* :class:`SceneSession` — place_hint, active_zone, map_quality for a visit/task.
* :class:`ChangeEvent` — appeared/disappeared/moved with before/after evidence.

Persistence is layered and never invents a parallel schema for core tables
(piège #11):

* last-seen + changes → ``visual_events_v19`` via ``store_visual_event`` (with an
  explicit ``memory_owner_id``);
* end-of-session summaries → ``scene_session_summaries_v19``;
* the current world state → the REAL ``brainlive_world_states`` /
  ``vision_scene_observations`` via ``v19_visual_context.publish_visual_context``.

Only *session* bookkeeping lives in a light service-local SQLite file — never a
new table in the core.

WorldBrain does **not** produce a psychological profile or arbitrary UI output
(handoff §ne-fait-pas). It reports facts; BrainLive decides what to say.
"""

import importlib.util
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_store():
    """Import the core V19 store lazily (kept out of import cycles / tests)."""
    from mlomega_audio_elite import v19_visual_store as store  # type: ignore

    return store


def _load_visual_context():
    from mlomega_audio_elite import v19_visual_context as ctx  # type: ignore

    return ctx


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- data
@dataclass
class Observation:
    """One dated, correctable sighting of a track (§10.2)."""

    observation_id: str
    frame_id: str
    track_id: str
    kind: str
    label: str
    state: str  # "visible" | "last_seen" | ...
    model: str
    confidence: float
    bbox: tuple[float, float, float, float]
    observed_at: str
    evidence_refs: list[str] = field(default_factory=list)
    entity_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "frame_id": self.frame_id,
            "track_id": self.track_id,
            "entity_id": self.entity_id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "model": self.model,
            "confidence": round(float(self.confidence), 3),
            "bbox": [round(float(v), 1) for v in self.bbox],
            "observed_at": self.observed_at,
            "evidence": list(self.evidence_refs),
        }


@dataclass
class WorldEntity:
    """A durable entity promoted from repeated confirmed tracks."""

    entity_id: str
    kind: str
    label: str
    confidence: float
    lifecycle: str = "candidate"  # candidate → confirmed → last_seen → gone
    track_id: str | None = None
    first_seen: str = ""
    last_seen: str = ""
    last_bbox: tuple[float, float, float, float] | None = None
    observation_count: int = 0
    evidence_refs: list[str] = field(default_factory=list)

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or _utc_now()
        try:
            last = datetime.fromisoformat(self.last_seen)
        except (ValueError, TypeError):
            return 0.0
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0.0, (now - last).total_seconds())

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "label": self.label,
            "confidence": round(float(self.confidence), 3),
            "lifecycle": self.lifecycle,
            "track_id": self.track_id,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_bbox": [round(float(v), 1) for v in self.last_bbox] if self.last_bbox else None,
            "observation_count": self.observation_count,
            "age_seconds": round(self.age_seconds(now), 1),
            "evidence": list(self.evidence_refs),
        }


@dataclass
class Relation:
    subject: str  # entity_id or track_id
    predicate: str  # on_top_of | near | holds
    object: str
    observed_at: str
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "observed_at": self.observed_at,
            "confidence": round(float(self.confidence), 3),
            "evidence": list(self.evidence_refs),
        }


@dataclass
class ChangeEvent:
    change_type: str  # appeared | disappeared | moved | attribute_changed
    entity_id: str
    label: str
    observed_at: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.change_type,
            "entity_id": self.entity_id,
            "label": self.label,
            "observed_at": self.observed_at,
            "before": self.before,
            "after": self.after,
            "evidence": list(self.evidence_refs),
        }


@dataclass
class SceneSession:
    session_id: str
    place_hint: str | None = None
    active_zone: str | None = None
    map_quality: float = 0.0
    started_at: str = ""
    keyframes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- config
@dataclass
class WorldBrainConfig:
    """Promotion / geometry / staleness thresholds (all config, never hardcoded)."""

    promote_min_observations: int = 3
    promote_min_confidence: float = 0.35
    near_iou_gap_ratio: float = 0.6      # centre distance / mean box size below → near
    on_top_overlap_ratio: float = 0.15   # horizontal overlap fraction for on_top_of
    holds_person_overlap_ratio: float = 0.10
    moved_center_ratio: float = 0.25     # centre shift / box diag above → moved
    stale_after_seconds: float = 20.0    # entity marked last_seen when unseen this long


# --------------------------------------------------------------------------- geometry
def _center(box: Sequence[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _size(box: Sequence[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (abs(x2 - x1), abs(y2 - y1))


def _diag(box: Sequence[float]) -> float:
    w, h = _size(box)
    return (w * w + h * h) ** 0.5


def _iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _horizontal_overlap_frac(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, _, ax2, _ = a
    bx1, _, bx2, _ = b
    ov = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    wa = max(1e-6, ax2 - ax1)
    return ov / wa


# --------------------------------------------------------------------------- WorldBrain
class WorldBrain:
    """Maintains the live entity/relation/change picture from SceneDeltas."""

    def __init__(
        self,
        *,
        person_id: str,
        live_session_id: str,
        config: WorldBrainConfig | None = None,
        db_path: Any = None,
        service_db_path: str | Path | None = None,
        spatial: Any = None,
        publish_world_state: bool = True,
    ) -> None:
        self.person_id = person_id
        self.live_session_id = live_session_id
        self.config = config or WorldBrainConfig()
        self.db_path = db_path  # core memory DB (visual_events_v19, world_states)
        self.spatial = spatial
        self.publish_world_state = publish_world_state

        self.session = SceneSession(
            session_id=live_session_id, started_at=_iso(_utc_now())
        )
        self.entities: dict[str, WorldEntity] = {}          # entity_id → entity
        # E35 §3: labels/zones the user has verbally corrected away ("ce n'est pas
        # mon téléphone", "on n'est pas au bureau"). A suspended label is filtered
        # out of every subsequent snapshot/SceneDelta; a suspended zone is dropped
        # from ``active_zone``. Correction is durable within the session.
        self._suspended_labels: set[str] = set()            # normalised labels
        self._suspended_zones: set[str] = set()             # zone ids / place hints
        self._track_to_entity: dict[str, str] = {}          # track_id → entity_id
        self._track_counts: dict[str, int] = {}             # track_id → confirmed hits
        self._track_last: dict[str, dict[str, Any]] = {}    # track_id → last raw entry
        self.relations: list[Relation] = []
        self.change_events: list[ChangeEvent] = []
        self._obs_seq = 0
        self._entity_seq = 0

        # Light service-local SQLite for session persistence (never a core table).
        self._svc_db = self._init_service_db(service_db_path)

        self.metrics = {
            "scene_deltas": 0,
            "entities_promoted": 0,
            "last_seen_count": 0,
            "change_events": 0,
            "relations": 0,
            "world_state_published": 0,
        }

    # -------------------------------------------------------- service-local store
    def _init_service_db(self, path: str | Path | None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path) if path else ":memory:")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS worldbrain_session_entities(
                 entity_id TEXT PRIMARY KEY, live_session_id TEXT, kind TEXT,
                 label TEXT, lifecycle TEXT, first_seen TEXT, last_seen TEXT,
                 observation_count INTEGER, confidence REAL, last_bbox TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS worldbrain_session_changes(
                 change_seq INTEGER PRIMARY KEY AUTOINCREMENT, live_session_id TEXT,
                 change_type TEXT, entity_id TEXT, label TEXT, observed_at TEXT)"""
        )
        conn.commit()
        return conn

    def _persist_entity(self, e: WorldEntity) -> None:
        import json as _json

        self._svc_db.execute(
            """INSERT INTO worldbrain_session_entities(
                 entity_id, live_session_id, kind, label, lifecycle, first_seen,
                 last_seen, observation_count, confidence, last_bbox)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(entity_id) DO UPDATE SET
                 lifecycle=excluded.lifecycle, last_seen=excluded.last_seen,
                 observation_count=excluded.observation_count,
                 confidence=excluded.confidence, last_bbox=excluded.last_bbox""",
            (
                e.entity_id, self.live_session_id, e.kind, e.label, e.lifecycle,
                e.first_seen, e.last_seen, e.observation_count, e.confidence,
                _json.dumps(list(e.last_bbox) if e.last_bbox else None),
            ),
        )
        self._svc_db.commit()

    # --------------------------------------------------------------- ingest
    def _next_obs_id(self) -> str:
        self._obs_seq += 1
        return f"obs-{self.live_session_id}-{self._obs_seq}"

    def _next_entity_id(self, label: str) -> str:
        self._entity_seq += 1
        return f"ent-{self.live_session_id}-{label}-{self._entity_seq}"

    def ingest_scene_delta(self, delta: Mapping[str, Any]) -> dict[str, Any]:
        """Consume one SceneDelta; return promoted/changed entities for this frame."""
        self.metrics["scene_deltas"] += 1
        now = _utc_now()
        now_iso = _iso(now)
        frame_id = str(delta.get("source_frame_id") or "unknown")
        evidence_ref = f"frame:{frame_id}"
        map_quality = float(delta.get("map_quality") or 0.0)

        # map quality: prefer the spatial provider's measured value when present.
        if self.spatial is not None:
            try:
                mq = self.spatial.map_quality()
                if mq is not None:
                    map_quality = float(mq)
            except Exception:
                pass
        self.session.map_quality = map_quality

        raw_entities = list(delta.get("entities") or [])
        seen_track_ids: set[str] = set()
        observations: list[Observation] = []
        promoted_now: list[WorldEntity] = []

        for ent in raw_entities:
            track_id = str(ent.get("track_id") or "")
            if not track_id:
                continue
            seen_track_ids.add(track_id)
            label = str(ent.get("label") or ent.get("kind") or "object")
            # E35 §3: a label the user corrected away never re-promotes or updates
            # an entity — the wrong label stays out of the world picture.
            if self.is_label_suspended(label):
                continue
            kind = str(ent.get("kind") or "object")
            conf = float(ent.get("confidence") or 0.0)
            bbox = tuple(float(v) for v in (ent.get("bbox") or (0, 0, 0, 0)))  # type: ignore[assignment]

            obs = Observation(
                observation_id=self._next_obs_id(), frame_id=frame_id,
                track_id=track_id, kind=kind, label=label, state="visible",
                model="visionrt", confidence=conf, bbox=bbox,  # type: ignore[arg-type]
                observed_at=now_iso, evidence_refs=[evidence_ref],
            )
            observations.append(obs)
            self._track_last[track_id] = {
                "label": label, "kind": kind, "confidence": conf,
                "bbox": bbox, "observed_at": now_iso, "frame_id": frame_id,
            }

            # Promotion: only confirmed tracks above the confidence floor count.
            if conf >= self.config.promote_min_confidence:
                self._track_counts[track_id] = self._track_counts.get(track_id, 0) + 1

            entity_id = self._track_to_entity.get(track_id)
            if entity_id is None and self._track_counts.get(track_id, 0) >= self.config.promote_min_observations:
                entity_id = self._promote(track_id, obs, now_iso, evidence_ref)
                promoted_now.append(self.entities[entity_id])

            if entity_id is not None:
                e = self.entities[entity_id]
                self._update_entity(e, obs, now_iso, evidence_ref)
                obs.entity_id = entity_id

        # Relations from the current frame geometry (only among visible tracks).
        frame_relations = self._derive_relations(observations, now_iso, evidence_ref)
        self.relations = frame_relations  # relations are frame-scoped
        self.metrics["relations"] = len(frame_relations)

        # Change detection: appeared / moved / disappeared (last-seen ageing).
        changes = self._detect_changes(seen_track_ids, now, now_iso, evidence_ref)

        # Persist last-seen + changes into visual_events_v19 (owner-scoped).
        self._persist_events(promoted_now, changes, now_iso)

        # Publish current world state into the REAL core tables.
        if self.publish_world_state:
            self._publish_world_state(observations, now_iso, map_quality)

        return {
            "frame_id": frame_id,
            "promoted": [e.entity_id for e in promoted_now],
            "changes": [c.to_dict() for c in changes],
            "relations": [r.to_dict() for r in frame_relations],
            "map_quality": map_quality,
        }

    def _promote(self, track_id: str, obs: Observation, now_iso: str, ev: str) -> str:
        entity_id = self._next_entity_id(obs.label)
        e = WorldEntity(
            entity_id=entity_id, kind=obs.kind, label=obs.label,
            confidence=obs.confidence, lifecycle="confirmed", track_id=track_id,
            first_seen=now_iso, last_seen=now_iso, last_bbox=obs.bbox,
            observation_count=self._track_counts.get(track_id, 1),
            evidence_refs=[ev],
        )
        self.entities[entity_id] = e
        self._track_to_entity[track_id] = entity_id
        self.metrics["entities_promoted"] += 1
        self._persist_entity(e)
        return entity_id

    def _update_entity(self, e: WorldEntity, obs: Observation, now_iso: str, ev: str) -> None:
        e.last_seen = now_iso
        e.last_bbox = obs.bbox
        e.observation_count += 1
        e.confidence = max(e.confidence, obs.confidence)
        e.lifecycle = "confirmed"
        if ev not in e.evidence_refs:
            e.evidence_refs.append(ev)
        self._persist_entity(e)

    # ---------------------------------------------------------------- relations
    def _derive_relations(
        self, observations: list[Observation], now_iso: str, ev: str
    ) -> list[Relation]:
        rels: list[Relation] = []
        cfg = self.config
        n = len(observations)
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                a, b = observations[i], observations[j]
                sid = a.entity_id or a.track_id
                oid = b.entity_id or b.track_id
                ca, cb = _center(a.bbox), _center(b.bbox)
                wa, ha = _size(a.bbox)
                mean_size = max(1e-6, (wa + ha) / 2.0)
                dist = ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5

                # holds: a person whose bbox overlaps a small object it carries.
                if a.kind == "object" and a.label == "person" and b.kind == "object" and b.label != "person":
                    if _iou(a.bbox, b.bbox) > 0 and _horizontal_overlap_frac(b.bbox, a.bbox) >= cfg.holds_person_overlap_ratio:
                        rels.append(Relation(sid, "holds", oid, now_iso, min(a.confidence, b.confidence), [ev]))
                        continue

                # on_top_of: b's bottom near a's top, with horizontal overlap.
                if _horizontal_overlap_frac(a.bbox, b.bbox) >= cfg.on_top_overlap_ratio:
                    a_top, b_bottom = a.bbox[1], b.bbox[3]
                    if 0 <= (a_top - b_bottom) < mean_size * 0.5 or _iou(a.bbox, b.bbox) > cfg.on_top_overlap_ratio:
                        if ca[1] < cb[1]:  # a is higher on screen than b
                            rels.append(Relation(sid, "on_top_of", oid, now_iso, min(a.confidence, b.confidence), [ev]))
                            continue

                # near: centres close relative to box size (report once, i<j).
                if i < j and dist <= mean_size * (1.0 / max(1e-6, cfg.near_iou_gap_ratio)):
                    rels.append(Relation(sid, "near", oid, now_iso, min(a.confidence, b.confidence), [ev]))
        return rels

    # ---------------------------------------------------------------- changes
    def _detect_changes(
        self, seen_track_ids: set[str], now: datetime, now_iso: str, ev: str
    ) -> list[ChangeEvent]:
        changes: list[ChangeEvent] = []
        for entity_id, e in self.entities.items():
            tid = e.track_id
            if tid in seen_track_ids:
                # Was it moved? Compare against the stored bbox before this update.
                prev = getattr(e, "_prev_bbox_for_change", None)
                cur = e.last_bbox
                if prev is not None and cur is not None:
                    shift = ((_center(prev)[0] - _center(cur)[0]) ** 2 + (_center(prev)[1] - _center(cur)[1]) ** 2) ** 0.5
                    if shift > _diag(cur) * self.config.moved_center_ratio:
                        changes.append(ChangeEvent(
                            "moved", entity_id, e.label, now_iso,
                            before={"bbox": [round(v, 1) for v in prev]},
                            after={"bbox": [round(v, 1) for v in cur]},
                            evidence_refs=[ev],
                        ))
                        if e.lifecycle == "last_seen":
                            e.lifecycle = "confirmed"
                e._prev_bbox_for_change = cur  # type: ignore[attr-defined]
                if e.lifecycle == "last_seen":
                    changes.append(ChangeEvent("appeared", entity_id, e.label, now_iso, after={"bbox": [round(v, 1) for v in cur] if cur else None}, evidence_refs=[ev]))
                    e.lifecycle = "confirmed"
            else:
                if e.lifecycle == "confirmed" and e.age_seconds(now) >= self.config.stale_after_seconds:
                    e.lifecycle = "last_seen"
                    self.metrics["last_seen_count"] += 1
                    changes.append(ChangeEvent(
                        "disappeared", entity_id, e.label, e.last_seen,
                        before={"bbox": [round(v, 1) for v in e.last_bbox] if e.last_bbox else None},
                        evidence_refs=e.evidence_refs[-1:] or [ev],
                    ))
        self.change_events.extend(changes)
        self.metrics["change_events"] += len(changes)
        for c in changes:
            self._svc_db.execute(
                "INSERT INTO worldbrain_session_changes(live_session_id, change_type, entity_id, label, observed_at) VALUES(?,?,?,?,?)",
                (self.live_session_id, c.change_type, c.entity_id, c.label, c.observed_at),
            )
        if changes:
            self._svc_db.commit()
        return changes

    # ---------------------------------------------------------------- attribute change
    def record_attribute_change(
        self,
        *,
        subject: str,
        attribute: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        evidence_refs: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Record an ``attribute_changed`` ChangeEvent (E38 §2).

        A value observed for a (subject, attribute) differs from a prior session's
        value — a bi-modal change (a SEEN value can contradict a HEARD one and
        vice-versa; the ``source`` on each side records which). Appended to
        ``change_events``, persisted into ``visual_events_v19`` with a truth_level
        derived from the two sources, and returned for the scene adapter to surface
        proactively if relevant. ``subject`` is used as the change's ``entity_id`` so
        it rides the same channel as spatial changes (a subject may be an entity, a
        person entity, or a place/zone key — all stable subject strings)."""
        now_iso = _iso(_utc_now())
        ev = list(evidence_refs or [])
        label = getattr(self.entities.get(subject), "label", subject) if isinstance(subject, str) else subject
        change = ChangeEvent(
            "attribute_changed", subject, str(label), now_iso,
            before={"attribute": attribute, **dict(before)},
            after={"attribute": attribute, **dict(after)},
            evidence_refs=ev,
        )
        self.change_events.append(change)
        self.metrics["change_events"] += 1
        try:
            self._svc_db.execute(
                "INSERT INTO worldbrain_session_changes(live_session_id, change_type, entity_id, label, observed_at) VALUES(?,?,?,?,?)",
                (self.live_session_id, change.change_type, subject, str(label), now_iso),
            )
            self._svc_db.commit()
        except Exception:
            pass
        # A change confirmed by two independent modalities (seen + heard) is
        # observed; a single-modality diff is probable (a re-reading could differ).
        sources = {str(before.get("source") or ""), str(after.get("source") or "")}
        truth_level = "observed" if len(sources - {""}) >= 2 else "probable"
        try:
            store = _load_store()
            store.store_visual_event({
                "memory_owner_id": self.person_id,
                "live_session_id": self.live_session_id,
                "event_type": "change_attribute_changed",
                "occurred_at": now_iso,
                "entity": {"entity_id": subject, "label": str(label), "attribute": attribute},
                "observation": {"before": change.before, "after": change.after},
                "truth_level": truth_level,
                "confidence": 0.7,
                "evidence": ev,
                "provenance": {"producer": "attribute_memory"},
            }, db_path=self.db_path)
        except Exception:
            pass
        return change.to_dict()

    # ---------------------------------------------------------------- correction
    @staticmethod
    def _norm_label(label: str | None) -> str:
        return (label or "").strip().lower()

    def suspend_label(self, label: str) -> int:
        """Suspend an object/place *label* the user corrected away (E35 §3).

        Every entity carrying this label is dropped now and filtered out of every
        subsequent snapshot/SceneDelta. Returns the number of live entities hidden.
        The label stays suspended for the session so a re-detection under the same
        (wrong) label does not resurface it."""
        norm = self._norm_label(label)
        if not norm:
            return 0
        self._suspended_labels.add(norm)
        hidden = 0
        for eid, e in list(self.entities.items()):
            if self._norm_label(e.label) == norm:
                self.entities.pop(eid, None)
                # forget the track binding so a new sighting must re-promote
                for tid, mapped in list(self._track_to_entity.items()):
                    if mapped == eid:
                        self._track_to_entity.pop(tid, None)
                        self._track_counts.pop(tid, None)
                hidden += 1
        return hidden

    def suspend_zone(self, zone: str) -> None:
        """Suspend a place/zone label the user corrected away ("on n'est pas au
        bureau"). Clears it from the current session place/active_zone and keeps it
        out of future snapshots until re-established."""
        norm = self._norm_label(zone)
        if not norm:
            return
        self._suspended_zones.add(norm)
        if self._norm_label(self.session.place_hint) == norm:
            self.session.place_hint = None
        if self._norm_label(self.session.active_zone) == norm:
            self.session.active_zone = None

    def is_label_suspended(self, label: str | None) -> bool:
        return self._norm_label(label) in self._suspended_labels

    # ---------------------------------------------------------------- last-seen
    def last_seen(self) -> list[dict[str, Any]]:
        """Every known entity with its age (visible or stale), minus any label the
        user has verbally suspended (E35 §3)."""
        now = _utc_now()
        return [e.to_dict(now) for e in self.entities.values()
                if not self.is_label_suspended(e.label)]

    def last_seen_entity(self, entity_id: str) -> WorldEntity | None:
        return self.entities.get(entity_id)

    def find_entity(self, query: str) -> WorldEntity | None:
        """Best last-seen entity whose label matches the query (for FocusSearch)."""
        q = (query or "").lower().strip()
        best: WorldEntity | None = None
        for e in self.entities.values():
            if q and q not in e.label.lower():
                continue
            if best is None or e.last_seen > best.last_seen:
                best = e
        return best

    # ---------------------------------------------------------------- persistence
    def _persist_events(
        self, promoted: list[WorldEntity], changes: list[ChangeEvent], now_iso: str
    ) -> None:
        store = _load_store()
        for e in promoted:
            store.store_visual_event({
                "memory_owner_id": self.person_id,
                "live_session_id": self.live_session_id,
                "event_type": "entity_last_seen",
                "occurred_at": e.last_seen or now_iso,
                "entity": {"entity_id": e.entity_id, "kind": e.kind, "label": e.label, "lifecycle": e.lifecycle},
                "observation": {"bbox": list(e.last_bbox) if e.last_bbox else None, "observation_count": e.observation_count},
                "truth_level": "observed",
                "confidence": e.confidence,
                "evidence": e.evidence_refs,
                "provenance": {"producer": "worldbrain"},
            }, db_path=self.db_path)
        for c in changes:
            store.store_visual_event({
                "memory_owner_id": self.person_id,
                "live_session_id": self.live_session_id,
                "event_type": f"change_{c.change_type}",
                "occurred_at": c.observed_at,
                "entity": {"entity_id": c.entity_id, "label": c.label},
                "observation": {"before": c.before, "after": c.after},
                "truth_level": "observed",
                "confidence": 0.7,
                "evidence": c.evidence_refs,
                "provenance": {"producer": "worldbrain"},
            }, db_path=self.db_path)

    def _publish_world_state(
        self, observations: list[Observation], now_iso: str, map_quality: float
    ) -> None:
        ctx = _load_visual_context()
        visible = [o.to_dict() for o in observations]
        world_state = {
            "state_time": now_iso,
            "where_am_i": self.session.place_hint,
            "who_is_active": [o["label"] for o in visible if o["label"] == "person"],
            "what_is_happening": None,
            "visual_context": {
                "visible_entities": visible,
                "map_quality": round(map_quality, 3),
                "active_zone": self.session.active_zone,
            },
            "evidence": sorted({r for o in observations for r in o.evidence_refs}),
            "confidence": 0.8,
        }
        scene_obs = [{
            "model": "worldbrain",
            "scene_summary": None,
            "location_hint": self.session.place_hint,
            "people_count": sum(1 for o in visible if o["label"] == "person"),
            "objects": [{"label": o["label"], "track_id": o["track_id"], "confidence": o["confidence"]} for o in visible],
            "confidence": 0.8,
        }] if visible else []
        try:
            ctx.publish_visual_context(
                person_id=self.person_id, live_session_id=self.live_session_id,
                world_state=world_state, observations=scene_obs, db_path=self.db_path,
            )
            self.metrics["world_state_published"] += 1
        except Exception:
            pass

    # ---------------------------------------------------------------- summary
    def end_session(self, *, place_hint: str | None = None) -> str:
        """Flush an end-of-session summary into scene_session_summaries_v19."""
        store = _load_store()
        now_iso = _iso(_utc_now())
        entities = self.last_seen()
        summary = {
            "entities": entities,
            "entity_count": len(entities),
            "change_count": len(self.change_events),
            "changes": [c.to_dict() for c in self.change_events[-50:]],
            "active_zone": self.session.active_zone,
        }
        evidence = sorted({r for e in self.entities.values() for r in e.evidence_refs})
        return store.store_scene_summary({
            "memory_owner_id": self.person_id,
            "live_session_id": self.live_session_id,
            "summary_start": self.session.started_at,
            "summary_end": now_iso,
            "place_hint": place_hint or self.session.place_hint,
            "map_quality": self.session.map_quality,
            "summary": summary,
            "evidence_refs": evidence,
        }, db_path=self.db_path)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.live_session_id,
            "place_hint": self.session.place_hint,
            "active_zone": self.session.active_zone,
            "map_quality": round(self.session.map_quality, 3),
            "entities": self.last_seen(),
            "relations": [r.to_dict() for r in self.relations],
            "recent_changes": [c.to_dict() for c in self.change_events[-10:]],
            "metrics": dict(self.metrics),
        }
