from __future__ import annotations

"""Local TTS — short spoken replies for the live glasses (E35 §1).

The live loop already speaks *to the eyes* (ContextCards / outlines). E35 adds a
voice for the short answers — « c'est quoi ça » in a capture-only / driving
context, confirmations, memory one-liners — when the profile opts in
(``tts: on``) or the user says « réponds à voix haute ».

Two real providers behind ONE interface (:class:`TTSProvider`):

* :class:`SherpaTTS` — **sherpa-onnx OfflineTts** (Piper/VITS voices, FR + EN),
  the SAME dependency family as the ASR (faster-whisper is separate, but sherpa
  is already a first-class local-inference citizen). Voices are declared in
  ``configs/MODEL_MANIFEST.yaml`` (url + sha256 + license) and fetched by
  ``scripts/fetch_models_v19.py`` — **never committed**. This is the primary path.
* :class:`Pyttsx3TTS` / :class:`WindowsSapiTTS` — the degraded fallback behind the
  **same** interface when the sherpa TTS models are not present or the package is
  unusable (ADR §E35). ``pyttsx3`` if installed, else Windows SAPI directly
  (``win32com`` → SpVoice → SpFileStream WAV). Honest degrade: a provider that
  cannot produce audio raises :class:`TTSUnavailable`, never a silent empty blob.

The public surface is intentionally tiny::

    provider = build_tts_provider(profile)          # picks sherpa, else fallback
    wav_bytes = provider.speak("Bonjour", lang="fr") # 16-bit PCM WAV bytes

:func:`tts_audio_message` wraps the bytes into a bounded ``tts_audio`` DataChannel
message (base64, capped at ``max_b64_chars``) — the device plays it, or the
companion-web viewer plays it as an audio blob. Audio is **never** streamed
unbounded over the DataChannel (interdit E35): a reply too long to fit is dropped
to a ContextCard fallback by the caller.
"""

import io
import os
import struct
import sys
import wave
from pathlib import Path
from typing import Any, Mapping, Protocol

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class TTSUnavailable(RuntimeError):
    """Raised when no provider can synthesise (missing models / package / voice)."""


# --------------------------------------------------------------------------- iface
class TTSProvider(Protocol):
    """Every TTS tier (sherpa, pyttsx3, SAPI) implements this."""

    name: str

    def speak(self, text: str, *, lang: str = "fr") -> bytes:
        """Synthesise ``text`` → 16-bit PCM mono WAV bytes. Raises TTSUnavailable."""
        ...


# --------------------------------------------------------------------------- helpers
def _pcm_to_wav(samples: Any, sample_rate: int) -> bytes:
    """Pack float [-1,1] or int16 samples into a mono 16-bit PCM WAV blob."""
    # Accept a numpy array, a list, or already-packed int16 bytes.
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(samples)
        if arr.dtype.kind == "f":
            arr = np.clip(arr, -1.0, 1.0)
            arr = (arr * 32767.0).astype("<i2")
        else:
            arr = arr.astype("<i2")
        pcm = arr.tobytes()
    except Exception:
        # Pure-python fallback: list of floats.
        pcm = b"".join(
            struct.pack("<h", max(-32768, min(32767, int(float(s) * 32767.0))))
            for s in (samples or [])
        )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate or 16000))
        w.writeframes(pcm)
    return buf.getvalue()


def _manifest_voice(lang: str) -> dict[str, Any] | None:
    """Read the sherpa TTS voice entry for ``lang`` from the model manifest."""
    manifest = _ROOT / "configs" / "MODEL_MANIFEST.yaml"
    if not manifest.exists():
        return None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    models = (data.get("models") or {}) if isinstance(data, Mapping) else {}
    key = "tts_fr" if str(lang).lower().startswith("fr") else "tts_en"
    entry = models.get(key)
    return dict(entry) if isinstance(entry, Mapping) else None


