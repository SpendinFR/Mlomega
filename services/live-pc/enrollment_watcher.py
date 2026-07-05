from __future__ import annotations

"""EnrollmentWatcher — voice enrollment / correction pre-router (E32 §4).

A *narrow, autonomous* pre-router that watches final transcripts for exactly two
identity intents (the general IntentRouter arrives in E33 — this stays simple):

* **enroll** — « retiens[,:]? c'est Sarah » / « souviens-toi de Sarah »
  (+ EN variants "remember (this is) Sarah") → capture the *current* face (best
  recent crop of the active person track) **and** the voice segment → enrol both
  galleries (face via :class:`FaceIdentity`, voice via :class:`VoiceIdentityLive`
  which reuses the core primitives when available) → confirm with a UIIntent
  ("Enregistré : Sarah").

* **correction** — « non, ce n'est pas Paul » / « oublie Paul » (+ EN "no that's
  not Paul" / "forget Paul") → suspend the label on the active track + WorldBrain
  entity, and record a durable trace via the core ``memory_correction.revise_memory``
  when a memory target exists → confirm with a UIIntent.

The watcher is fed:
  - ``on_transcript(text)`` for every final AudioRT transcript;
  - ``set_active_track(track_id, entity_id, crop_bgr)`` by the pipeline so the
    "current person" is known when a command lands;
  - ``set_active_segment(wav_path)`` for the most recent voice clip.

It emits UIIntents through an injected ``emit_ui_intent`` callback (the pipeline's
DataChannel push).
"""

import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Enrolment: "retiens[,:] c'est Sarah", "souviens-toi de Sarah",
#            "remember this is Sarah", "remember Sarah".
_NAME = r"([A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ'\-]{1,30})"
_ENROLL_PATTERNS = [
    re.compile(r"\bretiens\b[\s,:]*\b(?:c'?est|ç?a\s+c'?est)\b\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\bretiens\b[\s,:]+" + _NAME, re.IGNORECASE),
    re.compile(r"\bsouviens[\s-]?toi\b\s+(?:de\s+|d')" + _NAME, re.IGNORECASE),
    re.compile(r"\bremember\b\s+(?:this\s+is\s+|that'?s\s+)?" + _NAME, re.IGNORECASE),
]
# Correction: "non, ce n'est pas Paul", "c'est pas Paul", "oublie Paul",
#             "no that's not Paul", "forget Paul".
_CORRECTION_PATTERNS = [
    re.compile(r"\bce\s+n'?est\s+pas\b\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\bc'?est\s+pas\b\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\boublie\b\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\b(?:that'?s\s+not|not)\b\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\bforget\b\s+" + _NAME, re.IGNORECASE),
]

# Words that are never a person name even if they slot into the grammar. Includes
# determiners/possessives so "ce n'est pas mon téléphone" is NOT read as the
# person "Mon" — it falls through to the E35 object/place scene correction.
_STOP_NAMES = {"pas", "de", "ce", "que", "la", "le", "un", "une", "this", "that", "the",
               "not", "a", "an", "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa",
               "ses", "les", "des", "au", "aux", "my", "your", "his", "her"}

# E35 §3 — object/place correction. The target is a *label/place phrase*, not a
# capitalised person name: "ce n'est pas mon téléphone" (object), "on n'est pas au
# bureau" / "ce n'est pas la cuisine" (place). These are matched AFTER the person
# correction so "ce n'est pas Paul" still routes to identity (a capitalised single
# name), while a common-noun phrase routes to scene correction.
_PHRASE = r"([\wÀ-ÖØ-öø-ÿ' \-]{2,50})"
_PLACE_CORRECTION_PATTERNS = [
    re.compile(r"\bon\s+n'?est\s+pas\b\s+(?:à\s+|au\s+|dans\s+(?:la\s+|le\s+|l')?|en\s+)?" + _PHRASE, re.IGNORECASE),
    re.compile(r"\bce\s+n'?est\s+pas\b\s+(?:mon|ma|le|la|l'|un|une)\s+(?:bureau|maison|cuisine|salon|chambre|garage|jardin|salle[\w \-]*)", re.IGNORECASE),
    re.compile(r"\bwe'?re\s+not\s+(?:at|in)\b\s+(?:the\s+)?" + _PHRASE, re.IGNORECASE),
]
_OBJECT_CORRECTION_PATTERNS = [
    re.compile(r"\bce\s+n'?est\s+pas\b\s+(?:mon|ma|mes|un|une|le|la|l')\s+" + _PHRASE, re.IGNORECASE),
    re.compile(r"\bc'?est\s+pas\b\s+(?:mon|ma|mes|un|une|le|la|l')\s+" + _PHRASE, re.IGNORECASE),
    re.compile(r"\bthat'?s\s+not\b\s+(?:my|a|an|the)\s+" + _PHRASE, re.IGNORECASE),
]
# Place nouns that, when they trail an object pattern, mean it's really a place.
_PLACE_NOUNS = {"bureau", "maison", "cuisine", "salon", "chambre", "garage", "jardin",
                "office", "home", "kitchen", "bedroom", "garage", "garden", "salle"}


