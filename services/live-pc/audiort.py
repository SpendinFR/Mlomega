from __future__ import annotations

"""AudioRT — the PC live-audio pipeline (handoff §3.2/§3.6, reflex subtitle path).

WebRTC Opus audio → 16 kHz mono → VAD segmentation → faster-whisper streaming
transcription (small, int8, GPU if CUDA present) with language detection → local
translation without any LLM (Argos Translate / CTranslate2) when the detected
language differs from the target → ``UIIntent subtitle`` partials then finals
pushed **directly** over the DataChannel (never via BrainLive — this is the
reflex path §3.2). Speaker is not identified at this stage.

VAD choice (ADR §E27): ``webrtcvad`` (WebRTC's GMM VAD, BSD) over silero-onnx —
already a dependency, no extra ONNX weight, deterministic on 10/20/30 ms frames,
and cheap enough to run entirely on CPU so it never competes with the detector
for the GPU. Job class for the ASR GPU work is the dedicated ``asr`` budget.

Nothing here blocks: a missing whisper model or translation pack degrades to an
honest ``status`` on the subtitle intent, never an exception into the transport.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import numpy as np


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------- resampling
def to_mono_16k(samples: np.ndarray, src_rate: int) -> np.ndarray:
    """Downmix to mono and resample to 16 kHz float32 in [-1, 1]."""
    x = np.asarray(samples)
    if x.ndim == 2:  # (frames, channels) or (channels, frames)
        if x.shape[0] < x.shape[1]:
            x = x.T
        x = x.mean(axis=1)
    x = x.astype(np.float32)
    if np.issubdtype(samples.dtype, np.integer):
        x = x / float(np.iinfo(samples.dtype).max)
    if src_rate != 16000:
        n_out = int(round(len(x) * 16000 / src_rate))
        if n_out <= 1:
            return np.zeros(0, dtype=np.float32)
        x = np.interp(
            np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x
        ).astype(np.float32)
    return x


# -------------------------------------------------------------------------- VAD
@dataclass
class VadConfig:
    aggressiveness: int = 2      # webrtcvad 0..3
    frame_ms: int = 30           # 10/20/30 supported by webrtcvad
    min_speech_ms: int = 200     # segment must exceed this to be transcribed
    max_silence_ms: int = 400    # trailing silence that closes a segment


class VadSegmenter:
    """webrtcvad-based speech segmenter over a 16 kHz mono float stream."""

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        import webrtcvad

        self._vad = webrtcvad.Vad(self.config.aggressiveness)
        self._frame_len = int(16000 * self.config.frame_ms / 1000)
        self._buf = np.zeros(0, dtype=np.float32)
        self._in_speech = False
        self._speech: list[np.ndarray] = []
        self._silence_ms = 0

    @staticmethod
    def _to_pcm16(frame: np.ndarray) -> bytes:
        return (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def push(self, samples: np.ndarray) -> list[np.ndarray]:
        """Feed 16 kHz mono float samples, return any completed speech segments."""
        cfg = self.config
        self._buf = np.concatenate([self._buf, samples.astype(np.float32)])
        segments: list[np.ndarray] = []
        while len(self._buf) >= self._frame_len:
            frame = self._buf[: self._frame_len]
            self._buf = self._buf[self._frame_len :]
            is_speech = self._vad.is_speech(self._to_pcm16(frame), 16000)
            if is_speech:
                self._speech.append(frame)
                self._in_speech = True
                self._silence_ms = 0
            elif self._in_speech:
                self._speech.append(frame)
                self._silence_ms += cfg.frame_ms
                if self._silence_ms >= cfg.max_silence_ms:
                    seg = np.concatenate(self._speech)
                    dur_ms = len(seg) / 16000 * 1000
                    if dur_ms >= cfg.min_speech_ms:
                        segments.append(seg)
                    self._speech = []
                    self._in_speech = False
                    self._silence_ms = 0
        return segments

    def flush(self) -> np.ndarray | None:
        if self._speech:
            seg = np.concatenate(self._speech)
            self._speech = []
            self._in_speech = False
            if len(seg) / 16000 * 1000 >= self.config.min_speech_ms:
                return seg
        return None


# ---------------------------------------------------------------- transcription
class WhisperTranscriber:
    """faster-whisper streaming transcription (small, int8) with LID.

    Loaded lazily; GPU if a CUDA device is present, else CPU. ``available`` is
    False when the package/model cannot be loaded — the pipeline then degrades
    honestly instead of raising.
    """

    def __init__(self, model_size: str = "small", *, compute_type: str = "int8") -> None:
        self.model_size = model_size
        self.compute_type = compute_type
        self._model: Any | None = None
        self.available = False
        self.device = "cpu"
        self.last_infer_ms = 0.0

    def _ensure(self) -> Any | None:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel

            device = "cpu"
            compute = self.compute_type
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
                    compute = "float16" if self.compute_type == "int8" else self.compute_type
            except Exception:
                pass
            self._model = WhisperModel(self.model_size, device=device, compute_type=compute)
            self.device = device
            self.available = True
        except Exception:
            self.available = False
            self._model = None
        return self._model

    def transcribe(self, audio_16k: np.ndarray, *, language: str | None = None) -> dict[str, Any]:
        model = self._ensure()
        if model is None:
            return {"status": "asr_unavailable", "text": "", "language": None}
        t0 = time.perf_counter()
        segments, info = model.transcribe(
            audio_16k.astype(np.float32), language=language, beam_size=1, vad_filter=False
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        self.last_infer_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "status": "ok",
            "text": text,
            "language": getattr(info, "language", None),
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        }


# ------------------------------------------------------------------- translation
class ArgosTranslator:
    """Offline NMT via Argos Translate (CTranslate2, MIT). No LLM. Cached."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], Any] = {}
        self._checked = False
        self.available = False

    def _ensure(self) -> None:
        if self._checked:
            return
        self._checked = True
        try:
            import argostranslate.translate  # noqa: F401

            self.available = True
        except Exception:
            self.available = False

    def translate(self, text: str, from_lang: str, to_lang: str) -> dict[str, Any]:
        if not text or from_lang == to_lang:
            return {"status": "noop", "text": text}
        self._ensure()
        if not self.available:
            return {"status": "translate_unavailable", "text": None}
        try:
            import argostranslate.translate as t

            translated = t.translate(text, from_lang, to_lang)
            if translated is None or translated == text:
                return {"status": "no_pack", "text": None}
            return {"status": "ok", "text": translated}
        except Exception:
            return {"status": "translate_error", "text": None}