# --------------------------------------------------------------------------- sherpa
class SherpaTTS:
    """sherpa-onnx OfflineTts (Piper/VITS). Primary local voice. Lazy-loads per lang."""

    name = "sherpa"

    def __init__(self, *, num_threads: int = 1) -> None:
        self.num_threads = int(num_threads)
        self._engines: dict[str, Any] = {}  # lang → OfflineTts

    def _resolve_model_paths(self, lang: str) -> tuple[Path, Path, str] | None:
        """(model.onnx, tokens.txt, data_dir) for ``lang``, if the files exist.

        The manifest ``path`` points at the .onnx; tokens.txt + espeak-ng-data
        live beside it (the standard piper/vits sherpa layout)."""
        entry = _manifest_voice(lang)
        if not entry or not entry.get("path"):
            return None
        model = (_ROOT / str(entry["path"])).resolve()
        if not model.exists():
            return None
        tokens = model.parent / "tokens.txt"
        data_dir = model.parent / "espeak-ng-data"
        return model, tokens, str(data_dir if data_dir.exists() else "")

    def _engine(self, lang: str) -> Any:
        key = "fr" if str(lang).lower().startswith("fr") else "en"
        if key in self._engines:
            return self._engines[key]
        try:
            import sherpa_onnx  # type: ignore
        except Exception as exc:  # pragma: no cover - env without sherpa
            raise TTSUnavailable(f"sherpa-onnx not importable: {str(exc)[:120]}") from exc
        paths = self._resolve_model_paths(lang)
        if paths is None:
            raise TTSUnavailable(f"no sherpa TTS voice model for lang={lang} (fetch_models_v19)")
        model, tokens, data_dir = paths
        try:
            vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(model), tokens=str(tokens), data_dir=str(data_dir),
            )
            model_cfg = sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=self.num_threads)
            cfg = sherpa_onnx.OfflineTtsConfig(model=model_cfg)
            engine = sherpa_onnx.OfflineTts(cfg)
        except Exception as exc:
            raise TTSUnavailable(f"sherpa TTS init failed: {str(exc)[:150]}") from exc
        self._engines[key] = engine
        return engine

    def speak(self, text: str, *, lang: str = "fr") -> bytes:
        text = (text or "").strip()
        if not text:
            raise TTSUnavailable("empty text")
        engine = self._engine(lang)
        try:
            audio = engine.generate(text, sid=0, speed=1.0)
        except Exception as exc:
            raise TTSUnavailable(f"sherpa TTS generate failed: {str(exc)[:150]}") from exc
        samples = getattr(audio, "samples", None)
        sr = int(getattr(audio, "sample_rate", 22050) or 22050)
        if samples is None or len(samples) == 0:
            raise TTSUnavailable("sherpa TTS produced no audio")
        return _pcm_to_wav(samples, sr)


# --------------------------------------------------------------------------- pyttsx3
class Pyttsx3TTS:
    """pyttsx3 fallback (cross-platform driver). WAV via save_to_file."""

    name = "pyttsx3"

    def __init__(self) -> None:
        self._checked = False

    def speak(self, text: str, *, lang: str = "fr") -> bytes:
        text = (text or "").strip()
        if not text:
            raise TTSUnavailable("empty text")
        try:
            import pyttsx3  # type: ignore
        except Exception as exc:
            raise TTSUnavailable(f"pyttsx3 not importable: {str(exc)[:120]}") from exc
        import tempfile

        engine = pyttsx3.init()
        # Best-effort voice by language (fall through to default if none matches).
        try:
            want = "french" if str(lang).lower().startswith("fr") else "english"
            for v in engine.getProperty("voices"):
                if want in (getattr(v, "name", "") or "").lower():
                    engine.setProperty("voice", v.id)
                    break
        except Exception:
            pass
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            out_path = tf.name
        try:
            engine.save_to_file(text, out_path)
            engine.runAndWait()
            data = Path(out_path).read_bytes()
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        if not data:
            raise TTSUnavailable("pyttsx3 produced no audio")
        return data


