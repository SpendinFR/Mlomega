from __future__ import annotations

"""V18.5 offline deep-audio refinement for BrainLive bundles.

BrainLive remains intentionally fast during the day.  It records short VAD/ASR
turns and raw chunk provenance.  After a session is closed, this module groups
all *audio evidence that belongs to each assembled V15.14 bundle*, stitches a
bounded offline tape, runs the existing WhisperX + alignment + Pyannote path,
and exports a new immutable Brain2 conversation revision.

Important invariants:
- this is not ``flow-once`` and never creates a parallel direct-import
  conversation;
- original live turns and the original bundle export are preserved, then
  superseded through lineage rather than overwritten;
- every deep turn carries its raw chunk/event provenance and an explicit map
  from stitched-audio seconds back to the original absolute timeline;
- an audio-bearing bundle with missing raw evidence or a failed WhisperX pass is
  an explicit error.  It cannot silently unlock Brain2 or cleanup;
- retries reuse an artifact keyed by ``bundle_id + source digest``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable

from .config import get_settings
from .runtime_v18_7 import DeepAudioRuntime, classify_failure, gpu_phase, retry_operation, record_phase_event
from .db import connect, init_db, insert_only, upsert, write_transaction
from .governance_v18 import (
    Scope,
    StageGateError,
    ensure_v18_schema,
    invalidate_descendants,
    link_artifact,
    record_artifact_version,
    register_conversation_scope,
    strict_many,
    strict_one,
)
from .utils import json_dumps, json_loads, now_iso, sha256_file, stable_id

VERSION = "18.7.1-poststop-deep-audio-reusable-runtime"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_deep_audio_runs_v185(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  live_session_id TEXT,
  status TEXT NOT NULL,
  bundles_total INTEGER NOT NULL DEFAULT 0,
  bundles_refined INTEGER NOT NULL DEFAULT 0,
  bundles_without_audio INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_deep_audio_artifacts_v185(
  artifact_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  run_id TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  source_manifest_json TEXT NOT NULL DEFAULT '[]',
  processing_profile_json TEXT NOT NULL DEFAULT '{}',
  speaker_reconciliation_json TEXT NOT NULL DEFAULT '{}',
  time_map_json TEXT NOT NULL DEFAULT '[]',
  tape_duration_seconds REAL,
  stitched_audio_path TEXT,
  stitched_audio_sha256 TEXT,
  transcript_json TEXT NOT NULL DEFAULT '{}',
  transcript_sha256 TEXT,
  refined_conversation_id TEXT,
  superseded_conversation_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(bundle_id, source_digest)
);
CREATE INDEX IF NOT EXISTS idx_deep_audio_run_v185
  ON brainlive_deep_audio_runs_v185(person_id, package_date, updated_at);
CREATE INDEX IF NOT EXISTS idx_deep_audio_bundle_v185
  ON brainlive_deep_audio_artifacts_v185(bundle_id, status, updated_at);
"""


class DeepAudioError(StageGateError):
    """Required offline audio evidence could not be refined safely."""


@dataclass(frozen=True)
class AudioPiece:
    event_id: str
    source_event_id: str | None
    source_path: str
    absolute_start: str
    absolute_end: str
    original_start_s: float | None
    original_end_s: float | None
    needs_trim: bool
    live_speaker: dict[str, Any]


def ensure_deep_audio_schema() -> None:
    init_db()
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)
        # V18.5.1 is additive: an existing V18.5 database keeps its completed
        # artifacts, while new refinements record their exact processing profile.
        cols = {str(row[1]) for row in con.execute("PRAGMA table_info(brainlive_deep_audio_artifacts_v185)").fetchall()}
        if "processing_profile_json" not in cols:
            con.execute("ALTER TABLE brainlive_deep_audio_artifacts_v185 ADD COLUMN processing_profile_json TEXT NOT NULL DEFAULT '{}'" )
        if "tape_duration_seconds" not in cols:
            con.execute("ALTER TABLE brainlive_deep_audio_artifacts_v185 ADD COLUMN tape_duration_seconds REAL")
        if "speaker_reconciliation_json" not in cols:
            con.execute("ALTER TABLE brainlive_deep_audio_artifacts_v185 ADD COLUMN speaker_reconciliation_json TEXT NOT NULL DEFAULT '{}'")