# --------------------------------------------------------------------- pipeline
@dataclass
class AudioMetrics:
    asr_ms: list[float] = field(default_factory=list)
    segments: int = 0
    partials: int = 0
    finals: int = 0
    translations: int = 0

    def snapshot(self) -> dict[str, Any]:
        s = sorted(self.asr_ms)
        p50 = s[len(s) // 2] if s else 0.0
        return {
            "asr_ms_p50": p50,
            "segments": self.segments,
            "partials_emitted": self.partials,
            "finals_emitted": self.finals,
            "translations": self.translations,
        }


class AudioRT:
    """Audio pipeline orchestrator. Emits UIIntent subtitle dicts via ``on_intent``.

    ``target_language`` is the subtitle language; when the detected speech
    language differs, the final subtitle carries both the source text and the
    translated text. Partials are emitted from the source language (fast path);
    finals include translation.
    """

    def __init__(
        self,
        *,
        session_id: str = "audiort",
        target_language: str = "fr",
        vad: VadSegmenter | None = None,
        transcriber: WhisperTranscriber | None = None,
        translator: ArgosTranslator | None = None,
        arbiter: Any = None,
        on_intent: Callable[[dict[str, Any]], Any] | None = None,
        on_segment: Callable[[np.ndarray, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.target_language = target_language
        self.vad = vad or VadSegmenter()
        self.transcriber = transcriber or WhisperTranscriber()
        self.translator = translator or ArgosTranslator()
        self.arbiter = arbiter
        self.on_intent = on_intent
        # E37 §1: raw-segment hook. Fires with (segment float32 16 kHz mono, meta)
        # for every FINAL speech segment — the audio archive (night) and E32 voice
        # matching consume it. It runs AFTER subtitles are emitted and its failures
        # are swallowed, so it never disturbs the reflex subtitle path.
        self.on_segment = on_segment
        self.metrics = AudioMetrics()
        self._seq = 0

    def _admit_asr(self) -> bool:
        if self.arbiter is None:
            return True
        try:
            return bool(self.arbiter.request("asr").get("grant"))
        except Exception:
            return True

    def _intent(self, text: str, *, final: bool, language: str | None, translated: str | None, status: str) -> dict[str, Any]:
        self._seq += 1
        content: dict[str, Any] = {
            "text": text,
            "final": final,
            "language": language,
            "status": status,
        }
        if translated is not None:
            content["translated_text"] = translated
            content["target_language"] = self.target_language
        return {
            "ui_intent_id": f"audiort-{self.session_id}-{self._seq}",
            "producer": "ultralive",  # reflex path, not brainlive
            "component": "subtitle",
            "anchor": {"type": "subtitle"},
            "content": content,
            "truth_level": "observed",
            "confidence": 1.0 if final else 0.6,
            "priority": 0.7,
            "ttl_ms": 4000 if final else 1500,
            "ui_hint": {"partial": not final},
            "evidence_refs": [],
        }

    def _emit(self, intent: dict[str, Any]) -> None:
        if self.on_intent:
            try:
                self.on_intent(intent)
            except Exception:
                pass

    def push_audio(self, samples: np.ndarray, src_rate: int) -> list[dict[str, Any]]:
        """Feed a chunk of raw audio. Returns the list of subtitle intents emitted."""
        mono = to_mono_16k(samples, src_rate)
        segments = self.vad.push(mono)
        emitted: list[dict[str, Any]] = []
        for seg in segments:
            emitted.extend(self._handle_segment(seg))
        return emitted

    def flush(self) -> list[dict[str, Any]]:
        seg = self.vad.flush()
        return self._handle_segment(seg) if seg is not None else []

    def _handle_segment(self, seg: np.ndarray) -> list[dict[str, Any]]:
        self.metrics.segments += 1
        out: list[dict[str, Any]] = []
        if not self._admit_asr():
            intent = self._intent("", final=True, language=None, translated=None, status="asr_refused")
            self._emit(intent)
            return [intent]
        result = self.transcriber.transcribe(seg)
        if result["status"] != "ok":
            intent = self._intent("", final=True, language=None, translated=None, status=result["status"])
            self._emit(intent)
            return [intent]
        self.metrics.asr_ms.append(self.transcriber.last_infer_ms)
        text = result["text"]
        language = result.get("language")
        if not text:
            return []

        # Partial (source language, fast) then final (with translation).
        partial = self._intent(text, final=False, language=language, translated=None, status="ok")
        self.metrics.partials += 1
        self._emit(partial)
        out.append(partial)

        translated = None
        translate_status = "noop"
        if language and language != self.target_language:
            tr = self.translator.translate(text, language, self.target_language)
            translate_status = tr["status"]
            if tr["status"] == "ok":
                translated = tr["text"]
                self.metrics.translations += 1
        final = self._intent(
            text, final=True, language=language, translated=translated,
            status="ok" if translated is not None or translate_status in {"noop"} else translate_status,
        )
        self.metrics.finals += 1
        self._emit(final)
        out.append(final)
        # E37 §1: hand the raw FINAL segment to the archive / voice matcher AFTER the
        # subtitle is out. Duration derives from the sample count (16 kHz mono); the
        # window ends "now" (finalisation time). Never blocks / raises the reflex path.
        if self.on_segment is not None:
            try:
                dur_s = float(len(seg)) / 16000.0
                end = datetime.now(timezone.utc)
                start = end - timedelta(seconds=dur_s)
                self.on_segment(seg, {
                    "ui_intent_id": final.get("ui_intent_id"),
                    "text": text,
                    "language": language,
                    "absolute_start": start.isoformat(),
                    "absolute_end": end.isoformat(),
                    "duration_s": dur_s,
                    "sample_rate": 16000,
                })
            except Exception:
                pass
        return out
