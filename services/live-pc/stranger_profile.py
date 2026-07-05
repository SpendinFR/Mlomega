from __future__ import annotations

"""StrangerProfiler — provisional VLM description of an unidentified person (E36 §3).

When a *person* track stays visible and stays anonymous (not named by
:class:`IdentityFusion`) for longer than ``stable_seconds``, the profiler takes
**one** crop and asks the local VLM (the existing ``VisionRT`` un-job Ollama path)
for a short structured description of *appearance* — never a name:

    {"appearance": "homme, ~40 ans", "clothing": "tablier blanc",
     "age_apparent": "40s", "role_hint": "probablement boulanger"}

From that it labels the WorldBrain person entity with a **description** (e.g.
"? boulanger") at ``truth_level=inferred`` (§17.2 — a description is a hypothesis,
JAMAIS a name), and pushes an ``entity_hot_update`` to the device so the PersonTag
shows the hypothesis ("? boulanger", styled as a guess, §17.2).

**Fusionnable** (E32): if the user later enrolls the person ("retiens, c'est
Karim"), :meth:`fuse_into_named` folds the provisional description into the now
named entity as a durable attribute (``description`` kept, ``truth_level`` of the
name is ``observed``) and the provisional standalone marker is retired — no more
"? boulanger" hovering next to "Karim".

Cadence / dedup: **at most one VLM profile per person-track per session**. A track
that is already named, or already profiled, or whose VLM job is refused (Ollama
off / GPU pressure) is skipped honestly — nothing is invented.

Degraded honesty: the VLM path is the same one-job-at-a-time ``VlmCrop`` as the
rest of VisionRT; when it is unavailable the profiler records the attempt and
degrades (no entity, no hot update) rather than fabricating a description.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# The prompt asks the VLM for a compact JSON appearance description — never a name.
_VLM_PROMPT = (
    "Describe ONLY the visible appearance of the person in this image as compact JSON "
    "with keys appearance, clothing, age_apparent, role_hint. "
    "role_hint is a cautious guess of their role from visible cues "
    "(e.g. an apron suggests a baker/cook), prefixed with 'probably'. "
    "Do NOT guess or state a personal name. Answer with JSON only."
)


@dataclass
class StrangerConfig:
    stable_seconds: float = 4.0        # a track must be anonymous+visible this long
    max_label_chars: int = 40          # provisional label budget
    min_confidence: float = 0.0        # VLM description confidence (inferred, low)


@dataclass
class StrangerProfile:
    """A provisional, name-less description of an anonymous person track."""

    track_id: str
    entity_id: str | None
    description: str                    # the human-facing "? boulanger" style label
    attributes: dict[str, Any]         # structured {appearance, clothing, age_apparent, role_hint}
    created_at: str = field(default_factory=_iso_now)
    truth_level: str = "inferred"      # a description is a hypothesis, never a name
    fused_into: str | None = None      # person_id once enrolled/fused
    vlm_status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "entity_id": self.entity_id,
            "description": self.description,
            "attributes": dict(self.attributes),
            "truth_level": self.truth_level,
            "fused_into": self.fused_into,
            "created_at": self.created_at,
            "vlm_status": self.vlm_status,
        }


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).strip(".,;:!?").strip()


def parse_vlm_description(text: str | None) -> dict[str, Any]:
    """Parse the VLM reply into ``{appearance, clothing, age_apparent, role_hint}``.

    The VLM may return JSON (preferred) or free prose; we extract the first JSON
    object if present, else keep the prose as ``appearance``. Any personal name is
    NOT extracted — the schema has no name field."""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return {
                    "appearance": _clean(obj.get("appearance")),
                    "clothing": _clean(obj.get("clothing")),
                    "age_apparent": _clean(obj.get("age_apparent")),
                    "role_hint": _clean(obj.get("role_hint")),
                }
        except (ValueError, TypeError):
            pass
    return {"appearance": _clean(text)[:120]}


def description_label(attrs: Mapping[str, Any], *, max_chars: int = 40) -> str:
    """Human-facing provisional label — a hypothesis, e.g. "? boulanger".

    Prefers the role hint (the most useful "who is this" cue), else clothing, else
    appearance. Always prefixed with "? " so the device renders it as a guess."""
    role = _clean(attrs.get("role_hint"))
    if role:
        # trim a leading "probably/probablement" — the "? " already marks the guess
        role = re.sub(r"^(?:probably|probablement|likely|sans doute)\s+", "", role, flags=re.IGNORECASE)
    core = role or _clean(attrs.get("clothing")) or _clean(attrs.get("appearance")) or "inconnu"
    label = f"? {core}"
    return label[:max_chars]


class StrangerProfiler:
    """Tracks anonymous person tracks and profiles the persistent ones (once each)."""

    def __init__(
        self,
        *,
        vlm: Any = None,                                   # VisionRT.VlmCrop (describe)
        worldbrain: Any = None,
        config: StrangerConfig | None = None,
        on_entity_hot_update: Callable[[dict[str, Any]], Any] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.vlm = vlm
        self.worldbrain = worldbrain
        self.config = config or StrangerConfig()
        self._on_hot = on_entity_hot_update
        import time as _time
        self._now = now_fn or _time.monotonic
        # track_id -> monotonic first-seen-anonymous timestamp
        self._first_anon: dict[str, float] = {}
        # profiles keyed by track_id (dedup: one VLM profile per track per session)
        self.profiles: dict[str, StrangerProfile] = {}
        self._profiled_tracks: set[str] = set()
        self.metrics = {
            "tracks_watched": 0,
            "profiles_created": 0,
            "vlm_calls": 0,
            "vlm_unavailable": 0,
            "fused": 0,
            "hot_updates": 0,
        }

    # ----------------------------------------------------------------- observe
    def observe_track(
        self,
        track_id: str,
        *,
        entity_id: str | None,
        is_person: bool,
        is_named: bool,
        crop_bgr: Any = None,
        now: float | None = None,
    ) -> StrangerProfile | None:
        """Feed the current status of a person track.

        Creates a provisional profile the first time an *anonymous* person track
        has been continuously visible for ``stable_seconds`` and a crop is
        available. Returns the new profile, or None (not yet stable / named /
        already profiled / VLM unavailable)."""
        if not track_id or not is_person:
            return None
        now = self._now() if now is None else now
        # A named track never gets a provisional description; clear any pending timer.
        if is_named:
            self._first_anon.pop(track_id, None)
            return None
        if track_id in self._profiled_tracks:
            return None
        first = self._first_anon.get(track_id)
        if first is None:
            self._first_anon[track_id] = now
            self.metrics["tracks_watched"] += 1
            return None
        if (now - first) < self.config.stable_seconds:
            return None
        # Stable & anonymous long enough → attempt ONE VLM profile (dedup on entry).
        if crop_bgr is None:
            return None
        return self._profile(track_id, entity_id, crop_bgr)

    # ----------------------------------------------------------------- profile
    def _profile(self, track_id: str, entity_id: str | None, crop_bgr: Any) -> StrangerProfile | None:
        # Mark as profiled up-front so a busy/refused VLM does not re-fire every frame.
        self._profiled_tracks.add(track_id)
        self.metrics["vlm_calls"] += 1
        result: dict[str, Any] = {"status": "vlm_unavailable", "text": None}
        if self.vlm is not None:
            try:
                result = self.vlm.describe(crop_bgr, prompt=_VLM_PROMPT)
            except Exception:
                result = {"status": "vlm_error", "text": None}
        status = str(result.get("status") or "vlm_unavailable")
        if status != "ok" or not result.get("text"):
            self.metrics["vlm_unavailable"] += 1
            return None  # honest degrade — no invented description
        attrs = parse_vlm_description(result.get("text"))
        if not any(attrs.values()):
            self.metrics["vlm_unavailable"] += 1
            return None
        label = description_label(attrs, max_chars=self.config.max_label_chars)
        profile = StrangerProfile(
            track_id=track_id, entity_id=entity_id, description=label,
            attributes=attrs, truth_level="inferred", vlm_status=status,
        )
        self.profiles[track_id] = profile
        self.metrics["profiles_created"] += 1
        self._label_worldbrain_entity(entity_id, profile)
        self._push_hot_update(profile)
        return profile

    def _label_worldbrain_entity(self, entity_id: str | None, profile: StrangerProfile) -> None:
        """Attach the provisional description to the WorldBrain entity (inferred).

        Sets a name-less ``description`` + ``truth_level=inferred`` on the entity so
        the next SceneDelta carries the hypothesis. Never sets ``person_name``."""
        if not entity_id or self.worldbrain is None:
            return
        ent = getattr(self.worldbrain, "entities", {}).get(entity_id)
        if ent is None:
            return
        try:
            ent.description = profile.description            # type: ignore[attr-defined]
            ent.description_attributes = dict(profile.attributes)  # type: ignore[attr-defined]
            ent.description_truth_level = "inferred"         # type: ignore[attr-defined]
        except Exception:
            pass

    def _push_hot_update(self, profile: StrangerProfile) -> None:
        """Push a hypothesis PersonTag to the device (§17.2 styled as a guess)."""
        if self._on_hot is None:
            return
        message = {
            "type": "entity_hot_update",
            "kind": "person",
            "entity_id": profile.entity_id,
            "track_id": profile.track_id,
            "person_id": None,               # never a name for a stranger
            "name": None,
            "description": profile.description,   # "? boulanger"
            "attributes": dict(profile.attributes),
            "truth_level": "inferred",           # device renders as a hypothesis
            "as_of": _iso_now(),
        }
        try:
            self._on_hot(message)
            self.metrics["hot_updates"] += 1
        except Exception:
            pass

    # ----------------------------------------------------------------- fusion
    def fuse_into_named(
        self, *, track_id: str | None = None, entity_id: str | None = None,
        person_id: str, name: str,
    ) -> StrangerProfile | None:
        """Fold a provisional profile into a now-named entity (E32 enrollment).

        The description is kept as a durable attribute on the named entity; the
        standalone "? …" hypothesis is retired (a fused profile is no longer pushed
        as a guess). Returns the fused profile, or None if there was none."""
        profile = None
        if track_id and track_id in self.profiles:
            profile = self.profiles[track_id]
        elif entity_id:
            profile = next((p for p in self.profiles.values() if p.entity_id == entity_id), None)
        if profile is None:
            return None
        profile.fused_into = person_id
        self.metrics["fused"] += 1
        # Keep the description as an attribute on the named WorldBrain entity.
        target_eid = entity_id or profile.entity_id
        if target_eid and self.worldbrain is not None:
            ent = getattr(self.worldbrain, "entities", {}).get(target_eid)
            if ent is not None:
                try:
                    ent.person_id = person_id            # type: ignore[attr-defined]
                    ent.person_name = name               # type: ignore[attr-defined]
                    ent.description = profile.description  # kept as attribute
                    ent.description_attributes = dict(profile.attributes)  # type: ignore[attr-defined]
                    ent.description_truth_level = "observed"  # now backed by a name
                except Exception:
                    pass
        # Tell the device the entity is now named (the "?" hypothesis is superseded).
        if self._on_hot is not None:
            try:
                self._on_hot({
                    "type": "entity_hot_update", "kind": "person",
                    "entity_id": target_eid, "track_id": profile.track_id,
                    "person_id": person_id, "name": name,
                    "description": profile.description, "attributes": dict(profile.attributes),
                    "truth_level": "observed", "as_of": _iso_now(),
                })
                self.metrics["hot_updates"] += 1
            except Exception:
                pass
        return profile

    def profile_for_track(self, track_id: str) -> StrangerProfile | None:
        return self.profiles.get(track_id)

    def forget_track(self, track_id: str) -> None:
        """Drop timers/state for a track that has left the scene for good."""
        self._first_anon.pop(track_id, None)
