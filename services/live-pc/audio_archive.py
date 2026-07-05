from __future__ import annotations

"""AudioArchive — archive live VAD speech segments for the nightly deep-audio pass (E37 §1).

**The faille this closes.** The V18.8 nightly pipeline (WhisperX HQ + pyannote
diarisation + voice→personID attribution) is gated by
``brainlive_offline_deep_audio_v18_5.bundles_require_deep_audio``, which only fires
when a bundle owns a ``brainlive_sensor_events`` row with ``modality='audio'`` and
``event_type IN ('speech_segment','speech_segment_failed')``. The event assembler
(``collect_live_raw_timeline``) folds those exact rows into ``audio_timeline_json``.
No V19 code was writing them nor archiving the raw audio, so the night had nothing
to process for a V19 session. This module writes that row — at the exact format the
core deep-audio consumer reads — and persists the segment WAV it points at.

**Format fidelity (the keystone).** The format is *discovered in the core*, never
invented. The canonical writer is
``brainlive_sensor_fusion_v15_4._record_sensor_event(... event_type='speech_segment')``
and the consumer is ``brainlive_offline_deep_audio_v18_5._event_piece``. The
consumer's *VAD-chunk fallback* path (the one our already-cut 16 kHz mono segments
take) reads, from ``payload_json``:

* ``absolute_start`` / ``absolute_end`` — segment window (ISO-8601 UTC);
* ``chunk_path`` (or the event's ``source_path``) — the WAV to refine;
* ``segment`` ``{start, end, ...}`` — VAD offsets within the capture;
* ``speaker`` — the live speaker hint (owner/person) folded through as source_kind;
* ``source_event_id`` — capture identity for dedup.

and the row columns are exactly the core's:
``event_id, live_session_id, person_id, event_time, modality='audio',
event_type='speech_segment', source_path, source_sha256, confidence, payload_json,
model_status, created_at`` — plus the parallel ``brainlive_audio_segments_v154``
projection row the fusion path also writes, so the night sees an identical shape to a
Phone-Bridge capture.

**Bounded (interdit: audio non borné).** Every WAV is size-checked; a per-session and
per-day byte budget stops archiving (WARN, never an exception, never blocks the
subtitle path). The doctor ``-Quota`` accounts ``evidence/audio`` alongside
keyframes/clips.

**Storage layout.** Same evidence root as keyframes (``MLOMEGA_EVIDENCE`` else
``MLOMEGA_RAW/evidence`` else ``data/evidence``), subdir ``audio/<live_session_id>/``.
"""

