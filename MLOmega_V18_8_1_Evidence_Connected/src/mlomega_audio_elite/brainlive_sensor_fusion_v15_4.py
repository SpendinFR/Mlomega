from __future__ import annotations

"""V15.4 BrainLive real sensor fusion.

This layer is the operational bridge that was still missing in V15.3:

- real/proper VAD adapter (Silero when available, explicit energy fallback only
  for signal segmentation, never for psychology);
- ASR adapters wired to WhisperX / faster-whisper / whisper.cpp;
- speaker identity wired to existing SpeechBrain ECAPA profiles;
- multi-source place resolver (explicit/GPS/VLM/session/history); 
- normalized VLM observations;
- confidence fusion across audio/person/place/vision/context;
- intelligent active-context refresh;
- strict observation/proactive gate driven by model outputs and confidence,
  not regex or keyword psychology;
- one full live cycle that coordinates audio and vision on the same timeline.

Brain2 remains the deep/long-term truth. V15.4 only builds a hot, short-horizon
state and calls BrainLive H0/H1/H2 with evidence. If a required model is absent,
V15.4 records `*_required` rather than inventing meaning.
"""

import json
import math
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .audio_pipeline import EliteDependencyError, transcribe_with_whisperx
from .brainlive_daemon_v15_3 import enqueue_interventions_from_tick, outcome_watch
from .brainlive_realtime_v15_2 import (
    analyze_vision_with_vlm,
    live_cycle_all_horizons,
    live_tick,
    resolve_active_speaker,
)
from .brainlive_v15 import build_active_context, ensure_brainlive_schema, ingest_live_turn, ingest_vision_frame, start_live_session
from .config import get_settings
from .db import connect, init_db, upsert, write_transaction
from .governance_v18 import GovernanceError, Scope, canonical_time, ensure_v18_schema, register_event, EventTime
from .llm import EliteLLMError, OllamaJsonClient
from .utils import iso_add_seconds, json_dumps, json_loads, now_iso, sha256_file, stable_id

VERSION = "15.4.0-sensor-fusion-real-loop"

_FAST_WHISPER_CACHE: dict[tuple[str, str, str], Any] = {}
_SILERO_VAD_CACHE: dict[str, Any] = {}
_LIVE_SPEAKER_CACHE: dict[str, dict[str, Any]] = {}
_LIVE_UNKNOWN_VOICE_CLUSTERS: dict[str, list[dict[str, Any]]] = {}