def _clean_phrase(raw: str | None) -> str | None:
    if not raw:
        return None
    t = re.sub(r"\s+", " ", raw.strip().strip(".,!?;:").strip())
    return t or None


def parse_scene_correction(text: str) -> dict[str, Any] | None:
    """Return {intent: correct_place|correct_object, target} for a scene
    correction ("ce n'est pas mon téléphone" / "on n'est pas au bureau"), else
    None. Person corrections (a bare capitalised name) are NOT matched here — they
    stay with :func:`parse_identity_command`."""
    t = (text or "").strip()
    if not t:
        return None
    for pat in _PLACE_CORRECTION_PATTERNS:
        m = pat.search(t)
        if m:
            target = _clean_phrase(m.group(m.lastindex) if m.lastindex else None) or _clean_phrase(m.group(0))
            if target:
                return {"intent": "correct_place", "target": target}
    for pat in _OBJECT_CORRECTION_PATTERNS:
        m = pat.search(t)
        if m:
            target = _clean_phrase(m.group(1))
            if not target:
                continue
            first = target.split()[0].lower()
            if first in _PLACE_NOUNS:
                return {"intent": "correct_place", "target": target}
            return {"intent": "correct_object", "target": target}
    return None


def _clean_name(raw: str | None) -> str | None:
    if not raw:
        return None
    name = raw.strip().strip(".,!?;:")
    if not name or name.lower() in _STOP_NAMES:
        return None
    return name[:1].upper() + name[1:]


def parse_identity_command(text: str) -> dict[str, Any] | None:
    """Return {intent: enroll|correct, name} for an identity command, else None."""
    t = (text or "").strip()
    if not t:
        return None
    for pat in _CORRECTION_PATTERNS:  # correction checked first ("pas X" ⊄ enroll)
        m = pat.search(t)
        if m:
            name = _clean_name(m.group(1))
            if name:
                return {"intent": "correct", "name": name}
    for pat in _ENROLL_PATTERNS:
        m = pat.search(t)
        if m:
            name = _clean_name(m.group(1))
            if name:
                return {"intent": "enroll", "name": name}
    return None