# --------------------------------------------------------------------------- SAPI
class WindowsSapiTTS:
    """Windows SAPI directly via win32com (SpVoice → SpFileStream). Last resort."""

    name = "sapi"

    def speak(self, text: str, *, lang: str = "fr") -> bytes:
        text = (text or "").strip()
        if not text:
            raise TTSUnavailable("empty text")
        try:
            import win32com.client  # type: ignore
        except Exception as exc:
            raise TTSUnavailable(f"win32com not importable: {str(exc)[:120]}") from exc
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            out_path = tf.name
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            # Try to pick a voice matching the language (best-effort).
            try:
                want = "fr" if str(lang).lower().startswith("fr") else "en"
                for tok in voice.GetVoices():
                    lang_attr = (tok.GetAttribute("Language") or "").lower()
                    name_attr = (tok.GetAttribute("Name") or "").lower()
                    if want == "fr" and ("40c" in lang_attr or "french" in name_attr):
                        voice.Voice = tok
                        break
                    if want == "en" and ("409" in lang_attr or "english" in name_attr):
                        voice.Voice = tok
                        break
            except Exception:
                pass
            stream.Open(out_path, 3)  # SSFMCreateForWrite
            voice.AudioOutputStream = stream
            voice.Speak(text)
            stream.Close()
            data = Path(out_path).read_bytes()
        except Exception as exc:
            raise TTSUnavailable(f"SAPI synthesis failed: {str(exc)[:150]}") from exc
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        if not data:
            raise TTSUnavailable("SAPI produced no audio")
        return data


# --------------------------------------------------------------------------- factory
def build_tts_provider(profile: Mapping[str, Any] | None = None, *, prefer: str | None = None) -> TTSProvider:
    """Pick the best available provider: sherpa (if a voice model is present),
    else pyttsx3, else Windows SAPI. ``prefer`` forces a specific tier for tests.

    Never raises: returns a provider object even if it will later degrade — the
    caller learns availability at ``speak`` time (honest, lazy)."""
    order = [prefer] if prefer else []
    order += ["sherpa", "pyttsx3", "sapi"]
    seen: set[str] = set()
    for name in order:
        if not name or name in seen:
            continue
        seen.add(name)
        if name == "sherpa":
            # Only pick sherpa when at least one voice model is on disk, so we don't
            # hand back a provider that always fails when the models weren't fetched.
            if SherpaTTS()._resolve_model_paths("fr") or SherpaTTS()._resolve_model_paths("en"):
                return SherpaTTS()
        elif name == "pyttsx3":
            try:
                import pyttsx3  # noqa: F401  # type: ignore

                return Pyttsx3TTS()
            except Exception:
                continue
        elif name == "sapi":
            if sys.platform == "win32":
                try:
                    import win32com.client  # noqa: F401  # type: ignore

                    return WindowsSapiTTS()
                except Exception:
                    continue
    # Last resort: return a sherpa provider so ``speak`` raises a clear TTSUnavailable.
    return SherpaTTS()


# --------------------------------------------------------------------------- message
def tts_audio_message(
    wav_bytes: bytes,
    *,
    lang: str = "fr",
    text: str | None = None,
    max_b64_chars: int = 240_000,
) -> dict[str, Any] | None:
    """Wrap WAV bytes into a bounded ``tts_audio`` DataChannel message.

    Returns ``None`` when the base64 payload exceeds ``max_b64_chars`` (the reply
    is too long to send as audio — the caller falls back to a text ContextCard).
    Audio is never sent unbounded over the DataChannel (interdit E35)."""
    import base64
    import uuid

    if not wav_bytes:
        return None
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    if len(b64) > int(max_b64_chars):
        return None
    return {
        "type": "tts_audio",
        "tts_id": str(uuid.uuid4()),
        "format": "wav",
        "lang": lang,
        "text": (text or "")[:200],
        "audio_b64": b64,
        "bytes": len(wav_bytes),
    }


def profile_tts_enabled(profile: Mapping[str, Any] | None) -> bool:
    """True when the profile opts into spoken replies (``tts: on``, default off)."""
    if not isinstance(profile, Mapping):
        return False
    val = str(profile.get("tts") or "off").strip().lower()
    return val in ("on", "true", "1", "yes", "voice", "loud")
