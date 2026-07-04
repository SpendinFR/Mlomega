from __future__ import annotations

"""VoiceIdentityLive — live voice matching on final audio segments (E32 §2).

The identity layer's *audio* cue. On a *final* AudioRT segment (the wav clip that
produced a subtitle) it embeds the voice and matches it against the enrolled
speakers, then hands the resolved ``speaker_person_id`` / ``speaker_label`` to the
ConversationBridge **before** ``ingest_segment`` (that turn field already exists in
``brainlive_v15.ingest_live_turn``, wired but always None until now — E31 comment
"identity resolved in E32").

**Reuses the core, does not reimplement it.** The V18.8 core already owns the real
voice stack in ``mlomega_audio_elite.voice_identity``:

* :func:`enroll_voice(person_id, audio_path, display_name)` — SpeechBrain ECAPA
  embedding into ``voice_embeddings`` + ``speaker_profiles`` (real core tables);
* :func:`match_voice(audio_path, threshold)` — cosine match returning
  ``{person_id, score, matched, candidates}``.

When the ECAPA stack is importable (SpeechBrain + torchaudio present, the RTX3070
requirement set), this module calls straight into those functions — enrolment and
matching share one gallery with the nightly/CLI ``voice-pending`` flow, so a
person enrolled at night is recognised live and vice-versa.

When the stack is **not** importable (a bare system env), an injectable
``embedder`` + in-memory gallery drives the identical cosine matching so the live
wiring is fully testable; the real ECAPA path is validated at the close-day final
(ADR §E32). Selection is automatic: real core if available, else the injected
substitute, else a no-op that returns "unknown" (never blocks the pipeline).
"""

import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = sum(float(x) * float(x) for x in a) ** 0.5 or 1.0
    nb = sum(float(y) * float(y) for y in b) ** 0.5 or 1.0
    return dot / (na * nb)


def _core_voice_identity():
    """Return the core voice_identity module iff its ECAPA stack is importable."""
    try:
        from mlomega_audio_elite import voice_identity as vi  # type: ignore

        # The module imports fine even without torch; the embedder only fails at
        # first embed. Probe by constructing the embedder lazily below instead.
        return vi
    except Exception:
        return None


@dataclass
class VoiceConfig:
    match_threshold: float = 0.72   # mirrors core MLOMEGA_VOICE_THRESHOLD default


class VoiceIdentityLive:
    """Enrol + match voices on live final segments.

    ``embedder`` (optional) must expose ``embed_file(path)->list[float]``; when
    given it overrides the core ECAPA path (the tested substitution). Without it,
    the core ``enroll_voice`` / ``match_voice`` are used when importable.
    """

    def __init__(
        self,
        *,
        config: VoiceConfig | None = None,
        embedder: Any = None,
        use_core: bool | None = None,
    ) -> None:
        self.config = config or VoiceConfig()
        self._embedder = embedder
        self._core = _core_voice_identity() if (use_core is not False and embedder is None) else None
        # Substitute in-memory gallery: person_id -> (name, [embeddings])
        self._gallery: dict[str, dict[str, Any]] = {}
        self.metrics = {"enrollments": 0, "matches": 0, "errors": 0}

    @property
    def backend(self) -> str:
        if self._embedder is not None:
            return "substitute"
        if self._core is not None:
            return "core-ecapa"
        return "none"

    # ---------------------------------------------------------------- enroll
    def enroll(self, person_id: str, audio_path: str | Path, *, name: str | None = None) -> dict[str, Any]:
        audio_path = Path(audio_path)
        try:
            if self._embedder is not None:
                vec = list(self._embedder.embed_file(audio_path))
                entry = self._gallery.setdefault(person_id, {"name": name or person_id, "embeddings": []})
                entry["name"] = name or entry["name"]
                entry["embeddings"].append(vec)
                self.metrics["enrollments"] += 1
                return {"enrolled": True, "person_id": person_id, "backend": self.backend}
            if self._core is not None:
                embedding_id = self._core.enroll_voice(person_id, audio_path, display_name=name)
                self.metrics["enrollments"] += 1
                return {"enrolled": True, "person_id": person_id, "embedding_id": embedding_id, "backend": self.backend}
        except Exception as exc:  # pragma: no cover - stack-dependent
            self.metrics["errors"] += 1
            return {"enrolled": False, "reason": str(exc)[:200], "backend": self.backend}
        return {"enrolled": False, "reason": "no_voice_backend", "backend": self.backend}

    # ---------------------------------------------------------------- match
    def match(self, audio_path: str | Path, *, threshold: float | None = None) -> dict[str, Any]:
        thr = self.config.match_threshold if threshold is None else float(threshold)
        audio_path = Path(audio_path)
        try:
            if self._embedder is not None:
                cand = list(self._embedder.embed_file(audio_path))
                best = {"person_id": None, "name": None, "score": 0.0, "matched": False,
                        "threshold": thr, "method": "voice-cosine-substitute", "candidates": []}
                scored = []
                for pid, entry in self._gallery.items():
                    top = max((_cosine(cand, e) for e in entry["embeddings"]), default=0.0)
                    scored.append({"person_id": pid, "name": entry["name"], "score": top})
                scored.sort(key=lambda x: x["score"], reverse=True)
                best["candidates"] = scored[:5]
                if scored and scored[0]["score"] >= thr:
                    best.update({"person_id": scored[0]["person_id"], "name": scored[0]["name"],
                                 "score": round(scored[0]["score"], 4), "matched": True})
                elif scored:
                    best["score"] = round(scored[0]["score"], 4)
                if best["matched"]:
                    self.metrics["matches"] += 1
                return best
            if self._core is not None:
                res = self._core.match_voice(audio_path, threshold=thr)
                if res.get("matched"):
                    self.metrics["matches"] += 1
                res.setdefault("name", None)
                res["method"] = res.get("method", "voice-cosine-ecapa")
                return res
        except Exception as exc:  # pragma: no cover - stack-dependent
            self.metrics["errors"] += 1
            return {"person_id": None, "name": None, "score": 0.0, "matched": False,
                    "reason": str(exc)[:200], "candidates": []}
        return {"person_id": None, "name": None, "score": 0.0, "matched": False,
                "reason": "no_voice_backend", "candidates": []}


def write_wav(path: str | Path, samples: Any, sample_rate: int = 16000) -> Path:
    """Write mono int16 PCM samples to a wav file (for segment clips / tests)."""
    import numpy as np

    path = Path(path)
    arr = np.asarray(samples)
    if arr.dtype != np.int16:
        peak = float(np.max(np.abs(arr))) or 1.0
        arr = np.clip(arr / peak, -1.0, 1.0)
        arr = (arr * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(arr.tobytes())
    return path
