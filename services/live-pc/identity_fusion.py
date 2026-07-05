from __future__ import annotations

"""IdentityFusion — multi-cue identity decision (E32 §3).

Combines the three identity cues into one confident (or deliberately anonymous)
verdict per person track:

* **face** — a ``FaceIdentity.match`` result on the best recent crop of the track;
* **voice** — a ``VoiceIdentityLive.match`` result on the current speech segment;
* **track persistence** — once a track has been named, the name sticks to that
  ``track_id`` for the rest of the session (a face is not re-embedded every frame).

Decision (§17.2 — never a name under confidence):

* both cues present and **agree** (same person_id) → high confidence, named;
* a single strong cue above its own threshold → named at that cue's confidence;
* cues present but **disagree** (different person_id) → **anonymous** (a
  contradiction is never resolved to a guess);
* nothing above threshold → anonymous.

On a confident verdict it **names the WorldBrain person entity**: it sets the
entity ``label``/name via the scene adapter's ``known_people`` map (label →
{name, relation, person_id}) keyed by ``entity_id``. The scene adapter's existing
§12.4 trigger (``p.get("identified") and p.get("name")``) then fires the
ContextCard with no further wiring. The name is also written back onto the
WorldBrain entity so it rides the next ``SceneDelta`` → the device PersonTag shows
it.

This is a pure decision object — it holds no models. ``resolve`` takes the cue
results (already computed by the pipeline at an economical cadence) so tests and
the pipeline exercise the same logic.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FusionConfig:
    face_threshold: float = 0.363     # SFace cosine (single-cue naming floor)
    voice_threshold: float = 0.72     # ECAPA cosine (single-cue naming floor)
    both_agree_bonus: float = 0.15    # confidence bump when face+voice agree
    min_name_confidence: float = 0.45  # overall floor to display a name (§17.2)


@dataclass
class IdentityVerdict:
    entity_id: str | None
    track_id: str | None
    person_id: str | None
    name: str | None
    confidence: float
    identified: bool
    cues: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "track_id": self.track_id,
            "person_id": self.person_id,
            "name": self.name,
            "confidence": round(float(self.confidence), 4),
            "identified": self.identified,
            "cues": list(self.cues),
            "reason": self.reason,
        }


class IdentityFusion:
    """Fuses face + voice + track-persistence into a named-or-anonymous verdict."""

    def __init__(
        self,
        *,
        config: FusionConfig | None = None,
        worldbrain: Any = None,
        scene_adapter: Any = None,
    ) -> None:
        self.config = config or FusionConfig()
        self.worldbrain = worldbrain
        self.scene_adapter = scene_adapter
        # Sticky per-track identity within the session (track persistence cue).
        self._track_identity: dict[str, dict[str, Any]] = {}
        self.metrics = {"identity_matches": 0, "named_entities": 0, "contradictions": 0, "anonymous": 0}

    # ---------------------------------------------------------------- resolve
    def resolve(
        self,
        *,
        entity_id: str | None = None,
        track_id: str | None = None,
        face: dict[str, Any] | None = None,
        voice: dict[str, Any] | None = None,
    ) -> IdentityVerdict:
        cfg = self.config
        cues: list[str] = []
        face_pid = face_name = None
        face_score = 0.0
        if face and face.get("matched"):
            face_pid = face.get("person_id")
            face_name = face.get("name")
            face_score = float(face.get("score") or 0.0)
            cues.append("face")
        voice_pid = voice_name = None
        voice_score = 0.0
        if voice and voice.get("matched"):
            voice_pid = voice.get("person_id")
            voice_name = voice.get("name")
            voice_score = float(voice.get("score") or 0.0)
            cues.append("voice")

        person_id = name = None
        confidence = 0.0
        reason = "no_cue"

        if face_pid and voice_pid:
            if face_pid == voice_pid:
                person_id = face_pid
                name = face_name or voice_name
                confidence = min(1.0, max(face_score, voice_score) + cfg.both_agree_bonus)
                reason = "face+voice agree"
            else:
                # Contradiction: two identities claimed → stay anonymous (§17.2).
                self.metrics["contradictions"] += 1
                reason = "face/voice disagree"
                cues.append("track") if track_id in self._track_identity else None
        elif face_pid:
            person_id, name, confidence, reason = face_pid, face_name, face_score, "face only"
        elif voice_pid:
            person_id, name, confidence, reason = voice_pid, voice_name, voice_score, "voice only"

        # Track persistence: a previously-named track keeps its name when the
        # current frame has no fresh cue (economical cadence), but never overrides
        # a live contradiction.
        if person_id is None and reason != "face/voice disagree":
            sticky = self._track_identity.get(track_id or "")
            if sticky:
                person_id, name, confidence = sticky["person_id"], sticky["name"], sticky["confidence"]
                cues.append("track")
                reason = "track persistence"

        identified = bool(person_id) and confidence >= cfg.min_name_confidence
        verdict = IdentityVerdict(
            entity_id=entity_id, track_id=track_id,
            person_id=person_id if identified else None,
            name=name if identified else None,
            confidence=confidence, identified=identified, cues=cues, reason=reason,
        )

        if identified:
            self.metrics["identity_matches"] += 1
            if track_id:
                self._track_identity[track_id] = {
                    "person_id": person_id, "name": name, "confidence": confidence,
                }
            self._apply_identity(verdict)
        else:
            self.metrics["anonymous"] += 1
        return verdict

    # ---------------------------------------------------------------- apply
    def _apply_identity(self, verdict: IdentityVerdict) -> None:
        """Name the WorldBrain person entity + feed the scene adapter's map."""
        if verdict.entity_id and self.scene_adapter is not None:
            try:
                self.scene_adapter.known_people[verdict.entity_id] = {
                    "name": verdict.name, "person_id": verdict.person_id,
                    "relation": None, "confidence": verdict.confidence,
                }
                self.metrics["named_entities"] += 1
            except Exception:
                pass
            # E34 §5: prefetch the person's relation pack to the device SceneCache
            # so the ContextCard renders from the local cache with zero round-trip.
            prefetch = getattr(self.scene_adapter, "prefetch_relation_pack", None)
            if callable(prefetch):
                try:
                    prefetch(entity_id=verdict.entity_id, person_id=verdict.person_id, name=verdict.name)
                except Exception:
                    pass
        # Write the name onto the WorldBrain entity so the next SceneDelta / device
        # PersonTag carries it (identity in the entity label).
        if verdict.entity_id and self.worldbrain is not None:
            ent = getattr(self.worldbrain, "entities", {}).get(verdict.entity_id)
            if ent is not None:
                try:
                    ent.person_id = verdict.person_id  # type: ignore[attr-defined]
                    ent.person_name = verdict.name      # type: ignore[attr-defined]
                except Exception:
                    pass

    # ---------------------------------------------------------------- correction
    def suspend_track(self, track_id: str) -> None:
        """Drop the sticky name for a track (voice correction 'non, pas X')."""
        self._track_identity.pop(track_id or "", None)

    def suspend_entity(self, entity_id: str) -> None:
        """Remove a name from a WorldBrain entity + scene adapter map (correction)."""
        if self.scene_adapter is not None:
            self.scene_adapter.known_people.pop(entity_id, None)
        if self.worldbrain is not None:
            ent = getattr(self.worldbrain, "entities", {}).get(entity_id)
            if ent is not None:
                try:
                    ent.person_id = None      # type: ignore[attr-defined]
                    ent.person_name = None    # type: ignore[attr-defined]
                except Exception:
                    pass
