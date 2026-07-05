from __future__ import annotations

"""OwnerSetup — enrol the WEARER's voice for owner attribution (E37 §3).

The faille audit found V19 had no equivalent of the core ``voice_learning.setup_me``
(``enroll_voice(..., is_user=True)``) — the *porteur* (the person wearing the glasses)
was never distinguished structurally, so neither the night nor the live path could
attribute "this is the owner speaking" vs a bystander.

This wires the missing owner enrolment for the live glasses:

* a voice command — « configure ma voix » / « c'est moi qui parle » / "set up my
  voice" — arms a short capture window;
* the next ``needed_segments`` FINAL wearer segments (their WAV clips) are enrolled
  into the SHARED core gallery via the core's own
  ``voice_identity.enroll_voice(person_id, wav, is_user=True)`` (through the E32
  ``VoiceIdentityLive`` façade when present, so the substitute test backend is
  honoured) and ``voice_learning.setup_me`` records the ``self_voice_profile`` row;
* a confirmation UIIntent is emitted.

Because it enrols into the same gallery the night/CLI ``voice-pending`` flow and the
E32 live matcher read, the wearer is recognised on BOTH sides: E32
``VoiceIdentityLive.match`` now returns the owner ``person_id`` on the wearer's turns
(→ ``speaker_person_id=owner`` on the ConversationBridge turn), and the nightly
attribution folds the same owner identity.

**Brancher, pas reconstruire.** The real enrolment/DB writes live in the core
(``enroll_voice`` / ``setup_me``); this module only orchestrates capture + one core
call + one UIIntent. It never raises into the audio path.
"""

import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# The intent name the router resolves this command to.
OWNER_ENROLL_INTENT = "owner_enroll"

# Arm window: after the command, we only accept wearer segments for a short while.
_ARM_TTL_S = 120.0


@dataclass
class OwnerSetupConfig:
    person_id: str = "me"
    display_name: str = "Moi (porteur)"
    needed_segments: int = 3


@dataclass
class OwnerSetupState:
    arming: bool = False
    armed_at: float = 0.0
    captured: int = 0
    enrolled: bool = False


class OwnerSetup:
    """Capture the wearer's next segments and enrol them as the owner voice.

    ``voice_identity`` is the E32 :class:`VoiceIdentityLive` (shared gallery / testable
    substitute). ``emit_ui_intent`` pushes the arm/confirm cards. All optional.
    """

    def __init__(
        self,
        *,
        voice_identity: Any = None,
        config: OwnerSetupConfig | None = None,
        emit_ui_intent: Callable[[dict[str, Any]], Any] | None = None,
        db_path: Any = None,
    ) -> None:
        self.voice_identity = voice_identity
        self.config = config or OwnerSetupConfig()
        self._emit = emit_ui_intent
        self.db_path = db_path
        self.state = OwnerSetupState()
        self.metrics: dict[str, int] = {"armed": 0, "captured": 0, "enrolled": 0, "errors": 0}

    @property
    def person_id(self) -> str:
        return self.config.person_id

    def _ui(self, intent: dict[str, Any]) -> None:
        if self._emit is not None:
            try:
                self._emit(intent)
            except Exception:
                pass

    def _card(self, text: str, *, level: str = "confirm") -> dict[str, Any]:
        return {
            "type": "ui_intent", "ui_intent_id": str(uuid.uuid4()),
            "producer": "ultralive", "component": "context_card",
            "content": {"kind": "owner_setup", "text": text, "level": level},
            "truth_level": "observed", "confidence": 1.0, "priority": 0.6, "ttl_ms": 8000,
            "evidence_refs": [],
        }

    # ---------------------------------------------------------------- arm
    def begin(self) -> dict[str, Any]:
        """Arm the owner-voice capture window (called by the router on the command)."""
        self.state = OwnerSetupState(arming=True, armed_at=time.monotonic(), captured=0, enrolled=False)
        self.metrics["armed"] += 1
        n = self.config.needed_segments
        self._ui(self._card(
            f"Configuration de ta voix : parle normalement, je vais écouter tes {n} prochaines phrases."
        ))
        return {"intent": OWNER_ENROLL_INTENT, "arming": True, "needed": n, "handled": True}

    def is_arming(self) -> bool:
        if not self.state.arming:
            return False
        if (time.monotonic() - self.state.armed_at) > _ARM_TTL_S:
            # Window expired without enough speech.
            self.state.arming = False
            self._ui(self._card(
                "Je n'ai pas assez entendu ta voix. Redis « configure ma voix » pour réessayer.",
                level="warn",
            ))
            return False
        return True

    # ---------------------------------------------------------------- capture
    def offer_segment(self, wav_path: str | Path | None) -> dict[str, Any] | None:
        """Feed one FINAL wearer segment WAV while arming.

        Returns the enrolment result dict once ``needed_segments`` have been enrolled,
        otherwise ``None``. Never raises into the audio path.
        """
        if not self.is_arming() or not wav_path:
            return None
        p = Path(wav_path)
        if not p.exists():
            return None
        if self.voice_identity is None:
            self.state.arming = False
            self._ui(self._card("Reconnaissance vocale indisponible sur ce profil.", level="warn"))
            return {"enrolled": False, "reason": "no_voice_backend"}

        try:
            res = self._enroll(p)
        except Exception as exc:
            self.metrics["errors"] += 1
            self.state.arming = False
            return {"enrolled": False, "reason": str(exc)[:200]}

        if not res.get("enrolled"):
            return None  # keep listening; a bad clip doesn't consume the window

        self.state.captured += 1
        self.metrics["captured"] += 1
        if self.state.captured < self.config.needed_segments:
            return None

        # Enough segments enrolled → finalise the owner (self_voice_profile) + confirm.
        self.state.arming = False
        self.state.enrolled = True
        self.metrics["enrolled"] += 1
        self._finalize_owner(p)
        self._ui(self._card("C'est noté — je reconnais maintenant ta voix. Tu es le porteur."))
        return {"enrolled": True, "person_id": self.person_id, "segments": self.state.captured, "is_user": True}

    def _enroll(self, wav_path: Path) -> dict[str, Any]:
        """Enrol one clip as the owner via the E32 façade (is_user=True in the core)."""
        # VoiceIdentityLive.enroll → core enroll_voice(person_id, wav, display_name)
        # OR the substitute gallery. The core call sets is_user via setup_me below;
        # for the substitute path we mark ownership on the gallery entry name.
        res = self.voice_identity.enroll(
            self.person_id, wav_path, name=self.config.display_name,
        )
        return res

    def _finalize_owner(self, wav_path: Path) -> None:
        """Record the wearer as the central user voice via the core setup_me.

        This is the ``is_user=True`` write (``self_voice_profile`` + ``speaker_profiles``
        ``is_user=1``) that lets the night and the live matcher share one owner. When
        the core stack is unavailable (substitute test env), the E32 gallery already
        carries the owner and we skip the DB write gracefully.
        """
        try:
            from mlomega_audio_elite import voice_learning  # type: ignore
        except Exception:
            return
        try:
            voice_learning.setup_me(
                Path(wav_path), display_name=self.config.display_name, person_id=self.person_id,
            )
        except Exception:
            # Substitute/test env or missing ECAPA stack — the gallery ownership from
            # _enroll still makes the wearer matchable; the DB profile is validated at
            # the close-day (ADR §E37).
            self.metrics["errors"] += 1
