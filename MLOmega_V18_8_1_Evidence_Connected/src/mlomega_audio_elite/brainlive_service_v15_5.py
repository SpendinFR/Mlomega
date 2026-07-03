from __future__ import annotations

"""V15.5 BrainLive service: one living loop, not a collection of manual commands.

This module is the runtime wrapper around V15.4 Sensor Fusion and the existing
Brain2 V13/V14 stack. It deliberately does not create a second Brain2. Brain2 is
still the deep/nightly truth; BrainLive is the day/live hot projection.

Contract:
- no regex / keyword psychology;
- all meaning-making goes through Brain2 context, LLM JSON, VLM JSON, or explicit
  upstream sensor metadata;
- VAD/silence detection is signal processing only;
- unknown identity/place/meaning is recorded as unresolved instead of guessed;
- CLI surface is start/stop/status. Internals are automatic loop/tick/fusion.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .brainlive_longitudinal_v15_1 import scheduler_tick
from .brainlive_realtime_v15_2 import configure_runtime_profile
from .brainlive_sensor_fusion_v15_4 import (
    build_fused_situation,
    configure_sensor_fusion,
    ensure_sensor_fusion_schema,
    full_sensor_live_cycle,
    ingest_image_sensor,
    process_audio_sensor,
    run_fused_horizons,
)
from .brainlive_v15 import ensure_brainlive_schema, ingest_live_turn, start_live_session
from .brainlive_hotloop_v15_6 import ensure_hotloop_schema, resolve_speaker_hot, drain_due_hot_llm_decisions
from .brainlive_invalidation_v15_7 import optimized_hot_brainlive_cycle, ensure_invalidation_schema
from .config import get_settings
from .runtime_v18_7 import is_local_pid_alive
from .db import connect, init_db, upsert
from .governance_v18 import (
    EventTime, Scope, GovernanceError, claim_work, finish_work, register_event,
    source_key, work_scope_key, canonical_time, ensure_v18_schema,
)
from .llm import OllamaJsonClient
from .utils import iso_add_seconds, json_dumps, json_loads, now_iso, sha256_file, stable_id
from .v18_8_live_policy import (
    ensure_v18_8_live_policy_schema, plan_live_dispatch, mark_live_dispatch,
    plan_image_capture, enqueue_image_work, plan_image_worker_dispatch,
    claim_due_image_work, finish_image_work, pending_image_count,
    annotate_captured_frame,
    record_delivery_feedback, materialize_intervention_outcome_observation,
    materialize_open_delivery_observations,
)

VERSION = "15.5.0-brainlive-service-one-loop"

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TRANSCRIPT_EXTS = {".txt", ".json", ".jsonl"}

SERVICE_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_service_configs(
  service_config_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  audio_dir TEXT,
  transcript_dir TEXT,
  image_dir TEXT,
  gps_state_path TEXT,
  feedback_dir TEXT,
  location_hint TEXT,
  vad_backend TEXT DEFAULT 'silero',
  asr_backend TEXT DEFAULT 'faster_or_whispercpp',
  speaker_backend TEXT DEFAULT 'speechbrain_ecapa',
  vlm_model TEXT,
  h0_timeout_s REAL DEFAULT 2.0,
  h1_timeout_s REAL DEFAULT 5.0,
  h2_timeout_s REAL DEFAULT 12.0,
  sensor_tick_s REAL DEFAULT 1.0,
  image_tick_s REAL DEFAULT 10.0,
  context_refresh_s REAL DEFAULT 12.0,
  nightly_time TEXT DEFAULT '03:30',
  status TEXT DEFAULT 'active',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_service_runs(
  service_run_id TEXT PRIMARY KEY,
  service_config_id TEXT,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  stopped_at TEXT,
  last_heartbeat_at TEXT,
  iterations INTEGER DEFAULT 0,
  last_error TEXT,
  counters_json TEXT DEFAULT '{}',
  process_id INTEGER,
  process_host TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_service_processed_files(
  processed_id TEXT PRIMARY KEY,
  service_run_id TEXT,
  live_session_id TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT DEFAULT '{}',
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_service_state(
  state_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_service_signals(
  signal_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  service_run_id TEXT,
  signal_kind TEXT NOT NULL,
  source TEXT,
  payload_json TEXT DEFAULT '{}',
  confidence REAL DEFAULT 0.0,
  handled_as TEXT DEFAULT 'observation',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blsvc_run_session ON brainlive_service_runs(live_session_id, status);
CREATE INDEX IF NOT EXISTS idx_blsvc_processed_session ON brainlive_service_processed_files(live_session_id, processed_at);
CREATE INDEX IF NOT EXISTS idx_blsvc_state_session_key ON brainlive_service_state(live_session_id, key);
"""


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


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def ensure_service_schema() -> None:
    ensure_v18_schema()
    ensure_brainlive_schema()
    ensure_sensor_fusion_schema()
    ensure_hotloop_schema()
    ensure_invalidation_schema()
    ensure_v18_8_live_policy_schema()
    init_db()
    with connect() as con:
        con.executescript(SERVICE_SCHEMA)
        # Additive migration for V18.7 immediate crash recovery. A service PID
        # is not a lock; it is evidence that a fresh heartbeat is nevertheless
        # from a dead local process after a reboot.
        columns = {str(r[1]) for r in con.execute("PRAGMA table_info(brainlive_service_runs)").fetchall()}
        if "process_id" not in columns:
            con.execute("ALTER TABLE brainlive_service_runs ADD COLUMN process_id INTEGER")
        if "process_host" not in columns:
            con.execute("ALTER TABLE brainlive_service_runs ADD COLUMN process_host TEXT")
        cfg_columns = {str(r[1]) for r in con.execute("PRAGMA table_info(brainlive_service_configs)").fetchall()}
        if "feedback_dir" not in cfg_columns:
            con.execute("ALTER TABLE brainlive_service_configs ADD COLUMN feedback_dir TEXT")
        con.commit()

def _runtime_service_manifest_path() -> Path:
    path = get_settings().root_dir / "runtime" / "brainlive_service.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_runtime_service_manifest(payload: dict[str, Any]) -> None:
    """Atomically publish the active/last BrainLive run for Windows launchers.

    The DB remains canonical; this small file exists only so a detached
    PowerShell launcher can discover the session/run id immediately, rather
    than waiting for a long-running CLI process to print its final JSON.
    """
    path = _runtime_service_manifest_path()
    tmp = path.with_suffix(".json.tmp")
    data = {"updated_at": now_iso(), "pid": os.getpid(), **payload}
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def recover_stale_brainlive_service_runs(*, stale_after_s: int | None = None) -> dict[str, Any]:
    """Mark abandoned service processes orphaned after a PC crash.

    A stale DB heartbeat must never prevent the next close-day/resume forever.
    We do *not* invent a successful stop; ``orphaned`` is an explicit durable
    state that allows the operator to resume the safe post-stop flow.
    """
    ensure_service_schema()
    threshold = int(stale_after_s or get_settings().stage_stale_after_s)
    now_dt = datetime.now(timezone.utc)
    changed: list[str] = []
    with connect() as con:
        rows = _many(con, "SELECT * FROM brainlive_service_runs WHERE status IN ('running','stop_requested','drain_recovery')")
        for row in rows:
            raw = row.get("last_heartbeat_at") or row.get("updated_at") or row.get("started_at")
            try:
                text = str(raw).replace("Z", "+00:00")
                then = datetime.fromisoformat(text)
                if then.tzinfo is None:
                    then = then.replace(tzinfo=timezone.utc)
                age = (now_dt - then.astimezone(timezone.utc)).total_seconds()
            except Exception:
                age = float(threshold) + 1
            # A service run now persists its owning process.  After a PC
            # shutdown the heartbeat may be only seconds old, but the PID is
            # already gone; recover immediately instead of waiting 30 minutes.
            pid_dead = False
            owner_pid = row.get("process_id")
            owner_host = str(row.get("process_host") or "")
            if owner_pid and (not owner_host or owner_host == __import__("socket").gethostname()):
                pid_dead = not is_local_pid_alive(int(owner_pid))
            if age < threshold and not pid_dead:
                continue
            prior_status = str(row.get("status") or "")
            row["status"] = "stopped_pending_ingest" if prior_status == "drain_recovery" else "orphaned"
            row["stopped_at"] = row.get("stopped_at") or now_iso()
            row["last_error"] = (
                f"drain recovery heartbeat stale for {int(age)}s; raw inbox must be resumed before post-stop"
                if prior_status == "drain_recovery"
                else ("service process PID is no longer alive; process may have stopped unexpectedly" if pid_dead else f"service heartbeat stale for {int(age)}s; process may have stopped unexpectedly")
            )
            row["updated_at"] = now_iso()
            upsert(con, "brainlive_service_runs", row, "service_run_id")
            changed.append(str(row.get("service_run_id")))
        con.commit()
    if changed:
        _write_runtime_service_manifest({"status": "orphaned", "orphaned_service_run_ids": changed})
    return {"status": "ok", "stale_after_s": threshold, "orphaned_service_run_ids": changed}