def _one(con: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return strict_one(con, sql, params, purpose="deep-audio query")


def _rows(con: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return strict_many(con, sql, params, purpose="deep-audio query")


def _parse_iso(value: str | None) -> datetime:
    if not value:
        raise DeepAudioError("deep audio needs an absolute timestamp")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DeepAudioError(f"invalid absolute timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        raise DeepAudioError(f"timestamp must carry a timezone: {value!r}")
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if out >= 0 else default
    except (TypeError, ValueError):
        return default


def _overlaps(start: datetime, end: datetime, bundle_start: datetime, bundle_end: datetime) -> bool:
    return start < bundle_end and end > bundle_start


def _table_exists(con: Any, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _audio_expected(bundle: dict[str, Any]) -> bool:
    raw = json_loads(bundle.get("raw_timeline_json"), []) or []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("modality") or "") == "audio" and str(item.get("row_kind") or "") in {
            "speech_segment", "speech_segment_failed"
        }:
            return True
    return False


def _event_piece(
    event: dict[str, Any],
    bundle_start: datetime,
    bundle_end: datetime,
    *,
    include_full_capture: bool = False,
) -> AudioPiece | None:
    """Resolve one audio event to retained evidence.

    When an event is explicitly owned by a V15.14 bundle we use the complete
    original phone capture (normally 3–5 seconds), rather than only the VAD
    subsegment.  This is the only reliable way to let offline WhisperX/Pyannote
    recover word boundaries and speaker turns that the live VAD path missed.
    The bundle's raw-timeline membership is authoritative; timestamp overlap is
    merely the compatibility fallback for older bundles.
    """
    payload = json_loads(event.get("payload_json"), {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    segment = payload.get("segment") if isinstance(payload.get("segment"), dict) else {}
    absolute_start = payload.get("absolute_start") or event.get("event_time")
    if not absolute_start:
        return None
    try:
        segment_start = _parse_iso(str(absolute_start))
    except DeepAudioError:
        return None
    segment_end_raw = payload.get("absolute_end")
    if segment_end_raw:
        try:
            segment_end = _parse_iso(str(segment_end_raw))
        except DeepAudioError:
            segment_end = segment_start
    else:
        duration = max(0.05, _float(segment.get("end")) - _float(segment.get("start")))
        segment_end = segment_start + timedelta(seconds=duration)
    if segment_end <= segment_start:
        segment_end = segment_start + timedelta(milliseconds=50)
    if not include_full_capture and not _overlaps(segment_start, segment_end, bundle_start, bundle_end):
        return None

    raw_path = str(payload.get("raw_audio_path") or "").strip()
    chunk_path = str(payload.get("chunk_path") or "").strip()
    event_path = str(event.get("source_path") or "").strip()
    raw_candidate = Path(raw_path).expanduser() if raw_path else None
    speaker = payload.get("speaker") if isinstance(payload.get("speaker"), dict) else {}
    if raw_candidate and raw_candidate.exists():
        raw_duration = _duration_seconds(raw_candidate)
        segment_offset = _float(segment.get("start"))
        raw_start = segment_start - timedelta(seconds=segment_offset)
        raw_end = raw_start + timedelta(seconds=raw_duration)
        if include_full_capture:
            clip_start_at, clip_end_at = raw_start, raw_end
        else:
            clip_start_at = max(raw_start, bundle_start)
            clip_end_at = min(raw_end, bundle_end)
        if clip_end_at <= clip_start_at:
            return None
        clip_start_s = max(0.0, (clip_start_at - raw_start).total_seconds())
        clip_end_s = min(raw_duration, (clip_end_at - raw_start).total_seconds())
        if clip_end_s - clip_start_s < 0.05:
            return None
        return AudioPiece(
            event_id=str(event.get("event_id") or ""),
            source_event_id=str(payload.get("source_event_id")) if payload.get("source_event_id") else None,
            source_path=str(raw_candidate.resolve()),
            absolute_start=_iso(clip_start_at),
            absolute_end=_iso(clip_end_at),
            original_start_s=clip_start_s,
            original_end_s=clip_end_s,
            needs_trim=clip_start_s > 0.001 or clip_end_s < raw_duration - 0.001,
            live_speaker={**speaker, "source_kind": "raw_capture"},
        )

    # Compatibility fallback: a VAD child is all that survived.  Keep that
    # fact explicit; it is not equivalent to a full raw-capture refinement.
    candidates = [chunk_path, event_path]
    source = next((p for p in candidates if p and Path(p).expanduser().exists()), "")
    if not source:
        return None
    source_path = Path(source).expanduser().resolve()
    source_duration = _duration_seconds(source_path)
    if include_full_capture:
        clip_start_at = segment_start
        clip_end_at = segment_start + timedelta(seconds=source_duration)
    else:
        clip_start_at = max(segment_start, bundle_start)
        clip_end_at = min(segment_start + timedelta(seconds=source_duration), bundle_end)
    if clip_end_at <= clip_start_at:
        return None
    clip_start_s = max(0.0, (clip_start_at - segment_start).total_seconds())
    clip_end_s = min(source_duration, (clip_end_at - segment_start).total_seconds())
    if clip_end_s - clip_start_s < 0.05:
        return None
    return AudioPiece(
        event_id=str(event.get("event_id") or ""),
        source_event_id=str(payload.get("source_event_id")) if payload.get("source_event_id") else None,
        source_path=str(source_path),
        absolute_start=_iso(clip_start_at),
        absolute_end=_iso(clip_end_at),
        original_start_s=clip_start_s,
        original_end_s=clip_end_s,
        needs_trim=clip_start_s > 0.001 or clip_end_s < source_duration - 0.001,
        live_speaker={**speaker, "source_kind": "vad_chunk_fallback"},
    )


def _bundle_audio_source_event_ids(bundle: dict[str, Any]) -> set[str]:
    """Return exact sensor-event ownership recorded by V15.14 assembly."""
    raw = json_loads(bundle.get("raw_timeline_json"), []) or []
    ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_table") or "") == "brainlive_sensor_events" and item.get("source_id"):
            ids.add(str(item["source_id"]))
    return ids


def _pieces_for_bundle(bundle: dict[str, Any], *, person_id: str) -> tuple[list[AudioPiece], list[dict[str, Any]]]:
    """Read immutable, bundle-owned raw audio evidence.

    New bundles carry exact raw source IDs.  This avoids losing an audio capture
    that happens to start exactly at the displayed bundle boundary, and prevents
    the offline pass from guessing scene membership from a loose time window.
    """
    session_id = str(bundle.get("live_session_id") or "")
    if not session_id:
        return [], []
    start = _parse_iso(str(bundle.get("start_time")))
    end = _parse_iso(str(bundle.get("end_time")))
    source_ids = _bundle_audio_source_event_ids(bundle)
    missing: list[dict[str, Any]] = []
    with connect() as con:
        if not _table_exists(con, "brainlive_sensor_events"):
            return [], []
        params: list[Any] = [person_id, session_id]
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            owned_rows = _rows(
                con,
                f"""SELECT * FROM brainlive_sensor_events
                    WHERE person_id=? AND live_session_id=? AND event_id IN ({placeholders})
                    ORDER BY event_time,event_id""",
                tuple(params + sorted(source_ids)),
            )
            present_ids = {str(event.get("event_id") or "") for event in owned_rows}
            # Only an ID absent from the source table is a structural error. A
            # vision/GPS reference in the same bundle is not an audio failure.
            for missing_id in sorted(source_ids - present_ids):
                missing.append({"event_id": missing_id, "reason": "bundle_references_missing_sensor_event"})
            events = [
                event for event in owned_rows
                if str(event.get("modality") or "") == "audio"
                and str(event.get("event_type") or "") in {"speech_segment", "speech_segment_failed"}
            ]
        else:
            events = _rows(
                con,
                """SELECT * FROM brainlive_sensor_events
                   WHERE person_id=? AND live_session_id=? AND modality='audio'
                     AND event_type IN ('speech_segment','speech_segment_failed')
                   ORDER BY event_time,event_id""",
                tuple(params),
            )
    pieces: list[AudioPiece] = []

    # One phone capture may produce several VAD events.  Deduplicate by the
    # immutable capture identity (or its resolved path for older captures).
    seen: set[tuple[str, str]] = set()
    for event in events:
        exact_membership = bool(source_ids) and str(event.get("event_id") or "") in source_ids
        piece = _event_piece(event, start, end, include_full_capture=exact_membership)
        if not piece:
            payload = json_loads(event.get("payload_json"), {}) or {}
            if exact_membership or (isinstance(payload, dict) and payload.get("raw_audio_path")):
                missing.append({
                    "event_id": str(event.get("event_id") or ""),
                    "source_event_id": payload.get("source_event_id") if isinstance(payload, dict) else None,
                    "raw_audio_path": payload.get("raw_audio_path") if isinstance(payload, dict) else None,
                    "chunk_path": payload.get("chunk_path") if isinstance(payload, dict) else None,
                    "reason": "bundle_audio_evidence_unresolvable",
                })
            continue
        source_key = piece.source_event_id or str(Path(piece.source_path).resolve())
        key = (source_key, str(Path(piece.source_path).resolve()))
        if key in seen:
            continue
        seen.add(key)
        pieces.append(piece)
    pieces.sort(key=lambda p: (p.absolute_start, p.absolute_end, p.source_path, p.event_id))
    return pieces, missing

def _source_manifest(pieces: Iterable[AudioPiece]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for p in pieces:
        path = Path(p.source_path).expanduser().resolve()
        result.append(
            {
                "event_id": p.event_id,
                "source_event_id": p.source_event_id,
                "source_path": str(path),
                "source_sha256": sha256_file(path),
                "source_kind": str((p.live_speaker or {}).get("source_kind") or "unknown"),
                "absolute_start": p.absolute_start,
                "absolute_end": p.absolute_end,
                "clip_start_s": p.original_start_s,
                "clip_end_s": p.original_end_s,
                "needs_trim": p.needs_trim,
                "live_speaker_hint": {k: v for k, v in (p.live_speaker or {}).items() if k != "source_kind"},
            }
        )
    return result


def _voice_registry_fingerprint(person_id: str) -> str:
    """A voice enrollment change must permit a new deep-audio revision."""
    snapshot: dict[str, Any] = {"person_id": person_id, "profiles": [], "embeddings": []}
    with connect() as con:
        if _table_exists(con, "speaker_profiles") and _table_exists(con, "voice_embeddings"):
            # Only enrolled/known voices influence an offline identity decision.
            # Unknown clusters create ordinary speaker profiles too, but must not
            # perturb the input fingerprint after a successful first pass.
            snapshot["profiles"] = _rows(
                con,
                """SELECT DISTINCT sp.person_id,sp.display_name,sp.is_user,sp.aliases_json,sp.notes,sp.created_at
                   FROM speaker_profiles sp
                   JOIN voice_embeddings ve ON ve.person_id=sp.person_id
                   ORDER BY sp.person_id""",
            )
        if _table_exists(con, "voice_embeddings"):
            rows = _rows(
                con,
                "SELECT person_id,model,confidence,created_at,embedding_json FROM voice_embeddings ORDER BY person_id,model,created_at",
            )
            snapshot["embeddings"] = [
                {**{k: row.get(k) for k in ("person_id", "model", "confidence", "created_at")}, "embedding_sha256": hashlib.sha256(str(row.get("embedding_json") or "").encode("utf-8")).hexdigest()}
                for row in rows
            ]
    return hashlib.sha256(json_dumps(snapshot).encode("utf-8")).hexdigest()


def _processing_profile(*, person_id: str, language: str, max_gap_seconds: float) -> dict[str, Any]:
    settings = get_settings()
    return {
        "version": VERSION,
        "language": language,
        "stitch_policy": {
            "source_preference": "raw_capture_then_vad_chunk_fallback",
            "preserve_gap_seconds_up_to": round(float(max_gap_seconds), 3),
            "overlap_policy": "retain_source_order_and_record_overlap",
            "target_audio": "mono_16khz_pcm_s16le",
        },
        "whisperx": {
            "enabled": bool(settings.enable_whisperx),
            "model": settings.whisperx_model,
            "device": settings.whisperx_device,
            "compute_type": settings.whisperx_compute_type,
            "batch_size": settings.whisperx_batch_size,
            "pyannote_enabled": bool(settings.enable_pyannote),
        },
        "speaker_resolution": {
            "speechbrain_enabled": bool(settings.enable_speechbrain),
            "require_self_voice": bool(settings.require_self_voice),
            "voice_registry_sha256": _voice_registry_fingerprint(person_id),
        },
    }


def _duration_seconds(path: Path) -> float:
    cmd = [
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=min(60.0, get_settings().deep_audio_ffmpeg_timeout_s)).strip()
        value = float(output)
        if value > 0:
            return value
    except Exception:
        pass
    # The Android bridge defaults to WAV; this fallback keeps post-stop usable
    # on a minimal installation that has no ffprobe path in PATH.
    try:
        import wave
        with wave.open(str(path), "rb") as wav:
            return float(wav.getnframes()) / float(wav.getframerate())
    except Exception as exc:
        raise DeepAudioError(f"cannot determine audio duration for {path}") from exc


def _run_ffmpeg(cmd: list[str], *, label: str) -> None:
    timeout = get_settings().deep_audio_ffmpeg_timeout_s
    try:
        proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DeepAudioError("ffmpeg is required for V18.7 offline deep audio") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeepAudioError(f"deep_audio_retryable:ffmpeg timeout while {label} after {timeout}s") from exc
    if proc.returncode != 0:
        raise DeepAudioError(f"ffmpeg failed while {label}: {proc.stderr[-1200:]}")


def _materialize_piece(piece: AudioPiece, *, staging_dir: Path, index: int) -> Path:
    source = Path(piece.source_path).expanduser().resolve()
    duration = _duration_seconds(source)
    start_s = float(piece.original_start_s or 0.0)
    end_s = float(piece.original_end_s if piece.original_end_s is not None else duration)
    if start_s < 0 or end_s <= start_s or end_s > duration + 0.05:
        raise DeepAudioError(f"invalid source clip bounds for {piece.event_id}")
    if not piece.needs_trim and start_s <= 0.001 and end_s >= duration - 0.001:
        return source
    out = staging_dir / f"clip_{index:04d}.wav"
    if out.exists():
        return out
    _run_ffmpeg(
        [
            shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start_s:.6f}", "-t", f"{end_s - start_s:.6f}",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
        ],
        label="clipping raw capture to bundle bounds",
    )
    if not out.exists() or out.stat().st_size == 0:
        raise DeepAudioError(f"ffmpeg did not materialize audio clip {piece.event_id}")
    return out


def _materialize_silence(*, staging_dir: Path, index: int, duration_s: float) -> Path:
    if duration_s <= 0:
        raise DeepAudioError("silence duration must be positive")
    out = staging_dir / f"gap_{index:04d}_{duration_s:.3f}.wav"
    if out.exists():
        return out
    _run_ffmpeg(
        [
            shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", f"{duration_s:.6f}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
        ],
        label="preserving a temporal gap between raw captures",
    )
    if not out.exists() or out.stat().st_size == 0:
        raise DeepAudioError("ffmpeg did not materialize temporal-gap silence")
    return out


def _estimate_tape_seconds(pieces: list[AudioPiece], *, max_gap_seconds: float) -> float:
    total = 0.0
    previous_end: datetime | None = None
    for piece in pieces:
        start = _parse_iso(piece.absolute_start)
        end = _parse_iso(piece.absolute_end)
        total += max(0.0, (end - start).total_seconds())
        if previous_end is not None and start > previous_end:
            total += min((start - previous_end).total_seconds(), max_gap_seconds)
        previous_end = max(previous_end, end) if previous_end is not None else end
    return total


def _stitch_pieces(
    pieces: list[AudioPiece], *, artifact_dir: Path, artifact_id: str, max_gap_seconds: float
) -> tuple[Path, list[dict[str, Any]]]:
    """Build a gap-aware WAV tape and a local-seconds -> absolute-time map.

    We preserve short true silences so diarization does not receive unrelated
    utterances as one artificial continuous sentence.  Long idle periods are
    capped, recorded and mapped explicitly; this keeps a day close practical
    without lying about the original timestamps.
    """
    if max_gap_seconds < 0 or not math.isfinite(max_gap_seconds):
        raise DeepAudioError("max_gap_seconds must be a finite non-negative number")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    tape = artifact_dir / f"{artifact_id}.wav"
    map_path = artifact_dir / f"{artifact_id}.concat.txt"
    if not pieces:
        raise DeepAudioError("cannot stitch an empty deep-audio tape")

    concat_paths: list[Path] = []
    time_map: list[dict[str, Any]] = []
    local = 0.0
    previous_absolute_end: datetime | None = None
    for index, piece in enumerate(pieces):
        absolute_start = _parse_iso(piece.absolute_start)
        absolute_end = _parse_iso(piece.absolute_end)
        if previous_absolute_end is not None and absolute_start > previous_absolute_end:
            raw_gap = (absolute_start - previous_absolute_end).total_seconds()
            inserted_gap = min(raw_gap, max_gap_seconds)
            if inserted_gap >= 0.01:
                silence = _materialize_silence(staging_dir=artifact_dir, index=index, duration_s=inserted_gap)
                concat_paths.append(silence)
                time_map.append(
                    {
                        "kind": "silence_gap",
                        "event_id": None,
                        "source_event_id": None,
                        "local_start_s": round(local, 6),
                        "local_end_s": round(local + inserted_gap, 6),
                        "absolute_start": _iso(previous_absolute_end),
                        "absolute_end": _iso(absolute_start),
                        "source_duration_s": round(raw_gap, 6),
                        "tape_duration_s": round(inserted_gap, 6),
                        "gap_compressed": raw_gap > inserted_gap + 0.001,
                    }
                )
                local += inserted_gap
        materialized = _materialize_piece(piece, staging_dir=artifact_dir, index=index)
        duration = _duration_seconds(materialized)
        if duration <= 0:
            raise DeepAudioError(f"non-positive staged duration for {piece.event_id}")
        concat_paths.append(materialized)
        source_duration = max(0.001, (absolute_end - absolute_start).total_seconds())
        time_map.append(
            {
                "kind": "audio_piece",
                "event_id": piece.event_id,
                "source_event_id": piece.source_event_id,
                "source_path": piece.source_path,
                "source_kind": str((piece.live_speaker or {}).get("source_kind") or "unknown"),
                "local_start_s": round(local, 6),
                "local_end_s": round(local + duration, 6),
                "absolute_start": piece.absolute_start,
                "absolute_end": piece.absolute_end,
                "source_duration_s": round(source_duration, 6),
                "tape_duration_s": round(duration, 6),
            }
        )
        local += duration
        previous_absolute_end = max(previous_absolute_end, absolute_end) if previous_absolute_end is not None else absolute_end

    lines = [f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for p in concat_paths]
    map_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run_ffmpeg(
        [
            shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(map_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tape),
        ],
        label="stitching bundle raw audio",
    )
    if not tape.exists() or tape.stat().st_size == 0:
        raise DeepAudioError("deep-audio tape was not created")
    measured = _duration_seconds(tape)
    if abs(measured - local) > 0.25:
        raise DeepAudioError(f"stitch duration mismatch: expected {local:.3f}s, got {measured:.3f}s")
    return tape, time_map


def _map_local_time(time_map: list[dict[str, Any]], value: float) -> str:
    if not time_map:
        raise DeepAudioError("empty deep-audio time map")
    if not math.isfinite(value) or value < 0:
        raise DeepAudioError("invalid local transcript timestamp")
    for item in time_map:
        start = float(item["local_start_s"])
        end = float(item["local_end_s"])
        if start <= value <= end or (item is time_map[-1] and value >= start):
            ratio = 0.0 if end <= start else max(0.0, min(1.0, (value - start) / (end - start)))
            src_start = _parse_iso(str(item["absolute_start"]))
            src_end = _parse_iso(str(item["absolute_end"]))
            return _iso(src_start + (src_end - src_start) * ratio)
    return str(time_map[-1]["absolute_end"])


def _piece_refs_for_range(time_map: list[dict[str, Any]], start: float, end: float) -> list[str]:
    refs: list[str] = []
    for item in time_map:
        if item.get("kind") != "audio_piece":
            continue
        if float(item["local_start_s"]) < end and float(item["local_end_s"]) > start and item.get("event_id"):
            refs.append(str(item["event_id"]))
    return refs


def _validate_transcript_for_brain2(transcript: dict[str, Any], *, time_map: list[dict[str, Any]], person_id: str) -> None:
    """Reject parseable but semantically unusable offline output before Brain2."""
    if not isinstance(transcript, dict):
        raise DeepAudioError("WhisperX transcript must be an object")
    rows = transcript.get("turns")
    if not isinstance(rows, list):
        raise DeepAudioError("WhisperX transcript lacks a turns list")
    metadata = transcript.get("metadata") if isinstance(transcript.get("metadata"), dict) else {}
    speaker_map = metadata.get("speaker_map") if isinstance(metadata.get("speaker_map"), dict) else {}
    if not speaker_map:
        raise DeepAudioError("WhisperX transcript lacks an offline speaker map")
    max_tape = max(float(item["local_end_s"]) for item in time_map)
    accepted = 0
    with connect() as con:
        known_people = set()
        if _table_exists(con, "speaker_profiles"):
            known_people = {str(row["person_id"]) for row in _rows(con, "SELECT person_id FROM speaker_profiles")}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DeepAudioError(f"WhisperX turn {index} is not an object")
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(row["start"])
            end = float(row["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DeepAudioError(f"WhisperX turn {index} lacks numeric bounds") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > max_tape + 0.10:
            raise DeepAudioError(f"WhisperX turn {index} has invalid local bounds")
        if not _piece_refs_for_range(time_map, start, end):
            raise DeepAudioError(f"WhisperX turn {index} maps only to preserved silence, not source audio")
        label = str(row.get("speaker") or "SPEAKER_UNKNOWN")
        resolved = row.get("person_id")
        mapped = speaker_map.get(label)
        if resolved and str(mapped or "") != str(resolved):
            raise DeepAudioError(f"WhisperX turn {index} person_id is not backed by its speaker-map label")
        if mapped and not resolved:
            raise DeepAudioError(f"WhisperX turn {index} omitted the resolved person_id for {label}")
        if resolved not in (None, "", person_id) and str(resolved) not in known_people:
            raise DeepAudioError(f"WhisperX turn {index} cites unknown person_id {resolved!r}")
        words = row.get("words", [])
        if words is not None and not isinstance(words, list):
            raise DeepAudioError(f"WhisperX turn {index} has non-list word alignment")
        for word_index, word in enumerate(words or []):
            if not isinstance(word, dict):
                raise DeepAudioError(f"WhisperX turn {index} word {word_index} is not an object")
            if "start" not in word or "end" not in word:
                raise DeepAudioError(f"WhisperX turn {index} word {word_index} lacks bounds")
            try:
                ws, we = float(word["start"]), float(word["end"])
            except (TypeError, ValueError) as exc:
                raise DeepAudioError(f"WhisperX turn {index} word {word_index} has invalid bounds") from exc
            if not math.isfinite(ws) or not math.isfinite(we) or ws < start - 0.10 or we > end + 0.10 or we < ws:
                raise DeepAudioError(f"WhisperX turn {index} word {word_index} is outside its turn")
        accepted += 1
    if not accepted:
        raise DeepAudioError("WhisperX returned no non-empty aligned turns")


def _speaker_reconciliation(
    transcript: dict[str, Any], *, source_manifest: list[dict[str, Any]], person_id: str
) -> dict[str, Any]:
    """Record the offline SpeechBrain/diarization decision explicitly.

    It is deliberately evidence, not a silent overwrite of the live estimate.
    Brain2 receives both the resolved speaker map and the original per-capture
    live hints, so later corrections remain explainable.
    """
    settings = get_settings()
    metadata = transcript.get("metadata") if isinstance(transcript.get("metadata"), dict) else {}
    speaker_map_raw = metadata.get("speaker_map") if isinstance(metadata.get("speaker_map"), dict) else {}
    speaker_map = {str(k): str(v) for k, v in speaker_map_raw.items() if k and v}
    voice_identity = metadata.get("voice_identity") if isinstance(metadata.get("voice_identity"), dict) else {}
    pipeline = metadata.get("pipeline") if isinstance(metadata.get("pipeline"), dict) else {}
    details_raw = voice_identity.get("details") if isinstance(voice_identity.get("details"), list) else []
    details = [dict(item) for item in details_raw if isinstance(item, dict)]
    transcript_labels = {
        str(row.get("speaker") or "SPEAKER_UNKNOWN")
        for row in (transcript.get("turns") or [])
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    }
    if not bool(settings.enable_speechbrain):
        raise DeepAudioError("V18.5 deep-audio requires MLOMEGA_ENABLE_SPEECHBRAIN=true for offline speaker reconciliation")
    if not voice_identity:
        raise DeepAudioError("offline SpeechBrain reconciliation returned no provenance")
    if str(voice_identity.get("status") or "").lower() in {"voice_learning_skipped", "error", "failed"}:
        raise DeepAudioError(f"offline SpeechBrain reconciliation is not usable: {voice_identity.get('status')}")
    diarization = pipeline.get("diarization")
    if diarization is not True:
        raise DeepAudioError("V18.5 deep-audio requires WhisperX/Pyannote diarization=true")
    if not speaker_map:
        raise DeepAudioError("offline speaker reconciliation produced an empty speaker map")
    detail_labels = {str(item.get("speaker_label")) for item in details if item.get("speaker_label")}
    unresolved_labels = sorted(label for label in transcript_labels if label not in detail_labels)
    if unresolved_labels:
        raise DeepAudioError(f"offline SpeechBrain reconciliation lacks label provenance: {unresolved_labels}")
    live_hints = [
        {"event_id": item.get("event_id"), "source_event_id": item.get("source_event_id"), "speaker": item.get("live_speaker_hint")}
        for item in source_manifest
        if isinstance(item, dict) and isinstance(item.get("live_speaker_hint"), dict) and item.get("live_speaker_hint")
    ]
    return {
        "version": "v18.5-offline-speech-reconciliation",
        "owner_person_id": person_id,
        "engine": {
            "whisperx": str(pipeline.get("transcriber") or ""),
            "pyannote_diarization": True,
            "speechbrain_enabled": True,
        },
        "speaker_map": speaker_map,
        "voice_identity": voice_identity,
        "live_speaker_hints": live_hints,
        "resolved_labels": sorted(speaker_map),
        "requires_manual_review": any(str(item.get("decision") or "") != "known_person_match" for item in details),
    }


def _live_hints_for_refs(source_manifest: list[dict[str, Any]], refs: list[str]) -> list[dict[str, Any]]:
    wanted = set(refs)
    out: list[dict[str, Any]] = []
    for item in source_manifest:
        if not isinstance(item, dict) or str(item.get("event_id") or "") not in wanted:
            continue
        hint = item.get("live_speaker_hint")
        if isinstance(hint, dict) and hint:
            out.append({"event_id": item.get("event_id"), "source_event_id": item.get("source_event_id"), "speaker": hint})
    return out


def _deep_turns(
    transcript: dict[str, Any], *, time_map: list[dict[str, Any]], bundle_id: str, artifact_id: str,
    source_manifest: list[dict[str, Any]], speaker_reconciliation: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = transcript.get("turns") if isinstance(transcript.get("turns"), list) else []
    resolution_by_label = {
        str(item.get("speaker_label")): dict(item)
        for item in ((speaker_reconciliation.get("voice_identity") or {}).get("details") or [])
        if isinstance(item, dict) and item.get("speaker_label")
    }
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        local_start = _float(row.get("start"))
        local_end = _float(row.get("end"), local_start)
        if local_end <= local_start:
            local_end = local_start + 0.05
        refs = _piece_refs_for_range(time_map, local_start, local_end)
        if not refs:
            raise DeepAudioError(f"deep transcript turn {index} has no raw-audio evidence reference")
        global_start = _map_local_time(time_map, local_start)
        global_end = _map_local_time(time_map, local_end)
        label = str(row.get("speaker") or "SPEAKER_UNKNOWN")
        result.append(
            {
                "time": global_start,
                "end_time": global_end,
                "speaker_label": label,
                "speaker_person_id": row.get("person_id"),
                "text": text,
                "kind": "deep_audio_transcript",
                "evidence_role": "deep_audio_whisperx_pyannote_speechbrain_transcript",
                "metadata": {
                    "bundle_id": bundle_id,
                    "deep_audio_artifact_id": artifact_id,
                    "local_start_s": local_start,
                    "local_end_s": local_end,
                    "source_event_ids": refs,
                    "words": row.get("words") if isinstance(row.get("words"), list) else [],
                    "whisperx_metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                    "offline_speaker_resolution": resolution_by_label.get(label, {"speaker_label": label, "person_id": row.get("person_id")}),
                    "live_speaker_hints": _live_hints_for_refs(source_manifest, refs),
                },
            }
        )
    return result


def _refined_participants(bundle: dict[str, Any], transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Use the offline speaker map for Brain2 participants, retaining unknowns."""
    metadata = transcript.get("metadata") if isinstance(transcript.get("metadata"), dict) else {}
    speaker_map = metadata.get("speaker_map") if isinstance(metadata.get("speaker_map"), dict) else {}
    by_person: dict[str, dict[str, Any]] = {}
    for label, person in speaker_map.items():
        if not person:
            continue
        pid = str(person)
        row = by_person.setdefault(pid, {"person_id": pid, "speaker_labels": [], "source": "offline_whisperx_pyannote_speechbrain"})
        row["speaker_labels"].append(str(label))
    if not by_person:
        original = json_loads(bundle.get("participants_json"), []) or []
        return [dict(item) for item in original if isinstance(item, dict)]
    for row in by_person.values():
        row["speaker_labels"] = sorted(set(row["speaker_labels"]))
    return [by_person[key] for key in sorted(by_person)]


def _refined_conversation_bounds(bundle: dict[str, Any], time_map: list[dict[str, Any]]) -> tuple[str, str]:
    starts = [_parse_iso(str(bundle.get("start_time")))]
    ends = [_parse_iso(str(bundle.get("end_time")))]
    for item in time_map:
        if not isinstance(item, dict) or item.get("kind") != "audio_piece":
            continue
        starts.append(_parse_iso(str(item["absolute_start"])))
        ends.append(_parse_iso(str(item["absolute_end"])))
    return _iso(min(starts)), _iso(max(ends))

def _refined_timeline(bundle: dict[str, Any], deep_turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .brainlive_event_assembler_v15_14 import _pseudo_turns_for_bundle
    base = _pseudo_turns_for_bundle(bundle)
    # Live transcript entries are deliberately replaced only in the derived
    # revision.  Original bundle and original conversation remain immutable.
    context = [row for row in base if str(row.get("kind")) != "transcript"]
    timeline = [*deep_turns, *context]
    timeline.sort(key=lambda row: (str(row.get("time") or ""), str(row.get("kind") or ""), str(row.get("text") or "")))
    return timeline


def _find_live_turn_ids(con: Any, *, live_session_id: str, start_at: str, end_at: str) -> list[str]:
    if not live_session_id:
        return []
    rows = _rows(
        con,
        """SELECT live_turn_id FROM brainlive_turn_buffer
           WHERE live_session_id=? AND timestamp_start<?
             AND COALESCE(timestamp_end,timestamp_start)>?
           ORDER BY timestamp_start,live_turn_id""",
        (live_session_id, end_at, start_at),
    )
    return [str(row["live_turn_id"]) for row in rows]


def _export_refined_bundle(
    *,
    bundle: dict[str, Any],
    person_id: str,
    artifact_id: str,
    source_manifest: list[dict[str, Any]],
    time_map: list[dict[str, Any]],
    transcript: dict[str, Any],
    processing_profile: dict[str, Any],
    speaker_reconciliation: dict[str, Any],
) -> tuple[str, list[str]]:
    """Create a new immutable conversation/export revision for one bundle."""
    deep_turns = _deep_turns(
        transcript, time_map=time_map, bundle_id=str(bundle["bundle_id"]), artifact_id=artifact_id,
        source_manifest=source_manifest, speaker_reconciliation=speaker_reconciliation,
    )
    if not deep_turns:
        raise DeepAudioError(f"WhisperX returned no transcript turns for audio-bearing bundle {bundle['bundle_id']}")
    pseudo = _refined_timeline(bundle, deep_turns)
    source_payload = {
        "bundle_id": bundle["bundle_id"],
        "deep_audio_artifact_id": artifact_id,
        "source_manifest": source_manifest,
        "time_map": time_map,
        "processing_profile": processing_profile,
        "speaker_reconciliation": speaker_reconciliation,
        "deep_turns": deep_turns,
        "contexts": [item for item in pseudo if item.get("kind") != "deep_audio_transcript"],
    }
    refined_started_at, refined_ended_at = _refined_conversation_bounds(bundle, time_map)
    refined_participants = _refined_participants(bundle, transcript)
    digest = hashlib.sha256(json_dumps(source_payload).encode("utf-8")).hexdigest()
    conversation_id = stable_id("conv_blbundle_deep_audio_v185", bundle["bundle_id"], digest)
    export_id = stable_id("blexport_deep_audio_v185", bundle["bundle_id"], digest)
    scope = Scope(person_id=person_id, live_session_id=bundle.get("live_session_id"), mode="post_stop")
    previous_ids: list[str] = []
    with connect() as con, write_transaction(con):
        old_exports = _rows(
            con,
            """SELECT conversation_id FROM brainlive_brain2_event_exports_v1514
               WHERE bundle_id=? AND export_status IN ('active','ok','exported')""",
            (bundle["bundle_id"],),
        )
        previous_ids = [str(row["conversation_id"]) for row in old_exports if str(row.get("conversation_id") or "") != conversation_id]
        raw_payload = {
            "source": "brainlive_event_bundle_deep_audio_v185",
            "bundle_id": bundle["bundle_id"],
            "refines_conversation_ids": previous_ids,
            "deep_audio_artifact_id": artifact_id,
            "deep_audio_digest": digest,
            "source_manifest": source_manifest,
            "time_map": time_map,
            "processing_profile": processing_profile,
            "speaker_reconciliation": speaker_reconciliation,
            "bundle_declared_bounds": {"start": bundle.get("start_time"), "end": bundle.get("end_time")},
            "refined_audio_bounds": {"start": refined_started_at, "end": refined_ended_at},
        }
        insert_only(
            con,
            "conversations",
            {
                "conversation_id": conversation_id,
                "title": f"{bundle.get('title') or 'BrainLive event bundle'} — deep audio refined",
                "started_at": refined_started_at,
                "ended_at": refined_ended_at,
                "topic": "brainlive_event_bundle_deep_audio",
                "channel": "brainlive_event_bundle_deep_audio_v185",
                "participants_json": json_dumps(refined_participants),
                "speaker_map_json": json_dumps((transcript.get("metadata") or {}).get("speaker_map") or {}),
                "relationship_context_json": "{}",
                "source_asset_id": None,
                "raw_json": json_dumps(raw_payload),
                "created_at": now_iso(),
            },
            on_conflict="ignore",
        )
        turn_ids: list[str] = []
        for idx, row in enumerate(pseudo):
            metadata = dict(row.get("metadata") or {})
            start_at = str(row.get("time") or bundle.get("start_time"))
            end_at = str(row.get("end_time") or start_at)
            if row.get("kind") == "deep_audio_transcript":
                metadata["reconciles_live_turn_ids"] = _find_live_turn_ids(
                    con,
                    live_session_id=str(bundle.get("live_session_id") or ""),
                    start_at=start_at,
                    end_at=end_at,
                )
            try:
                relative_start = max(0.0, (_parse_iso(start_at) - _parse_iso(refined_started_at)).total_seconds())
                relative_end = max(relative_start, (_parse_iso(end_at) - _parse_iso(refined_started_at)).total_seconds())
            except DeepAudioError:
                relative_start = None
                relative_end = None
            turn_id = stable_id("turn_blbundle_deep_audio_v185", conversation_id, idx, row.get("kind"), start_at, metadata.get("source_event_ids"))
            turn_ids.append(turn_id)
            insert_only(
                con,
                "turns",
                {
                    "turn_id": turn_id,
                    "conversation_id": conversation_id,
                    "idx": idx,
                    "speaker_label": row.get("speaker_label"),
                    "person_id": row.get("speaker_person_id"),
                    "start_s": relative_start,
                    "end_s": relative_end,
                    "text": str(row.get("text") or ""),
                    "previous_turn_id": turn_ids[idx - 1] if idx else None,
                    "metadata_json": json_dumps(
                        {
                            "brainlive_bundle_id": bundle.get("bundle_id"),
                            "kind": row.get("kind"),
                            "time": start_at,
                            "end_time": end_at,
                            "evidence_role": row.get("evidence_role"),
                            "deep_audio_artifact_id": artifact_id,
                            "source": metadata,
                        }
                    ),
                },
                on_conflict="ignore",
            )
        if previous_ids:
            con.execute(
                """UPDATE brainlive_brain2_event_exports_v1514
                   SET export_status='superseded',updated_at=?
                   WHERE bundle_id=? AND export_status IN ('active','ok','exported')""",
                (now_iso(), bundle["bundle_id"]),
            )
            # A refined export is the only active Brain2 conversation for this
            # bundle.  The old conversation remains in SQLite for audit, but
            # must no longer be discoverable by any V18 scope-based global
            # reader (longitudinal, Life Model, retrieval).  Otherwise both
            # the fast live transcript and its WhisperX revision could be
            # counted as independent evidence.
            marks = ",".join("?" for _ in previous_ids)
            con.execute(
                f"UPDATE v18_conversation_scopes SET active=0,updated_at=? "
                f"WHERE person_id=? AND conversation_id IN ({marks}) AND active=1",
                (now_iso(), person_id, *previous_ids),
            )
        upsert(
            con,
            "brainlive_brain2_event_exports_v1514",
            {
                "export_id": export_id,
                "person_id": person_id,
                "bundle_id": bundle["bundle_id"],
                "conversation_id": conversation_id,
                "turn_ids_json": json_dumps(turn_ids),
                "export_status": "exported",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
            "export_id",
        )
        con.execute(
            "UPDATE brainlive_event_bundles_v1514 SET brain2_conversation_id=?,updated_at=? WHERE bundle_id=?",
            (conversation_id, now_iso(), bundle["bundle_id"]),
        )
    for old_id in previous_ids:
        invalidate_descendants(
            root_table="conversations",
            root_id=old_id,
            scope=scope,
            reason=f"bundle {bundle['bundle_id']} re-exported with WhisperX/Pyannote deep-audio refinement",
        )
    record_artifact_version(
        artifact_table="conversations",
        artifact_id=conversation_id,
        identity_key=f"bundle:{bundle['bundle_id']}:deep_audio",
        scope=scope,
        source_payload=source_payload,
        metadata={"export_id": export_id, "deep_audio_artifact_id": artifact_id, "processing_profile": processing_profile, "speaker_reconciliation": speaker_reconciliation},
    )
    register_conversation_scope(
        conversation_id=conversation_id,
        person_id=person_id,
        evidence_kind="explicit_export",
        evidence={"export_id": export_id, "bundle_id": bundle["bundle_id"], "deep_audio_artifact_id": artifact_id},
    )
    link_artifact(
        child_table="conversations",
        child_id=conversation_id,
        parent_table="brainlive_event_bundles_v1514",
        parent_id=str(bundle["bundle_id"]),
        scope=scope,
        relation_type="deep_audio_refined_export",
    )
    return conversation_id, previous_ids


def bundles_require_deep_audio(*, person_id: str, package_date: str, live_session_id: str | None = None) -> bool:
    """Whether assembled scope contains audio evidence that must be refined before cleanup.

    This deliberately uses the V15.14 raw source ownership map plus the sensor
    table, not a fragile timeline label: older bundle JSON may omit `modality`
    even though it still owns an immutable speech-segment event.
    """
    ensure_deep_audio_schema()
    where = ["person_id=?", "package_date=?", "status='assembled'"]
    params: list[Any] = [person_id, package_date]
    if live_session_id:
        where.append("live_session_id=?")
        params.append(live_session_id)
    with connect() as con:
        bundles = _rows(
            con,
            f"SELECT raw_timeline_json FROM brainlive_event_bundles_v1514 WHERE {' AND '.join(where)}",
            tuple(params),
        )
        for bundle in bundles:
            source_ids = _bundle_audio_source_event_ids(bundle)
            if source_ids:
                marks = ",".join("?" for _ in source_ids)
                row = con.execute(
                    f"""SELECT 1 FROM brainlive_sensor_events
                         WHERE person_id=? AND event_id IN ({marks})
                           AND modality='audio' AND event_type IN ('speech_segment','speech_segment_failed')
                         LIMIT 1""",
                    (person_id, *sorted(source_ids)),
                ).fetchone()
                if row:
                    return True
                continue
            # Compatibility fallback for pre-membership bundles.
            raw = json_loads(bundle.get("raw_timeline_json"), []) or []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("modality") or "") == "audio" and str(item.get("row_kind") or "") in {"speech_segment", "speech_segment_failed"}:
                    return True
    return False

def run_offline_deep_audio_for_bundles(
    *,
    person_id: str,
    package_date: str,
    live_session_id: str | None = None,
    language: str = "fr",
    max_bundle_audio_seconds: float | None = None,
    max_gap_seconds: float = 8.0,
) -> dict[str, Any]:
    """Refine bundle conversations from retained raw audio.

    ``max_bundle_audio_seconds`` is a safety cap for offline work.  It does not
    truncate silently: a bundle over the cap fails with a visible reason and
    cannot be used by downstream Brain2/cleanup until an operator changes the
    cap or splits the scene.
    """
    if not person_id:
        raise DeepAudioError("deep audio requires explicit person_id")
    if max_bundle_audio_seconds is None:
        max_bundle_audio_seconds = get_settings().deep_audio_bundle_max_seconds
    if not math.isfinite(float(max_bundle_audio_seconds)) or float(max_bundle_audio_seconds) <= 0:
        raise DeepAudioError("max_bundle_audio_seconds must be a positive finite number")
    if not math.isfinite(float(max_gap_seconds)) or float(max_gap_seconds) < 0:
        raise DeepAudioError("max_gap_seconds must be a finite non-negative number")
    ensure_deep_audio_schema()
    bundle_schema_missing = False
    with connect() as con:
        # A resume of a pre-V18.5 run can legitimately have completed its
        # legacy Brain2 stage before V15.14 bundle tables ever existed.  There
        # is no retained bundle/source to refine in that migration state.  A
        # normal new post-stop run cannot reach here without the assembler
        # creating the table first, and its assembly/export gate rejects that
        # inconsistency before deep audio starts.
        if not _table_exists(con, "brainlive_event_bundles_v1514"):
            bundle_schema_missing = True
            bundles = []
        else:
            where = ["person_id=?", "package_date=?", "status='assembled'"]
            params: list[Any] = [person_id, package_date]
            if live_session_id:
                where.append("live_session_id=?")
                params.append(live_session_id)
            bundles = _rows(
                con,
                f"SELECT * FROM brainlive_event_bundles_v1514 WHERE {' AND '.join(where)} ORDER BY start_time,bundle_id",
                tuple(params),
            )
    run_id = stable_id("deep_audio_run_v185", person_id, package_date, live_session_id or "day", json_dumps([b.get("bundle_id") for b in bundles]))
    result: dict[str, Any] = {
        "version": VERSION,
        "run_id": run_id,
        "person_id": person_id,
        "package_date": package_date,
        "live_session_id": live_session_id,
        "status": "failed",
        "bundles_total": len(bundles),
        "bundles_refined": 0,
        "bundles_without_audio": 0,
        "artifacts": [],
        "errors": [],
    }
    if not bundles:
        result.update(status="ok", reason="no_bundle_schema" if bundle_schema_missing else "no_active_bundles")
        _persist_run(result)
        return result
    artifact_root = Path(os.environ.get("MLOMEGA_DEEP_AUDIO_DIR", str(Path(os.environ.get("MLOMEGA_HOME", ".mlomega_audio_elite")) / "deep_audio"))).expanduser().resolve()
    processing_profile = _processing_profile(person_id=person_id, language=language, max_gap_seconds=float(max_gap_seconds))
    result["processing_profile"] = processing_profile
    # One heavyweight runtime for all *pending* bundles in this closure.  It is
    # intentionally lazy: a resumed close-day with every artifact already
    # completed must not reload WhisperX/Pyannote merely to read checkpoints.
    # The runtime is shared by every bundle that genuinely needs transcription,
    # then released before deep vision and Brain2 obtain GPU time.
    with gpu_phase("post_stop_deep_audio", release_before=True, release_after=True):
        runtime: DeepAudioRuntime | None = None
        try:
            for bundle in bundles:
                expected = _audio_expected(bundle)
                pieces, missing = _pieces_for_bundle(bundle, person_id=person_id)
                if missing:
                    result["errors"].append({"bundle_id": bundle["bundle_id"], "error": "raw_audio_missing", "missing": missing})
                    continue
                if not pieces:
                    if expected:
                        result["errors"].append({"bundle_id": bundle["bundle_id"], "error": "audio_evidence_unresolvable"})
                    else:
                        result["bundles_without_audio"] += 1
                        result["artifacts"].append({"bundle_id": bundle["bundle_id"], "status": "no_audio"})
                    continue
                manifest = _source_manifest(pieces)
                source_digest = hashlib.sha256(json_dumps({"source_manifest": manifest, "processing_profile": processing_profile}).encode("utf-8")).hexdigest()
                artifact_id = stable_id("deep_audio_artifact_v185", bundle["bundle_id"], source_digest)
                with connect() as con:
                    existing = _one(
                        con,
                        "SELECT * FROM brainlive_deep_audio_artifacts_v185 WHERE bundle_id=? AND source_digest=?",
                        (bundle["bundle_id"], source_digest),
                    )
                if existing and str(existing.get("status")) == "completed" and existing.get("refined_conversation_id"):
                    result["bundles_refined"] += 1
                    result["artifacts"].append({"bundle_id": bundle["bundle_id"], "artifact_id": artifact_id, "status": "resumed", "conversation_id": existing.get("refined_conversation_id")})
                    continue
                try:
                    estimated_seconds = _estimate_tape_seconds(pieces, max_gap_seconds=float(max_gap_seconds))
                    if estimated_seconds > float(max_bundle_audio_seconds):
                        raise DeepAudioError(f"bundle_tape_too_long:{estimated_seconds:.1f}s>{float(max_bundle_audio_seconds):.1f}s")
                    tape, time_map = _stitch_pieces(
                        pieces,
                        artifact_dir=artifact_root / str(bundle["bundle_id"]),
                        artifact_id=artifact_id,
                        max_gap_seconds=float(max_gap_seconds),
                    )
                    # Import the same deep engine as the old direct flow.  The difference
                    # is only where the resulting transcript is attached: bundle revision,
                    # not a parallel flow-once conversation.
                    from .audio_pipeline import transcribe_with_whisperx
                    if runtime is None:
                        runtime = DeepAudioRuntime(language=language)
                        record_phase_event("deep_audio_runtime_opened", reason="first_pending_bundle")
                    transcript = retry_operation(
                        lambda: transcribe_with_whisperx(tape, language=language, runtime=runtime),
                        component=f"deep_audio:{bundle['bundle_id']}",
                        max_retries=get_settings().deep_audio_retry_max,
                        on_retry=lambda attempt, failure, delay: record_phase_event(
                            "deep_audio_retry", bundle_id=bundle["bundle_id"], attempt=attempt, error_code=failure.code, delay_s=delay
                        ),
                    )
                    speaker_reconciliation = _speaker_reconciliation(transcript, source_manifest=manifest, person_id=person_id)
                    _validate_transcript_for_brain2(transcript, time_map=time_map, person_id=person_id)
                    conversation_id, previous_ids = _export_refined_bundle(
                        bundle=bundle,
                        person_id=person_id,
                        artifact_id=artifact_id,
                        source_manifest=manifest,
                        time_map=time_map,
                        transcript=transcript,
                        processing_profile=processing_profile,
                        speaker_reconciliation=speaker_reconciliation,
                    )
                    with connect() as con, write_transaction(con):
                        upsert(
                            con,
                            "brainlive_deep_audio_artifacts_v185",
                            {
                                "artifact_id": artifact_id,
                                "person_id": person_id,
                                "package_date": package_date,
                                "run_id": run_id,
                                "bundle_id": bundle["bundle_id"],
                                "source_digest": source_digest,
                                "source_manifest_json": json_dumps(manifest),
                                "processing_profile_json": json_dumps(processing_profile),
                                "speaker_reconciliation_json": json_dumps(speaker_reconciliation),
                                "time_map_json": json_dumps(time_map),
                                "tape_duration_seconds": _duration_seconds(tape),
                                "stitched_audio_path": str(tape),
                                "stitched_audio_sha256": sha256_file(tape),
                                "transcript_json": json_dumps(transcript),
                                "transcript_sha256": hashlib.sha256(json_dumps(transcript).encode("utf-8")).hexdigest(),
                                "refined_conversation_id": conversation_id,
                                "superseded_conversation_ids_json": json_dumps(previous_ids),
                                "status": "completed",
                                "error_text": None,
                                "created_at": now_iso(),
                                "updated_at": now_iso(),
                            },
                            "artifact_id",
                        )
                    result["bundles_refined"] += 1
                    result["artifacts"].append({"bundle_id": bundle["bundle_id"], "artifact_id": artifact_id, "status": "completed", "conversation_id": conversation_id, "superseded_conversation_ids": previous_ids})
                except Exception as exc:
                    error = str(exc)[:2000]
                    with connect() as con, write_transaction(con):
                        upsert(
                            con,
                            "brainlive_deep_audio_artifacts_v185",
                            {
                                "artifact_id": artifact_id,
                                "person_id": person_id,
                                "package_date": package_date,
                                "run_id": run_id,
                                "bundle_id": bundle["bundle_id"],
                                "source_digest": source_digest,
                                "source_manifest_json": json_dumps(manifest),
                                "processing_profile_json": json_dumps(processing_profile),
                                "speaker_reconciliation_json": "{}",
                                "time_map_json": "[]",
                                "tape_duration_seconds": None,
                                "stitched_audio_path": None,
                                "stitched_audio_sha256": None,
                                "transcript_json": "{}",
                                "transcript_sha256": None,
                                "refined_conversation_id": None,
                                "superseded_conversation_ids_json": "[]",
                                "status": "error",
                                "error_text": error,
                                "created_at": now_iso(),
                                "updated_at": now_iso(),
                            },
                            "artifact_id",
                        )
                    failure = classify_failure(exc)
                    result["errors"].append({"bundle_id": bundle["bundle_id"], "artifact_id": artifact_id, "error": error, "error_code": failure.code, "retryable": failure.retryable})
        finally:
            if runtime is not None:
                runtime.close()
                record_phase_event("deep_audio_runtime_closed")
    if not result["errors"]:
        result["status"] = "ok"
    elif all(bool(item.get("retryable")) for item in result["errors"]):
        result["status"] = "retryable_error"
    else:
        result["status"] = "blocked"
    _persist_run(result)
    if result["status"] != "ok":
        prefix = "deep_audio_retryable" if result["status"] == "retryable_error" else "deep_audio_blocked"
        raise DeepAudioError(f"{prefix}: deep audio failed for {len(result['errors'])} bundle(s)")
    return result


def _persist_run(result: dict[str, Any]) -> None:
    ensure_deep_audio_schema()
    with connect() as con, write_transaction(con):
        existing = _one(con, "SELECT created_at FROM brainlive_deep_audio_runs_v185 WHERE run_id=?", (result["run_id"],)) or {}
        upsert(
            con,
            "brainlive_deep_audio_runs_v185",
            {
                "run_id": result["run_id"],
                "person_id": result["person_id"],
                "package_date": result["package_date"],
                "live_session_id": result.get("live_session_id"),
                "status": result["status"],
                "bundles_total": int(result.get("bundles_total") or 0),
                "bundles_refined": int(result.get("bundles_refined") or 0),
                "bundles_without_audio": int(result.get("bundles_without_audio") or 0),
                "result_json": json_dumps(result),
                "error_text": json_dumps(result.get("errors") or []) if result.get("errors") else None,
                "created_at": existing.get("created_at") or now_iso(),
                "updated_at": now_iso(),
            },
            "run_id",
        )


def deep_audio_audit(person_id: str, *, package_date: str | None = None) -> dict[str, Any]:
    ensure_deep_audio_schema()
    where = "person_id=?"
    params: list[Any] = [person_id]
    if package_date:
        where += " AND package_date=?"
        params.append(package_date)
    with connect() as con:
        runs = _rows(con, f"SELECT * FROM brainlive_deep_audio_runs_v185 WHERE {where} ORDER BY updated_at DESC LIMIT 20", tuple(params))
        artifacts = _rows(con, f"SELECT status,COUNT(*) AS n FROM brainlive_deep_audio_artifacts_v185 WHERE {where} GROUP BY status", tuple(params))
    return {
        "version": VERSION,
        "person_id": person_id,
        "package_date": package_date,
        "runs": runs,
        "artifact_status_counts": {str(row["status"]): int(row["n"]) for row in artifacts},
        "verdict": "ready" if runs and str(runs[0].get("status")) == "ok" else "attention",
    }
