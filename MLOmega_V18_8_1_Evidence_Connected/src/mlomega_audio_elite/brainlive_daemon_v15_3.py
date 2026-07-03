from __future__ import annotations

"""V15.3 BrainLive daemon: audio/VAD + vision + active context loop.

This module is intentionally operational rather than another Brain2 clone.
Brain2 remains the long-term/deep source of truth. BrainLive daemon only keeps a
hot short-horizon loop alive during the day:

  audio/image/location/person signals -> H0/H1/H2 BrainLive ticks -> intervention
  queue / observation / outcome watch -> nightly Brain2 consolidation.

No psychological interpretation is done with regex or keywords. Signal-processing
fallbacks (energy VAD, file polling, timestamps) are allowed; any meaning-making
requires LLM/VLM output or already persisted Brain2 context.
"""

import json
import os
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .brainlive_longitudinal_v15_1 import evaluate_outcomes_auto, scheduler_tick
from .brainlive_realtime_v15_2 import live_cycle_all_horizons, live_tick, resolve_active_speaker
from .brainlive_v15 import build_active_context, ensure_brainlive_schema, start_live_session
from .config import get_settings
from .db import connect, init_db, upsert, write_transaction
from .utils import json_dumps, json_loads, now_iso, sha256_file, stable_id
from .governance_v18 import Scope, claim_work, finish_work, work_scope_key
from .v18_delivery import enqueue_delivery, ensure_delivery_schema
from .v18_runtime_hardening import classify_llm_exception

VERSION = "15.3.0-brainlive-daemon-vad-loop"

