from __future__ import annotations

import json
import os
import shutil
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .config import get_settings
from .segmentation import normalize_transcript_turns
from .utils import now_iso, stable_id, sha256_file


class EliteDependencyError(RuntimeError):
    pass


def require_module(name: str, package_hint: str | None = None):
    try:
        return __import__(name)
    except Exception as exc:  # pragma: no cover - only executed when elite deps are missing
        hint = package_hint or name
        raise EliteDependencyError(f"Module '{name}' absent. Installe: {hint}") from exc


def transcribe_with_whisperx(
    audio_path: Path,
    *,
    language: str = "fr",
    speaker_map: dict[str, str] | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Real WhisperX + pyannote pipeline.

    Output is normalized to the project transcript schema. This is the path for RTX 3070:
    word-level timestamps, speaker labels, then speaker_map/person resolution when a map is provided.
    """
    settings = get_settings()
    if not settings.enable_whisperx:
        raise EliteDependencyError("MLOMEGA_ENABLE_WHISPERX=false. Active-le pour l'audio réel.")

    audio_path = audio_path.expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)

    # A V18.7 post-stop pass provides one DeepAudioRuntime for every bundle.
    # Direct flow-once still works: it creates a short-lived runtime here.
    from .runtime_v18_7 import DeepAudioRuntime
    owns_runtime = runtime is None
    runtime_ctx = DeepAudioRuntime(language=language) if owns_runtime else runtime
    context = runtime_ctx if owns_runtime else nullcontext(runtime_ctx)
    with context as active_runtime:
        whisperx = active_runtime.whisperx_module()
        device = settings.whisperx_device
        compute_type = settings.whisperx_compute_type
        model = active_runtime.transcription_model(language)
        audio = whisperx.load_audio(str(audio_path))
        # A batch size that fits a cold 8–12 GB card can still OOM after a live
        # session.  Retry the *same* transcription with smaller batches before
        # turning a recoverable VRAM fluctuation into a failed close-day.
        from .runtime_v18_7 import classify_failure, release_gpu_memory, record_phase_event
        requested_batch = max(1, int(settings.whisperx_batch_size))
        batch_candidates: list[int] = []
        current_batch = requested_batch
        while current_batch >= 1:
            if current_batch not in batch_candidates:
                batch_candidates.append(current_batch)
            if current_batch == 1:
                break
            current_batch = max(1, current_batch // 2)
        result = None
        effective_batch_size = requested_batch
        last_exc: Exception | None = None
        for index, batch_size in enumerate(batch_candidates):
            try:
                result = model.transcribe(audio, batch_size=batch_size, language=language)
                effective_batch_size = batch_size
                if index:
                    record_phase_event(
                        "deep_audio_batch_fallback_succeeded",
                        requested_batch=requested_batch,
                        effective_batch_size=batch_size,
                    )
                break
            except Exception as exc:
                last_exc = exc
                failure = classify_failure(exc)
                if failure.code != "gpu_oom" or batch_size == 1:
                    raise
                release_gpu_memory(reason=f"deep_audio_batch_oom:{batch_size}")
                record_phase_event(
                    "deep_audio_batch_fallback",
                    requested_batch=requested_batch,
                    failed_batch_size=batch_size,
                    next_batch_size=batch_candidates[index + 1],
                    error_code=failure.code,
                )
        if result is None:
            assert last_exc is not None
            raise last_exc

        # Alignment for word-level timestamps.  Cache by detected language in the
        # per-close runtime because WhisperX can identify a different language.
        detected_language = str(result.get("language") or language)
        model_a, metadata = active_runtime.align_model(detected_language)
        result = whisperx.align(result["segments"], model_a, metadata, audio, device, return_char_alignments=False)

        if settings.enable_pyannote:
            diarize_model = active_runtime.diarization_model()
            diarize_segments = diarize_model(str(audio_path))
            result = whisperx.assign_word_speakers(diarize_segments, result)

    started_at = now_iso()
    conv_id = stable_id("conv", "audio", sha256_file(audio_path), started_at)

    # Active voice learning: after pyannote has produced SPEAKER_x labels,
    # extract real per-speaker audio samples, match against enrolled voices,
    # and cluster recurring unknown voices. The raw diarization label is still
    # preserved; person_id is the resolved identity. If SELF VOICE is not
    # enrolled, audio ingestion fails unless explicitly disabled by env.
    identity_details: dict[str, Any] = {}
    if settings.enable_speechbrain and result.get("segments"):
        try:
            from .voice_learning import resolve_speakers_for_audio
            resolved = resolve_speakers_for_audio(
                audio_path,
                result.get("segments", []),
                conversation_id=conv_id,
                require_self_voice=settings.require_self_voice,
            )
            speaker_map = {**(speaker_map or {}), **resolved.get("speaker_map", {})}
            identity_details = resolved
        except Exception as exc:
            if settings.voice_learning_strict:
                raise
            identity_details = {"status": "voice_learning_skipped", "error": str(exc)[:500]}

    turns: list[dict[str, Any]] = []
    for idx, seg in enumerate(result.get("segments", [])):
        speaker = seg.get("speaker") or "SPEAKER_UNKNOWN"
        person = (speaker_map or {}).get(speaker)
        turns.append({
            "turn_id": stable_id("turn", conv_id, idx, speaker, seg.get("start"), seg.get("end")),
            "speaker": speaker,
            "person_id": person,
            "start": seg.get("start"),
            "end": seg.get("end"),
            "text": (seg.get("text") or "").strip(),
            "words": seg.get("words", []),
            "metadata": {"whisperx_segment": seg},
        })

    transcript = {
        "metadata": {
            "conversation_id": conv_id,
            "title": f"Audio {audio_path.name}",
            "started_at": started_at,
            "topic": "conversation audio",
            "channel": "audio",
            "source": str(audio_path),
            "language": result.get("language", language),
            "speaker_map": speaker_map or {},
            "participants": sorted(set((speaker_map or {}).values()) or []),
            "relationship_context": {},
            "voice_identity": identity_details,
            "pipeline": {
                "transcriber": "whisperx",
                "model": settings.whisperx_model,
                "device": device,
                "compute_type": compute_type,
                "requested_batch_size": requested_batch,
                "effective_batch_size": effective_batch_size,
                "diarization": settings.enable_pyannote,
            },
        },
        "turns": turns,
    }
    return normalize_transcript_turns(transcript)


def ingest_audio_to_transcript_json(audio_path: Path, out_dir: Path, *, language: str = "fr", speaker_map: dict[str, str] | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = transcribe_with_whisperx(audio_path, language=language, speaker_map=speaker_map)
    out = out_dir / f"{data['metadata']['conversation_id']}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