def _read_json_path(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _state_set(con, live_session_id: str, key: str, value: dict[str, Any]) -> None:
    now = now_iso()
    upsert(con, "brainlive_service_state", {
        "state_id": stable_id("blsvcstate", live_session_id, key),
        "live_session_id": live_session_id,
        "key": key,
        "value_json": json_dumps(value),
        "updated_at": now,
    }, "state_id")


def _state_get(con, live_session_id: str, key: str) -> dict[str, Any]:
    row = _one(con, "SELECT value_json FROM brainlive_service_state WHERE live_session_id=? AND key=?", (live_session_id, key))
    return json_loads(row["value_json"], {}) if row else {}


def _record_signal(con, *, live_session_id: str, service_run_id: str | None, kind: str, source: str | None, payload: dict[str, Any], confidence: float = 0.0, handled_as: str = "observation") -> str:
    sid = stable_id("blsvcsig", live_session_id, kind, source or "none", now_iso(), uuid4().hex)
    upsert(con, "brainlive_service_signals", {
        "signal_id": sid,
        "live_session_id": live_session_id,
        "service_run_id": service_run_id,
        "signal_kind": kind,
        "source": source,
        "payload_json": json_dumps(payload),
        "confidence": float(confidence or 0.0),
        "handled_as": handled_as,
        "created_at": now_iso(),
    }, "signal_id")
    return sid


def _file_sha(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except Exception:
        return None


def _is_capture_sidecar(path: Path) -> bool:
    """Return True for ``media.ext.json`` sidecars, never ingestable sources.

    A transcript source may itself end in ``.json``.  Therefore we only skip a
    JSON file when removing its final suffix yields an existing file whose
    suffix is a configured media/transcript extension.  This prevents a valid
    ``meeting.json`` transcript from being hidden while preventing
    ``meeting.json.json`` or ``clip.wav.json`` from being re-ingested.
    """
    if path.suffix.lower() != ".json":
        return False
    primary = path.with_suffix("")
    return primary.is_file() and primary.suffix.lower() in (AUDIO_EXTS | IMAGE_EXTS | TRANSCRIPT_EXTS)


def _read_sidecar(path: Path) -> dict[str, Any]:
    """Read and validate the optional capture sidecar without guessing time.

    The accepted sidecars are ``file.wav.json``/``file.jpg.json`` and the older
    ``file.json`` convention.  A hash mismatch is an integrity error, not a
    harmless metadata mismatch.
    """
    candidates = [path.with_suffix(path.suffix + ".json"), path.with_suffix(".json")]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            raise GovernanceError(f"invalid sidecar {candidate}: {exc}") from exc
        if not isinstance(data, dict):
            raise GovernanceError(f"sidecar {candidate} must be a JSON object")
        actual_sha = _file_sha(path)
        declared_sha = data.get("sha256") or data.get("source_sha256")
        if declared_sha and actual_sha and str(declared_sha) != actual_sha:
            raise GovernanceError(f"sidecar hash mismatch for {path}")
        data["_sidecar_path"] = str(candidate)
        return data
    return {}


def _source_descriptor(path: Path, *, kind: str) -> dict[str, Any]:
    # Intervention feedback is an immutable JSON event in its own right.  It
    # does not have a media sidecar, but it still needs the same explicit time,
    # source identity and durable dedupe contract as audio/image/transcript.
    if kind == "feedback":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise GovernanceError(f"invalid feedback JSON: {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise GovernanceError(f"feedback JSON must be an object: {path}")
        event_time = payload.get("observed_at") or payload.get("occurred_at") or payload.get("received_at")
        if not isinstance(event_time, str) or not event_time.strip():
            raise GovernanceError(f"feedback requires observed_at/occurred_at: {path}")
        delivery_id = str(payload.get("delivery_id") or "").strip()
        feedback_type = str(payload.get("feedback_type") or payload.get("status") or "").strip().lower()
        if not delivery_id or not feedback_type:
            raise GovernanceError(f"feedback requires delivery_id and feedback_type: {path}")
        source_device = str(payload.get("source_device") or payload.get("feedback_source") or "bridge_or_dashboard")
        source_event_id = str(payload.get("feedback_id") or stable_id("v188feedbacksource", delivery_id, feedback_type, event_time, _file_sha(path) or "nohash"))
        sha = _file_sha(path)
        return {
            "source_device": source_device,
            "source_event_id": source_event_id,
            "source_sha256": sha,
            "occurred_at": event_time,
            "captured_at": event_time,
            "received_at": payload.get("received_at") if isinstance(payload.get("received_at"), str) else None,
            "sidecar": payload,
            "source_key": source_key(source_device=source_device, source_event_id=source_event_id, source_sha256=sha, occurred_at=event_time, source_path=str(path)),
        }
    sidecar = _read_sidecar(path)
    event_time = (
        sidecar.get("timestamp_start") or sidecar.get("captured_at") or
        sidecar.get("recorded_at") or sidecar.get("start_at") or sidecar.get("occurred_at")
    )
    # V18 intentionally does not silently use mtime as an observation time.
    # It may be recorded as filesystem metadata by the caller, but a source
    # without a real capture/occurrence time is quarantined before learning.
    if not isinstance(event_time, str) or not event_time.strip():
        raise GovernanceError(f"missing canonical capture time for {kind} source: {path}")
    source_device = str(sidecar.get("source_device") or sidecar.get("device_id") or "legacy_local_unverified")
    event_id = sidecar.get("source_event_id") or sidecar.get("event_id") or sidecar.get("upload_id") or sidecar.get("sequence_id")
    sha = _file_sha(path)
    return {
        "source_device": source_device,
        "source_event_id": str(event_id) if event_id is not None else None,
        "source_sha256": sha,
        "occurred_at": event_time,
        "captured_at": sidecar.get("captured_at") if isinstance(sidecar.get("captured_at"), str) else event_time,
        "received_at": sidecar.get("received_at") if isinstance(sidecar.get("received_at"), str) else None,
        "sidecar": sidecar,
        "source_key": source_key(source_device=source_device, source_event_id=str(event_id) if event_id is not None else None, source_sha256=sha, occurred_at=event_time, source_path=str(path)),
    }


def _file_ready(path: Path) -> bool:
    """Avoid consuming a capture still being written.

    Preferred producer contract: write ``.part``, fsync, atomically rename the
    media and sidecar, then create ``.ready``.  For legacy producers V18 waits
    for a configurable settle period; it never treats a zero-byte/new file as
    ready.
    """
    if path.name.endswith('.part') or path.suffix.lower() in {'.tmp', '.partial'}:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size <= 0:
        return False
    ready_marker = path.with_suffix(path.suffix + '.ready')
    if ready_marker.exists():
        return True
    try:
        settle = max(1.0, float(os.environ.get('MLOMEGA_INBOX_SETTLE_SECONDS', '2.0')))
    except ValueError:
        settle = 2.0
    return (time.time() - st.st_mtime) >= settle


def _already_processed(con, live_session_id: str, path: Path, sha: str | None, *, person_id: str | None = None, kind: str | None = None) -> bool:
    """Only terminal work is deduplicated; errors remain retryable.

    Legacy rows are consulted only for a completed result.  V18 work identity
    is durable across sessions and uses device/event/time/hash, not merely a
    session-local file path.
    """
    if person_id and kind:
        try:
            desc = _source_descriptor(path, kind=kind)
            scoped_key = work_scope_key(person_id=person_id, source_key_value=desc["source_key"])
            row = _one(con, "SELECT state FROM v18_work_leases WHERE work_type=? AND source_key=?", (f"inbox:{kind}", scoped_key))
            if row:
                return str(row.get("state")) in {"completed", "quarantined", "cancelled"}
            # Read-only compatibility for RC1 rows written before source keys
            # were owner-scoped.  Never inspect a different owner's raw key.
            legacy = _one(con, "SELECT state FROM v18_work_leases WHERE work_type=? AND source_key=? AND person_id=?", (f"inbox:{kind}", desc["source_key"], person_id))
            if legacy:
                return str(legacy.get("state")) in {"completed", "quarantined", "cancelled"}
        except GovernanceError:
            # A malformed descriptor cannot be passed to the normal source-key
            # writer, but it must still get a durable terminal quarantine.
            # Otherwise every service iteration retries the same broken file
            # forever while claiming it was "not processed".
            fallback = _invalid_descriptor_source_key(path, kind=kind, sha=sha)
            scoped = work_scope_key(person_id=person_id, source_key_value=fallback)
            row = _one(
                con,
                "SELECT state FROM v18_work_leases WHERE work_type=? AND source_key=? AND person_id=?",
                (f"inbox:{kind}:invalid_descriptor", scoped, person_id),
            )
            return bool(row and str(row.get("state")) in {"completed", "quarantined", "cancelled"})
    # The durable V18 lease is the authority. The legacy audit table is only
    # available after the service schema has been installed, so a fresh/minimal
    # database must not crash merely because a now-valid descriptor has no
    # legacy audit row yet.
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brainlive_service_processed_files'"
    ).fetchone()
    if not exists:
        return False
    if sha:
        row = _one(con, "SELECT processed_id FROM brainlive_service_processed_files WHERE sha256=? AND kind=? AND status IN ('ok','empty','completed','quarantined') LIMIT 1", (sha, kind or ''))
        return bool(row)
    row = _one(con, "SELECT processed_id FROM brainlive_service_processed_files WHERE path=? AND kind=? AND status IN ('ok','empty','completed','quarantined') LIMIT 1", (str(path), kind or ''))
    return bool(row)


def _invalid_descriptor_source_key(path: Path, *, kind: str, sha: str | None = None) -> str:
    """Stable fallback identity for a source rejected before sidecar parsing.

    It is deliberately distinct from valid media identity: repairing the
    sidecar later must make the source eligible again, while an unchanged
    malformed artifact must not create an infinite retry loop.
    """
    return stable_id("v18_invalid_descriptor", kind, str(path.expanduser()), sha or _file_sha(path) or "nohash")


def _quarantine_unclaimable_input(path: Path, *, kind: str, person_id: str, live_session_id: str, error: BaseException) -> None:
    """Persist a terminal quarantine for an input with no valid descriptor."""
    scope = Scope(person_id=person_id, live_session_id=live_session_id, mode="live")
    key = _invalid_descriptor_source_key(path, kind=kind)
    lease = claim_work(
        work_type=f"inbox:{kind}:invalid_descriptor",
        scope=scope,
        source_key_value=key,
        lease_seconds=60,
        max_attempts=1,
    )
    if lease is None:
        return
    finish_work(
        work_key=lease["work_key"],
        lease_token=lease["lease_token"],
        status="quarantined",
        result={"path": str(path), "kind": kind, "reason": "invalid_descriptor"},
        error_text=str(error)[:1500],
    )


def _claim_input(path: Path, *, kind: str, person_id: str, live_session_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Claim a durable item and create its immutable event envelope."""
    scope = Scope(person_id=person_id, live_session_id=live_session_id, mode='live')
    desc = _source_descriptor(path, kind=kind)
    lease = claim_work(work_type=f"inbox:{kind}", scope=scope, source_key_value=desc["source_key"], lease_seconds=120, max_attempts=5)
    if not lease:
        return None
    try:
        event = register_event(
            scope=scope, modality={'audio':'audio','image':'vision','transcript':'text'}.get(kind, kind),
            source_device=desc['source_device'], source_event_id=desc['source_event_id'], source_sha256=desc['source_sha256'],
            time=EventTime(occurred_at=desc['occurred_at'], captured_at=desc['captured_at'], received_at=desc['received_at'], processed_at=now_iso()),
            source_path=str(path), payload={'kind':kind, 'sidecar':desc['sidecar']},
        )
    except Exception as exc:
        finish_work(work_key=lease['work_key'], lease_token=lease['lease_token'], status='quarantined', result={'path':str(path)}, error_text=str(exc))
        raise
    desc['event_id'] = event['event_id']
    desc['work'] = lease
    return desc, event


def _finish_input(desc: dict[str, Any], *, status: str, result: dict[str, Any]) -> None:
    work = desc.get('work') or {}
    if not work:
        return
    terminal_ok = status in {'ok','empty','completed','captured_no_vlm'}
    retryable = status in {'error','asr_error','asr_required','vad_error','vlm_error','partial'}
    next_state = 'completed' if terminal_ok else 'retryable_error' if retryable else 'quarantined'
    finish_work(
        work_key=work['work_key'], lease_token=work['lease_token'], status=next_state,
        result=result, error_text=(result.get('error') or result.get('error_text')) if isinstance(result, dict) else None,
        retry_delay_seconds=30,
    )


def _mark_processed(con, *, service_run_id: str, live_session_id: str, path: Path, kind: str, status: str, result: dict[str, Any]) -> None:
    sha = _file_sha(path)
    # Audit trail only. Durable retry/dedupe decisions live in v18_work_leases.
    upsert(con, "brainlive_service_processed_files", {
        "processed_id": stable_id("blsvcfile", str(path), sha or "nohash", kind),
        "service_run_id": service_run_id,
        "live_session_id": live_session_id,
        "path": str(path),
        "sha256": sha,
        "kind": kind,
        "status": status,
        "result_json": json_dumps(result),
        "processed_at": now_iso(),
    }, "processed_id")


def _iter_new_files(con, live_session_id: str, directory: str | None, exts: set[str], *, person_id: str | None = None, kind: str | None = None) -> list[Path]:
    if not directory:
        return []
    d = Path(directory).expanduser()
    if not d.exists() or not d.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0.0):
        if not p.is_file() or p.suffix.lower() not in exts or not _file_ready(p):
            continue
        if _is_capture_sidecar(p):
            continue
        sha = _file_sha(p)
        if not _already_processed(con, live_session_id, p, sha, person_id=person_id, kind=kind):
            out.append(p)
    return out

def _normalize_transcript_speaker(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "label": value.get("label") or value.get("speaker_label") or value.get("name"),
            "speaker_label": value.get("speaker_label") or value.get("label") or value.get("name"),
            "person_id": value.get("person_id"),
            "confidence": value.get("confidence") or value.get("speaker_confidence"),
        }
    if isinstance(value, str) and value.strip():
        return {"label": value.strip(), "speaker_label": value.strip()}
    return {}


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _normalize_transcript_turns(items: Any) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return turns
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("transcript") or "").strip()
        if not text:
            continue
        speaker = _normalize_transcript_speaker(item.get("speaker") or item.get("speaker_json") or item)
        # Do not use ``a or b``: a legitimate 0.0 offset was previously lost.
        turns.append({
            "idx": item.get("idx", idx),
            "text": text,
            "speaker": speaker,
            "timestamp_start": _first_present(item, "timestamp_start", "start", "start_time", "start_s"),
            "timestamp_end": _first_present(item, "timestamp_end", "end", "end_time", "end_s"),
            "confidence": item.get("confidence"),
            "raw_turn": item,
        })
    return turns


def _parse_json_or_jsonl(raw: str) -> Any:
    """Accept one JSON document or newline-delimited JSON objects."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first:
        records: list[Any] = []
        for line_no, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_no}: {exc}") from first
        if records:
            return records
        raise


def _extract_transcript_text(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".txt":
        return {"status": "ok", "text": path.read_text(encoding="utf-8", errors="ignore").strip(), "speaker": {}, "turns": [], "raw": {}}
    try:
        data = _parse_json_or_jsonl(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return {"status": "error", "text": "", "turns": [], "error": str(exc)[:500], "raw": {}}
    if isinstance(data, dict):
        if isinstance(data.get("turns"), list):
            turns = _normalize_transcript_turns(data.get("turns"))
            text = " ".join(t["text"] for t in turns)
            raw_parent = {k: v for k, v in data.items() if k != "turns"}
            return {"status": "ok" if turns else "empty", "text": text, "speaker": {}, "turns": turns, "raw": raw_parent, "raw_turn_count": len(turns)}
        if isinstance(data.get("text"), str):
            return {"status": "ok", "text": data["text"].strip(), "speaker": _normalize_transcript_speaker(data.get("speaker") or {}), "turns": [], "raw": data}
    if isinstance(data, list):
        turns = _normalize_transcript_turns(data)
        text = " ".join(t["text"] for t in turns)
        return {"status": "ok" if turns else "empty", "text": text, "speaker": {}, "turns": turns, "raw": {"jsonl_or_list_transcript": True, "item_count": len(data)}, "raw_turn_count": len(turns)}
    return {"status": "empty", "text": "", "speaker": {}, "turns": [], "raw": {}}


def _absolute_transcript_time(value: Any, *, anchor: str, field: str) -> str:
    if isinstance(value, str) and value.strip():
        try:
            # Absolute timezone-aware timestamps are already canonical.
            from .integrity_v176 import parse_iso_utc, iso_utc
            return iso_utc(parse_iso_utc(value))
        except Exception:
            try:
                seconds = float(value)
            except (TypeError, ValueError) as exc:
                raise GovernanceError(f"invalid transcript {field}: {value!r}") from exc
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif value is None:
        seconds = 0.0
    else:
        raise GovernanceError(f"invalid transcript {field}: {value!r}")
    if not math.isfinite(seconds) or seconds < 0:
        raise GovernanceError(f"invalid transcript offset {field}: {value!r}")
    return iso_add_seconds(anchor, seconds)

def _safe_confidence(value: Any) -> float:
    try:
        parsed = float(0.0 if value is None else value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _is_confident_speaker(speaker: Any, *, threshold: float = 0.65) -> bool:
    return isinstance(speaker, dict) and bool(speaker.get("person_id")) and _safe_confidence(speaker.get("confidence")) >= threshold


def _latest_confident_audio_speaker(result: dict[str, Any], *, threshold: float = 0.65) -> tuple[str | None, str | None, str | None]:
    """Extract the first confident recognized speaker from one processed audio file.

    process_audio_sensor() returns segments in chronological order. The caller
    stores this tuple as the latest upstream identity signal for the hotloop;
    later files in the same service iteration can overwrite it.
    """
    for seg in result.get("processed_segments", []) or []:
        if not isinstance(seg, dict):
            continue
        speaker = seg.get("speaker") or {}
        if _is_confident_speaker(speaker, threshold=threshold):
            return str(speaker.get("person_id")), speaker.get("label") or speaker.get("speaker_label"), seg.get("chunk_path")
    return None, None, None



def _default_inbox_dirs() -> dict[str, str]:
    """Canonical BrainLive inbox folders.

    The service can still receive explicit directories, but when none are
    provided it now has a ready-to-drop inbox:

      .mlomega_audio_elite/brainlive_inbox/audio
      .mlomega_audio_elite/brainlive_inbox/transcripts
      .mlomega_audio_elite/brainlive_inbox/images
      .mlomega_audio_elite/brainlive_inbox/gps/current.json

    This is only an ingestion surface. Meaning-making remains LLM/VLM/Brain2.
    """
    root = get_settings().root_dir / "brainlive_inbox"
    paths = {
        "root": root,
        "audio_dir": root / "audio",
        "transcript_dir": root / "transcripts",
        "image_dir": root / "images",
        "gps_dir": root / "gps",
        "feedback_dir": root / "feedback",
        "archive_dir": root / "archive",
        "errors_dir": root / "errors",
    }
    for key, value in paths.items():
        if key.endswith("_dir") or key in {"root"}:
            Path(value).mkdir(parents=True, exist_ok=True)
    gps_state = paths["gps_dir"] / "current.json"
    if not gps_state.exists():
        gps_state.write_text("{}", encoding="utf-8")
    readme = root / "README_BRAINLIVE_INBOX.md"
    if not readme.exists():
        readme.write_text(
            "# BrainLive inbox\n\n"
            "Dépose les flux ici si tu ne fournis pas de dossiers explicites au service.\n\n"
            "- `audio/` : chunks `.wav/.flac/.mp3/.m4a/.ogg` pour VAD/ASR.\n"
            "- `transcripts/` : `.txt/.json/.jsonl` déjà transcrits.\n"
            "- `images/` : frames `.jpg/.jpeg/.png/.webp` pour VLM.\n"
            "- `gps/current.json` : état lieu/GPS courant optionnel.\n"
            "- `feedback/` : retours de livraison d’intervention du téléphone/dashboard.\n\n"
            "BrainLive marque les fichiers déjà traités dans la base; il ne refait pas le même fichier si son hash est déjà vu.\n",
            encoding="utf-8",
        )
    return {k: str(v) for k, v in {**paths, "gps_state_path": gps_state}.items()}


def brainlive_inbox_status() -> dict[str, Any]:
    ensure_service_schema()
    dirs = _default_inbox_dirs()
    with connect() as con:
        processed = int((_one(con, "SELECT COUNT(*) AS n FROM brainlive_service_processed_files") or {"n": 0})["n"])
    def count_files(path: str, exts: set[str] | None = None) -> int:
        p = Path(path)
        if not p.exists():
            return 0
        return sum(1 for f in p.iterdir() if f.is_file() and (exts is None or f.suffix.lower() in exts))
    return {
        "version": VERSION,
        "status": "ready",
        "inbox": {
            "root": dirs["root"],
            "audio_dir": dirs["audio_dir"],
            "transcript_dir": dirs["transcript_dir"],
            "image_dir": dirs["image_dir"],
            "gps_state_path": dirs["gps_state_path"],
            "feedback_dir": dirs["feedback_dir"],
        },
        "pending_counts": {
            "audio": count_files(dirs["audio_dir"], AUDIO_EXTS),
            "transcripts": count_files(dirs["transcript_dir"], TRANSCRIPT_EXTS),
            "images": count_files(dirs["image_dir"], IMAGE_EXTS),
            "feedback": count_files(dirs["feedback_dir"], {".json"}),
        },
        "processed_total": processed,
    }


def configure_brainlive_service(
    *,
    person_id: str | None = None,
    audio_dir: str | None = None,
    transcript_dir: str | None = None,
    image_dir: str | None = None,
    gps_state_path: str | None = None,
    feedback_dir: str | None = None,
    location_hint: str | None = None,
    vad_backend: str = "silero",
    asr_backend: str | None = None,
    vlm_model: str | None = None,
    sensor_tick_s: float = 1.0,
    image_tick_s: float = 10.0,
    context_refresh_s: float = 12.0,
    nightly_time: str = "03:30",
) -> dict[str, Any]:
    if not person_id:
        raise GovernanceError("V18 service configuration requires explicit person_id")
    ensure_service_schema()
    inbox = _default_inbox_dirs()
    audio_dir = audio_dir or inbox["audio_dir"]
    transcript_dir = transcript_dir or inbox["transcript_dir"]
    image_dir = image_dir or inbox["image_dir"]
    gps_state_path = gps_state_path or inbox["gps_state_path"]
    feedback_dir = feedback_dir or inbox["feedback_dir"]
    now = now_iso()
    asr_backend = asr_backend or os.environ.get("MLOMEGA_BRAINLIVE_ASR_BACKEND") or "faster_or_whispercpp"
    with connect() as con:
        person_id = person_id or _default_user(con)
        cid = stable_id("blsvc_cfg", person_id)
        upsert(con, "brainlive_service_configs", {
            "service_config_id": cid,
            "person_id": person_id,
            "audio_dir": audio_dir,
            "transcript_dir": transcript_dir,
            "image_dir": image_dir,
            "gps_state_path": gps_state_path,
            "feedback_dir": feedback_dir,
            "location_hint": location_hint,
            "vad_backend": vad_backend,
            "asr_backend": asr_backend,
            "speaker_backend": "speechbrain_ecapa",
            "vlm_model": vlm_model,
            "h0_timeout_s": 2.0,
            "h1_timeout_s": 5.0,
            "h2_timeout_s": 12.0,
            "sensor_tick_s": float(sensor_tick_s),
            "image_tick_s": float(image_tick_s),
            "context_refresh_s": float(context_refresh_s),
            "nightly_time": nightly_time,
            "status": "active",
            "metadata_json": json_dumps({"contract": "single living BrainLive loop; canonical inbox; Brain2 deep truth; no regex psychology"}),
            "created_at": now,
            "updated_at": now,
        }, "service_config_id")
        con.commit()
    # Keep lower layers aligned with Brain2's Ollama client/config.
    configure_runtime_profile(person_id=person_id, h0_timeout=2.0, h1_timeout=5.0, h2_timeout=12.0, vlm_timeout=8.0)
    configure_sensor_fusion(person_id=person_id, vad_backend=vad_backend, asr_backend=asr_backend, vlm_backend="ollama_multimodal")
    return {"service_config_id": cid, "person_id": person_id, "version": VERSION}


def _get_config(con, service_config_id: str | None = None, person_id: str | None = None) -> dict[str, Any]:
    if service_config_id:
        row = _one(con, "SELECT * FROM brainlive_service_configs WHERE service_config_id=?", (service_config_id,))
        if not row:
            raise ValueError(f"Config BrainLive service introuvable: {service_config_id}")
        return row
    if not person_id:
        raise GovernanceError("V18 service config lookup requires explicit person_id or service_config_id")
    row = _one(con, "SELECT * FROM brainlive_service_configs WHERE person_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (person_id,))
    if row:
        return row
    configure_brainlive_service(person_id=person_id)
    row = _one(con, "SELECT * FROM brainlive_service_configs WHERE person_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (person_id,))
    if not row:
        raise RuntimeError("Impossible de créer la config BrainLive service.")
    return row


def _ensure_live_session(person_id: str, live_session_id: str | None, title: str | None, location_hint: str | None) -> str:
    if live_session_id:
        return live_session_id
    res = start_live_session(person_id=person_id, title=title or "BrainLive day loop", location_hint=location_hint)
    return str(res.get("live_session_id"))


def _mark_run(con, run_id: str, *, status: str, iterations: int | None = None, counters: dict[str, Any] | None = None, error: str | None = None, stopped: bool = False) -> None:
    fields = {"status": status, "last_heartbeat_at": now_iso(), "updated_at": now_iso()}
    if iterations is not None:
        fields["iterations"] = iterations
    if counters is not None:
        fields["counters_json"] = json_dumps(counters)
    if error is not None:
        fields["last_error"] = error[:2000]
    if stopped:
        fields["stopped_at"] = now_iso()
    row = _one(con, "SELECT * FROM brainlive_service_runs WHERE service_run_id=?", (run_id,))
    if row:
        row.update(fields)
        upsert(con, "brainlive_service_runs", row, "service_run_id")


def _service_stop_requested(con, live_session_id: str, run_id: str) -> bool:
    row = _one(con, "SELECT status FROM brainlive_service_runs WHERE service_run_id=?", (run_id,))
    if row and row.get("status") == "stop_requested":
        return True
    state = _state_get(con, live_session_id, "control")
    return state.get("stop_requested") is True


def _nightly_due(last: dict[str, Any], nightly_time: str) -> bool:
    # Lightweight: actual scheduler remains in V15.1/Brain2. This only triggers once per local date.
    from datetime import datetime
    try:
        h, m = [int(x) for x in str(nightly_time or "03:30").split(":")[:2]]
    except Exception:
        h, m = 3, 30
    now_dt = datetime.now()
    if now_dt.hour < h or (now_dt.hour == h and now_dt.minute < m):
        return False
    today = now_dt.date().isoformat()
    return last.get("last_nightly_date") != today


def _run_nightly_if_due(con, *, live_session_id: str, person_id: str, nightly_time: str, counters: dict[str, Any]) -> None:
    state = _state_get(con, live_session_id, "nightly")
    if not _nightly_due(state, nightly_time):
        return
    try:
        result = scheduler_tick(person_id=person_id, kind="nightly")
        _state_set(con, live_session_id, "nightly", {"last_nightly_date": __import__("datetime").date.today().isoformat(), "result": result})
        counters["nightly"] = counters.get("nightly", 0) + 1
        _record_signal(con, live_session_id=live_session_id, service_run_id=None, kind="nightly_brain2", source="scheduler", payload=result, confidence=1.0, handled_as="brain2_consolidation")
    except Exception as exc:
        _state_set(con, live_session_id, "nightly", {"error": str(exc)[:1000], "at": now_iso()})



def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 200) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))

def _audit_processed(run_id: str, live_session_id: str, path: Path, kind: str, status: str, result: dict[str, Any]) -> None:
    with connect() as con:
        _mark_processed(con, service_run_id=run_id, live_session_id=live_session_id, path=path, kind=kind, status=str(status), result=result)
        con.commit()


def _apply_intervention_feedback_file(path: Path, *, person_id: str, live_session_id: str) -> dict[str, Any]:
    """Consume a Bridge/dashboard feedback receipt as durable live evidence."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GovernanceError(f"invalid intervention feedback JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GovernanceError("intervention feedback must be a JSON object")
    delivery_id = str(payload.get("delivery_id") or "").strip()
    feedback_type = str(payload.get("feedback_type") or payload.get("status") or "").strip().lower()
    if not delivery_id or not feedback_type:
        raise GovernanceError("intervention feedback requires delivery_id and feedback_type")
    feedback = record_delivery_feedback(
        delivery_id=delivery_id,
        feedback_type=feedback_type,
        feedback_source=str(payload.get("feedback_source") or "bridge"),
        note=str(payload.get("note") or "") or None,
        evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {},
        observed_at=str(payload.get("observed_at") or payload.get("occurred_at") or now_iso()),
    )
    outcome = materialize_intervention_outcome_observation(
        delivery_id=delivery_id,
        outcome_status="feedback_explicit" if feedback_type in {"acted", "dismissed", "ignored"} else "observation_pending",
        observed_later_summary=str(payload.get("note") or "") or None,
        did_help=True if feedback_type == "acted" else False if feedback_type in {"dismissed", "ignored"} else None,
        evidence={"feedback": feedback, "source_file": str(path)},
        observed_at=str(payload.get("observed_at") or payload.get("occurred_at") or now_iso()),
    )
    materialize_open_delivery_observations(live_session_id=live_session_id, person_id=person_id)
    return {"status": "ok", "feedback": feedback, "outcome": outcome}


def _transcript_turn_times(turn: dict[str, Any], *, anchor: str) -> tuple[str, str]:
    start = _absolute_transcript_time(turn.get("timestamp_start"), anchor=anchor, field="timestamp_start")
    end_value = turn.get("timestamp_end")
    end = _absolute_transcript_time(end_value if end_value is not None else turn.get("timestamp_start"), anchor=anchor, field="timestamp_end")
    from .integrity_v176 import parse_iso_utc, iso_utc
    if parse_iso_utc(end) < parse_iso_utc(start):
        raise GovernanceError("transcript timestamp_end precedes timestamp_start")
    return iso_utc(parse_iso_utc(start)), iso_utc(parse_iso_utc(end))


def service_iteration(run_id: str, live_session_id: str, cfg: dict[str, Any], counters: dict[str, Any], *, drain_only: bool = False) -> dict[str, Any]:
    """One governed BrainLive iteration.

    Work is leased before processing and only terminal success is deduplicated.
    A malformed/untimed source is quarantined or retried; it can no longer turn
    into a timestamped live fact at the wall-clock time of this loop.
    """
    person_id = str(cfg["person_id"])
    processed_any = False
    fused_result: dict[str, Any] | None = None
    horizon_result: dict[str, Any] | None = None
    latest_audio_sample_path: str | None = None
    latest_speaker_label: str | None = None
    explicit_speaker_person_id: str | None = None
    audio_content_seen = False
    # True only when an audio chunk was inspected this iteration.  It prevents
    # an idle service loop from accidentally clearing the speech→silence state.
    audio_observed = False
    silence_seen = False
    image_live_observation: dict[str, Any] | None = None
    image_scheduler: dict[str, Any] = {"run": False, "reason": "not_evaluated"}
    gps_json = _read_json_path(cfg.get("gps_state_path"))
    explicit_location = cfg.get("location_hint")
    # Durable hot LLM attempts are independent from new sensor arrivals. A
    # timeout/truncated output therefore retries its immutable capsule even when
    # the next loop has no new audio/image/transcript file.
    retry_results = [] if drain_only else drain_due_hot_llm_decisions(live_session_id=live_session_id, limit=2)
    if retry_results:
        counters["hot_llm_retries"] = counters.get("hot_llm_retries", 0) + len(retry_results)
    with connect() as con:
        audio_files = _iter_new_files(con, live_session_id, cfg.get("audio_dir"), AUDIO_EXTS, person_id=person_id, kind="audio")
        transcript_files = _iter_new_files(con, live_session_id, cfg.get("transcript_dir"), TRANSCRIPT_EXTS, person_id=person_id, kind="transcript")
        image_files = _iter_new_files(con, live_session_id, cfg.get("image_dir"), IMAGE_EXTS, person_id=person_id, kind="image")

    for p in audio_files[:_env_int("MLOMEGA_BRAINLIVE_AUDIO_BATCH", 2, max_value=20)]:
        desc: dict[str, Any] | None = None
        try:
            claim = _claim_input(p, kind="audio", person_id=person_id, live_session_id=live_session_id)
            if not claim:
                continue
            desc, _event = claim
            result = process_audio_sensor(
                live_session_id, p, person_id=person_id,
                vad_backend=cfg.get("vad_backend") or "silero",
                asr_backend=os.environ.get("MLOMEGA_BRAINLIVE_ASR_BACKEND") or cfg.get("asr_backend") or "faster_or_whispercpp",
                source_event_id=desc["event_id"], source_occurred_at=desc["occurred_at"],
            )
            status = str(result.get("status") or "ok")
            audio_observed = True
            processed_segments = result.get("processed_segments") if isinstance(result.get("processed_segments"), list) else []
            if any(str(segment.get("text") or "").strip() for segment in processed_segments if isinstance(segment, dict)):
                audio_content_seen = True
            vad_segments = (result.get("vad") or {}).get("segments") if isinstance(result.get("vad"), dict) else None
            if isinstance(vad_segments, list) and not vad_segments:
                silence_seen = True
            _finish_input(desc, status=status, result=result)
            counters["audio"] = counters.get("audio", 0) + 1
            processed_any = processed_any or status in {"ok", "empty", "completed"}
            sp_person_id, sp_label, sp_sample = _latest_confident_audio_speaker(result)
            if sp_person_id:
                explicit_speaker_person_id = sp_person_id
                latest_speaker_label = sp_label
                latest_audio_sample_path = sp_sample
        except Exception as exc:
            result = {"error": str(exc)[:1000], "path": str(p)}
            status = "error"
            if desc:
                _finish_input(desc, status=status, result=result)
            else:
                _quarantine_unclaimable_input(p, kind="audio", person_id=person_id, live_session_id=live_session_id, error=exc)
                status = "quarantined"
        _audit_processed(run_id, live_session_id, p, "audio", status, result)

    for p in transcript_files[:_env_int("MLOMEGA_BRAINLIVE_TRANSCRIPT_BATCH", 4, max_value=50)]:
        desc: dict[str, Any] | None = None
        try:
            claim = _claim_input(p, kind="transcript", person_id=person_id, live_session_id=live_session_id)
            if not claim:
                continue
            desc, _event = claim
            tr = _extract_transcript_text(p)
            result = tr
            status = str(tr.get("status") or "unknown")
            if str(tr.get("text") or "").strip() or bool(tr.get("turns")):
                audio_content_seen = True
            if status == "error":
                _finish_input(desc, status=status, result=result)
                _audit_processed(run_id, live_session_id, p, "transcript", status, result)
                continue
            transcript_turns = tr.get("turns") if isinstance(tr.get("turns"), list) else []
            if transcript_turns:
                for turn in transcript_turns:
                    speaker = turn.get("speaker") or {}
                    speaker_confidence = _safe_confidence(speaker.get("confidence") if speaker.get("confidence") is not None else turn.get("confidence"))
                    ts_start, ts_end = _transcript_turn_times(turn, anchor=desc["occurred_at"])
                    ingest_live_turn(
                        live_session_id, turn["text"],
                        speaker_label=speaker.get("label") or speaker.get("speaker_label") or "upstream",
                        speaker_person_id=speaker.get("person_id"), speaker_confidence=speaker_confidence,
                        is_final=True, timestamp_start=ts_start, timestamp_end=ts_end,
                        metadata={"source": "brainlive_service_transcript", "path": str(p), "event_id": desc["event_id"], "occurred_at": desc["occurred_at"], "raw": tr.get("raw"), "raw_turn": turn.get("raw_turn"), "raw_turn_index": turn.get("idx")},
                    )
                    if _is_confident_speaker(speaker):
                        explicit_speaker_person_id = str(speaker.get("person_id"))
                        latest_speaker_label = speaker.get("label") or speaker.get("speaker_label")
                        latest_audio_sample_path = None
            elif tr.get("text"):
                speaker = tr.get("speaker") or {}
                speaker_confidence = _safe_confidence(speaker.get("confidence"))
                ingest_live_turn(
                    live_session_id, tr["text"], speaker_label=speaker.get("label") or speaker.get("speaker_label") or "upstream",
                    speaker_person_id=speaker.get("person_id"), speaker_confidence=speaker_confidence, is_final=True,
                    timestamp_start=desc["occurred_at"], timestamp_end=desc["occurred_at"],
                    metadata={"source": "brainlive_service_transcript", "path": str(p), "event_id": desc["event_id"], "occurred_at": desc["occurred_at"], "raw": tr.get("raw")},
                )
                if _is_confident_speaker(speaker):
                    explicit_speaker_person_id = str(speaker.get("person_id"))
                    latest_speaker_label = speaker.get("label") or speaker.get("speaker_label")
                    latest_audio_sample_path = None
            with connect() as con:
                _record_signal(con, live_session_id=live_session_id, service_run_id=run_id, kind="transcript", source=str(p), payload={"event_id": desc["event_id"], "turn_count": len(transcript_turns), "status":status}, confidence=0.9, handled_as="speech")
                con.commit()
            _finish_input(desc, status=status, result=result)
            processed_any = processed_any or status in {"ok", "empty", "completed"}
            counters["transcripts"] = counters.get("transcripts", 0) + 1
        except Exception as exc:
            result = {"error": str(exc)[:1000], "path": str(p)}
            status = "error"
            if desc:
                _finish_input(desc, status=status, result=result)
            else:
                _quarantine_unclaimable_input(p, kind="transcript", person_id=person_id, live_session_id=live_session_id, error=exc)
                status = "quarantined"
        _audit_processed(run_id, live_session_id, p, "transcript", status, result)

    # Images are first accepted as immutable frame evidence without a VLM call.
    # A persistent fair-share queue decides later whether live VLM work is useful.
    image_ingest_limit = _env_int("MLOMEGA_BRAINLIVE_IMAGE_INGEST_BATCH", 4, max_value=50)
    for p in image_files[:image_ingest_limit]:
        desc: dict[str, Any] | None = None
        try:
            claim = _claim_input(p, kind="image", person_id=person_id, live_session_id=live_session_id)
            if not claim:
                continue
            desc, _event = claim
            captured = ingest_image_sensor(
                live_session_id, p, model=cfg.get("vlm_model"), use_vlm=False,
                source_event_id=desc["event_id"], source_occurred_at=desc["occurred_at"],
                source_device=desc["source_device"],
            )
            policy = plan_image_capture(live_session_id=live_session_id, path=p, descriptor=desc)
            frame = captured.get("frame") if isinstance(captured.get("frame"), dict) else {}
            annotate_captured_frame(frame_id=frame.get("frame_id"), policy=policy)
            result = {**captured, "live_vlm_policy": policy}
            if policy.get("analyze_live_vlm"):
                queued = enqueue_image_work(
                    live_session_id=live_session_id,
                    person_id=person_id,
                    descriptor={
                        **desc,
                        "v188_image_signature": policy.get("signature"),
                        "v188_image_signature_kind": policy.get("signature_kind"),
                        "v188_live_vlm_reason": policy.get("reason"),
                    },
                    path=p,
                )
                result["image_work"] = queued
            else:
                result["image_work"] = {"status": "not_queued", "reason": policy.get("reason")}
            status = "captured_no_vlm"
            _finish_input(desc, status=status, result=result)
            counters["images_captured"] = counters.get("images_captured", 0) + 1
            processed_any = True
        except Exception as exc:
            result = {"error": str(exc)[:1000], "path": str(p)}
            status = "error"
            if desc:
                _finish_input(desc, status=status, result=result)
            else:
                _quarantine_unclaimable_input(p, kind="image", person_id=person_id, live_session_id=live_session_id, error=exc)
                status = "quarantined"
        _audit_processed(run_id, live_session_id, p, "image", status, result)

    # Audio gets first claim in each iteration. Image VLM work then uses an idle
    # slot, a silence slot, or a bounded fair-share maximum wait.
    try:
        pending_now = _pending_inbox_counts(live_session_id=live_session_id, cfg=cfg)
        image_scheduler = plan_image_worker_dispatch(
            live_session_id=live_session_id,
            audio_pending=int(pending_now.get("audio", 0)) + int(pending_now.get("transcript", 0)),
            silence_seen=silence_seen,
        )
        if image_scheduler.get("run"):
            work = claim_due_image_work(live_session_id=live_session_id, lease_seconds=float(os.environ.get("MLOMEGA_BRAINLIVE_IMAGE_LEASE_S", "120")))
            if work:
                try:
                    descriptor = json_loads(work.get("descriptor_json"), {}) or {}
                    observed = ingest_image_sensor(
                        live_session_id, work["source_path"], model=cfg.get("vlm_model"),
                        timeout=float(os.environ.get("MLOMEGA_BRAINLIVE_VLM_TIMEOUT_S", "8")), use_vlm=True,
                        source_event_id=work["source_event_id"],
                        source_occurred_at=work["source_occurred_at"],
                        source_device=work.get("source_device"),
                    )
                    image_live_observation = observed.get("normalized") if isinstance(observed.get("normalized"), dict) else None
                    observed["image_signature"] = descriptor.get("v188_image_signature")
                    observed["image_signature_kind"] = descriptor.get("v188_image_signature_kind")
                    work_status = "completed" if str(observed.get("status")) in {"ok", "captured_no_vlm"} else "retryable_error"
                    finish_image_work(
                        image_work_id=work["image_work_id"], lease_token=work["lease_token"],
                        status=work_status, result=observed, error_text=observed.get("error_text"),
                    )
                    counters["images_live_vlm"] = counters.get("images_live_vlm", 0) + 1
                except Exception as exc:
                    finish_image_work(
                        image_work_id=work["image_work_id"], lease_token=work["lease_token"],
                        status="retryable_error", result={"error": str(exc)[:1000]}, error_text=str(exc),
                    )
                    counters["image_live_vlm_errors"] = counters.get("image_live_vlm_errors", 0) + 1
    except Exception as exc:
        image_scheduler = {"run": False, "reason": "image_scheduler_error", "error": str(exc)[:1000]}

    # Bridge/dashboard delivery receipts are treated as durable evidence. They
    # never fabricate an outcome: Brain2 receives feedback plus later context.
    feedback_dir = cfg.get("feedback_dir") or _default_inbox_dirs()["feedback_dir"]
    with connect() as con:
        feedback_files = _iter_new_files(con, live_session_id, feedback_dir, {".json"}, person_id=person_id, kind="feedback")
    for p in feedback_files[:_env_int("MLOMEGA_BRAINLIVE_FEEDBACK_BATCH", 20, max_value=100)]:
        desc: dict[str, Any] | None = None
        try:
            claim = _claim_input(p, kind="feedback", person_id=person_id, live_session_id=live_session_id)
            if not claim:
                continue
            desc, _event = claim
            result = _apply_intervention_feedback_file(p, person_id=person_id, live_session_id=live_session_id)
            _finish_input(desc, status="ok", result=result)
            counters["intervention_feedback"] = counters.get("intervention_feedback", 0) + 1
            processed_any = True
            _audit_processed(run_id, live_session_id, p, "feedback", "ok", result)
        except Exception as exc:
            result = {"error": str(exc)[:1000], "path": str(p)}
            if desc:
                _finish_input(desc, status="error", result=result)
            else:
                _quarantine_unclaimable_input(p, kind="feedback", person_id=person_id, live_session_id=live_session_id, error=exc)
            _audit_processed(run_id, live_session_id, p, "feedback", "error", result)

    with connect() as con:
        last_fusion = _state_get(con, live_session_id, "last_fusion")
    cadence_due = (time.time() - float(last_fusion.get("time", 0.0) or 0.0)) >= float(cfg.get("context_refresh_s") or 12.0)
    dispatch_plan = plan_live_dispatch(
        live_session_id=live_session_id,
        audio_content=audio_content_seen,
        image_observation=image_live_observation,
        gps=gps_json,
        cadence_due=cadence_due,
        silence_boundary=silence_seen,
        audio_observed=audio_observed,
    )
    if not drain_only and (dispatch_plan.get("should_dispatch_llm") or dispatch_plan.get("should_refresh_context_only")):
        try:
            meaningful_signal = bool(dispatch_plan.get("should_dispatch_llm"))
            hot_result = optimized_hot_brainlive_cycle(
                live_session_id, person_id=person_id, explicit_location=explicit_location, gps_json=gps_json,
                latest_audio_sample_path=latest_audio_sample_path, latest_speaker_label=latest_speaker_label,
                explicit_speaker_person_id=explicit_speaker_person_id, meaningful_signal=meaningful_signal,
                force_context=bool(dispatch_plan.get("force_context")),
            )
            hot_status = str(hot_result.get("status") or "ok")
            mark_live_dispatch(live_session_id=live_session_id, plan=dispatch_plan, status="ok" if hot_status not in {"error", "failed", "llm_error"} else hot_status)
            fused_result = hot_result.get("fused")
            horizon_result = hot_result.get("prediction") if meaningful_signal else {"status": "hot_context_ready", "hot": hot_result}
            counters["hot_cycles"] = counters.get("hot_cycles", 0) + 1
            if meaningful_signal:
                counters["llm_dispatches"] = counters.get("llm_dispatches", 0) + 1
            with connect() as con:
                _state_set(con, live_session_id, "last_fusion", {
                    "time": time.time(), "fused_id": (fused_result or {}).get("fused_id"),
                    "dispatch_reason": dispatch_plan.get("reason"), "llm_dispatched": meaningful_signal,
                    "hot_status": hot_result.get("status"), "image_scheduler": image_scheduler,
                })
                _record_signal(con, live_session_id=live_session_id, service_run_id=run_id, kind="brainlive_hot_cycle", source="service_v18_8", payload=hot_result, confidence=((fused_result or {}).get("confidence") or {}).get("overall", 0.0) if isinstance((fused_result or {}).get("confidence"), dict) else 0.0, handled_as="proactive_or_observation" if meaningful_signal else "hot_context_refresh")
                con.commit()
        except Exception as exc:
            mark_live_dispatch(live_session_id=live_session_id, plan=dispatch_plan, status="retryable_error")
            with connect() as con:
                _record_signal(con, live_session_id=live_session_id, service_run_id=run_id, kind="brainlive_hot_cycle_error", source="service_v18_8", payload={"error": str(exc)[:1000], "dispatch_plan": dispatch_plan}, handled_as="error")
                con.commit()
            raise
    if not drain_only:
        with connect() as con:
            _run_nightly_if_due(con, live_session_id=live_session_id, person_id=person_id, nightly_time=str(cfg.get("nightly_time") or "03:30"), counters=counters)
            con.commit()
    return {"processed_any": processed_any, "fused": fused_result, "horizons": horizon_result, "hot_llm_retries": retry_results, "image_scheduler": image_scheduler, "pending_live_image_work": pending_image_count(live_session_id=live_session_id), "counters": counters}


def _pending_inbox_counts(*, live_session_id: str, cfg: dict[str, Any]) -> dict[str, int]:
    """Count source files that do not yet have a terminal durable work result.

    This is stricter than ``processed_any``: an ASR/VLM failure remains pending
    even though the loop handled the exception, so stop cannot falsely declare
    the inbox drained and hand raw files to a later cleanup.
    """
    person_id = str(cfg["person_id"])
    definitions = {
        "audio": (cfg.get("audio_dir"), AUDIO_EXTS),
        "transcript": (cfg.get("transcript_dir"), TRANSCRIPT_EXTS),
        "image": (cfg.get("image_dir"), IMAGE_EXTS),
        "feedback": (cfg.get("feedback_dir") or _default_inbox_dirs()["feedback_dir"], {".json"}),
    }
    counts: dict[str, int] = {key: 0 for key in definitions}
    with connect() as con:
        for kind, (directory, extensions) in definitions.items():
            if not directory:
                continue
            root = Path(str(directory)).expanduser()
            if not root.exists() or not root.is_dir():
                continue
            for path in root.iterdir():
                if not path.is_file() or path.suffix.lower() not in extensions or _is_capture_sidecar(path):
                    continue
                if not _already_processed(con, live_session_id, path, _file_sha(path), person_id=person_id, kind=kind):
                    counts[kind] += 1
    return counts


def _drain_inbox_before_stop(
    *, run_id: str, live_session_id: str, cfg: dict[str, Any], counters: dict[str, Any]
) -> dict[str, Any]:
    """Consume the final bridge-delivered files before ending the service.

    The Phone Bridge first writes media into the canonical inbox and then
    requests stop.  Older versions checked the stop flag *before* another
    ingestion pass, creating a real last-media race.  V18.7 drains until two
    consecutive idle passes (or a bounded timeout) before allowing post-stop.
    """
    try:
        timeout_s = max(5.0, float(os.environ.get("MLOMEGA_STOP_DRAIN_TIMEOUT_S", "300")))
        idle_needed = max(1, int(os.environ.get("MLOMEGA_STOP_DRAIN_IDLE_PASSES", "2")))
    except Exception:
        timeout_s, idle_needed = 300.0, 2
    started = time.monotonic()
    idle = 0
    passes = 0
    errors: list[str] = []
    while time.monotonic() - started < timeout_s:
        passes += 1
        try:
            result = service_iteration(run_id, live_session_id, cfg, counters, drain_only=True)
            pending = _pending_inbox_counts(live_session_id=live_session_id, cfg=cfg)
            pending_total = sum(pending.values())
            if bool(result.get("processed_any")) or pending_total:
                idle = 0
            else:
                idle += 1
            if idle >= idle_needed:
                return {
                    "status": "drained", "passes": passes, "idle_passes": idle,
                    "timeout_s": timeout_s, "pending": pending,
                }
            if pending_total:
                errors.append("pending_ingest=" + json_dumps(pending))
        except Exception as exc:
            errors.append(str(exc)[:500])
            # Do not discard raw media after an error; one retry may resolve a
            # transient local model/SQLite issue while the source remains safe.
            time.sleep(min(2.0, float(cfg.get("sensor_tick_s") or 1.0)))
        time.sleep(min(0.25, float(cfg.get("sensor_tick_s") or 1.0)))
    pending = _pending_inbox_counts(live_session_id=live_session_id, cfg=cfg)
    return {"status": "pending_ingest", "passes": passes, "idle_passes": idle, "timeout_s": timeout_s, "pending": pending, "errors": errors[-3:]}



def resume_brainlive_pending_ingest(
    *,
    person_id: str,
    live_session_id: str | None = None,
    service_run_id: str | None = None,
) -> dict[str, Any]:
    """Safely consume an inbox left after a power loss before post-stop resumes.

    This intentionally does *not* run deep audio, vision or Brain2.  It creates
    a small, durable drain-recovery run for the same live session, reuses the
    normal idempotent inbox processing, and only reports ``stopped`` after two
    idle passes.  A subsequent close-day can then resume its own checkpoints.
    """
    if not person_id:
        raise GovernanceError("resume inbox drain requires an explicit person_id")
    ensure_service_schema()
    recover_stale_brainlive_service_runs()
    with connect() as con:
        if service_run_id:
            previous = _one(con, "SELECT * FROM brainlive_service_runs WHERE service_run_id=? AND person_id=?", (service_run_id, person_id))
        elif live_session_id:
            previous = _one(con, """SELECT * FROM brainlive_service_runs
                WHERE live_session_id=? AND person_id=?
                ORDER BY COALESCE(stopped_at, started_at) DESC LIMIT 1""", (live_session_id, person_id))
        else:
            previous = _one(con, """SELECT * FROM brainlive_service_runs
                WHERE person_id=? AND status IN ('stopped_pending_ingest','orphaned','drain_recovery')
                ORDER BY COALESCE(stopped_at, started_at) DESC LIMIT 1""", (person_id,))
        if not previous:
            return {"status": "no_pending_ingest", "person_id": person_id}
        source_status = str(previous.get("status") or "")
        if source_status not in {"stopped_pending_ingest", "orphaned", "drain_recovery"}:
            return {"status": "not_needed", "person_id": person_id, "service_run_id": previous.get("service_run_id"), "source_status": source_status}
        cfg = _get_config(con, service_config_id=previous.get("service_config_id"), person_id=person_id)
        session_id = str(previous["live_session_id"])
        inherited_counters = json_loads(previous.get("counters_json"), {}) or {}
        if not isinstance(inherited_counters, dict):
            inherited_counters = {}
        recovery_run_id = stable_id("blsvc_drain_recovery", str(previous["service_run_id"]), now_iso(), uuid4().hex)
        now = now_iso()
        upsert(con, "brainlive_service_runs", {
            "service_run_id": recovery_run_id,
            "service_config_id": cfg.get("service_config_id"),
            "live_session_id": session_id,
            "person_id": person_id,
            "status": "drain_recovery",
            "started_at": now,
            "stopped_at": None,
            "last_heartbeat_at": now,
            "iterations": 0,
            "last_error": None,
            "counters_json": json_dumps(inherited_counters),
            "process_id": os.getpid(),
            "process_host": __import__("socket").gethostname(),
            "created_at": now,
            "updated_at": now,
        }, "service_run_id")
        _state_set(con, session_id, "control", {"stop_requested": True, "close_day_requested": False, "drain_recovery_of": previous["service_run_id"], "requested_at": now})
        con.commit()
    _write_runtime_service_manifest({"status": "drain_recovery", "service_run_id": recovery_run_id, "source_service_run_id": previous["service_run_id"], "live_session_id": session_id, "person_id": person_id})
    drain = _drain_inbox_before_stop(run_id=recovery_run_id, live_session_id=session_id, cfg=dict(cfg), counters=inherited_counters)
    with connect() as con:
        if str(drain.get("status")) == "drained":
            _mark_run(con, recovery_run_id, status="stopped", iterations=int(inherited_counters.get("iterations") or 0), counters=inherited_counters, stopped=True)
            old = _one(con, "SELECT * FROM brainlive_service_runs WHERE service_run_id=?", (previous["service_run_id"],))
            if old:
                old["status"] = "recovered_after_drain"
                old["last_error"] = "inbox drain resumed by V18.7; use the recovery run for post-stop"
                old["updated_at"] = now_iso()
                upsert(con, "brainlive_service_runs", old, "service_run_id")
            _state_set(con, session_id, "stop_drain", drain | {"at": now_iso(), "recovered_from": previous["service_run_id"], "recovery_run_id": recovery_run_id})
            con.commit()
            status = "drained"
        else:
            _mark_run(con, recovery_run_id, status="stopped_pending_ingest", iterations=int(inherited_counters.get("iterations") or 0), counters=inherited_counters, error=json_dumps(drain), stopped=True)
            _state_set(con, session_id, "stop_drain", drain | {"at": now_iso(), "recovery_run_id": recovery_run_id})
            con.commit()
            status = "pending_ingest"
    _write_runtime_service_manifest({"status": "stopped" if status == "drained" else "stopped_pending_ingest", "service_run_id": recovery_run_id, "source_service_run_id": previous["service_run_id"], "live_session_id": session_id, "person_id": person_id, "drain": drain})
    return {"status": status, "person_id": person_id, "live_session_id": session_id, "service_run_id": recovery_run_id, "source_service_run_id": previous["service_run_id"], "drain": drain}


def start_brainlive_service(
    *,
    person_id: str | None = None,
    service_config_id: str | None = None,
    live_session_id: str | None = None,
    title: str | None = None,
    audio_dir: str | None = None,
    transcript_dir: str | None = None,
    image_dir: str | None = None,
    gps_state_path: str | None = None,
    location_hint: str | None = None,
    max_iterations: int = 0,
    post_stop_deep_flow: bool | None = None,
    post_stop_use_llm: bool = True,
) -> dict[str, Any]:
    """Start the living BrainLive loop.

    This is meant to be the only command you normally run. Directories can be
    supplied once; the service then continuously coordinates VAD/ASR/speaker/VLM/
    place/context/H0-H1-H2 without manual CLI ticks.

    In normal long-running mode, stopping the service automatically launches the
    V15.15 post-stop deep flow: V15.14 event assembly -> classic Brain2 V13/V14
    analysis on assembled conversations. Day-wide V15.12/V15.13/V15.9 promotion is
    intentionally performed by a requested close-day after the session flow. Tests and bounded
    max_iterations runs skip it by default unless post_stop_deep_flow=True.
    """
    if not person_id and not service_config_id:
        raise GovernanceError("V18 service start requires explicit person_id or an explicit service_config_id")
    ensure_service_schema()
    recover_stale_brainlive_service_runs()
    with connect() as con:
        cfg = _get_config(con, service_config_id=service_config_id, person_id=person_id)
    # Runtime args override stored config for this run.
    cfg = dict(cfg)
    for key, val in {"audio_dir": audio_dir, "transcript_dir": transcript_dir, "image_dir": image_dir, "gps_state_path": gps_state_path, "location_hint": location_hint}.items():
        if val is not None:
            cfg[key] = val
    person_id = str(cfg["person_id"])
    live_session_id = _ensure_live_session(person_id, live_session_id, title, cfg.get("location_hint"))
    run_id = stable_id("blsvc_run", live_session_id, now_iso(), uuid4().hex)
    now = now_iso()
    counters: dict[str, Any] = {}
    auto_post_stop = (max_iterations == 0) if post_stop_deep_flow is None else bool(post_stop_deep_flow)
    with connect() as con:
        upsert(con, "brainlive_service_runs", {
            "service_run_id": run_id,
            "service_config_id": cfg.get("service_config_id"),
            "live_session_id": live_session_id,
            "person_id": person_id,
            "status": "running",
            "started_at": now,
            "stopped_at": None,
            "last_heartbeat_at": now,
            "iterations": 0,
            "last_error": None,
            "counters_json": json_dumps(counters),
            "process_id": os.getpid(),
            "process_host": __import__("socket").gethostname(),
            "created_at": now,
            "updated_at": now,
        }, "service_run_id")
        _state_set(con, live_session_id, "control", {"stop_requested": False, "close_day_requested": False, "started_at": now})
        con.commit()
    _write_runtime_service_manifest({
        "status": "running", "service_run_id": run_id, "live_session_id": live_session_id,
        "person_id": person_id, "started_at": now, "last_heartbeat_at": now, "auto_post_stop": auto_post_stop,
    })
    iterations = 0
    last_error: str | None = None
    try:
        while True:
            with connect() as con:
                stop_requested = _service_stop_requested(con, live_session_id, run_id)
            if stop_requested:
                drain = _drain_inbox_before_stop(run_id=run_id, live_session_id=live_session_id, cfg=cfg, counters=counters)
                if str(drain.get("status")) != "drained":
                    # The service process may end (including a user shutdown),
                    # but post-stop/cleanup must not run over an unacknowledged
                    # inbox.  RESUME will first re-enter this retained state.
                    with connect() as con:
                        _mark_run(con, run_id, status="stopped_pending_ingest", iterations=iterations, counters=counters, error=json_dumps(drain), stopped=True)
                        _state_set(con, live_session_id, "stop_drain", drain | {"at": now_iso()})
                        con.commit()
                    _write_runtime_service_manifest({"status": "stopped_pending_ingest", "service_run_id": run_id, "live_session_id": live_session_id, "person_id": person_id, "drain": drain})
                    break
                with connect() as con:
                    _mark_run(con, run_id, status="stopped", iterations=iterations, counters=counters, stopped=True)
                    _state_set(con, live_session_id, "stop_drain", drain | {"at": now_iso()})
                    con.commit()
                break
            if max_iterations and iterations >= max_iterations:
                with connect() as con:
                    _mark_run(con, run_id, status="completed", iterations=iterations, counters=counters, stopped=True)
                    con.commit()
                break
            try:
                service_iteration(run_id, live_session_id, cfg, counters)
                last_error = None
            except Exception as exc:
                last_error = str(exc)[:2000]
                counters["errors"] = counters.get("errors", 0) + 1
            iterations += 1
            with connect() as con:
                _mark_run(con, run_id, status="running", iterations=iterations, counters=counters, error=last_error)
                con.commit()
            _write_runtime_service_manifest({"status": "running", "service_run_id": run_id, "live_session_id": live_session_id, "person_id": person_id, "iterations": iterations, "last_heartbeat_at": now_iso(), "last_error": last_error})
            time.sleep(float(cfg.get("sensor_tick_s") or 1.0))
    except KeyboardInterrupt:
        # Console interruption is still a stop path, not permission to skip
        # acknowledgement of phone-delivered media.  Preserve a pending state
        # when the inbox cannot be drained; RESUME will continue safely.
        drain = _drain_inbox_before_stop(run_id=run_id, live_session_id=live_session_id, cfg=cfg, counters=counters)
        with connect() as con:
            if str(drain.get("status")) == "drained":
                _mark_run(con, run_id, status="stopped", iterations=iterations, counters=counters, error="KeyboardInterrupt after inbox drain", stopped=True)
            else:
                _mark_run(con, run_id, status="stopped_pending_ingest", iterations=iterations, counters=counters, error=json_dumps(drain), stopped=True)
            _state_set(con, live_session_id, "stop_drain", drain | {"at": now_iso(), "reason": "KeyboardInterrupt"})
            con.commit()
    post_stop_result: dict[str, Any] | None = None
    close_day_result: dict[str, Any] | None = None
    with connect() as con:
        final_service_row = _one(con, "SELECT status,last_error FROM brainlive_service_runs WHERE service_run_id=?", (run_id,)) or {}
    can_post_stop = str(final_service_row.get("status")) in {"stopped", "completed"}
    if auto_post_stop and can_post_stop:
        # The actual live loop is over.  End the live session and run the heavy
        # offline path now.  This is intentionally outside service_iteration so
        # H0/H1/H2 speed is never affected during the day.
        try:
            from .brainlive_v15 import end_live_session
            end_live_session(live_session_id, notes="BrainLive service stopped; V15.15 post-stop deep flow starting.")
        except Exception:
            pass
        try:
            from .brainlive_poststop_deep_flow_v15_15 import run_brainlive_post_stop_deep_flow
            post_stop_result = run_brainlive_post_stop_deep_flow(
                person_id=person_id,
                live_session_id=live_session_id,
                service_run_id=run_id,
                use_llm=post_stop_use_llm,
            )
            with connect() as con:
                state_status = str((post_stop_result or {}).get("status") or "unknown")
                _state_set(con, live_session_id, "post_stop_deep_flow", {"status": state_status, "result": post_stop_result, "at": now_iso()})
                con.commit()
        except Exception as exc:
            post_stop_result = {"status": "error", "error": str(exc)[:2000]}
            with connect() as con:
                _state_set(con, live_session_id, "post_stop_deep_flow", post_stop_result | {"at": now_iso()})
                con.commit()
    if auto_post_stop and not can_post_stop:
        post_stop_result = {"status": "blocked_pending_ingest", "error": "stop drain did not reach an idle inbox; raw sources retained", "cleanup": {"eligible": False}}
        with connect() as con:
            _state_set(con, live_session_id, "post_stop_deep_flow", post_stop_result | {"at": now_iso()})
            con.commit()
    # A close-day request is made through the usual stop command.  It executes
    # only here, after the service loop and its session-scoped post-stop flow
    # have completed, so day-wide Life Model promotion cannot race live input.
    if auto_post_stop and can_post_stop:
        with connect() as con:
            control = _state_get(con, live_session_id, "control")
        if bool(control.get("close_day_requested")):
            if not isinstance(post_stop_result, dict) or str(post_stop_result.get("status")) != "completed":
                close_day_result = {"status": "blocked", "error": "close-day blocked because post-stop did not complete", "cleanup": {"eligible": False}}
            else:
                try:
                    from .v18_close_day import close_brainlive_day
                    close_day_result = close_brainlive_day(
                        person_id=person_id,
                        live_session_id=live_session_id,
                        service_run_id=run_id,
                        use_llm=post_stop_use_llm,
                        post_stop_result=post_stop_result,
                    )
                except Exception as exc:
                    close_day_result = {"status": "retryable_error", "error": str(exc)[:2000], "cleanup": {"eligible": False}}
            with connect() as con:
                _state_set(con, live_session_id, "close_day", close_day_result | {"at": now_iso()})
                con.commit()
    with connect() as con:
        row = _one(con, "SELECT * FROM brainlive_service_runs WHERE service_run_id=?", (run_id,)) or {}
    _write_runtime_service_manifest({
        "status": row.get("status") or "stopped", "service_run_id": run_id, "live_session_id": live_session_id,
        "person_id": person_id, "iterations": row.get("iterations"), "last_heartbeat_at": row.get("last_heartbeat_at"), "last_error": row.get("last_error"),
        "post_stop_status": (post_stop_result or {}).get("status") if isinstance(post_stop_result, dict) else None,
        "close_day_status": (close_day_result or {}).get("status") if isinstance(close_day_result, dict) else None,
    })
    return {
        "version": VERSION,
        "service_run_id": run_id,
        "live_session_id": live_session_id,
        "status": row.get("status"),
        "iterations": row.get("iterations"),
        "counters": json_loads(row.get("counters_json"), {}),
        "post_stop_deep_flow": post_stop_result,
        "close_day": close_day_result,
    }


def stop_brainlive_service(*, live_session_id: str | None = None, service_run_id: str | None = None, close_day: bool = False) -> dict[str, Any]:
    ensure_service_schema()
    recover_stale_brainlive_service_runs()
    with connect() as con:
        if service_run_id:
            row = _one(con, "SELECT * FROM brainlive_service_runs WHERE service_run_id=?", (service_run_id,))
        elif live_session_id:
            row = _one(con, "SELECT * FROM brainlive_service_runs WHERE live_session_id=? AND status='running' ORDER BY started_at DESC LIMIT 1", (live_session_id,))
        else:
            row = _one(con, "SELECT * FROM brainlive_service_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1")
        if not row:
            return {"status": "no_running_service"}
        row["status"] = "stop_requested"
        row["updated_at"] = now_iso()
        upsert(con, "brainlive_service_runs", row, "service_run_id")
        _state_set(con, row["live_session_id"], "control", {"stop_requested": True, "close_day_requested": bool(close_day), "requested_at": now_iso()})
        if close_day:
            _state_set(con, row["live_session_id"], "close_day", {"status": "requested", "requested_at": now_iso()})
        con.commit()
    _write_runtime_service_manifest({"status": "stop_requested", "service_run_id": row["service_run_id"], "live_session_id": row["live_session_id"], "person_id": row.get("person_id"), "close_day_requested": bool(close_day)})
    return {"status": "stop_requested", "service_run_id": row["service_run_id"], "live_session_id": row["live_session_id"], "close_day_requested": bool(close_day)}


def brainlive_service_status(*, live_session_id: str | None = None, service_run_id: str | None = None) -> dict[str, Any]:
    ensure_service_schema()
    with connect() as con:
        if service_run_id:
            run = _one(con, "SELECT * FROM brainlive_service_runs WHERE service_run_id=?", (service_run_id,))
        elif live_session_id:
            run = _one(con, "SELECT * FROM brainlive_service_runs WHERE live_session_id=? ORDER BY started_at DESC LIMIT 1", (live_session_id,))
        else:
            run = _one(con, "SELECT * FROM brainlive_service_runs ORDER BY started_at DESC LIMIT 1")
        if not run:
            return {"version": VERSION, "status": "not_started"}
        counts = {}
        for table in ["brainlive_sensor_events", "brainlive_fused_situations", "brainlive_proactive_decisions", "brainlive_intervention_delivery_queue", "brainlive_service_signals"]:
            if _table_exists(con, table):
                col = "live_session_id"
                counts[table] = int((_one(con, f"SELECT COUNT(*) AS n FROM {table} WHERE {col}=?", (run["live_session_id"],)) or {"n": 0})["n"])
        deliveries = []
        if _table_exists(con, "brainlive_intervention_delivery_queue"):
            deliveries = _many(con, "SELECT delivery_id,horizon,message,priority,delivery_status,created_at FROM brainlive_intervention_delivery_queue WHERE live_session_id=? ORDER BY priority DESC, created_at DESC LIMIT 10", (run["live_session_id"],))
        latest_fused = None
        if _table_exists(con, "brainlive_fused_situations"):
            latest_fused = _one(con, "SELECT fused_id, confidence_json, readiness_json, created_at FROM brainlive_fused_situations WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (run["live_session_id"],))
            if latest_fused:
                latest_fused["confidence"] = json_loads(latest_fused.pop("confidence_json", "{}"), {})
                latest_fused["readiness"] = json_loads(latest_fused.pop("readiness_json", "{}"), {})
        control = _state_get(con, str(run["live_session_id"]), "control")
        close_day = _state_get(con, str(run["live_session_id"]), "close_day")
    return {
        "version": VERSION,
        "service_run_id": run["service_run_id"],
        "live_session_id": run["live_session_id"],
        "status": run["status"],
        "iterations": run["iterations"],
        "last_heartbeat_at": run["last_heartbeat_at"],
        "last_error": run.get("last_error"),
        "counters": json_loads(run.get("counters_json"), {}),
        "counts": counts,
        "latest_fused": latest_fused,
        "queued_interventions": deliveries,
        "control": control,
        "close_day": close_day,
        "contract": "start/stop/status service; Brain2 deep truth; BrainLive live H0/H1/H2; close-day is gated",
    }


def service_audit() -> dict[str, Any]:
    ensure_service_schema()
    settings = get_settings()
    deps = {}
    for mod in ["silero_vad", "whisperx", "faster_whisper", "speechbrain"]:
        try:
            __import__(mod)
            deps[mod] = "available"
        except Exception:
            deps[mod] = "missing"
    llm_status = "unknown"
    try:
        # Only instantiate same local client Brain2 uses; no prompt call here.
        OllamaJsonClient()
        llm_status = "configured"
    except Exception as exc:
        llm_status = "error:" + str(exc)[:120]
    with connect() as con:
        counts = {}
        for t in ["brainlive_service_configs", "brainlive_service_runs", "brainlive_service_signals"]:
            counts[t] = int((_one(con, f"SELECT COUNT(*) AS n FROM {t}") or {"n": 0})["n"])
    return {"version": VERSION, "status": "ok", "deps": deps, "llm_client": llm_status, "ollama_model": settings.ollama_model, "vlm_model_env": os.environ.get("MLOMEGA_VLM_MODEL"), "counts": counts}