DAEMON_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_daemon_configs(
  config_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  mode TEXT DEFAULT 'live_daytime',
  vad_backend TEXT DEFAULT 'energy',
  asr_backend TEXT DEFAULT 'external_or_whispercpp',
  transcript_watch_dir TEXT,
  image_watch_dir TEXT,
  audio_watch_dir TEXT,
  mic_device TEXT,
  h0_enabled INTEGER DEFAULT 1,
  h1_enabled INTEGER DEFAULT 1,
  h2_enabled INTEGER DEFAULT 1,
  h1_interval_s REAL DEFAULT 5.0,
  h2_interval_s REAL DEFAULT 12.0,
  vision_interval_s REAL DEFAULT 10.0,
  active_context_refresh_s REAL DEFAULT 15.0,
  outcome_watch_interval_s REAL DEFAULT 60.0,
  max_iterations INTEGER DEFAULT 0,
  sleep_s REAL DEFAULT 1.0,
  status TEXT DEFAULT 'active',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_daemon_runs(
  daemon_run_id TEXT PRIMARY KEY,
  config_id TEXT,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  stopped_at TEXT,
  iterations INTEGER DEFAULT 0,
  last_heartbeat_at TEXT,
  counters_json TEXT DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_audio_chunks(
  chunk_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  source_path TEXT,
  chunk_path TEXT,
  source_sha256 TEXT,
  start_s REAL,
  end_s REAL,
  vad_backend TEXT,
  vad_score REAL DEFAULT 0.0,
  asr_backend TEXT,
  asr_status TEXT DEFAULT 'pending',
  transcript_text TEXT,
  speaker_resolution_json TEXT DEFAULT '{}',
  processed_at TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_signal_events(
  signal_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  signal_type TEXT NOT NULL,
  source_path TEXT,
  source_sha256 TEXT,
  payload_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'queued',
  consumed_at TEXT,
  result_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_intervention_delivery_queue(
  delivery_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  tick_id TEXT,
  candidate_id TEXT,
  horizon TEXT,
  message TEXT,
  action_type TEXT DEFAULT 'notify',
  delivery_status TEXT DEFAULT 'queued',
  priority REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_intervention_delivery_dedupes(
  dedupe_key TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  owner_horizon TEXT NOT NULL,
  candidate_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bld_delivery_dedupes_session ON brainlive_intervention_delivery_dedupes(live_session_id, created_at);
CREATE TABLE IF NOT EXISTS brainlive_context_refresh_log(
  refresh_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  active_context_id TEXT,
  trigger_reason TEXT,
  latency_ms INTEGER,
  summary_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_outcome_watch_log(
  watch_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bld_cfg_person ON brainlive_daemon_configs(person_id, status);
CREATE INDEX IF NOT EXISTS idx_bld_run_session ON brainlive_daemon_runs(live_session_id, status);
CREATE INDEX IF NOT EXISTS idx_bld_chunks_session ON brainlive_audio_chunks(live_session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bld_signal_session ON brainlive_signal_events(live_session_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_bld_delivery_session ON brainlive_intervention_delivery_queue(live_session_id, delivery_status, created_at);
"""

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TRANSCRIPT_EXTS = {".txt", ".json", ".jsonl"}


@dataclass
class DaemonConfig:
    person_id: str
    transcript_watch_dir: str | None = None
    image_watch_dir: str | None = None
    audio_watch_dir: str | None = None
    vad_backend: str = "energy"
    asr_backend: str = "external_or_whispercpp"
    h1_interval_s: float = 5.0
    h2_interval_s: float = 12.0
    vision_interval_s: float = 10.0
    active_context_refresh_s: float = 15.0
    outcome_watch_interval_s: float = 60.0
    sleep_s: float = 1.0
    max_iterations: int = 0
    metadata: dict[str, Any] | None = None


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = _one(con, "SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1")
    return str(row["person_id"]) if row and row.get("person_id") else "me"


def _migrate_delivery_dedupe_reservation_schema(con) -> None:
    """Remove the legacy FK that made reserve-before-queue impossible.

    The dedupe row is intentionally inserted *before* its queue projection to
    establish a cross-process unique reservation.  A foreign key to the queue
    therefore defeats the atomicity pattern.  The reservation remains auditable
    by ``delivery_id`` but is not a child row.
    """
    fks = [dict(row) for row in con.execute("PRAGMA foreign_key_list(brainlive_intervention_delivery_dedupes)").fetchall()]
    if not any(row.get("table") == "brainlive_intervention_delivery_queue" for row in fks):
        return
    con.executescript("""
        CREATE TABLE brainlive_intervention_delivery_dedupes_v18_new(
          dedupe_key TEXT PRIMARY KEY,
          delivery_id TEXT NOT NULL,
          live_session_id TEXT NOT NULL,
          owner_horizon TEXT NOT NULL,
          candidate_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        INSERT INTO brainlive_intervention_delivery_dedupes_v18_new
          (dedupe_key,delivery_id,live_session_id,owner_horizon,candidate_fingerprint,created_at)
          SELECT dedupe_key,delivery_id,live_session_id,owner_horizon,candidate_fingerprint,created_at
          FROM brainlive_intervention_delivery_dedupes;
        DROP TABLE brainlive_intervention_delivery_dedupes;
        ALTER TABLE brainlive_intervention_delivery_dedupes_v18_new RENAME TO brainlive_intervention_delivery_dedupes;
        CREATE INDEX IF NOT EXISTS idx_bld_delivery_dedupes_session
          ON brainlive_intervention_delivery_dedupes(live_session_id, created_at);
    """)


def ensure_daemon_schema() -> None:
    ensure_brainlive_schema()
    init_db()
    ensure_delivery_schema()
    # Realtime schema is ensured indirectly by live_tick/live_cycle; create daemon tables here.
    with connect() as con:
        con.executescript(DAEMON_SCHEMA)
        _migrate_delivery_dedupe_reservation_schema(con)
        con.commit()


def configure_daemon(
    *,
    person_id: str | None = None,
    transcript_watch_dir: str | None = None,
    image_watch_dir: str | None = None,
    audio_watch_dir: str | None = None,
    vad_backend: str = "energy",
    asr_backend: str = "external_or_whispercpp",
    h1_interval_s: float = 5.0,
    h2_interval_s: float = 12.0,
    vision_interval_s: float = 10.0,
    active_context_refresh_s: float = 15.0,
    outcome_watch_interval_s: float = 60.0,
    sleep_s: float = 1.0,
    max_iterations: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not person_id:
        from .governance_v18 import ScopeError
        raise ScopeError("BrainLive daemon configuration requires explicit person_id")
    ensure_daemon_schema()
    now = now_iso()
    with connect() as con:
        config_id = stable_id("bldcfg", person_id, transcript_watch_dir, image_watch_dir, audio_watch_dir)
        upsert(con, "brainlive_daemon_configs", {
            "config_id": config_id,
            "person_id": person_id,
            "mode": "live_daytime",
            "vad_backend": vad_backend,
            "asr_backend": asr_backend,
            "transcript_watch_dir": transcript_watch_dir,
            "image_watch_dir": image_watch_dir,
            "audio_watch_dir": audio_watch_dir,
            "mic_device": None,
            "h0_enabled": 1,
            "h1_enabled": 1,
            "h2_enabled": 1,
            "h1_interval_s": float(h1_interval_s),
            "h2_interval_s": float(h2_interval_s),
            "vision_interval_s": float(vision_interval_s),
            "active_context_refresh_s": float(active_context_refresh_s),
            "outcome_watch_interval_s": float(outcome_watch_interval_s),
            "max_iterations": int(max_iterations),
            "sleep_s": float(sleep_s),
            "status": "active",
            "metadata_json": json_dumps(metadata or {}),
            "created_at": now,
            "updated_at": now,
        }, "config_id")
        con.commit()
    return {"config_id": config_id, "person_id": person_id, "status": "active"}


def _load_config(con, config_id: str | None = None, person_id: str | None = None) -> dict[str, Any]:
    if not person_id:
        from .governance_v18 import ScopeError
        raise ScopeError("BrainLive daemon requires explicit person_id")
    if config_id:
        row = _one(con, "SELECT * FROM brainlive_daemon_configs WHERE config_id=?", (config_id,))
        if not row:
            raise ValueError(f"Configuration daemon introuvable: {config_id}")
        if row.get("person_id") != person_id:
            from .governance_v18 import ScopeError
            raise ScopeError("BrainLive daemon config owner does not match explicit person_id")
        return row
    row = _one(con, "SELECT * FROM brainlive_daemon_configs WHERE person_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (person_id,))
    if row:
        return row
    # ephemeral default, not persisted
    return {
        "config_id": None,
        "person_id": person_id,
        "vad_backend": "energy",
        "asr_backend": "external_or_whispercpp",
        "transcript_watch_dir": None,
        "image_watch_dir": None,
        "audio_watch_dir": None,
        "h1_interval_s": 5.0,
        "h2_interval_s": 12.0,
        "vision_interval_s": 10.0,
        "active_context_refresh_s": 15.0,
        "outcome_watch_interval_s": 60.0,
        "max_iterations": 0,
        "sleep_s": 1.0,
    }


def _seconds_since(iso_value: str | None) -> float:
    if not iso_value:
        return 10**9
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 10**9


def _file_seen(con, live_session_id: str, source_path: str | Path) -> bool:
    p = Path(source_path).expanduser().resolve()
    try:
        h = sha256_file(p)
    except Exception:
        h = None
    row = _one(con, "SELECT signal_id FROM brainlive_signal_events WHERE live_session_id=? AND source_path=? AND COALESCE(source_sha256,'')=COALESCE(?, '') LIMIT 1", (live_session_id, str(p), h))
    return bool(row)


def _record_signal(con, live_session_id: str, signal_type: str, source_path: str | Path | None, payload: dict[str, Any], status: str = "queued") -> str:
    p = Path(source_path).expanduser().resolve() if source_path else None
    h = sha256_file(p) if p and p.exists() and p.is_file() else None
    sid = stable_id("bldsig", live_session_id, signal_type, str(p), h, payload.get("offset") or payload.get("timestamp") or "")
    upsert(con, "brainlive_signal_events", {
        "signal_id": sid,
        "live_session_id": live_session_id,
        "signal_type": signal_type,
        "source_path": str(p) if p else None,
        "source_sha256": h,
        "payload_json": json_dumps(payload),
        "status": status,
        "consumed_at": None,
        "result_json": json_dumps({}),
        "created_at": now_iso(),
    }, "signal_id")
    return sid


def _iter_new_files(con, live_session_id: str, directory: str | None, exts: set[str], limit: int = 20) -> list[Path]:
    if not directory:
        return []
    d = Path(directory).expanduser().resolve()
    if not d.exists():
        return []
    files = sorted([p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts], key=lambda p: p.stat().st_mtime)
    out: list[Path] = []
    for p in files:
        if not _file_seen(con, live_session_id, p):
            out.append(p)
        if len(out) >= limit:
            break
    return out


def parse_transcript_signal(path: str | Path) -> list[dict[str, Any]]:
    """Parse external ASR output without semantic heuristics.

    Supported shapes:
    - .txt: one final turn with raw file content
    - .json: object with text/speaker_person_id/speaker_label/location_hint, or list of such objects
    - .jsonl: one object per line
    """
    p = Path(path).expanduser().resolve()
    if p.suffix.lower() == ".txt":
        text = p.read_text(encoding="utf-8", errors="ignore").strip()
        return [{"text": text, "source": "txt", "metadata": {"path": str(p)}}] if text else []
    if p.suffix.lower() == ".jsonl":
        items = []
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("text"):
                    items.append(obj)
            except Exception:
                continue
        return items
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict) and x.get("text")]
        if isinstance(data, dict):
            if isinstance(data.get("segments"), list):
                return [x for x in data["segments"] if isinstance(x, dict) and x.get("text")]
            if data.get("text"):
                return [data]
    return []


def segment_wav_energy_vad(path: str | Path, *, min_speech_s: float = 0.35, frame_ms: int = 30, threshold_ratio: float = 2.5, max_chunks: int = 20) -> list[dict[str, Any]]:
    """Deterministic signal-processing VAD for WAV only.

    This is not psychological inference and does not read content. It detects
    speech-like energy regions so the daemon can hand chunks to ASR.
    """
    p = Path(path).expanduser().resolve()
    if p.suffix.lower() != ".wav":
        return []
    with wave.open(str(p), "rb") as w:
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        frames = w.getnframes()
        raw = w.readframes(frames)
    if sampwidth != 2 or rate <= 0:
        return []
    import array
    samples = array.array("h")
    samples.frombytes(raw)
    if channels > 1:
        samples = array.array("h", samples[::channels])
    frame_len = max(1, int(rate * frame_ms / 1000))
    energies = []
    for i in range(0, len(samples), frame_len):
        frame = samples[i:i+frame_len]
        if not frame:
            continue
        e = sum(abs(x) for x in frame) / len(frame)
        energies.append(e)
    if not energies:
        return []
    sorted_e = sorted(energies)
    noise = sorted_e[max(0, int(len(sorted_e) * 0.2) - 1)] or 1.0
    threshold = max(noise * threshold_ratio, 120.0)
    segments = []
    in_seg = False
    start_idx = 0
    best_score = 0.0
    for idx, e in enumerate(energies):
        speech = e >= threshold
        if speech and not in_seg:
            in_seg = True
            start_idx = idx
            best_score = e / threshold
        elif speech and in_seg:
            best_score = max(best_score, e / threshold)
        elif not speech and in_seg:
            end_idx = idx
            start_s = start_idx * frame_ms / 1000.0
            end_s = end_idx * frame_ms / 1000.0
            if end_s - start_s >= min_speech_s:
                segments.append({"start_s": start_s, "end_s": end_s, "vad_score": min(1.0, best_score / 10.0), "backend": "energy"})
            in_seg = False
            if len(segments) >= max_chunks:
                break
    if in_seg and len(segments) < max_chunks:
        end_idx = len(energies)
        start_s = start_idx * frame_ms / 1000.0
        end_s = end_idx * frame_ms / 1000.0
        if end_s - start_s >= min_speech_s:
            segments.append({"start_s": start_s, "end_s": end_s, "vad_score": min(1.0, best_score / 10.0), "backend": "energy"})
    return segments


def _copy_audio_chunk(source: Path, out_dir: Path, start_s: float, end_s: float) -> Path:
    """Create a chunk if ffmpeg exists; otherwise copy source as evidence."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{source.stem}_{start_s:.2f}_{end_s:.2f}.wav"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start_s), "-to", str(end_s), "-i", str(source), "-ac", "1", "-ar", "16000", str(out)]
        subprocess.run(cmd, check=False)
        if out.exists():
            return out
    # Fallback evidence only, not perfect chunking.
    out = out_dir / source.name
    if not out.exists():
        shutil.copy2(source, out)
    return out


def transcribe_audio_chunk(path: str | Path, *, backend: str = "external_or_whispercpp") -> dict[str, Any]:
    """Transcribe audio if a configured backend exists; otherwise require ASR.

    Supported non-invasive options:
    - sidecar .txt next to chunk/source
    - MLOMEGA_WHISPERCPP_BIN pointing to whisper.cpp CLI
    - faster_whisper if installed and backend includes 'faster'
    """
    p = Path(path).expanduser().resolve()
    sidecar = p.with_suffix(".txt")
    if sidecar.exists():
        return {"status": "ok", "backend": "sidecar_txt", "text": sidecar.read_text(encoding="utf-8", errors="ignore").strip()}
    whispercpp = os.environ.get("MLOMEGA_WHISPERCPP_BIN")
    model = os.environ.get("MLOMEGA_WHISPERCPP_MODEL")
    if whispercpp and model and Path(whispercpp).exists() and Path(model).exists():
        try:
            cmd = [whispercpp, "-m", model, "-f", str(p), "-nt", "-otxt", "-of", str(p.with_suffix(""))]
            subprocess.run(cmd, check=False, timeout=float(os.environ.get("MLOMEGA_ASR_TIMEOUT", "60")))
            if sidecar.exists():
                return {"status": "ok", "backend": "whispercpp", "text": sidecar.read_text(encoding="utf-8", errors="ignore").strip()}
        except Exception as exc:
            return {"status": "asr_error", "backend": "whispercpp", "text": "", "error": str(exc)[:500]}
    if "faster" in backend:
        try:
            from faster_whisper import WhisperModel  # type: ignore
            settings = get_settings()
            model_name = os.environ.get("MLOMEGA_FAST_WHISPER_MODEL", settings.whisperx_model or "small")
            device = os.environ.get("MLOMEGA_FAST_WHISPER_DEVICE", settings.whisperx_device or "cpu")
            compute_type = os.environ.get("MLOMEGA_FAST_WHISPER_COMPUTE", settings.whisperx_compute_type or "int8")
            model_obj = WhisperModel(model_name, device=device, compute_type=compute_type)
            segments, _info = model_obj.transcribe(str(p), beam_size=1, vad_filter=False)
            text = " ".join(s.text.strip() for s in segments if getattr(s, "text", "").strip()).strip()
            return {"status": "ok" if text else "empty", "backend": "faster_whisper", "text": text}
        except Exception as exc:
            return {"status": "asr_error", "backend": "faster_whisper", "text": "", "error": str(exc)[:500]}
    return {"status": "asr_required", "backend": backend, "text": "", "hint": "Provide transcript_watch_dir, sidecar .txt, whisper.cpp env vars, or faster-whisper."}


def process_audio_file(live_session_id: str, audio_path: str | Path, *, speaker_person_id: str | None = None, speaker_label: str | None = None, vad_backend: str = "energy", asr_backend: str = "external_or_whispercpp") -> dict[str, Any]:
    ensure_daemon_schema()
    p = Path(audio_path).expanduser().resolve()
    settings = get_settings()
    chunks_dir = settings.raw_dir / "brainlive_chunks" / live_session_id
    if vad_backend == "energy":
        segments = segment_wav_energy_vad(p)
    else:
        # No fake VAD. Unknown backends must be provided upstream.
        segments = []
    if not segments:
        segments = [{"start_s": None, "end_s": None, "vad_score": 0.0, "backend": "no_vad_full_file"}]
    results = []
    with connect() as con:
        for seg in segments:
            chunk_path = _copy_audio_chunk(p, chunks_dir, seg.get("start_s") or 0.0, seg.get("end_s") or 0.0) if seg.get("start_s") is not None else p
            asr = transcribe_audio_chunk(chunk_path, backend=asr_backend)
            speaker = resolve_active_speaker(audio_sample_path=str(chunk_path), explicit_person_id=speaker_person_id, speaker_label=speaker_label)
            chunk_id = stable_id("bldchunk", live_session_id, str(p), seg.get("start_s"), seg.get("end_s"), sha256_file(p))
            upsert(con, "brainlive_audio_chunks", {
                "chunk_id": chunk_id,
                "live_session_id": live_session_id,
                "source_path": str(p),
                "chunk_path": str(chunk_path),
                "source_sha256": sha256_file(p),
                "start_s": seg.get("start_s"),
                "end_s": seg.get("end_s"),
                "vad_backend": seg.get("backend") or vad_backend,
                "vad_score": float(seg.get("vad_score") or 0.0),
                "asr_backend": asr.get("backend"),
                "asr_status": asr.get("status"),
                "transcript_text": asr.get("text"),
                "speaker_resolution_json": json_dumps(speaker),
                "processed_at": now_iso(),
                "metadata_json": json_dumps({"asr": asr, "segment": seg}),
                "created_at": now_iso(),
            }, "chunk_id")
            payload = {"chunk_id": chunk_id, "text": asr.get("text"), "speaker": speaker, "asr": asr}
            _record_signal(con, live_session_id, "audio_chunk", p, payload, status="queued" if asr.get("text") else "asr_required")
            results.append(payload)
        con.commit()
    return {"live_session_id": live_session_id, "audio_path": str(p), "segments": len(segments), "chunks": results}


def refresh_active_context_hot(live_session_id: str, *, trigger_reason: str = "daemon_refresh", limit: int = 32) -> dict[str, Any]:
    ensure_daemon_schema()
    started = time.time()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        person_id = sess["person_id"]
        people = _many(con, "SELECT person_id FROM brainlive_person_presence WHERE live_session_id=? AND status='active' AND person_id IS NOT NULL ORDER BY confidence DESC LIMIT 8", (live_session_id,)) if _one(con, "SELECT name FROM sqlite_master WHERE type='table' AND name='brainlive_person_presence'") else []
    active_people = [p["person_id"] for p in people if p.get("person_id")]
    ctx = build_active_context(live_session_id, active_people=active_people or None, limit=limit)
    latency_ms = int((time.time() - started) * 1000)
    with connect() as con:
        rid = stable_id("bldctx", live_session_id, trigger_reason, now_iso(), uuid4().hex)
        summary = {
            "active_people": active_people,
            "loaded_keys": sorted(list((ctx.get("context") or {}).keys()))[:40],
            "contract": "Brain2 source of truth; BrainLive hot projection only",
        }
        upsert(con, "brainlive_context_refresh_log", {
            "refresh_id": rid,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "active_context_id": ctx.get("active_context_id"),
            "trigger_reason": trigger_reason,
            "latency_ms": latency_ms,
            "summary_json": json_dumps(summary),
            "created_at": now_iso(),
        }, "refresh_id")
        con.commit()
    return {"refresh_id": rid, "active_context_id": ctx.get("active_context_id"), "latency_ms": latency_ms, "summary": summary}


def _delivery_dedupe_key(live_session_id: str, tick_result: dict[str, Any], candidate: dict[str, Any], message: str, action_type: str) -> str:
    """Stable delivery identity for a shared live analysis.

    V18 may expose the same analysis through H0/H1/H2 for observability.  A
    delivery is owned by H1, and this key makes accidental legacy fan-out
    idempotent even when a caller invokes the enqueue function repeatedly.
    """
    analysis = tick_result.get("analysis") if isinstance(tick_result, dict) else {}
    result = tick_result.get("result") if isinstance(tick_result, dict) else {}
    analysis = analysis if isinstance(analysis, dict) else {}
    result = result if isinstance(result, dict) else {}
    shared_ref = (
        tick_result.get("shared_tick_id")
        or tick_result.get("tick_id")
        or result.get("shared_analysis_run_id")
        or analysis.get("run_id")
        or tick_result.get("analysis_run_id")
        or "legacy-no-analysis-id"
    )
    candidate_fingerprint = {
        "candidate_id": candidate.get("candidate_id"),
        "message": str(message).strip(),
        "action_type": str(action_type or "notify"),
        "cooldown_key": candidate.get("cooldown_key"),
        "recommended_timing": candidate.get("recommended_timing"),
    }
    return stable_id("blddelivery-v18", live_session_id, shared_ref, candidate_fingerprint)


def enqueue_interventions_from_tick(
    live_session_id: str,
    tick_result: dict[str, Any],
    *,
    delivery_owner_horizon: str = "H1",
) -> list[str]:
    """Compatibility bridge to the one V18 H1 delivery primitive.

    The historical daemon may still surface a shared analysis under three
    horizon views.  This adapter keeps its caller contract but delegates the
    actual enqueue to the exact same durable queue used by the hot service and
    Phone Bridge.  No legacy path owns an independent queue any more.
    """
    ensure_daemon_schema()
    if str(delivery_owner_horizon).upper() != "H1":
        return []
    analysis = tick_result.get("analysis") or {}
    result = tick_result.get("result") or {}
    q = analysis.get("qwen_json") or analysis.get("output") or result.get("output") or {}
    if isinstance(q, str):
        q = json_loads(q, {})
    candidates: list[dict[str, Any]] = []
    for key in ("intervention_candidates", "interventions", "actions", "recommended_interventions"):
        val = q.get(key) if isinstance(q, dict) else None
        if isinstance(val, list):
            candidates.extend([x for x in val if isinstance(x, dict)])
        nested = result.get(key) if isinstance(result, dict) else None
        if isinstance(nested, list):
            candidates.extend([x for x in nested if isinstance(x, dict)])
    if isinstance(q, dict) and isinstance(q.get("intervention"), dict):
        candidates.append(q["intervention"])
    ids: list[str] = []
    for candidate in candidates:
        message = candidate.get("message") or candidate.get("text") or candidate.get("say") or candidate.get("intervention_message")
        speak = candidate.get("speak_now") if "speak_now" in candidate else candidate.get("speak")
        if not message or speak is False:
            continue
        action_type = str(candidate.get("action_type") or "notify")
        old_key = _delivery_dedupe_key(live_session_id, tick_result, candidate, str(message), action_type)
        # Keep an already-written pre-V18 reservation authoritative, then
        # create all new records through the central primitive.
        with connect() as con:
            old = _one(con, "SELECT delivery_id FROM brainlive_intervention_delivery_dedupes WHERE dedupe_key=?", (old_key,))
        if old:
            ids.append(str(old["delivery_id"]))
            continue
        queued = enqueue_delivery(
            live_session_id=live_session_id,
            source_key=old_key,
            candidate={**candidate, "message": str(message), "decision": "queue", "action_type": action_type},
        )
        if queued.get("delivery_id"):
            ids.append(str(queued["delivery_id"]))
    return list(dict.fromkeys(ids))

def outcome_watch(live_session_id: str, *, person_id: str | None = None, limit: int = 20, timeout: float = 120.0) -> dict[str, Any]:
    ensure_daemon_schema()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        person_id = person_id or sess["person_id"]
    try:
        result = evaluate_outcomes_auto(person_id=person_id, limit=limit, timeout=timeout)
        status = "ok"
    except Exception as exc:
        result = {"error": str(exc)[:2000]}
        status = "error"
    with connect() as con:
        wid = stable_id("bldwatch", live_session_id, now_iso(), uuid4().hex)
        upsert(con, "brainlive_outcome_watch_log", {
            "watch_id": wid,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "status": status,
            "result_json": json_dumps(result),
            "created_at": now_iso(),
        }, "watch_id")
        con.commit()
    return {"watch_id": wid, "status": status, "result": result}


def _raise_if_cycle_failed(cycle: Mapping[str, Any]) -> None:
    """Convert hidden per-horizon errors into durable work failures.

    Older realtime helpers sometimes return an error status inside a perfectly
    valid dictionary.  Treating that dictionary as success consumed the source
    signal and silently lost the forecast/intervention opportunity.  A daemon
    source is successful only when every requested horizon is not in an error
    state; the V18 lease wrapper then selects retry/quarantine explicitly.
    """
    terminal_statuses = {
        "error", "llm_error", "retryable_error", "terminal_error",
        "quarantined", "quarantined_invalid_llm_output", "context_incomplete",
        "llm_required", "invalid_contract", "truncated_output",
    }
    for horizon in ("H0", "H1", "H2"):
        item = cycle.get(horizon) if isinstance(cycle, Mapping) else None
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or item.get("state") or "").lower()
        result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
        nested = str(result.get("status") or result.get("state") or "").lower()
        if status in terminal_statuses or nested in terminal_statuses:
            detail = item.get("error") or result.get("error") or result.get("error_text") or status or nested
            raise RuntimeError(f"BrainLive {horizon} did not complete for daemon source: {detail}")


def _consume_signal(live_session_id: str, signal: dict[str, Any], *, use_llm: bool = True, use_vlm: bool = True) -> dict[str, Any]:
    payload = json_loads(signal.get("payload_json"), {}) or {}
    signal_type = signal.get("signal_type")
    if signal_type == "transcript":
        results = []
        for item in payload.get("items", []):
            text = item.get("text")
            if not text:
                continue
            cycle = live_cycle_all_horizons(
                live_session_id,
                text=text,
                speaker_label=item.get("speaker_label"),
                speaker_person_id=item.get("speaker_person_id"),
                location_hint=item.get("location_hint"),
                use_vlm=False,
                use_llm=use_llm,
            )
            _raise_if_cycle_failed(cycle)
            delivery_ids = enqueue_interventions_from_tick(live_session_id, cycle.get("H1") or {}, delivery_owner_horizon="H1")
            results.append({**cycle, "delivery_ids": delivery_ids, "delivery_owner_horizon": "H1"})
        return {"type": signal_type, "cycles": results}
    if signal_type == "audio_chunk":
        text = payload.get("text")
        if not text:
            return {"type": signal_type, "status": "asr_required"}
        speaker = payload.get("speaker") or {}
        cycle = live_cycle_all_horizons(live_session_id, text=text, speaker_label=speaker.get("label"), speaker_person_id=speaker.get("person_id"), use_vlm=False, use_llm=use_llm)
        _raise_if_cycle_failed(cycle)
        delivery_ids = enqueue_interventions_from_tick(live_session_id, cycle.get("H1") or {}, delivery_owner_horizon="H1")
        return {"type": signal_type, "cycle": cycle, "delivery_ids": delivery_ids, "delivery_owner_horizon": "H1"}
    if signal_type == "image":
        # The daemon must carry its durable signal identity into the V18 image
        # source map.  Calling the historical live_tick(image_path=...) lost
        # that identity and made VLM retries capable of duplicating a frame.
        from .brainlive_sensor_fusion_v15_4 import ingest_image_sensor, build_fused_situation, run_fused_horizons
        image_path = signal.get("source_path")
        if not image_path:
            raise ValueError("image daemon signal has no source_path")
        observed = ingest_image_sensor(
            live_session_id,
            image_path,
            use_vlm=use_vlm,
            source_event_id=str(signal.get("signal_id") or ""),
            source_occurred_at=str(signal.get("created_at") or now_iso()),
            source_device="brainlive_daemon",
        )
        fused = build_fused_situation(live_session_id, use_llm=use_llm)
        run = run_fused_horizons(live_session_id, fused_id=fused.get("fused_id"), use_llm=use_llm, use_vlm=False)
        _raise_if_cycle_failed(run.get("cycle") if isinstance(run, Mapping) else {})
        return {"type": signal_type, "image": observed, "fused": fused, "run": run, "delivery_ids": list(run.get("delivery_ids") or []), "delivery_owner_horizon": "H1"}
    return {"type": signal_type, "status": "ignored_unknown_signal"}

def poll_sources_once(live_session_id: str, config: dict[str, Any]) -> dict[str, Any]:
    ensure_daemon_schema()
    discovered = {"transcripts": 0, "images": 0, "audio": 0}
    with connect() as con:
        for p in _iter_new_files(con, live_session_id, config.get("transcript_watch_dir"), TRANSCRIPT_EXTS, limit=20):
            items = parse_transcript_signal(p)
            _record_signal(con, live_session_id, "transcript", p, {"items": items}, status="queued" if items else "empty")
            discovered["transcripts"] += 1
        for p in _iter_new_files(con, live_session_id, config.get("image_watch_dir"), IMAGE_EXTS, limit=5):
            _record_signal(con, live_session_id, "image", p, {}, status="queued")
            discovered["images"] += 1
        for p in _iter_new_files(con, live_session_id, config.get("audio_watch_dir"), AUDIO_EXTS, limit=5):
            _record_signal(con, live_session_id, "audio_file", p, {}, status="queued")
            discovered["audio"] += 1
        con.commit()
    return discovered



def _claim_daemon_signal(*, person_id: str, live_session_id: str, signal: Mapping[str, Any]) -> dict[str, Any] | None:
    signal_id = str(signal.get("signal_id") or "")
    if not signal_id:
        raise ValueError("daemon signal has no signal_id")
    # A daemon retry is identity-bound to the original inbox signal, not to
    # a recalculated payload/tick.  This remains stable across retries and
    # concurrent workers while the Scope keeps it owner-isolated.
    key = f"signal:{signal_id}"
    return claim_work(
        work_type="brainlive:daemon_signal",
        scope=Scope(person_id=person_id, live_session_id=live_session_id, mode="live"),
        source_key_value=key,
        lease_seconds=120,
        max_attempts=5,
    )


def _set_signal_result(signal_id: str, *, status: str, result: dict[str, Any]) -> None:
    with connect() as con, write_transaction(con):
        terminal = str(status) in {"consumed", "quarantined", "ignored", "empty"}
        con.execute(
            "UPDATE brainlive_signal_events SET status=?, consumed_at=CASE WHEN ? THEN ? ELSE NULL END, result_json=? WHERE signal_id=?",
            (status, 1 if terminal else 0, now_iso(), json_dumps(result), signal_id),
        )


def _work_state(work_key: str) -> str | None:
    with connect() as con:
        row = con.execute("SELECT state FROM v18_work_leases WHERE work_key=?", (work_key,)).fetchone()
    return str(row["state"]) if row else None


def _reconcile_daemon_signal_state(*, person_id: str, signal: Mapping[str, Any]) -> str | None:
    """Repair the small crash window between lease completion and inbox projection."""
    signal_id = str(signal.get("signal_id") or "")
    if not signal_id:
        return None
    scoped = work_scope_key(person_id=person_id, source_key_value=f"signal:{signal_id}")
    with connect() as con:
        row = con.execute(
            "SELECT state,result_json,error_text FROM v18_work_leases WHERE work_type='brainlive:daemon_signal' AND person_id=? AND source_key=? ORDER BY updated_at DESC LIMIT 1",
            (person_id, scoped),
        ).fetchone()
    if not row:
        return None
    state = str(row["state"])
    # Another process may legitimately hold a live lease.  Do not project it
    # as a completed/reconciled inbox signal; leave it untouched until its
    # lease resolves or expires.
    if state == "leased":
        return None
    if state == "completed":
        _set_signal_result(signal_id, status="consumed", result=json_loads(row["result_json"], {}) or {"reconciled": True})
    elif state == "quarantined":
        _set_signal_result(signal_id, status="quarantined", result={"reconciled": True, "error": row["error_text"]})
    elif state == "retryable_error":
        _set_signal_result(signal_id, status="retryable_error", result={"reconciled": True, "error": row["error_text"]})
    return state


def _consume_daemon_signal_durable(*, person_id: str, live_session_id: str, signal: dict[str, Any], use_llm: bool, use_vlm: bool) -> dict[str, Any] | None:
    lease = _claim_daemon_signal(person_id=person_id, live_session_id=live_session_id, signal=signal)
    if not lease:
        reconciled = _reconcile_daemon_signal_state(person_id=person_id, signal=signal)
        return {"signal_id": signal.get("signal_id"), "work_state": reconciled, "status": "reconciled"} if reconciled else None
    try:
        result = _consume_signal(live_session_id, signal, use_llm=use_llm, use_vlm=use_vlm)
        finish_work(work_key=lease["work_key"], lease_token=lease["lease_token"], status="completed", result=result)
        _set_signal_result(str(signal["signal_id"]), status="consumed", result=result)
        return {"signal_id": signal["signal_id"], "result": result, "work_state": "completed"}
    except Exception as exc:
        kind = classify_llm_exception(exc)
        retryable = kind in {"transient_runtime_error", "runtime_error"}
        finish_work(
            work_key=lease["work_key"], lease_token=lease["lease_token"],
            status="retryable_error" if retryable else "quarantined",
            result={"signal_id": signal.get("signal_id"), "status": "error"}, error_text=str(exc)[:1500], retry_delay_seconds=20,
        )
        state = _work_state(str(lease["work_key"])) or ("retryable_error" if retryable else "quarantined")
        signal_status = "retryable_error" if state == "retryable_error" else "quarantined"
        payload = {"error": str(exc)[:1000], "error_kind": kind, "work_state": state}
        _set_signal_result(str(signal["signal_id"]), status=signal_status, result=payload)
        return {"signal_id": signal["signal_id"], "error": payload, "work_state": state}


def daemon_iteration(live_session_id: str, *, config_id: str | None = None, person_id: str | None = None, use_llm: bool = True, use_vlm: bool = True) -> dict[str, Any]:
    if not person_id:
        from .governance_v18 import ScopeError
        raise ScopeError("BrainLive daemon iteration requires explicit person_id")
    ensure_daemon_schema()
    with connect() as con:
        cfg = _load_config(con, config_id=config_id, person_id=person_id)
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        if sess.get("person_id") != person_id:
            from .governance_v18 import ScopeError
            raise ScopeError("BrainLive session owner does not match explicit person_id")
    discovered = poll_sources_once(live_session_id, cfg)
    processed = []
    # Every daemon signal uses the same V18 lease contract as the service.
    # A crash, concurrent daemon or temporary LLM failure can no longer turn a
    # queued source into a terminal silent error or a duplicate delivery.
    with connect() as con:
        queued = _many(
            con,
            "SELECT * FROM brainlive_signal_events WHERE live_session_id=? AND status IN ('queued','retryable_error') ORDER BY created_at LIMIT 13",
            (live_session_id,),
        )
    for sig in queued:
        if sig.get("signal_type") == "audio_file":
            lease = _claim_daemon_signal(person_id=person_id, live_session_id=live_session_id, signal=sig)
            if not lease:
                reconciled = _reconcile_daemon_signal_state(person_id=person_id, signal=sig)
                if reconciled:
                    processed.append({"signal_id": sig.get("signal_id"), "work_state": reconciled, "status": "reconciled"})
                continue
            try:
                res = process_audio_file(live_session_id, sig["source_path"], vad_backend=cfg.get("vad_backend") or "energy", asr_backend=cfg.get("asr_backend") or "external_or_whispercpp")
                finish_work(work_key=lease["work_key"], lease_token=lease["lease_token"], status="completed", result=res)
                _set_signal_result(str(sig["signal_id"]), status="consumed", result=res)
                processed.append({"signal_id": sig["signal_id"], "result": res, "work_state": "completed"})
            except Exception as exc:
                finish_work(work_key=lease["work_key"], lease_token=lease["lease_token"], status="retryable_error", result={}, error_text=str(exc)[:1500], retry_delay_seconds=20)
                state = _work_state(str(lease["work_key"])) or "retryable_error"
                err = {"error": str(exc)[:1000], "work_state": state}
                _set_signal_result(str(sig["signal_id"]), status="retryable_error" if state == "retryable_error" else "quarantined", result=err)
                processed.append({"signal_id": sig["signal_id"], "error": err})
            continue
        outcome = _consume_daemon_signal_durable(person_id=person_id, live_session_id=live_session_id, signal=sig, use_llm=use_llm, use_vlm=use_vlm)
        if outcome is not None:
            processed.append(outcome)
    # Hot context refresh when due.
    with connect() as con:
        last_refresh = _one(con, "SELECT created_at FROM brainlive_context_refresh_log WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
    refreshed = None
    if _seconds_since((last_refresh or {}).get("created_at")) >= float(cfg.get("active_context_refresh_s") or 15.0):
        refreshed = refresh_active_context_hot(live_session_id, trigger_reason="daemon_due_refresh")
    # H2 observation tick when no signals but context still needs live watch.
    passive_tick = None
    if not queued and not processed:
        with connect() as con:
            last_h2 = _one(con, "SELECT created_at FROM brainlive_realtime_ticks WHERE live_session_id=? AND horizon='H2' ORDER BY created_at DESC LIMIT 1", (live_session_id,)) if _one(con, "SELECT name FROM sqlite_master WHERE type='table' AND name='brainlive_realtime_ticks'") else None
        if _seconds_since((last_h2 or {}).get("created_at")) >= float(cfg.get("h2_interval_s") or 12.0):
            passive_tick = live_tick(live_session_id, horizon="H2", use_vlm=False, use_llm=use_llm)
            # H2 is observation-only.  Only a fused H1 decision may own a delivery.
            if isinstance(passive_tick, dict):
                passive_tick["delivery_owner_horizon"] = "H1"
    # Outcome watcher due.
    watched = None
    with connect() as con:
        last_watch = _one(con, "SELECT created_at FROM brainlive_outcome_watch_log WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
    if _seconds_since((last_watch or {}).get("created_at")) >= float(cfg.get("outcome_watch_interval_s") or 60.0):
        watched = outcome_watch(live_session_id, person_id=person_id, limit=20, timeout=120.0)
    return {"live_session_id": live_session_id, "discovered": discovered, "processed": processed, "refreshed_context": refreshed, "passive_tick": passive_tick, "outcome_watch": watched}


def run_daemon(
    *,
    live_session_id: str | None = None,
    person_id: str | None = None,
    config_id: str | None = None,
    title: str | None = None,
    location_hint: str | None = None,
    iterations: int | None = None,
    use_llm: bool = True,
    use_vlm: bool = True,
) -> dict[str, Any]:
    if not person_id:
        from .governance_v18 import ScopeError
        raise ScopeError("BrainLive daemon run requires explicit person_id")
    ensure_daemon_schema()
    with connect() as con:
        cfg = _load_config(con, config_id=config_id, person_id=person_id)
    if not live_session_id:
        sess = start_live_session(person_id=person_id, title=title or "BrainLive daemon session", location_hint=location_hint, mode="daemon_live")
        live_session_id = sess["live_session_id"]
    max_iterations = int(iterations if iterations is not None else (cfg.get("max_iterations") or 0))
    sleep_s = float(cfg.get("sleep_s") or 1.0)
    run_id = stable_id("bldrun", live_session_id, now_iso(), uuid4().hex)
    now = now_iso()
    counters = {"iterations": 0, "signals_processed": 0, "errors": 0}
    with connect() as con:
        upsert(con, "brainlive_daemon_runs", {
            "daemon_run_id": run_id,
            "config_id": cfg.get("config_id"),
            "live_session_id": live_session_id,
            "person_id": person_id,
            "status": "running",
            "started_at": now,
            "stopped_at": None,
            "iterations": 0,
            "last_heartbeat_at": now,
            "counters_json": json_dumps(counters),
            "error_text": None,
            "created_at": now,
            "updated_at": now,
        }, "daemon_run_id")
        con.commit()
    error_text = None
    try:
        i = 0
        while True:
            if max_iterations and i >= max_iterations:
                break
            result = daemon_iteration(live_session_id, config_id=cfg.get("config_id"), person_id=person_id, use_llm=use_llm, use_vlm=use_vlm)
            i += 1
            counters["iterations"] = i
            counters["signals_processed"] += len(result.get("processed") or [])
            with connect() as con:
                con.execute("UPDATE brainlive_daemon_runs SET iterations=?, last_heartbeat_at=?, counters_json=?, updated_at=? WHERE daemon_run_id=?", (i, now_iso(), json_dumps(counters), now_iso(), run_id))
                con.commit()
            if not max_iterations:
                time.sleep(sleep_s)
            elif i < max_iterations:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        error_text = "keyboard_interrupt"
    except Exception as exc:
        error_text = str(exc)[:2000]
        counters["errors"] += 1
    status = "stopped" if not error_text or error_text == "keyboard_interrupt" else "error"
    with connect() as con:
        con.execute("UPDATE brainlive_daemon_runs SET status=?, stopped_at=?, counters_json=?, error_text=?, updated_at=? WHERE daemon_run_id=?", (status, now_iso(), json_dumps(counters), error_text, now_iso(), run_id))
        con.commit()
    return {"daemon_run_id": run_id, "live_session_id": live_session_id, "person_id": person_id, "status": status, "counters": counters, "error_text": error_text}


def daemon_audit() -> dict[str, Any]:
    ensure_daemon_schema()
    tables = [
        "brainlive_daemon_configs",
        "brainlive_daemon_runs",
        "brainlive_audio_chunks",
        "brainlive_signal_events",
        "brainlive_intervention_delivery_queue",
        "brainlive_context_refresh_log",
        "brainlive_outcome_watch_log",
    ]
    with connect() as con:
        counts = {t: int(con.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]) for t in tables}
    return {
        "version": VERSION,
        "status": "ok",
        "counts": counts,
        "separation_contract": "BrainLive is hot H0/H1/H2 projection; Brain2 remains long-term source of truth.",
        "no_keyword_psychology": True,
        "vad": "energy WAV VAD available; external/silero upstream allowed; no content inference.",
        "loop": "poll sources -> VAD/ASR -> speaker/place/vision -> active context -> H0/H1/H2 -> delivery queue -> outcomes -> nightly Brain2",
    }