SENSOR_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_sensor_configs(
  config_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  vad_backend TEXT DEFAULT 'silero',
  asr_backend TEXT DEFAULT 'faster_or_whispercpp',
  speaker_backend TEXT DEFAULT 'speechbrain_ecapa',
  place_backends_json TEXT DEFAULT '["explicit","gps","vlm","session","history"]',
  vlm_backend TEXT DEFAULT 'ollama_multimodal',
  h0_target_ms INTEGER DEFAULT 2000,
  h1_target_ms INTEGER DEFAULT 5000,
  h2_target_ms INTEGER DEFAULT 12000,
  sensor_window_s REAL DEFAULT 18.0,
  context_refresh_s REAL DEFAULT 12.0,
  proactive_confidence_min REAL DEFAULT 0.62,
  proactive_gain_min REAL DEFAULT 0.45,
  observation_confidence_min REAL DEFAULT 0.20,
  status TEXT DEFAULT 'active',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_sensor_events(
  event_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT,
  event_time TEXT NOT NULL,
  modality TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source_path TEXT,
  source_sha256 TEXT,
  confidence REAL DEFAULT 0.0,
  payload_json TEXT DEFAULT '{}',
  model_status TEXT DEFAULT 'observed',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_audio_segments_v154(
  segment_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT,
  source_event_id TEXT,
  source_path TEXT NOT NULL,
  start_s REAL,
  end_s REAL,
  absolute_start TEXT,
  absolute_end TEXT,
  vad_backend TEXT NOT NULL,
  vad_confidence REAL DEFAULT 0.0,
  chunk_path TEXT,
  asr_backend TEXT,
  asr_status TEXT DEFAULT 'pending',
  transcript_text TEXT,
  speaker_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  processed_at TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_place_resolutions(
  resolution_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  place_label TEXT,
  confidence REAL DEFAULT 0.0,
  sources_json TEXT DEFAULT '[]',
  evidence_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'resolved',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_vlm_observations_v154(
  observation_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  frame_id TEXT,
  image_path TEXT,
  normalized_json TEXT DEFAULT '{}',
  raw_json TEXT DEFAULT '{}',
  model TEXT,
  status TEXT NOT NULL,
  confidence REAL DEFAULT 0.0,
  latency_ms INTEGER,
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_fused_situations(
  fused_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  window_start TEXT,
  window_end TEXT NOT NULL,
  active_people_json TEXT DEFAULT '[]',
  active_place_json TEXT DEFAULT '{}',
  speech_json TEXT DEFAULT '{}',
  vision_json TEXT DEFAULT '{}',
  brain2_context_id TEXT,
  confidence_json TEXT DEFAULT '{}',
  readiness_json TEXT DEFAULT '{}',
  event_ids_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_context_refresh_decisions(
  decision_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason_json TEXT DEFAULT '{}',
  previous_context_id TEXT,
  new_context_id TEXT,
  latency_ms INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_proactive_decisions(
  proactive_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  fused_id TEXT,
  horizon TEXT,
  decision TEXT NOT NULL,
  reason_json TEXT DEFAULT '{}',
  tick_json TEXT DEFAULT '{}',
  delivery_ids_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bls_events_session_time ON brainlive_sensor_events(live_session_id, event_time);
CREATE INDEX IF NOT EXISTS idx_bls_audio_session ON brainlive_audio_segments_v154(live_session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bls_audio_absolute ON brainlive_audio_segments_v154(live_session_id, absolute_start);
CREATE INDEX IF NOT EXISTS idx_bls_audio_source_event ON brainlive_audio_segments_v154(live_session_id, source_event_id);
CREATE INDEX IF NOT EXISTS idx_bls_fused_session ON brainlive_fused_situations(live_session_id, created_at);
"""

NORMALIZED_VLM_SCHEMA: dict[str, Any] = {
    "scene_summary": "",
    "location_hint": {"label": "", "confidence": 0.0, "evidence": []},
    "people": [{"label": "", "position": "", "confidence": 0.0, "evidence": []}],
    "objects": [{"label": "", "position": "", "confidence": 0.0, "evidence": []}],
    "visible_text": [{"text": "", "position": "", "confidence": 0.0}],
    "spatial_relations": [{"subject": "", "relation": "", "object": "", "confidence": 0.0}],
    "affordances": [{"label": "", "world_element": "", "position_hint": "", "personal_relevance": "unknown", "confidence": 0.0, "evidence": []}],
    "risks_or_attention": [{"label": "", "confidence": 0.0, "evidence": []}],
    "uncertainties": [],
    "confidence": 0.0,
}

FUSION_LLM_SCHEMA: dict[str, Any] = {
    "situation_label": "",
    "situation_confidence": 0.0,
    "probable_needs_h0_h1_h2": [{"need": "", "horizon": "H0|H1|H2", "confidence": 0.0, "evidence": []}],
    "active_risks_or_opportunities": [{"label": "", "horizon": "H0|H1|H2", "confidence": 0.0, "time_sensitive": False, "evidence": []}],
    "watch_mode": {"should_watch": True, "why": "", "signals_to_watch": []},
    "proactive_policy": {"should_consider_intervention": False, "why": "", "minimum_horizon": "H1"},
    "missing_evidence": [],
}


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _table_exists(con, name: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = _one(con, "SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1")
    return str(row["person_id"]) if row and row.get("person_id") else "me"


def _clamp(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except Exception:
        v = default
    return max(0.0, min(1.0, v))


def _weighted_conf(items: Iterable[tuple[float, float]]) -> float:
    total_w = 0.0
    total = 0.0
    for conf, weight in items:
        c = _clamp(conf)
        w = max(0.0, float(weight))
        total += c * w
        total_w += w
    return _clamp(total / total_w if total_w else 0.0)


def ensure_sensor_fusion_schema() -> None:
    ensure_v18_schema()
    ensure_brainlive_schema()
    init_db()
    with connect() as con:
        con.executescript(SENSOR_SCHEMA)
        # V18.5 keeps absolute audio provenance on the segment row as well as
        # the immutable sensor event. Existing databases are migrated additively.
        cols = {str(row[1]) for row in con.execute("PRAGMA table_info(brainlive_audio_segments_v154)").fetchall()}
        for name, ddl in (("person_id", "TEXT"), ("source_event_id", "TEXT"), ("absolute_start", "TEXT"), ("absolute_end", "TEXT")):
            if name not in cols:
                con.execute(f"ALTER TABLE brainlive_audio_segments_v154 ADD COLUMN {name} {ddl}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bls_audio_absolute ON brainlive_audio_segments_v154(live_session_id, absolute_start)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bls_audio_source_event ON brainlive_audio_segments_v154(live_session_id, source_event_id)")
        con.commit()


def configure_sensor_fusion(
    *,
    person_id: str | None = None,
    vad_backend: str = "silero",
    asr_backend: str = "faster_or_whispercpp",
    speaker_backend: str = "speechbrain_ecapa",
    vlm_backend: str = "ollama_multimodal",
    sensor_window_s: float = 18.0,
    context_refresh_s: float = 12.0,
    proactive_confidence_min: float = 0.62,
    proactive_gain_min: float = 0.45,
) -> dict[str, Any]:
    if not person_id:
        raise GovernanceError("V18 sensor fusion configuration requires explicit person_id")
    ensure_sensor_fusion_schema()
    now = now_iso()
    with connect() as con:
        cfg_id = stable_id("blsf_cfg", person_id)
        upsert(con, "brainlive_sensor_configs", {
            "config_id": cfg_id,
            "person_id": person_id,
            "vad_backend": vad_backend,
            "asr_backend": asr_backend,
            "speaker_backend": speaker_backend,
            "place_backends_json": json_dumps(["explicit", "gps", "vlm", "session", "history"]),
            "vlm_backend": vlm_backend,
            "h0_target_ms": 2000,
            "h1_target_ms": 5000,
            "h2_target_ms": 12000,
            "sensor_window_s": float(sensor_window_s),
            "context_refresh_s": float(context_refresh_s),
            "proactive_confidence_min": float(proactive_confidence_min),
            "proactive_gain_min": float(proactive_gain_min),
            "observation_confidence_min": 0.20,
            "status": "active",
            "metadata_json": json_dumps({"contract": "no regex/no keyword psychology; sensor evidence + LLM/VLM only"}),
            "created_at": now,
            "updated_at": now,
        }, "config_id")
        con.commit()
    return {"config_id": cfg_id, "person_id": person_id, "status": "active", "version": VERSION}


def _get_config(con, person_id: str | None = None, config_id: str | None = None) -> dict[str, Any]:
    if config_id:
        row = _one(con, "SELECT * FROM brainlive_sensor_configs WHERE config_id=?", (config_id,))
        if not row:
            raise ValueError(f"Config V15.4 introuvable: {config_id}")
        return row
    person_id = person_id or _default_user(con)
    row = _one(con, "SELECT * FROM brainlive_sensor_configs WHERE person_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (person_id,))
    if row:
        return row
    return {
        "config_id": None,
        "person_id": person_id,
        "vad_backend": "silero",
        "asr_backend": os.environ.get("MLOMEGA_BRAINLIVE_ASR_BACKEND") or "faster_or_whispercpp",
        "speaker_backend": "speechbrain_ecapa",
        "vlm_backend": "ollama_multimodal",
        "sensor_window_s": 18.0,
        "context_refresh_s": 12.0,
        "proactive_confidence_min": 0.62,
        "proactive_gain_min": 0.45,
    }


def _record_sensor_event(
    con,
    *,
    live_session_id: str,
    modality: str,
    event_type: str,
    payload: dict[str, Any],
    person_id: str | None = None,
    event_time: str | None = None,
    source_path: str | None = None,
    confidence: float = 0.0,
    model_status: str = "observed",
    source_event_id: str | None = None,
) -> str:
    """Record an event with source time, never a random fallback identity."""
    now = now_iso()
    source_sha = None
    if source_path and Path(source_path).exists():
        source_sha = sha256_file(Path(source_path))
    canonical = canonical_time({"event_time": event_time}, "event_time")
    if canonical is None:
        # System bookkeeping may use processing time, but must say so in the
        # model status and never be mistaken for a capture observation.
        canonical = now
        payload = {**payload, "time_quality": "processing_time_only"}
        model_status = f"{model_status}|time_unverified"
    eid = stable_id("blse", live_session_id, source_event_id or "no_source_event", modality, event_type, canonical, source_sha or json_dumps(payload))
    upsert(con, "brainlive_sensor_events", {
        "event_id": eid,
        "live_session_id": live_session_id,
        "person_id": person_id,
        "event_time": canonical,
        "modality": modality,
        "event_type": event_type,
        "source_path": source_path,
        "source_sha256": source_sha,
        "confidence": _clamp(confidence),
        "payload_json": json_dumps(payload),
        "model_status": model_status,
        "created_at": now,
    }, "event_id")
    return eid


# ---------------------------------------------------------------------------
# VAD / ASR
# ---------------------------------------------------------------------------

def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate() or 1)
    except Exception:
        return None


def _energy_vad_segments(path: Path, *, frame_ms: int = 30, min_speech_s: float = 0.35, threshold_ratio: float = 2.8) -> list[dict[str, Any]]:
    """Signal fallback only. It never infers meaning."""
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        fr = wf.getframerate()
        if sampwidth not in {1, 2, 4}:
            return [{"start_s": 0.0, "end_s": _wav_duration(path), "confidence": 0.25, "backend": "energy_vad_unsupported_width_full"}]
        frame_n = max(1, int(fr * frame_ms / 1000.0))
        energies: list[tuple[float, float]] = []
        idx = 0
        while True:
            raw = wf.readframes(frame_n)
            if not raw:
                break
            if sampwidth == 1:
                vals = [b - 128 for b in raw[::max(1, n_channels)]]
            elif sampwidth == 2:
                import struct
                vals = list(struct.unpack("<" + "h" * (len(raw) // 2), raw))[::max(1, n_channels)]
            else:
                import struct
                vals = list(struct.unpack("<" + "i" * (len(raw) // 4), raw))[::max(1, n_channels)]
            if not vals:
                continue
            rms = math.sqrt(sum(float(v) * float(v) for v in vals) / len(vals))
            t = idx * frame_n / float(fr)
            energies.append((t, rms))
            idx += 1
    if not energies:
        return []
    sorted_e = sorted(e for _, e in energies)
    floor = sorted_e[max(0, int(len(sorted_e) * 0.25) - 1)] or 1.0
    threshold = max(floor * threshold_ratio, 50.0)
    segments: list[dict[str, Any]] = []
    in_seg = False
    start = 0.0
    peak = 0.0
    last_t = 0.0
    for t, e in energies:
        speech = e >= threshold
        if speech and not in_seg:
            start, peak, in_seg = t, e, True
        elif speech and in_seg:
            peak = max(peak, e)
        elif not speech and in_seg:
            end = t
            if end - start >= min_speech_s:
                segments.append({"start_s": start, "end_s": end, "confidence": _clamp((peak / threshold) / 4.0, 0.45), "backend": "energy_vad_signal_fallback"})
            in_seg = False
        last_t = t
    if in_seg:
        end = last_t + frame_ms / 1000.0
        if end - start >= min_speech_s:
            segments.append({"start_s": start, "end_s": end, "confidence": _clamp((peak / threshold) / 4.0, 0.45), "backend": "energy_vad_signal_fallback"})
    return segments


def _silero_vad_segments(path: Path) -> list[dict[str, Any]]:
    """Use one resident Silero model for the BrainLive process."""
    try:
        from silero_vad import load_silero_vad, read_audio, get_speech_timestamps  # type: ignore
    except Exception as exc:
        raise EliteDependencyError("silero-vad package absent. Install: pip install silero-vad") from exc
    model = _SILERO_VAD_CACHE.get("default")
    if model is None:
        model = load_silero_vad()
        _SILERO_VAD_CACHE["default"] = model
    wav = read_audio(str(path), sampling_rate=16000)
    stamps = get_speech_timestamps(wav, model, sampling_rate=16000, return_seconds=True)
    out = []
    for s in stamps:
        out.append({"start_s": float(s.get("start", 0.0)), "end_s": float(s.get("end", 0.0)), "confidence": 0.85, "backend": "silero_vad"})
    return out


def run_vad(audio_path: str | Path, *, backend: str = "silero", allow_energy_fallback: bool = True) -> dict[str, Any]:
    ensure_sensor_fusion_schema()
    p = Path(audio_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    started = time.time()
    status = "ok"
    error = None
    backend_used = backend
    segments: list[dict[str, Any]] = []
    try:
        if backend == "silero":
            segments = _silero_vad_segments(p)
        elif backend == "energy":
            segments = _energy_vad_segments(p)
        elif backend == "none":
            segments = [{"start_s": 0.0, "end_s": _wav_duration(p), "confidence": 0.2, "backend": "no_vad_full_file"}]
        else:
            raise ValueError(f"VAD backend inconnu: {backend}")
    except Exception as exc:
        if allow_energy_fallback:
            backend_used = "energy_after_silero_unavailable"
            try:
                segments = _energy_vad_segments(p)
                status = "fallback_energy_signal_only"
                error = str(exc)[:800]
            except Exception as exc2:
                status = "vad_error"
                error = f"{exc}; fallback_failed={exc2}"[:1000]
        else:
            status = "vad_error"
            error = str(exc)[:1000]
    latency_ms = int((time.time() - started) * 1000)
    return {"status": status, "backend": backend_used, "segments": segments, "latency_ms": latency_ms, "error": error}


def _cut_wav_segment(src: Path, start_s: float | None, end_s: float | None, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if start_s is None or end_s is None:
        return src
    # Prefer ffmpeg if present, because it supports non-WAV input too.
    ffmpeg = shutil.which("ffmpeg")
    out = out_dir / f"{src.stem}_{start_s:.2f}_{end_s:.2f}.wav"
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start_s), "-to", str(end_s), "-i", str(src), "-ar", "16000", "-ac", "1", str(out)]
        subprocess.run(cmd, check=True, timeout=30)
        return out
    # WAV-only fallback.
    with wave.open(str(src), "rb") as wf:
        fr = wf.getframerate()
        sw = wf.getsampwidth()
        ch = wf.getnchannels()
        start_f = max(0, int(float(start_s) * fr))
        end_f = max(start_f, int(float(end_s) * fr))
        wf.setpos(start_f)
        raw = wf.readframes(end_f - start_f)
        with wave.open(str(out), "wb") as ow:
            ow.setnchannels(ch); ow.setsampwidth(sw); ow.setframerate(fr); ow.writeframes(raw)
    return out


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except Exception:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 1000) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _audio_anchor_iso(path: Path) -> str:
    """Return an absolute UTC-ish anchor for relative chunk/VAD offsets.

    Capture daemons may write a sidecar JSON next to the audio chunk with
    `timestamp_start`, `captured_at`, `recorded_at` or `start_at`.  If no sidecar
    exists, file mtime is the least bad anchor and is still much better than
    storing `0.0..4.0` as if it were a clock time.
    """
    for candidate in (path.with_suffix(path.suffix + ".json"), path.with_suffix(".json")):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(data, dict):
                    for key in ("timestamp_start", "captured_at", "recorded_at", "start_at", "created_at", "mtime"):
                        dt = _parse_iso(data.get(key))
                        if dt:
                            return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
            except Exception:
                pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return now_iso()


def _faster_whisper_model(model_name: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel  # type: ignore
    key = (model_name, device, compute_type)
    model = _FAST_WHISPER_CACHE.get(key)
    if model is None:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _FAST_WHISPER_CACHE[key] = model
    return model


def _speaker_cache_key(live_session_id: str, audio_path: Path) -> str:
    # One active acoustic stream per BrainLive session by default.  If the user
    # later records multiple devices, setting MLOMEGA_BRAINLIVE_SPEAKER_CACHE_BY_PATH=1
    # keeps separate continuity caches per path stem.
    if _env_bool("MLOMEGA_BRAINLIVE_SPEAKER_CACHE_BY_PATH", "false"):
        return f"{live_session_id}:{audio_path.parent}:{audio_path.stem}"
    return live_session_id


def _cosine_vec(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    na = math.sqrt(sum(float(a[i]) * float(a[i]) for i in range(n))) or 1.0
    nb = math.sqrt(sum(float(b[i]) * float(b[i]) for i in range(n))) or 1.0
    return _clamp(dot / (na * nb))


def _unknown_voice_cluster(live_session_id: str, embedding: list[float] | None, *, abs_start: str | None) -> dict[str, Any]:
    """Return a stable unknown speaker label for live conversation tracking.

    This is intentionally not a real identity.  It only lets BrainLive follow
    `other_unknown_1: ... / me: ... / other_unknown_1: ...` during a long
    exchange while Brain2 later performs deep diarization/correction.
    """
    threshold = _env_float("MLOMEGA_BRAINLIVE_UNKNOWN_CLUSTER_MIN", 0.72, min_value=0.0, max_value=1.0)
    clusters = _LIVE_UNKNOWN_VOICE_CLUSTERS.setdefault(live_session_id, [])
    best_idx = -1
    best_score = 0.0
    if embedding:
        for i, c in enumerate(clusters):
            score = _cosine_vec(embedding, c.get("prototype"))
            if score > best_score:
                best_score = score
                best_idx = i
    if best_idx >= 0 and best_score >= threshold:
        c = clusters[best_idx]
        count = int(c.get("count") or 1) + 1
        old = c.get("prototype") or embedding or []
        if embedding and old:
            n = min(len(old), len(embedding))
            c["prototype"] = [((float(old[i]) * (count - 1)) + float(embedding[i])) / count for i in range(n)]
        c["count"] = count
        c["last_seen_at"] = abs_start or now_iso()
        return {"label": c.get("label"), "cluster_id": c.get("cluster_id"), "cluster_match_score": best_score, "cluster_observations": count}
    idx = len(clusters) + 1
    label = f"other_unknown_{idx}"
    cluster_id = stable_id("liveunkvoice", live_session_id, label)
    clusters.append({"cluster_id": cluster_id, "label": label, "prototype": embedding or [], "count": 1, "first_seen_at": abs_start or now_iso(), "last_seen_at": abs_start or now_iso()})
    return {"label": label, "cluster_id": cluster_id, "cluster_match_score": 1.0 if embedding else 0.0, "cluster_observations": 1}


def _live_speaker_context_hints(live_session_id: str, *, text: str | None = None) -> dict[str, Any]:
    """Collect non-identity context for unknown speakers.

    These hints let BrainLive reason about the situation (place/image/topic) when
    the acoustic identity is unknown, without pretending the unknown speech is
    William's speech.
    """
    try:
        with connect() as con:
            place = _one(con, "SELECT * FROM brainlive_place_resolutions WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
            vision = _one(con, "SELECT * FROM brainlive_vlm_observations_v154 WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
            recent = _many(con, """
                SELECT speaker_label, speaker_person_id, text_final, timestamp_start, created_at
                FROM brainlive_turn_buffer
                WHERE live_session_id=?
                ORDER BY COALESCE(timestamp_start, created_at) DESC
                LIMIT 8
            """, (live_session_id,))
    except Exception:
        return {"policy": "no_context_hints_available"}
    vision_norm = json_loads(vision.get("normalized_json") if vision else None, {}) if vision else {}
    return {
        "policy": "context_hint_only_not_identity",
        "current_place": {"label": place.get("place_label"), "confidence": _clamp(place.get("confidence"))} if place else None,
        "recent_visual_scene": {
            "scene_summary": vision_norm.get("scene_summary"),
            "location_hint": vision_norm.get("location_hint"),
            "visible_text": (vision_norm.get("visible_text") or [])[:5],
            "objects": (vision_norm.get("objects") or [])[:8],
        } if vision_norm else None,
        "recent_turn_speakers": [{"speaker_label": r.get("speaker_label"), "speaker_person_id": r.get("speaker_person_id"), "text_excerpt": (r.get("text_final") or "")[:160]} for r in recent],
        "current_text_excerpt": (text or "")[:220],
        "safe_use": "Use for situation/place/topic retrieval only. Do not treat as a verified identity or as William speech.",
    }


def _probe_live_chunk_speaker(live_session_id: str, chunk: Path, *, session_owner_person_id: str | None, abs_start: str | None, abs_end: str | None) -> dict[str, Any]:
    """Run a lightweight SpeechBrain voice match for a single live chunk.

    This replaces blind diarization for 3-5s chunks.  We match every chunk by
    default, but SpeechBrain itself is cached in voice_identity, so this avoids
    model reload while preserving speaker alternation like me/Max/me/other.
    """
    try:
        from .voice_identity import match_voice
        m = match_voice(chunk, include_query_embedding=True, top_k=5)
    except Exception as exc:
        cluster = _unknown_voice_cluster(live_session_id, None, abs_start=abs_start)
        return {
            "person_id": None,
            "label": cluster.get("label") or "other_unknown",
            "confidence": 0.0,
            "source": "speechbrain_voice_match_error",
            "identity_status": "unknown",
            "speaker_role": "other",
            "hypothesis_only": True,
            "error": str(exc)[:500],
            "unknown_cluster": cluster,
        }
    raw_best_id = m.get("person_id")
    score = _clamp(m.get("score"))
    threshold = _clamp(m.get("threshold"), 0.72)
    verified_min = _env_float("MLOMEGA_BRAINLIVE_VERIFIED_SPEAKER_MIN", threshold, min_value=0.0, max_value=1.0)
    possible_min = _env_float("MLOMEGA_BRAINLIVE_POSSIBLE_SPEAKER_MIN", 0.58, min_value=0.0, max_value=1.0)
    owner_id = session_owner_person_id or "me"
    if m.get("matched") and raw_best_id and score >= verified_min:
        return {
            "person_id": raw_best_id,
            "label": raw_best_id,
            "confidence": score,
            "source": "speechbrain_voice_match_every_chunk",
            "identity_status": "verified",
            "speaker_role": "owner" if raw_best_id == owner_id else "known_other",
            "hypothesis_only": False,
            "candidate_person_id": raw_best_id,
            "raw": {k: v for k, v in m.items() if k != "query_embedding"},
        }
    # If William is not verified, do not assign the turn to William.  At most keep
    # a possible_me / possible_known_person candidate for contextual caution.
    if raw_best_id and score >= possible_min:
        label = "possible_me" if raw_best_id == owner_id else f"possible_{raw_best_id}"
        return {
            "person_id": None,
            "label": label,
            "confidence": score,
            "source": "speechbrain_voice_match_every_chunk",
            "identity_status": "hypothesis",
            "speaker_role": "possible_owner" if raw_best_id == owner_id else "possible_known_other",
            "candidate_person_id": raw_best_id,
            "hypothesis_only": True,
            "raw": {k: v for k, v in m.items() if k != "query_embedding"},
        }
    cluster = _unknown_voice_cluster(live_session_id, m.get("query_embedding"), abs_start=abs_start)
    return {
        "person_id": None,
        "label": cluster.get("label") or "other_unknown",
        "confidence": max(score, _clamp(cluster.get("cluster_match_score"), 0.0) * 0.55),
        "source": "speechbrain_voice_unknown_cluster",
        "identity_status": "unknown",
        "speaker_role": "other",
        "candidate_person_id": raw_best_id if score >= possible_min else None,
        "hypothesis_only": True,
        "unknown_cluster": cluster,
        "raw": {k: v for k, v in m.items() if k != "query_embedding"},
    }


def _speaker_continuity_resolution(cache: dict[str, Any], *, turns_since_probe: int, now_abs: str | None) -> dict[str, Any]:
    previous = dict(cache.get("resolution") or {})
    base_conf = _clamp(previous.get("confidence"))
    decay = _env_float("MLOMEGA_BRAINLIVE_SPEAKER_CACHE_DECAY", 0.08, min_value=0.0, max_value=0.5) * max(1, turns_since_probe)
    conf = _clamp(base_conf - decay)
    person_min = _env_float("MLOMEGA_BRAINLIVE_CONTINUITY_PERSON_MIN", 0.80, min_value=0.0, max_value=1.0)
    candidate_person_id = previous.get("person_id") or ((previous.get("raw") or {}).get("person_id") if isinstance(previous.get("raw"), dict) else None)
    person_id = candidate_person_id if conf >= person_min else None
    label = previous.get("label") or candidate_person_id or "other_unknown"
    if not person_id and candidate_person_id:
        label = f"possible_{candidate_person_id}"
    out = {
        "person_id": person_id,
        "label": label,
        "confidence": conf,
        "source": "live_speaker_continuity_cache",
        "candidate_person_id": candidate_person_id,
        "identity_status": "verified" if person_id else "hypothesis",
        "speaker_role": previous.get("speaker_role") or ("owner" if person_id == "me" else "possible_known_other"),
        "hypothesis_only": person_id is None,
        "turns_since_probe": turns_since_probe,
        "cache_time": now_abs,
        "raw": {"previous": previous},
    }
    return out


def _resolve_live_chunk_speaker(live_session_id: str, chunk: Path, *, abs_start: str | None, abs_end: str | None, session_owner_person_id: str | None = None) -> dict[str, Any]:
    """Resolve speaker for fast live chunks.

    Default V17.4.2 behaviour: match every chunk against enrolled voices using
    SpeechBrain ECAPA (model cached once), then fall back to stable unknown voice
    clusters.  This lets BrainLive follow me/Max/me/other in a 30-minute
    conversation without running pyannote live.  Continuity cache remains as an
    opt-out fallback only.
    """
    if _env_bool("MLOMEGA_BRAINLIVE_SPEAKER_MATCH_EVERY_CHUNK", "true"):
        res = _probe_live_chunk_speaker(live_session_id, chunk, session_owner_person_id=session_owner_person_id, abs_start=abs_start, abs_end=abs_end)
        _LIVE_SPEAKER_CACHE[_speaker_cache_key(live_session_id, chunk)] = {"resolution": res, "turns_since_probe": 0, "last_abs_end": abs_end, "last_used_at": now_iso(), "unknown_label": res.get("label") if not res.get("person_id") else None}
        return res
    if not _env_bool("MLOMEGA_BRAINLIVE_SPEAKER_CACHE", "true"):
        return _probe_live_chunk_speaker(live_session_id, chunk, session_owner_person_id=session_owner_person_id, abs_start=abs_start, abs_end=abs_end)
    key = _speaker_cache_key(live_session_id, chunk)
    cache = _LIVE_SPEAKER_CACHE.get(key) or {}
    probe_every = _env_int("MLOMEGA_BRAINLIVE_SPEAKER_PROBE_EVERY_N", 2, min_value=1, max_value=50)
    ttl_s = _env_float("MLOMEGA_BRAINLIVE_SPEAKER_CACHE_TTL_S", 12.0, min_value=0.0, max_value=600.0)
    turns_since_probe = int(cache.get("turns_since_probe") or 0) + 1
    gap_s = 999999.0
    prev_end = _parse_iso(cache.get("last_abs_end"))
    cur_start = _parse_iso(abs_start)
    if prev_end and cur_start:
        gap_s = max(0.0, (cur_start - prev_end).total_seconds())
    due_probe = (not cache) or turns_since_probe >= probe_every or gap_s > ttl_s
    if not due_probe:
        resolution = _speaker_continuity_resolution(cache, turns_since_probe=turns_since_probe, now_abs=abs_start)
        cache.update({"turns_since_probe": turns_since_probe, "last_abs_end": abs_end, "last_used_at": now_iso()})
        _LIVE_SPEAKER_CACHE[key] = cache
        return resolution
    resolution = _probe_live_chunk_speaker(live_session_id, chunk, session_owner_person_id=session_owner_person_id, abs_start=abs_start, abs_end=abs_end)
    _LIVE_SPEAKER_CACHE[key] = {
        "resolution": resolution,
        "turns_since_probe": 0,
        "last_abs_end": abs_end,
        "last_used_at": now_iso(),
        "unknown_label": resolution.get("label") if not resolution.get("person_id") else cache.get("unknown_label"),
    }
    return resolution

def transcribe_segment(audio_path: str | Path, *, backend: str = "faster_or_whispercpp", language: str = "fr") -> dict[str, Any]:
    """ASR adapters. No transcript inference fallback."""
    p = Path(audio_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    sidecar = p.with_suffix(".txt")
    if sidecar.exists() and "external" in backend:
        return {"status": "ok", "backend": "sidecar_txt", "text": sidecar.read_text(encoding="utf-8", errors="ignore").strip(), "segments": []}
    # Fast-live mode deliberately avoids WhisperX alignment+pyannote on tiny
    # 3-5s chunks.  The deep WhisperX/pyannote path remains available for
    # explicit offline/deep calls with backend='whisperx' or a backend containing
    # 'whisperx'.
    if "whisperx" in backend and "fast" not in backend:
        try:
            data = transcribe_with_whisperx(p, language=language)
            turns = data.get("turns", [])
            text = " ".join((t.get("text") or "").strip() for t in turns if (t.get("text") or "").strip())
            return {"status": "ok" if text else "empty", "backend": "whisperx", "text": text, "segments": turns, "raw": data}
        except Exception as exc:
            if backend == "whisperx":
                return {"status": "asr_error", "backend": "whisperx", "text": "", "error": str(exc)[:1000]}
    if "faster" in backend:
        try:
            settings = get_settings()
            model_name = os.environ.get("MLOMEGA_FAST_WHISPER_MODEL", settings.whisperx_model or "small")
            device = os.environ.get("MLOMEGA_FAST_WHISPER_DEVICE", settings.whisperx_device or "cpu")
            compute_type = os.environ.get("MLOMEGA_FAST_WHISPER_COMPUTE", settings.whisperx_compute_type or "int8")
            model = _faster_whisper_model(model_name, device, compute_type)
            segs, info = model.transcribe(str(p), beam_size=1, vad_filter=False, language=language)
            materialized = [{"start": s.start, "end": s.end, "text": s.text} for s in segs]
            text = " ".join(s["text"].strip() for s in materialized if s["text"].strip())
            return {"status": "ok" if text else "empty", "backend": "faster_whisper", "text": text, "segments": materialized, "raw": {"language": getattr(info, "language", None)}}
        except Exception as exc:
            if backend == "faster":
                return {"status": "asr_error", "backend": "faster_whisper", "text": "", "error": str(exc)[:1000]}
    whispercpp = os.environ.get("MLOMEGA_WHISPERCPP_BIN")
    whispercpp_model = os.environ.get("MLOMEGA_WHISPERCPP_MODEL")
    if whispercpp and whispercpp_model and Path(whispercpp).exists() and Path(whispercpp_model).exists():
        try:
            out_base = p.with_suffix("")
            cmd = [whispercpp, "-m", whispercpp_model, "-f", str(p), "-nt", "-otxt", "-of", str(out_base)]
            subprocess.run(cmd, check=True, timeout=120)
            out_txt = out_base.with_suffix(".txt")
            text = out_txt.read_text(encoding="utf-8", errors="ignore").strip() if out_txt.exists() else ""
            return {"status": "ok" if text else "empty", "backend": "whispercpp", "text": text, "segments": []}
        except Exception as exc:
            return {"status": "asr_error", "backend": "whispercpp", "text": "", "error": str(exc)[:1000]}
    return {"status": "asr_required", "backend": backend, "text": "", "segments": [], "hint": "Enable WhisperX/faster-whisper/whisper.cpp or provide sidecar .txt."}


def process_audio_sensor(
    live_session_id: str,
    audio_path: str | Path,
    *,
    person_id: str | None = None,
    vad_backend: str | None = None,
    asr_backend: str | None = None,
    language: str = "fr",
    allow_energy_fallback: bool = True,
    source_event_id: str | None = None,
    source_occurred_at: str | None = None,
) -> dict[str, Any]:
    """Process audio without hidden VAD truncation or full-file fallback.

    A chunk-cut failure is a failed segment, not permission to transcribe the
    whole recording repeatedly.  A required/unavailable ASR is not a completed
    capture.  The service lease decides retry/quarantine from the returned
    status.
    """
    ensure_sensor_fusion_schema()
    p = Path(audio_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        if str(sess.get("status") or "") != "active":
            raise GovernanceError("cannot process audio for a non-active live session")
        person_id = person_id or sess["person_id"]
        if person_id != sess["person_id"]:
            raise GovernanceError("audio processing owner differs from live session owner")
        cfg = _get_config(con, person_id=person_id)
    vad_backend = vad_backend or cfg.get("vad_backend") or "silero"
    asr_backend = asr_backend or cfg.get("asr_backend") or os.environ.get("MLOMEGA_BRAINLIVE_ASR_BACKEND") or "faster_or_whispercpp"
    if source_occurred_at is None:
        if os.environ.get("MLOMEGA_ALLOW_UNTIMED_LEGACY", "false").lower() not in {"1","true","yes"}:
            raise GovernanceError("audio source_occurred_at is mandatory in V18; mtime fallback is disabled")
        source_occurred_at = _audio_anchor_iso(p)
    from .integrity_v176 import iso_utc, parse_iso_utc
    file_anchor = iso_utc(parse_iso_utc(source_occurred_at))
    vad = run_vad(p, backend=vad_backend, allow_energy_fallback=allow_energy_fallback)
    if vad.get("status") == "vad_error":
        with connect() as con:
            _record_sensor_event(con, live_session_id=live_session_id, person_id=person_id, event_time=file_anchor, modality="audio", event_type="vad_run", source_path=str(p), confidence=0.0, model_status="vad_error", source_event_id=source_event_id, payload={"source_event_id":source_event_id, "vad":vad})
            con.commit()
        return {"status":"vad_error", "vad":vad, "processed_segments":[], "failed_segments":[]}
    out_dir = get_settings().root_dir / "brainlive_chunks" / live_session_id
    processed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    now = now_iso()
    segments = list(vad.get("segments", []) or [])
    with connect() as con:
        _record_sensor_event(con, live_session_id=live_session_id, person_id=person_id, event_time=file_anchor, modality="audio", event_type="vad_run", source_path=str(p), confidence=0.75, model_status=str(vad.get("status") or "unknown"), source_event_id=source_event_id, payload={"source_event_id":source_event_id, "vad":vad, "segment_count":len(segments)})
        con.commit()
    # No [:30]. A producer can split audio upstream for latency, but the system
    # must either process all segments or return a durable partial/failure state.
    for seg_index, seg in enumerate(segments):
        start_s = float(seg.get("start_s") or 0.0)
        end_s = float(seg.get("end_s") or 0.0)
        if not math.isfinite(start_s) or not math.isfinite(end_s) or end_s <= start_s:
            failed.append({"segment_index":seg_index, "reason":"invalid_vad_bounds", "segment":seg})
            continue
        abs_start = iso_add_seconds(file_anchor, start_s)
        abs_end = iso_add_seconds(file_anchor, end_s)
        try:
            chunk = _cut_wav_segment(p, start_s, end_s, out_dir)
        except Exception as exc:
            failed.append({"segment_index":seg_index, "reason":"chunk_cut_failed", "error":str(exc)[:500], "start_s":start_s, "end_s":end_s})
            with connect() as con:
                _record_sensor_event(con, live_session_id=live_session_id, person_id=person_id, event_time=abs_start, modality="audio", event_type="speech_segment_failed", source_path=str(p), confidence=0.0, model_status="chunk_cut_failed", source_event_id=source_event_id, payload={"source_event_id":source_event_id,"segment":seg,"absolute_start":abs_start,"absolute_end":abs_end,"raw_audio_path":str(p),"error":str(exc)[:500]})
                con.commit()
            continue
        asr = transcribe_segment(chunk, backend=asr_backend, language=language)
        if asr.get("status") not in {"ok", "empty"}:
            failed.append({"segment_index":seg_index, "reason":str(asr.get("status")), "start_s":start_s, "end_s":end_s, "error":asr.get("error")})
            with connect() as con:
                _record_sensor_event(con, live_session_id=live_session_id, person_id=person_id, event_time=abs_start, modality="audio", event_type="speech_segment_failed", source_path=str(chunk), confidence=0.0, model_status=str(asr.get("status")), source_event_id=source_event_id, payload={"source_event_id":source_event_id,"segment":seg,"absolute_start":abs_start,"absolute_end":abs_end,"raw_audio_path":str(p),"chunk_path":str(chunk),"asr":asr})
                con.commit()
            continue
        speaker = _resolve_live_chunk_speaker(live_session_id, chunk, abs_start=abs_start, abs_end=abs_end, session_owner_person_id=person_id)
        text = asr.get("text") or ""
        speaker_context_hints = _live_speaker_context_hints(live_session_id, text=text)
        segment_id = stable_id("blseg154", source_event_id or str(p), seg_index, start_s, end_s, sha256_file(chunk))
        speaker_identity_status = str(speaker.get("identity_status") or ("verified" if speaker.get("person_id") and not speaker.get("hypothesis_only") else ("hypothesis" if (speaker.get("candidate_person_id") or speaker.get("hypothesis_only")) else "unknown")))
        payload = {
            "source_event_id":source_event_id, "segment_index":seg_index, "segment":seg,
            "absolute_start":abs_start, "absolute_end":abs_end, "audio_anchor":file_anchor,
            "raw_audio_path":str(p), "chunk_path":str(chunk),
            "asr":asr, "speaker":speaker, "session_owner_person_id":person_id,
            "speaker_person_id":speaker.get("person_id"), "speaker_label":speaker.get("label"),
            "speaker_candidate_person_id":speaker.get("candidate_person_id"), "speaker_identity_status":speaker_identity_status,
            "speaker_hypothesis_only":bool(speaker.get("hypothesis_only") or speaker_identity_status != "verified"),
            "speaker_use_policy":"speaker_verified" if speaker_identity_status == "verified" else "scene_evidence_not_owner_claim",
            "speaker_context_hints":speaker_context_hints,
        }
        with connect() as con:
            upsert(con, "brainlive_audio_segments_v154", {
                "segment_id":segment_id, "live_session_id":live_session_id, "person_id":person_id,
                "source_event_id":source_event_id, "source_path":str(p), "start_s":start_s, "end_s":end_s,
                "absolute_start":abs_start, "absolute_end":abs_end,
                "vad_backend":seg.get("backend") or vad_backend, "vad_confidence":_clamp(seg.get("confidence")), "chunk_path":str(chunk),
                "asr_backend":asr.get("backend"), "asr_status":asr.get("status"), "transcript_text":text,
                "speaker_json":json_dumps(speaker), "created_at":now, "processed_at":now_iso(),
            }, "segment_id")
            _record_sensor_event(con, live_session_id=live_session_id, person_id=person_id, event_time=abs_start, modality="audio", event_type="speech_segment", source_path=str(chunk), confidence=_weighted_conf([(seg.get("confidence"),0.35),(1.0 if text else 0.0,0.35),(speaker.get("confidence"),0.30)]), model_status=str(asr.get("status")), source_event_id=source_event_id, payload=payload)
            con.commit()
        if text:
            ingest_live_turn(live_session_id, text, speaker_label=speaker.get("label"), speaker_person_id=speaker.get("person_id"), speaker_confidence=_clamp(speaker.get("confidence")), is_final=True, timestamp_start=abs_start, timestamp_end=abs_end, metadata={"source":"v18_audio_sensor","event_id":source_event_id,"segment_id":segment_id,"asr":asr.get("backend"),"vad":seg.get("backend"),"audio_anchor":file_anchor,"source_audio_path":str(p),"chunk_path":str(chunk),"relative_start_s":start_s,"relative_end_s":end_s,"speaker_resolution":speaker,"speaker_identity_status":speaker_identity_status,"speaker_use_policy":payload.get("speaker_use_policy"),"speaker_context_hints":speaker_context_hints,"session_owner_person_id":person_id})
        processed.append({"segment_id":segment_id,"text":text,"asr_status":asr.get("status"),"speaker":speaker,"chunk_path":str(chunk)})
    if failed and processed:
        status = "partial"
    elif failed:
        failure_statuses={str(row.get("reason")) for row in failed}
        status="asr_required" if "asr_required" in failure_statuses else "asr_error" if "asr_error" in failure_statuses else "error"
    else:
        status="ok"
    return {"status":status,"vad":vad,"processed_segments":processed,"failed_segments":failed,"source_event_id":source_event_id,"occurred_at":file_anchor}


# ---------------------------------------------------------------------------
# Vision normalization / place / fusion
# ---------------------------------------------------------------------------

def normalize_vlm_observation(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize VLM JSON. This is structural only, no text keyword analysis."""
    if not isinstance(raw, dict):
        raw = {}
    location_raw = raw.get("location_hint")
    if isinstance(location_raw, str):
        location = {"label": location_raw, "confidence": _clamp(raw.get("confidence"), 0.45), "evidence": ["vlm_location_hint"] if location_raw else []}
    elif isinstance(location_raw, dict):
        location = {"label": location_raw.get("label") or location_raw.get("location") or location_raw.get("place") or "", "confidence": _clamp(location_raw.get("confidence"), _clamp(raw.get("confidence"), 0.45)), "evidence": location_raw.get("evidence") or []}
    else:
        location = {"label": "", "confidence": 0.0, "evidence": []}
    def list_of_dict(name: str) -> list[dict[str, Any]]:
        v = raw.get(name, [])
        if isinstance(v, list):
            out = []
            for item in v:
                if isinstance(item, dict):
                    out.append(item)
                elif isinstance(item, str):
                    out.append({"label": item, "confidence": _clamp(raw.get("confidence"), 0.4), "evidence": ["vlm_list_item"]})
            return out
        return []
    people = list_of_dict("people") or ([{"label": f"person_{i+1}", "confidence": _clamp(raw.get("confidence"), 0.4), "evidence": ["people_count"]} for i in range(int(raw.get("people_count") or 0))] if str(raw.get("people_count") or "").isdigit() else [])
    objects = list_of_dict("objects")
    aff = raw.get("affordances") or raw.get("available_affordances") or []
    affordances = []
    if isinstance(aff, list):
        for item in aff:
            if isinstance(item, dict):
                affordances.append({
                    "label": item.get("label") or item.get("affordance") or "",
                    "world_element": item.get("world_element") or item.get("thing") or item.get("object") or "",
                    "position_hint": item.get("position_hint") or item.get("position") or "",
                    "personal_relevance": item.get("personal_relevance") or item.get("why_relevant") or "unknown",
                    "confidence": _clamp(item.get("confidence"), _clamp(raw.get("confidence"), 0.4)),
                    "evidence": item.get("evidence") or [],
                })
    normalized = {
        "scene_summary": str(raw.get("scene_summary") or raw.get("summary") or ""),
        "location_hint": location,
        "people": people,
        "objects": objects,
        "possible_user_activities": list_of_dict("possible_user_activities") or list_of_dict("activities"),
        "visible_text": list_of_dict("visible_text"),
        "spatial_relations": list_of_dict("spatial_relations"),
        "affordances": affordances,
        "risks_or_attention": raw.get("risks_or_attention") if isinstance(raw.get("risks_or_attention"), list) else (raw.get("risks") if isinstance(raw.get("risks"), list) else []),
        "uncertainties": raw.get("uncertainties") if isinstance(raw.get("uncertainties"), list) else [],
        "confidence": _clamp(raw.get("confidence"), 0.0),
    }
    return normalized


def ingest_image_sensor(
    live_session_id: str,
    image_path: str | Path,
    *,
    model: str | None = None,
    timeout: float = 8.0,
    use_vlm: bool = True,
    source_event_id: str | None = None,
    source_occurred_at: str | None = None,
    source_device: str | None = None,
) -> dict[str, Any]:
    """Ingest one image occurrence atomically and idempotently.

    The image-capture fact is immutable and contains only source/frame identity.
    VLM analysis is a mutable projection keyed to that occurrence, so a temporary
    VLM failure can be retried without adding a frame or changing the sensor fact.
    """
    ensure_sensor_fusion_schema()
    from .v18_runtime_hardening import ensure_runtime_hardening_schema
    ensure_runtime_hardening_schema()
    p = Path(image_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    with connect() as con:
        sess = _one(con, "SELECT person_id,status FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise GovernanceError("image references missing live session")
        if str(sess.get("status") or "") != "active":
            raise GovernanceError("cannot process image for a non-active live session")
    if source_occurred_at is None:
        if os.environ.get("MLOMEGA_ALLOW_UNTIMED_LEGACY", "false").lower() not in {"1","true","yes"}:
            raise GovernanceError("image source_occurred_at is mandatory in V18")
        source_occurred_at = now_iso()
    from .integrity_v176 import iso_utc, parse_iso_utc
    captured_at = iso_utc(parse_iso_utc(source_occurred_at))
    started = time.time()
    raw: dict[str, Any] = {}
    status = "captured_no_vlm"
    error = None
    if use_vlm:
        try:
            raw_result = analyze_vision_with_vlm(p, live_session_id=live_session_id, model=model, timeout=timeout)
            status = str(raw_result.get("status") or "unknown")
            raw = raw_result.get("observation") or {}
            error = raw_result.get("error_text")
        except Exception as exc:
            status = "vlm_error"
            error = str(exc)[:1000]
    normalized = normalize_vlm_observation(raw)
    latency_ms = int((time.time() - started) * 1000)
    with connect() as con, write_transaction(con):
        ing = ingest_vision_frame(
            p, live_session_id=live_session_id, captured_at=captured_at,
            device_source=source_device or "unknown", observation=raw if status == "ok" and raw else None,
            model=model or ("ollama_vlm" if raw else "captured_no_vlm"), source_event_id=source_event_id,
            con=con, schema_ready=True,
        )
        # This projection is intentionally stable across retries.  Its status may
        # improve from ``vlm_error`` to ``ok`` without emitting a second source.
        obs_id = stable_id("blvlmnorm", source_event_id or ing["occurrence_key"], sha256_file(p), captured_at, model or "default")
        upsert(con, "brainlive_vlm_observations_v154", {
            "observation_id": obs_id, "live_session_id": live_session_id, "frame_id": ing.get("frame_id"),
            "image_path": str(p), "normalized_json": json_dumps(normalized), "raw_json": json_dumps(raw),
            "model": model or os.environ.get("MLOMEGA_VLM_MODEL", get_settings().ollama_model),
            "status": status, "confidence": _clamp(normalized.get("confidence")), "latency_ms": latency_ms,
            "error_text": error, "created_at": now_iso(),
        }, "observation_id")
        # Exactly one immutable sensor event per phone/source occurrence.  The
        # VLM payload deliberately is not embedded here: retries may change it.
        source_payload = {
            "source_event_id": source_event_id, "occurrence_key": ing["occurrence_key"],
            "frame_id": ing["frame_id"], "source_asset_id": ing["source_asset_id"],
            "source_item_id": ing["source_item_id"], "captured_at": captured_at,
        }
        event_id = _record_sensor_event(
            con, live_session_id=live_session_id, person_id=sess.get("person_id"), event_time=captured_at,
            modality="vision", event_type="image_captured", source_path=str(p), confidence=0.0,
            model_status="captured", source_event_id=source_event_id or ing["occurrence_key"], payload=source_payload,
        )
    return {
        "observation_id": obs_id, "sensor_event_id": event_id, "status": status,
        "normalized": normalized if status == "ok" else {}, "frame": ing,
        "latency_ms": latency_ms, "error_text": error, "source_event_id": source_event_id,
        "captured_at": captured_at,
    }

def resolve_place_multisource(
    live_session_id: str,
    *,
    explicit_location: str | None = None,
    gps_json: dict[str, Any] | None = None,
    person_id: str | None = None,
) -> dict[str, Any]:
    ensure_sensor_fusion_schema()
    evidence: list[dict[str, Any]] = []
    if explicit_location:
        evidence.append({"source": "explicit", "label": explicit_location, "confidence": 1.0, "evidence": {"input": explicit_location}})
    if gps_json:
        label = gps_json.get("label") or gps_json.get("place") or gps_json.get("name") or gps_json.get("geohash")
        if label:
            evidence.append({"source": "gps", "label": label, "confidence": _clamp(gps_json.get("confidence"), 0.85), "evidence": gps_json})
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,)) or {}
        person_id = person_id or sess.get("person_id")
        if sess.get("active_location_hint"):
            evidence.append({"source": "session", "label": sess.get("active_location_hint"), "confidence": 0.72, "evidence": {"session": live_session_id}})
        vlm = _one(con, "SELECT normalized_json, confidence, created_at FROM brainlive_vlm_observations_v154 WHERE live_session_id=? AND status='ok' ORDER BY created_at DESC LIMIT 1", (live_session_id,))
        if vlm:
            norm = json_loads(vlm.get("normalized_json"), {}) or {}
            loc = norm.get("location_hint") if isinstance(norm.get("location_hint"), dict) else {}
            if loc.get("label"):
                evidence.append({"source": "vlm", "label": loc.get("label"), "confidence": _clamp(loc.get("confidence"), vlm.get("confidence") or 0.5), "evidence": loc})
        # Brain2/history source: only if tables expose places/locations explicitly.
        for table, col in [("brainlive_place_presence", "place_label"), ("vision_scene_observations", "location_hint")]:
            if _table_exists(con, table):
                try:
                    row = _one(con, f"SELECT {col} AS label, confidence FROM {table} WHERE {col} IS NOT NULL AND {col}!='' AND live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,)) if 'live_session_id' in {str(x['name']) for x in con.execute(f'PRAGMA table_info({table})').fetchall()} else None
                    if row and row.get("label"):
                        evidence.append({"source": "history", "label": row.get("label"), "confidence": min(0.55, _clamp(row.get("confidence"), 0.4)), "evidence": {"table": table}})
                except Exception:
                    pass
    # Choose by sum confidence per exact label (not regex, no interpretation).
    scores: dict[str, float] = {}
    for e in evidence:
        label = str(e.get("label") or "").strip()
        if not label:
            continue
        scores[label] = scores.get(label, 0.0) + _clamp(e.get("confidence"))
    if not scores:
        result = {"place_label": None, "confidence": 0.0, "sources": evidence, "status": "unresolved"}
    else:
        best = max(scores, key=scores.get)
        best_sources = [e for e in evidence if str(e.get("label") or "").strip() == best]
        conf = _weighted_conf([(_clamp(e.get("confidence")), 1.0) for e in best_sources])
        result = {"place_label": best, "confidence": conf, "sources": best_sources, "status": "resolved"}
    with connect() as con:
        rid = stable_id("blplace154", live_session_id, result.get("place_label") or "unknown", now_iso())
        upsert(con, "brainlive_place_resolutions", {
            "resolution_id": rid,
            "live_session_id": live_session_id,
            "place_label": result.get("place_label"),
            "confidence": _clamp(result.get("confidence")),
            "sources_json": json_dumps(result.get("sources") or []),
            "evidence_json": json_dumps(result),
            "status": result.get("status") or "unresolved",
            "created_at": now_iso(),
        }, "resolution_id")
        if result.get("place_label"):
            _record_sensor_event(con, live_session_id=live_session_id, person_id=person_id, modality="place", event_type="place_resolved", confidence=_clamp(result.get("confidence")), model_status=result.get("status") or "resolved", payload=result)
        con.commit()
    result["resolution_id"] = rid
    return result


def _recent_events(con, live_session_id: str, *, window_s: float) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(0.0, float(window_s or 0.0)))).isoformat()
    # event_time is the observation time. created_at is only an ingestion-time fallback.
    rows = _many(con, """
        SELECT * FROM brainlive_sensor_events
        WHERE live_session_id=?
          AND COALESCE(event_time, created_at)>=?
        ORDER BY COALESCE(event_time, created_at) DESC
        LIMIT 80
    """, (live_session_id, cutoff))
    return rows


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    speech = []
    vision = []
    people = []
    place = []
    for e in events:
        payload = json_loads(e.get("payload_json"), {}) or {}
        if e.get("modality") == "audio":
            if payload.get("asr", {}).get("text") or payload.get("text"):
                speech.append(payload)
            sp = payload.get("speaker")
            if isinstance(sp, dict):
                people.append(sp)
        elif e.get("modality") == "vision":
            vision.append(payload.get("normalized") or payload)
        elif e.get("modality") == "place":
            place.append(payload)
    return {"speech": speech[:12], "vision": vision[:8], "people": people[:8], "place": place[:5]}


def should_refresh_active_context(live_session_id: str, *, person_id: str, fused_summary: dict[str, Any], force: bool = False) -> dict[str, Any]:
    ensure_sensor_fusion_schema()
    if force:
        return {"refresh": True, "reason": {"force": True}}
    with connect() as con:
        cfg = _get_config(con, person_id=person_id)
        last = _one(con, "SELECT * FROM brainlive_context_refresh_decisions WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
    if not last:
        return {"refresh": True, "reason": {"first_refresh": True}}
    # Staleness by count of new events since last decision. No semantic regex.
    with connect() as con:
        n = _one(con, "SELECT COUNT(*) AS n FROM brainlive_sensor_events WHERE live_session_id=? AND created_at > ?", (live_session_id, last.get("created_at"))) or {"n": 0}
    place_conf = 0.0
    places = fused_summary.get("place") or []
    if places:
        place_conf = max(_clamp(p.get("confidence")) for p in places if isinstance(p, dict))
    people_conf = 0.0
    people = fused_summary.get("people") or []
    if people:
        people_conf = max(_clamp(p.get("confidence")) for p in people if isinstance(p, dict))
    refresh = int(n.get("n") or 0) >= 3 or place_conf >= 0.65 or people_conf >= 0.65
    return {"refresh": refresh, "reason": {"new_events_since_last": int(n.get("n") or 0), "place_confidence": place_conf, "people_confidence": people_conf}}


def build_fused_situation(
    live_session_id: str,
    *,
    person_id: str | None = None,
    explicit_location: str | None = None,
    gps_json: dict[str, Any] | None = None,
    force_context_refresh: bool = False,
    use_llm: bool = True,
) -> dict[str, Any]:
    ensure_sensor_fusion_schema()
    started = time.time()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        person_id = person_id or sess["person_id"]
        cfg = _get_config(con, person_id=person_id)
        events = _recent_events(con, live_session_id, window_s=float(cfg.get("sensor_window_s") or 18.0))
    place = resolve_place_multisource(live_session_id, explicit_location=explicit_location, gps_json=gps_json, person_id=person_id)
    with connect() as con:
        events = _recent_events(con, live_session_id, window_s=float(cfg.get("sensor_window_s") or 18.0))
        vlm_rows = _many(con, """SELECT observation_id,frame_id,normalized_json,confidence,created_at
                                 FROM brainlive_vlm_observations_v154
                                WHERE live_session_id=? AND status='ok'
                                ORDER BY created_at DESC LIMIT 8""", (live_session_id,))
    summary = _summarize_events(events)
    # The immutable sensor event carries source identity only.  Successful VLM
    # analyses are joined as an explicit projection, so a retry cannot rewrite
    # the historical capture or add a duplicate image fact.
    for row in vlm_rows:
        normalized = json_loads(row.get("normalized_json"), {}) or {}
        if normalized:
            summary.setdefault("vision", []).append({"observation_id": row.get("observation_id"), "frame_id": row.get("frame_id"), "normalized": normalized, "confidence": row.get("confidence"), "created_at": row.get("created_at")})
    context_decision = should_refresh_active_context(live_session_id, person_id=person_id, fused_summary=summary, force=force_context_refresh)
    active_context_id = None
    ctx = {}
    latency_ms = None
    if context_decision.get("refresh"):
        t0 = time.time()
        built = build_active_context(live_session_id, limit=32)
        ctx = built.get("context", {})
        active_context_id = built.get("active_context_id") or ctx.get("active_context_id")
        latency_ms = int((time.time() - t0) * 1000)
    else:
        with connect() as con:
            last_ctx = _one(con, "SELECT * FROM brainlive_active_contexts WHERE live_session_id=? ORDER BY updated_at DESC LIMIT 1", (live_session_id,))
            active_context_id = last_ctx.get("active_context_id") if last_ctx else None
    confidence = {
        "audio": max([_clamp(e.get("confidence")) for e in events if e.get("modality") == "audio"] or [0.0]),
        "vision": max([_clamp(e.get("confidence")) for e in events if e.get("modality") == "vision"] or [0.0]),
        "place": _clamp(place.get("confidence")),
        "context": 0.85 if active_context_id else 0.0,
    }
    confidence["overall"] = _weighted_conf([(confidence["audio"], 0.30), (confidence["vision"], 0.20), (confidence["place"], 0.15), (confidence["context"], 0.35)])
    readiness = {
        "has_speech": bool(summary.get("speech")),
        "has_vision": bool(summary.get("vision")),
        "has_place": bool(place.get("place_label")),
        "has_context": bool(active_context_id),
        "llm_required_for_meaning": True,
    }
    # Optional LLM fusion: situation/needs/opportunities, no keyword fallback.
    llm_fusion: dict[str, Any] | None = None
    llm_status = "not_requested"
    if use_llm:
        try:
            llm_fusion = OllamaJsonClient().require_json(
                "Tu es BrainLive Sensor Fusion. Tu fusionnes des signaux capteurs et le contexte Brain2. Tu ne fais aucune règle par mots-clés. Tu produis des hypothèses probabilistes, preuves/contre-preuves et manques. JSON strict.",
                json_dumps({
                    "mission": "Reconnaître la situation actuelle, les besoins probables H0/H1/H2, risques/opportunités et mode observation/proactif.",
                    "sensor_summary": summary,
                    "place_resolution": place,
                    "confidence": confidence,
                    "brain2_context_light": {
                        "active_context_id": active_context_id,
                        "recent_turns_summary": ctx.get("recent_turns_summary") if ctx else None,
                        "relationship_packs": (ctx.get("relationship_packs_json") or ctx.get("relationship_packs") or []) if ctx else [],
                        "open_loops": (ctx.get("open_loops_json") or ctx.get("open_loops") or []) if ctx else [],
                        "patterns": (ctx.get("pattern_cards_json") or ctx.get("pattern_cards") or []) if ctx else [],
                    },
                    "rules": [
                        "no regex",
                        "no keyword psychology",
                        "mark uncertainty",
                        "only use evidence",
                        "unknown/other speech is scene evidence, not a claim that William said it",
                        "if speaker is unknown, use place/vision/recent context to infer the interaction situation, not a verified identity",
                        "low-risk affordance suggestions may use place+vision+William history even when the interlocutor is unknown",
                    ],
                }),
                schema_hint=FUSION_LLM_SCHEMA,
                timeout=12,
            )
            llm_status = "ok"
        except Exception as exc:
            llm_fusion = {"error": str(exc)[:1000], "llm_required": True}
            llm_status = "llm_error"
    now = now_iso()
    fused_id = stable_id("blfused", live_session_id, now, uuid4().hex)
    with connect() as con:
        upsert(con, "brainlive_fused_situations", {
            "fused_id": fused_id,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "window_start": None,
            "window_end": now,
            "active_people_json": json_dumps(summary.get("people") or []),
            "active_place_json": json_dumps(place),
            "speech_json": json_dumps(summary.get("speech") or []),
            "vision_json": json_dumps(summary.get("vision") or []),
            "brain2_context_id": active_context_id,
            "confidence_json": json_dumps({**confidence, "llm_fusion_status": llm_status}),
            "readiness_json": json_dumps({**readiness, "llm_fusion": llm_fusion}),
            "event_ids_json": json_dumps([e.get("event_id") for e in events]),
            "created_at": now,
        }, "fused_id")
        decision_id = stable_id("blctxdec", live_session_id, now, uuid4().hex)
        upsert(con, "brainlive_context_refresh_decisions", {
            "decision_id": decision_id,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "decision": "refresh" if context_decision.get("refresh") else "reuse",
            "reason_json": json_dumps(context_decision.get("reason") or {}),
            "previous_context_id": None,
            "new_context_id": active_context_id,
            "latency_ms": latency_ms,
            "created_at": now,
        }, "decision_id")
        con.commit()
    return {"fused_id": fused_id, "person_id": person_id, "place": place, "summary": summary, "confidence": confidence, "readiness": readiness, "active_context_id": active_context_id, "context_decision": context_decision, "llm_fusion_status": llm_status, "llm_fusion": llm_fusion, "latency_ms": int((time.time() - started) * 1000)}


def _tick_decision(tick: dict[str, Any], *, proactive_confidence_min: float, proactive_gain_min: float) -> dict[str, Any]:
    result = tick.get("result") or {}
    if not isinstance(result, dict):
        return {"decision": "observe", "reason": {"no_result_object": True}}
    candidates = result.get("intervention_candidates") or result.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    best = None
    best_score = 0.0
    for c in candidates:
        if not isinstance(c, dict):
            continue
        conf = _clamp(c.get("confidence"), _clamp(result.get("confidence"), 0.0))
        gain = _clamp(c.get("expected_gain"), _clamp(c.get("urgency"), 0.0))
        speak = c.get("speak_now") if "speak_now" in c else c.get("speak")
        msg = c.get("message") or c.get("intervention_message") or c.get("suggested_message")
        score = _weighted_conf([(conf, 0.55), (gain, 0.45)])
        if msg and speak is not False and conf >= proactive_confidence_min and gain >= proactive_gain_min and score > best_score:
            best, best_score = c, score
    if best:
        return {"decision": "proactive", "reason": {"best_score": best_score, "candidate": best}}
    # Watch if model says to watch or confidence is not enough.
    watch = result.get("watch_mode") or result.get("watch") or {}
    return {"decision": "observe", "reason": {"candidate_count": len(candidates), "watch": watch, "thresholds": {"confidence": proactive_confidence_min, "gain": proactive_gain_min}}}


def run_fused_horizons(
    live_session_id: str,
    *,
    fused_id: str | None = None,
    text: str | None = None,
    use_llm: bool = True,
    use_vlm: bool = True,
    config_id: str | None = None,
) -> dict[str, Any]:
    ensure_sensor_fusion_schema()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        cfg = _get_config(con, person_id=sess["person_id"], config_id=config_id)
        if not fused_id:
            last = _one(con, "SELECT fused_id, readiness_json, speech_json FROM brainlive_fused_situations WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,))
        else:
            last = _one(con, "SELECT fused_id, readiness_json, speech_json FROM brainlive_fused_situations WHERE fused_id=?", (fused_id,))
    if last:
        fused_id = last["fused_id"]
        speech = json_loads(last.get("speech_json"), []) or []
        if not text:
            # Last ASR text from structured events, no keyword analysis.
            texts = []
            for s in speech:
                asr = s.get("asr") if isinstance(s, dict) else None
                if isinstance(asr, dict) and asr.get("text"):
                    texts.append(asr.get("text"))
            text = " ".join(texts[-3:]) if texts else None
    if not text:
        text = "[no_new_speech_text_available; use fused sensor context and Brain2 active context only]"
    cycle = live_cycle_all_horizons(live_session_id, text=text, use_vlm=False, use_llm=use_llm)
    decisions = {}
    delivery_ids_all: list[str] = []
    for horizon in ("H0", "H1", "H2"):
        tick = cycle.get(horizon) or {}
        decision = _tick_decision(tick, proactive_confidence_min=float(cfg.get("proactive_confidence_min") or 0.62), proactive_gain_min=float(cfg.get("proactive_gain_min") or 0.45))
        delivery_ids: list[str] = []
        if decision["decision"] == "proactive":
            delivery_ids = enqueue_interventions_from_tick(live_session_id, tick)
            delivery_ids_all.extend(delivery_ids)
        decisions[horizon] = {**decision, "delivery_ids": delivery_ids}
        with connect() as con:
            pid = stable_id("blpro", live_session_id, fused_id or "none", horizon, now_iso(), uuid4().hex)
            upsert(con, "brainlive_proactive_decisions", {
                "proactive_id": pid,
                "live_session_id": live_session_id,
                "fused_id": fused_id,
                "horizon": horizon,
                "decision": decision["decision"],
                "reason_json": json_dumps(decision.get("reason") or {}),
                "tick_json": json_dumps(tick),
                "delivery_ids_json": json_dumps(delivery_ids),
                "created_at": now_iso(),
            }, "proactive_id")
            con.commit()
    return {"fused_id": fused_id, "cycle": cycle, "decisions": decisions, "delivery_ids": delivery_ids_all}


def full_sensor_live_cycle(
    live_session_id: str,
    *,
    audio_path: str | None = None,
    text: str | None = None,
    image_path: str | None = None,
    explicit_location: str | None = None,
    gps_json: dict[str, Any] | None = None,
    person_id: str | None = None,
    vad_backend: str | None = None,
    asr_backend: str | None = None,
    use_llm: bool = True,
    use_vlm: bool = True,
    force_context_refresh: bool = False,
) -> dict[str, Any]:
    """One complete V15.4 loop: sensors -> fusion -> H0/H1/H2 -> queue/watch."""
    ensure_sensor_fusion_schema()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        person_id = person_id or sess["person_id"]
        cfg = _get_config(con, person_id=person_id)
    processed: dict[str, Any] = {}
    if audio_path:
        processed["audio"] = process_audio_sensor(live_session_id, audio_path, person_id=person_id, vad_backend=vad_backend or cfg.get("vad_backend"), asr_backend=asr_backend or cfg.get("asr_backend"))
    if text:
        # Explicit upstream transcript is a trusted sensor event and live turn.
        turn = ingest_live_turn(live_session_id, text, speaker_label="upstream", is_final=True, metadata={"source": "v15_4_explicit_text"})
        with connect() as con:
            _record_sensor_event(con, live_session_id=live_session_id, person_id=person_id, modality="audio", event_type="upstream_transcript", confidence=0.9, payload={"text": text, "turn": turn}, model_status="upstream")
            con.commit()
        processed["text"] = turn
    if image_path:
        processed["vision"] = ingest_image_sensor(live_session_id, image_path, use_vlm=use_vlm)
    fused = build_fused_situation(live_session_id, person_id=person_id, explicit_location=explicit_location, gps_json=gps_json, force_context_refresh=force_context_refresh, use_llm=use_llm)
    horizons = run_fused_horizons(live_session_id, fused_id=fused.get("fused_id"), text=text, use_llm=use_llm, use_vlm=use_vlm)
    # Outcome watcher is non-blocking-ish and can be disabled by env.
    outcome = None
    if os.environ.get("MLOMEGA_BRAINLIVE_OUTCOME_WATCH_INLINE", "false").lower() in {"1", "true", "yes", "on"}:
        try:
            outcome = outcome_watch(live_session_id, person_id=person_id, limit=10, timeout=60)
        except Exception as exc:
            outcome = {"status": "error", "error": str(exc)[:1000]}
    return {"version": VERSION, "processed": processed, "fused": fused, "horizons": horizons, "outcome_watch": outcome}


def sensor_fusion_audit() -> dict[str, Any]:
    ensure_sensor_fusion_schema()
    settings = get_settings()
    deps = {}
    for name in ["silero_vad", "faster_whisper", "whisperx", "speechbrain"]:
        try:
            __import__(name)
            deps[name] = "available"
        except Exception:
            deps[name] = "missing"
    with connect() as con:
        counts = {}
        for t in ["brainlive_sensor_configs", "brainlive_sensor_events", "brainlive_audio_segments_v154", "brainlive_vlm_observations_v154", "brainlive_fused_situations", "brainlive_proactive_decisions"]:
            counts[t] = int((_one(con, f"SELECT COUNT(*) AS n FROM {t}") or {"n": 0})["n"])
    return {
        "version": VERSION,
        "contract": "V15.4 sensor fusion: no regex/no keyword psychology; model-required for meaning; Brain2 remains deep truth.",
        "dependencies": deps,
        "ollama_enabled": settings.enable_ollama,
        "tables": counts,
        "targets": {"H0_ms": 2000, "H1_ms": 5000, "H2_ms": 12000},
    }

# V18 remediation: consume the single shared live analysis once and avoid
# duplicate prediction/intervention writes across H0/H1/H2.
from . import brainlive_realtime_v15_2 as _v18_realtime_module
from .v18_live_execution import install_sensor as _install_v18_sensor_execution
_globals_v18_sensor_execution = _install_v18_sensor_execution(__import__(__name__, fromlist=['*']), _v18_realtime_module)
globals().update(_globals_v18_sensor_execution)

# V18 remediation: unknown voice continuity is persisted per memory owner when
# an embedding exists; labels remain non-identifying scene evidence.
from .v18_sensor_identity import install as _install_v18_sensor_identity
_globals_v18_sensor_identity = _install_v18_sensor_identity(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_sensor_identity)