import os
import sys
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def evidence_root() -> Path:
    """Resolve the evidence root exactly like the doctor -Quota check (E36 §2)."""
    ev = os.environ.get("MLOMEGA_EVIDENCE")
    if ev:
        return Path(ev)
    raw = os.environ.get("MLOMEGA_RAW")
    if raw:
        return Path(raw) / "evidence"
    return _ROOT / "data" / "evidence"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(text: str) -> datetime:
    t = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def write_segment_wav(path: str | Path, samples: Any, sample_rate: int = 16000) -> Path:
    """Write a mono 16 kHz int16 WAV from AudioRT's float VAD segment.

    AudioRT segments are float32 in [-1, 1] at 16 kHz mono (``audiort.to_mono_16k``).
    We persist them as 16-bit PCM — the format WhisperX/pyannote expect at night.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(samples)
    if arr.dtype != np.int16:
        arr = np.clip(arr.astype(np.float32), -1.0, 1.0)
        arr = (arr * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(arr.tobytes())
    return path


@dataclass
class AudioArchiveConfig:
    sample_rate: int = 16000
    # A single speech segment is short (VAD-closed). Cap it defensively so a
    # runaway stream can never write an unbounded WAV.
    max_segment_seconds: float = 30.0
    # Byte budgets: stop archiving (WARN, never raise) past these. Defaults are
    # conservative for a personal box; the doctor watches the on-disk total too.
    session_budget_bytes: int = 512 * 1024 * 1024      # 512 MiB / session
    day_budget_bytes: int = 4 * 1024 * 1024 * 1024      # 4 GiB / day


@dataclass
class ArchiveResult:
    archived: bool
    reason: str = ""
    wav_path: str | None = None
    event_id: str | None = None
    segment_id: str | None = None
    bytes_written: int = 0


class AudioArchive:
    """Persist live VAD speech segments + their canonical night-facing sensor event.

    One instance per session. ``archive_segment`` is called off the subtitle path
    (never blocking it); on any failure it returns a non-archived result rather than
    raising, so the reflex subtitle path is untouched.
    """

    def __init__(
        self,
        *,
        person_id: str,
        live_session_id: str,
        config: AudioArchiveConfig | None = None,
        db_path: Any = None,
        root: Path | None = None,
    ) -> None:
        self.person_id = person_id
        self.live_session_id = live_session_id
        self.config = config or AudioArchiveConfig()
        self.db_path = db_path
        self._root = (root or evidence_root()) / "audio" / str(live_session_id)
        self._session_bytes = 0
        self.metrics: dict[str, int] = {
            "segments_archived": 0,
            "segments_skipped_budget": 0,
            "segments_skipped_empty": 0,
            "errors": 0,
            "bytes_written": 0,
        }

    # ------------------------------------------------------------------ budget
    def _day_bytes(self) -> int:
        base = self._root.parent
        if not base.exists():
            return 0
        total = 0
        for p in base.rglob("*.wav"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def _budget_ok(self) -> bool:
        if self._session_bytes >= self.config.session_budget_bytes:
            return False
        if self._day_bytes() >= self.config.day_budget_bytes:
            return False
        return True

    # ------------------------------------------------------------------ archive
    def archive_segment(
        self,
        samples: Any,
        *,
        absolute_start: str | None = None,
        absolute_end: str | None = None,
        speaker: dict[str, Any] | None = None,
        source_event_id: str | None = None,
        vad_backend: str = "webrtcvad",
        vad_confidence: float = 0.0,
        asr_status: str = "pending",
        transcript_text: str | None = None,
    ) -> ArchiveResult:
        """Archive one final VAD segment (float32 16 kHz mono) + write its event.

        Never raises: any error is captured and returned as a non-archived result so
        the caller's subtitle path is never disturbed.
        """
        try:
            arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        except Exception as exc:
            self.metrics["errors"] += 1
            return ArchiveResult(False, reason=f"bad_samples:{exc}"[:120])

        n = int(arr.size)
        if n == 0:
            self.metrics["segments_skipped_empty"] += 1
            return ArchiveResult(False, reason="empty_segment")

        sr = int(self.config.sample_rate)
        duration_s = n / float(sr)
        if duration_s > self.config.max_segment_seconds:
            # Defensive clamp: keep the leading window, never an unbounded WAV.
            keep = int(self.config.max_segment_seconds * sr)
            arr = arr[:keep]
            n = keep
            duration_s = n / float(sr)

        if not self._budget_ok():
            self.metrics["segments_skipped_budget"] += 1
            return ArchiveResult(False, reason="budget_exceeded")

        start_dt = _parse_iso(absolute_start) if absolute_start else _now()
        if absolute_end:
            end_dt = _parse_iso(absolute_end)
        else:
            end_dt = start_dt + timedelta(seconds=duration_s)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(seconds=max(duration_s, 0.05))

        try:
            from mlomega_audio_elite.utils import stable_id, now_iso, json_dumps, sha256_file  # type: ignore
        except Exception as exc:  # pragma: no cover - import guard
            self.metrics["errors"] += 1
            return ArchiveResult(False, reason=f"core_utils_unavailable:{exc}"[:120])

        # Write the WAV first so the event's source_path/sha256 point at real bytes.
        wav_name = stable_id("v19audio", self.live_session_id, _iso(start_dt), _iso(end_dt), source_event_id or "no_src") + ".wav"
        wav_path = self._root / wav_name
        try:
            write_segment_wav(wav_path, arr, sample_rate=sr)
        except Exception as exc:
            self.metrics["errors"] += 1
            return ArchiveResult(False, reason=f"wav_write_failed:{exc}"[:120])

        try:
            size = wav_path.stat().st_size
        except OSError:
            size = 0
        self._session_bytes += size

        try:
            event_id, segment_id = self._write_event(
                wav_path=wav_path,
                start_dt=start_dt,
                end_dt=end_dt,
                duration_s=duration_s,
                speaker=speaker or {},
                source_event_id=source_event_id,
                vad_backend=vad_backend,
                vad_confidence=float(vad_confidence),
                asr_status=asr_status,
                transcript_text=transcript_text,
                stable_id=stable_id,
                now_iso=now_iso,
                json_dumps=json_dumps,
                sha256_file=sha256_file,
            )
        except Exception as exc:
            self.metrics["errors"] += 1
            return ArchiveResult(False, reason=f"event_write_failed:{exc}"[:120], wav_path=str(wav_path), bytes_written=size)

        self.metrics["segments_archived"] += 1
        self.metrics["bytes_written"] += size
        return ArchiveResult(True, wav_path=str(wav_path), event_id=event_id, segment_id=segment_id, bytes_written=size)

    # ------------------------------------------------------------------ event
    def _write_event(
        self,
        *,
        wav_path: Path,
        start_dt: datetime,
        end_dt: datetime,
        duration_s: float,
        speaker: dict[str, Any],
        source_event_id: str | None,
        vad_backend: str,
        vad_confidence: float,
        asr_status: str,
        transcript_text: str | None,
        stable_id: Any,
        now_iso: Any,
        json_dumps: Any,
        sha256_file: Any,
    ) -> tuple[str, str]:
        """Write the canonical ``speech_segment`` sensor event + its segment row.

        Columns and payload match ``brainlive_sensor_fusion_v15_4._record_sensor_event``
        for ``event_type='speech_segment'`` — the format
        ``brainlive_offline_deep_audio_v18_5._event_piece`` reads (VAD-chunk fallback:
        ``chunk_path``/``source_path`` + ``absolute_start``/``absolute_end`` + ``segment``).
        """
        from mlomega_audio_elite.db import connect, upsert  # type: ignore
        from mlomega_audio_elite.brainlive_sensor_fusion_v15_4 import ensure_sensor_fusion_schema  # type: ignore

        ensure_sensor_fusion_schema()

        abs_start = _iso(start_dt)
        abs_end = _iso(end_dt)
        chunk_path = str(wav_path.resolve())
        sha = sha256_file(wav_path)
        now = now_iso()

        # VAD-child segment descriptor (offsets relative to this capture: 0..duration).
        segment = {"start": 0.0, "end": round(duration_s, 4), "backend": vad_backend, "confidence": vad_confidence}
        speaker_out = dict(speaker or {})
        speaker_out.setdefault("source_kind", "v19_live_vad")

        payload = {
            "source_event_id": source_event_id,
            "segment": segment,
            "absolute_start": abs_start,
            "absolute_end": abs_end,
            # No separate full raw capture in V19 (audiort already cut the VAD child);
            # chunk_path IS the retained evidence — the deep-audio fallback path.
            "raw_audio_path": None,
            "chunk_path": chunk_path,
            "vad_backend": vad_backend,
            "vad_confidence": vad_confidence,
            "asr_status": asr_status,
            "transcript_text": transcript_text or "",
            "speaker": speaker_out,
            "capture_source": "v19_live_audio_archive",
        }

        # Deterministic id mirroring the core's _record_sensor_event scheme
        # (blse | live_session | source_event | modality | event_type | time | sha).
        event_id = stable_id(
            "blse", self.live_session_id, source_event_id or "no_source_event",
            "audio", "speech_segment", abs_start, sha,
        )
        segment_id = stable_id("blseg154", source_event_id or chunk_path, 0, 0.0, duration_s, sha)

        confidence = max(0.0, min(1.0, 0.5 + 0.5 * float(vad_confidence)))
        with connect(self.db_path) as con:
            upsert(con, "brainlive_sensor_events", {
                "event_id": event_id,
                "live_session_id": self.live_session_id,
                "person_id": self.person_id,
                "event_time": abs_start,
                "modality": "audio",
                "event_type": "speech_segment",
                "source_path": chunk_path,
                "source_sha256": sha,
                "confidence": confidence,
                "payload_json": json_dumps(payload),
                "model_status": asr_status,
                "created_at": now,
            }, "event_id")
            # Parallel segment projection the fusion path also writes, so the night
            # sees an identical shape (brainlive_audio_segments_v154).
            upsert(con, "brainlive_audio_segments_v154", {
                "segment_id": segment_id,
                "live_session_id": self.live_session_id,
                "person_id": self.person_id,
                "source_event_id": source_event_id,
                "source_path": chunk_path,
                "start_s": 0.0,
                "end_s": round(duration_s, 4),
                "absolute_start": abs_start,
                "absolute_end": abs_end,
                "vad_backend": vad_backend,
                "vad_confidence": vad_confidence,
                "chunk_path": chunk_path,
                "asr_backend": None,
                "asr_status": asr_status,
                "transcript_text": transcript_text or "",
                "speaker_json": json_dumps(speaker_out),
                "created_at": now,
                "processed_at": now,
            }, "segment_id")
            con.commit()
        return event_id, segment_id