def _person_id_for(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "person"
    return f"live-{slug}"


class EnrollmentWatcher:
    def __init__(
        self,
        *,
        face_identity: Any = None,
        voice_identity: Any = None,
        fusion: Any = None,
        worldbrain: Any = None,
        person_id: str = "me",
        emit_ui_intent: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.face = face_identity
        self.voice = voice_identity
        self.fusion = fusion
        # E35 §3: WorldBrain handle so object/place corrections suspend the label /
        # zone (kept out of subsequent SceneDeltas) + trace revise_memory.
        self.worldbrain = worldbrain
        self.person_id = person_id
        self._emit = emit_ui_intent
        self._active_track: str | None = None
        self._active_entity: str | None = None
        self._active_crop: Any = None
        self._active_segment: Path | None = None
        self.metrics = {"enrollments": 0, "corrections": 0, "commands_seen": 0,
                        "object_corrections": 0, "place_corrections": 0}

    # ------------------------------------------------------------- context feed
    def set_active_track(self, track_id: str | None, entity_id: str | None = None, crop_bgr: Any = None) -> None:
        if track_id is not None:
            self._active_track = track_id
        if entity_id is not None:
            self._active_entity = entity_id
        if crop_bgr is not None:
            self._active_crop = crop_bgr

    def set_active_segment(self, wav_path: str | Path | None) -> None:
        self._active_segment = Path(wav_path) if wav_path else None

    def _ui(self, intent: dict[str, Any]) -> None:
        if self._emit is not None:
            try:
                self._emit(intent)
            except Exception:
                pass

    # ------------------------------------------------------------- main entry
    def on_transcript(self, text: str) -> dict[str, Any] | None:
        cmd = parse_identity_command(text)
        if cmd is not None:
            self.metrics["commands_seen"] += 1
            if cmd["intent"] == "enroll":
                return self._do_enroll(cmd["name"])
            return self._do_correct(cmd["name"])
        # E35 §3: object / place correction (no person name matched).
        scene = parse_scene_correction(text)
        if scene is not None:
            self.metrics["commands_seen"] += 1
            if scene["intent"] == "correct_place":
                return self._do_correct_place(scene["target"])
            return self._do_correct_object(scene["target"])
        return None

    # ------------------------------------------------------------- enroll
    def _do_enroll(self, name: str) -> dict[str, Any]:
        pid = _person_id_for(name)
        result: dict[str, Any] = {"intent": "enroll", "name": name, "person_id": pid,
                                  "face": None, "voice": None}
        if self.face is not None and self._active_crop is not None:
            try:
                result["face"] = self.face.enroll(pid, name, self._active_crop, source="enrollment")
            except Exception as exc:
                result["face"] = {"enrolled": False, "reason": str(exc)[:150]}
        if self.voice is not None and self._active_segment is not None:
            try:
                result["voice"] = self.voice.enroll(pid, self._active_segment, name=name)
            except Exception as exc:
                result["voice"] = {"enrolled": False, "reason": str(exc)[:150]}
        # If the current entity is known, name it immediately (latency-zero card).
        if self.fusion is not None and self._active_entity is not None:
            try:
                from identity_fusion import IdentityVerdict  # type: ignore
            except Exception:
                IdentityVerdict = None  # type: ignore
            if IdentityVerdict is not None:
                v = IdentityVerdict(entity_id=self._active_entity, track_id=self._active_track,
                                    person_id=pid, name=name, confidence=1.0, identified=True,
                                    cues=["enrollment"], reason="explicit enrollment")
                try:
                    self.fusion._track_identity[self._active_track or ""] = {
                        "person_id": pid, "name": name, "confidence": 1.0}
                    self.fusion._apply_identity(v)
                except Exception:
                    pass
        self.metrics["enrollments"] += 1
        intent = {"type": "ui_intent", "ui_intent_id": str(uuid.uuid4()), "kind": "toast",
                  "content": {"text": f"Enregistré : {name}", "level": "confirm"}}
        self._ui(intent)
        result["ui_intent"] = intent
        return result

    # ------------------------------------------------------------- correction
    def _do_correct(self, name: str) -> dict[str, Any]:
        result: dict[str, Any] = {"intent": "correct", "name": name, "suspended": False, "memory_revision": None}
        if self.fusion is not None:
            if self._active_track:
                self.fusion.suspend_track(self._active_track)
                result["suspended"] = True
            if self._active_entity:
                self.fusion.suspend_entity(self._active_entity)
                result["suspended"] = True
        # Durable trace via the core memory correction, when a memory target exists.
        rev = self._record_memory_correction(name)
        if rev:
            result["memory_revision"] = rev
        self.metrics["corrections"] += 1
        intent = {"type": "ui_intent", "ui_intent_id": str(uuid.uuid4()), "kind": "toast",
                  "content": {"text": f"Corrigé : ce n'est pas {name}", "level": "confirm"}}
        self._ui(intent)
        result["ui_intent"] = intent
        return result

    # -------------------------------------------------- object / place correction
    def _do_correct_object(self, target: str) -> dict[str, Any]:
        """« ce n'est pas mon téléphone » → suspend that object label in WorldBrain
        (dropped from subsequent SceneDeltas) + durable revise_memory trace."""
        result: dict[str, Any] = {"intent": "correct_object", "target": target,
                                  "suspended": False, "hidden": 0, "memory_revision": None}
        if self.worldbrain is not None:
            try:
                hidden = self.worldbrain.suspend_label(target)
                result["suspended"] = True
                result["hidden"] = int(hidden or 0)
            except Exception:
                pass
        rev = self._record_memory_correction(target, reason=f"correction objet: ce n'est pas {target}")
        if rev:
            result["memory_revision"] = rev
        self.metrics["object_corrections"] += 1
        intent = {"type": "ui_intent", "ui_intent_id": str(uuid.uuid4()), "kind": "toast",
                  "content": {"text": f"Corrigé : ce n'est pas {target}", "level": "confirm"}}
        self._ui(intent)
        result["ui_intent"] = intent
        return result

    def _do_correct_place(self, target: str) -> dict[str, Any]:
        """« on n'est pas au bureau » → suspend that zone/place in WorldBrain +
        durable revise_memory trace."""
        result: dict[str, Any] = {"intent": "correct_place", "target": target,
                                  "suspended": False, "memory_revision": None}
        if self.worldbrain is not None:
            try:
                self.worldbrain.suspend_zone(target)
                result["suspended"] = True
            except Exception:
                pass
        rev = self._record_memory_correction(target, reason=f"correction lieu: on n'est pas {target}")
        if rev:
            result["memory_revision"] = rev
        self.metrics["place_corrections"] += 1
        intent = {"type": "ui_intent", "ui_intent_id": str(uuid.uuid4()), "kind": "toast",
                  "content": {"text": f"Corrigé : on n'est pas {target}", "level": "confirm"}}
        self._ui(intent)
        result["ui_intent"] = intent
        return result

    def _record_memory_correction(self, name: str, *, reason: str | None = None) -> dict[str, Any] | None:
        """Best-effort durable trace: invalidate an atomic identity memory of ``name``.

        Uses the core ``memory_correction.revise_memory`` (never reimplemented).
        If no matching memory target exists (fresh live-only name), returns None —
        the label suspension above is still the operative correction.
        """
        try:
            from mlomega_audio_elite.memory_correction import revise_memory  # type: ignore
            from mlomega_audio_elite.db import connect  # type: ignore
        except Exception:
            return None
        try:
            with connect() as con:
                row = con.execute(
                    "SELECT memory_id FROM atomic_memories WHERE content LIKE ? ORDER BY rowid DESC LIMIT 1",
                    (f"%{name}%",),
                ).fetchone()
            if not row:
                return None
            memory_id = row["memory_id"] if hasattr(row, "keys") else row[0]
            return revise_memory(
                target_table="atomic_memories", target_id=memory_id,
                revision_type="invalidate",
                reason=reason or f"voice correction: ce n'est pas {name}",
                person_id=self.person_id,
            )
        except Exception:
            return None
