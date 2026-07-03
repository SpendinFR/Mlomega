from __future__ import annotations

"""V18.8 adaptive live policy.

This module centralizes three durable policies that must survive service restarts:

* semantic dispatch debouncing for the live LLM path;
* audio-priority image work scheduling (capture is accepted immediately, VLM work
  is delayed while speech/transcripts are waiting);
* intervention delivery/feedback/outcome lineage for Brain2 reconciliation.

It deliberately uses no keyword psychology.  Equality/digests are used only to
avoid re-running models on identical sensor state; semantic activity changes are
provided by the VLM and later reconciled by Brain2.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .db import connect, upsert, write_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id

VERSION = "18.8.1-adaptive-live-policy"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_live_dispatch_state_v188(
  live_session_id TEXT PRIMARY KEY,
  last_dispatch_epoch REAL DEFAULT 0,
  last_dispatch_at TEXT,
  last_gps_digest TEXT,
  last_vision_digest TEXT,
  pending_signals_json TEXT DEFAULT '[]',
  first_pending_epoch REAL DEFAULT 0,
  last_reason_json TEXT DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_image_work_queue_v188(
  image_work_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_sha256 TEXT,
  source_occurred_at TEXT NOT NULL,
  source_device TEXT,
  descriptor_json TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at_epoch REAL DEFAULT 0,
  lease_token TEXT,
  leased_until_epoch REAL DEFAULT 0,
  result_json TEXT DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(live_session_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_bl_image_work_due_v188
  ON brainlive_image_work_queue_v188(live_session_id,status,available_at_epoch,created_at);
CREATE TABLE IF NOT EXISTS brainlive_live_visual_state_v188(
  live_session_id TEXT PRIMARY KEY,
  last_seen_signature TEXT,
  last_seen_signature_kind TEXT,
  last_seen_at_epoch REAL DEFAULT 0,
  last_analyzed_signature TEXT,
  last_analyzed_signature_kind TEXT,
  last_vlm_started_epoch REAL DEFAULT 0,
  last_vlm_completed_epoch REAL DEFAULT 0,
  last_vlm_status TEXT,
  last_decision_json TEXT DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_intervention_feedback_events_v188(
  feedback_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL,
  candidate_id TEXT,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  feedback_type TEXT NOT NULL,
  feedback_source TEXT NOT NULL,
  note TEXT,
  evidence_json TEXT DEFAULT '{}',
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bl_delivery_feedback_v188
  ON brainlive_intervention_feedback_events_v188(delivery_id,observed_at);
CREATE TABLE IF NOT EXISTS brainlive_intervention_outcomes_v188(
  intervention_outcome_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL,
  candidate_id TEXT,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  outcome_status TEXT NOT NULL,
  did_help INTEGER,
  observed_later_summary TEXT,
  evidence_json TEXT DEFAULT '{}',
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(delivery_id, outcome_status)
);
CREATE INDEX IF NOT EXISTS idx_bl_intervention_outcome_v188
  ON brainlive_intervention_outcomes_v188(person_id,live_session_id,observed_at);
"""


def _columns(con: Any, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_v18_8_live_policy_schema() -> None:
    """Install additive V18.8 schema safely on a V18.7 database."""
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)
        # Additive state fields: a silence is meaningful only on a transition
        # from observed speech to observed silence, never for every empty chunk.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brainlive_live_dispatch_state_v188'").fetchone():
            existing_state = _columns(con, "brainlive_live_dispatch_state_v188")
            for name, ddl in {
                "last_audio_content": "INTEGER NOT NULL DEFAULT 0",
                "last_silence_boundary_epoch": "REAL DEFAULT 0",
            }.items():
                if name not in existing_state:
                    con.execute(f"ALTER TABLE brainlive_live_dispatch_state_v188 ADD COLUMN {name} {ddl}")
        # The existing delivery queue is public/Bridge-facing.  Add only
        # additive, nullable columns so old rows and clients remain valid.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brainlive_intervention_delivery_queue'").fetchone():
            existing = _columns(con, "brainlive_intervention_delivery_queue")
            for name, ddl in {
                "displayed_at": "TEXT",
                "seen_at": "TEXT",
                "feedback_at": "TEXT",
                "feedback_type": "TEXT",
                "feedback_note": "TEXT",
                "updated_at": "TEXT",
            }.items():
                if name not in existing:
                    con.execute(f"ALTER TABLE brainlive_intervention_delivery_queue ADD COLUMN {name} {ddl}")


def _float_env(name: str, default: float, *, minimum: float = 0.0, maximum: float = 86400.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _compact_values(value: Any) -> Any:
    """Canonicalize sensor data for equality/deduplication, not interpretation."""
    if isinstance(value, Mapping):
        return {str(k): _compact_values(v) for k, v in sorted(value.items(), key=lambda item: str(item[0])) if str(k) not in {"captured_at", "received_at", "updated_at", "timestamp", "timestamp_start", "timestamp_end"}}
    if isinstance(value, list):
        return [_compact_values(v) for v in value]
    if isinstance(value, float):
        return round(value, 5)
    return value


def gps_digest(gps: Mapping[str, Any] | None) -> str | None:
    if not isinstance(gps, Mapping) or not gps:
        return None
    # Precision is intentionally reduced for movement detection.  The full
    # coordinates remain in the original GPS evidence; this digest only avoids
    # treating unchanged current.json as a new semantic signal.
    payload: dict[str, Any] = {}
    for key in ("label", "place", "place_name", "name", "address", "category", "confidence"):
        if gps.get(key) not in (None, ""):
            payload[key] = gps.get(key)
    for key in ("lat", "lon"):
        if gps.get(key) is not None:
            try:
                payload[key] = round(float(gps[key]), 4)  # roughly 11m; enough for meaningful movement
            except (TypeError, ValueError):
                payload[key] = gps[key]
    return stable_id("v188gps", _compact_values(payload)) if payload else None


def vision_activity_digest(observation: Mapping[str, Any] | None) -> str | None:
    """Build a VLM-provided activity signature without text heuristics."""
    if not isinstance(observation, Mapping) or not observation:
        return None
    activities = observation.get("possible_user_activities") or observation.get("activities") or []
    activity_payload: list[Any] = []
    if isinstance(activities, list):
        for item in activities[:5]:
            if isinstance(item, Mapping):
                activity_payload.append({
                    "activity": item.get("activity") or item.get("label") or item.get("name"),
                    "confidence": round(float(item.get("confidence") or 0.0), 2),
                })
            elif item:
                activity_payload.append(str(item))
    # Scene/objects are not used to invent an activity, only to distinguish
    # two VLM observations when the VLM did not provide an activity list.
    payload = {
        "activities": activity_payload,
        "scene_summary": observation.get("scene_summary") or observation.get("summary"),
        "objects": observation.get("objects") or [],
        "people": observation.get("people") or [],
        "location_hint": observation.get("location_hint") or {},
    }
    if not any(payload.values()):
        return None
    return stable_id("v188vision", _compact_values(payload))


def _dispatch_state(con: Any, live_session_id: str) -> dict[str, Any]:
    row = con.execute("SELECT * FROM brainlive_live_dispatch_state_v188 WHERE live_session_id=?", (live_session_id,)).fetchone()
    if row:
        return dict(row)
    now = now_iso()
    out = {
        "live_session_id": live_session_id,
        "last_dispatch_epoch": 0.0,
        "last_dispatch_at": None,
        "last_gps_digest": None,
        "last_vision_digest": None,
        "pending_signals_json": "[]",
        "first_pending_epoch": 0.0,
        "last_reason_json": "{}",
        "last_audio_content": 0,
        "last_silence_boundary_epoch": 0.0,
        "updated_at": now,
    }
    upsert(con, "brainlive_live_dispatch_state_v188", out, "live_session_id")
    return out


def plan_live_dispatch(
    *,
    live_session_id: str,
    audio_content: bool = False,
    image_observation: Mapping[str, Any] | None = None,
    gps: Mapping[str, Any] | None = None,
    cadence_due: bool = False,
    silence_boundary: bool = False,
    audio_observed: bool | None = None,
) -> dict[str, Any]:
    """Return a durable LLM dispatch plan.

    New audio is accumulated into one context window.  New images/gps become
    signals only when their VLM/GPS signatures changed.  A context-only cadence
    is kept separate from LLM dispatch, so a parked GPS JSON cannot create calls
    forever.
    """
    ensure_v18_8_live_policy_schema()
    # Backward-compatible direct callers that predate the explicit flag still
    # describe an observed audio result whenever they pass audio/silence data.
    if audio_observed is None:
        audio_observed = bool(audio_content or silence_boundary)
    now_epoch = time.time()
    current_gps = gps_digest(gps)
    current_vision = vision_activity_digest(image_observation)
    min_interval = _float_env("MLOMEGA_BRAINLIVE_LLM_MIN_INTERVAL_S", 12.0, minimum=1.0)
    audio_window = _float_env("MLOMEGA_BRAINLIVE_LLM_AUDIO_WINDOW_S", 45.0, minimum=min_interval)
    max_window = _float_env("MLOMEGA_BRAINLIVE_LLM_MAX_WINDOW_S", 90.0, minimum=audio_window)

    with connect() as con, write_transaction(con):
        state = _dispatch_state(con, live_session_id)
        pending = json_loads(state.get("pending_signals_json"), [])
        if not isinstance(pending, list):
            pending = []
        new_signals: list[str] = []
        gps_changed = bool(current_gps and current_gps != state.get("last_gps_digest"))
        vision_changed = bool(current_vision and current_vision != state.get("last_vision_digest"))
        previous_audio_content = bool(state.get("last_audio_content"))
        # An empty chunk is not a new semantic event by itself.  It only closes
        # a window when it follows observed speech.  This avoids Qwen calls every
        # N seconds during a long quiet period while preserving the useful
        # speech→silence boundary after a sentence or conversation turn.
        silence_transition = bool(silence_boundary and (previous_audio_content or audio_content))
        if audio_content:
            new_signals.append("audio_window")
        if silence_transition:
            new_signals.append("silence_boundary")
        if gps_changed:
            new_signals.append("gps_change")
        if vision_changed:
            new_signals.append("vision_activity_change")
        for signal in new_signals:
            if signal not in pending:
                pending.append(signal)
        first_pending = float(state.get("first_pending_epoch") or 0.0)
        if pending and not first_pending:
            first_pending = now_epoch
        elapsed = now_epoch - float(state.get("last_dispatch_epoch") or 0.0)
        pending_age = now_epoch - first_pending if first_pending else 0.0
        semantic_changed = bool(gps_changed or vision_changed)
        audio_due = "audio_window" in pending and pending_age >= audio_window
        silence_due = "silence_boundary" in pending and elapsed >= min_interval
        semantic_due = semantic_changed and elapsed >= min_interval
        hard_due = pending_age >= max_window
        should_dispatch = bool(pending) and (
            float(state.get("last_dispatch_epoch") or 0.0) == 0.0
            or semantic_due
            or silence_due
            or audio_due
            or hard_due
        )
        reason = {
            "new_signals": new_signals,
            "pending_signals": pending,
            "gps_changed": gps_changed,
            "vision_changed": vision_changed,
            "cadence_due": bool(cadence_due),
            "silence_boundary": bool(silence_boundary),
            "silence_transition": silence_transition,
            "previous_audio_content": previous_audio_content,
            "audio_observed": bool(audio_observed),
            "last_dispatch_elapsed_s": round(elapsed, 3),
            "pending_age_s": round(pending_age, 3),
            "min_interval_s": min_interval,
            "audio_window_s": audio_window,
            "max_window_s": max_window,
        }
        updated = {
            **state,
            "last_gps_digest": current_gps or state.get("last_gps_digest"),
            "last_vision_digest": current_vision or state.get("last_vision_digest"),
            # Preserve speech state across service iterations that did not
            # inspect an audio input.  A processed silent chunk clears it.
            "last_audio_content": (
                0 if (audio_observed and silence_boundary)
                else (1 if (audio_observed and audio_content) else state.get("last_audio_content") or 0)
            ),
            "last_silence_boundary_epoch": now_epoch if silence_transition else state.get("last_silence_boundary_epoch") or 0.0,
            "pending_signals_json": json_dumps(pending),
            "first_pending_epoch": first_pending,
            "last_reason_json": json_dumps(reason),
            "updated_at": now_iso(),
        }
        upsert(con, "brainlive_live_dispatch_state_v188", updated, "live_session_id")
    return {
        "should_dispatch_llm": should_dispatch,
        "should_refresh_context_only": bool(cadence_due and not should_dispatch),
        "reason": reason,
        "force_context": bool(gps_changed or vision_changed),
    }


def mark_live_dispatch(*, live_session_id: str, plan: Mapping[str, Any], status: str) -> None:
    """Commit or retain the pending live context after a dispatch attempt."""
    ensure_v18_8_live_policy_schema()
    success = status in {"ok", "completed", "needs_evidence", "queued"}
    with connect() as con, write_transaction(con):
        state = _dispatch_state(con, live_session_id)
        pending = [] if success else json_loads(state.get("pending_signals_json"), [])
        if not isinstance(pending, list):
            pending = []
        now_epoch = time.time()
        row = {
            **state,
            "last_dispatch_epoch": now_epoch if success else state.get("last_dispatch_epoch") or 0.0,
            "last_dispatch_at": now_iso() if success else state.get("last_dispatch_at"),
            "pending_signals_json": json_dumps(pending),
            "first_pending_epoch": 0.0 if success else float(state.get("first_pending_epoch") or now_epoch),
            "last_reason_json": json_dumps({**dict(plan.get("reason") or {}), "dispatch_status": status}),
            "updated_at": now_iso(),
        }
        upsert(con, "brainlive_live_dispatch_state_v188", row, "live_session_id")



def _image_signature(path: str | Path) -> tuple[str, str]:
    """Cheap, deterministic visual-change signature.

    This is intentionally *not* a semantic model. It only filters duplicate or
    near-duplicate frames before a live VLM call. Semantic activity, people,
    games, text and scene changes remain VLM/Brain2 responsibilities.
    """
    p = Path(path).expanduser().resolve()
    try:
        from PIL import Image  # type: ignore
        with Image.open(p) as image:
            image = image.convert("L").resize((9, 8))
            pixels = list(image.getdata())
        bits: list[str] = []
        for row in range(8):
            offset = row * 9
            for col in range(8):
                bits.append("1" if int(pixels[offset + col]) > int(pixels[offset + col + 1]) else "0")
        value = int("".join(bits), 2)
        return "dhash64", f"{value:016x}"
    except Exception:
        # Exact SHA fallback is still safe: it only reduces duplicate model
        # work when Pillow/decoding is unavailable; it never discards a frame.
        try:
            return "sha256", hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception:
            return "path", stable_id("v188imagepath", str(p))


def _signature_distance(kind_a: str | None, value_a: str | None, kind_b: str | None, value_b: str | None) -> int | None:
    if not kind_a or not value_a or not kind_b or not value_b or kind_a != kind_b:
        return None
    if kind_a == "dhash64":
        try:
            return (int(value_a, 16) ^ int(value_b, 16)).bit_count()
        except Exception:
            return None
    if kind_a == "sha256":
        return 0 if value_a == value_b else 256
    return 0 if value_a == value_b else 1


def _visual_state(con: Any, live_session_id: str) -> dict[str, Any]:
    row = con.execute("SELECT * FROM brainlive_live_visual_state_v188 WHERE live_session_id=?", (live_session_id,)).fetchone()
    if row:
        return dict(row)
    state = {
        "live_session_id": live_session_id,
        "last_seen_signature": None,
        "last_seen_signature_kind": None,
        "last_seen_at_epoch": 0.0,
        "last_analyzed_signature": None,
        "last_analyzed_signature_kind": None,
        "last_vlm_started_epoch": 0.0,
        "last_vlm_completed_epoch": 0.0,
        "last_vlm_status": None,
        "last_decision_json": "{}",
        "updated_at": now_iso(),
    }
    upsert(con, "brainlive_live_visual_state_v188", state, "live_session_id")
    return state


def _image_float_env(name: str, default: float, *, minimum: float = 0.0, maximum: float = 86400.0) -> float:
    return _float_env(name, default, minimum=minimum, maximum=maximum)


def plan_image_capture(*, live_session_id: str, path: str | Path, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """Decide whether an accepted frame needs *live* VLM analysis.

    Every frame remains captured and eligible for later deep-vision selection.
    Only the expensive live VLM projection is skipped for near-identical images.
    A periodic refresh bounds the chance of overlooking a subtle gradual change.
    """
    ensure_v18_8_live_policy_schema()
    kind, signature = _image_signature(path)
    now_epoch = time.time()
    distance_limit = int(_image_float_env("MLOMEGA_BRAINLIVE_IMAGE_DHASH_CHANGE_BITS", 8.0, minimum=0, maximum=64))
    refresh_s = _image_float_env("MLOMEGA_BRAINLIVE_IMAGE_LIVE_REFRESH_S", 600.0, minimum=10.0)
    with connect() as con, write_transaction(con):
        state = _visual_state(con, live_session_id)
        # Before the first queued VLM job completes, compare against the last
        # *seen* frame.  Otherwise every identical frame arriving during one
        # long conversation would look like a new “first” frame and create a
        # burst of redundant VLM jobs.  Once a VLM result exists, it remains the
        # semantic refresh anchor and the last-seen state is only a capture
        # dedupe guard.
        analyzed_kind = state.get("last_analyzed_signature_kind")
        analyzed_signature = state.get("last_analyzed_signature")
        last_kind = analyzed_kind or state.get("last_seen_signature_kind")
        last_signature = analyzed_signature or state.get("last_seen_signature")
        comparison_anchor = "last_analyzed" if analyzed_signature else ("last_seen" if last_signature else "none")
        distance = _signature_distance(kind, signature, last_kind, last_signature)
        elapsed = now_epoch - float(state.get("last_vlm_completed_epoch") or 0.0)
        first = not bool(last_signature)
        changed = (not first) and (distance is None or (distance > distance_limit))
        refresh_due = bool(analyzed_signature) and elapsed >= refresh_s
        analyze = bool(first or changed or refresh_due)
        if first:
            reason = "first_live_visual"
        elif changed:
            reason = "visual_delta"
        elif refresh_due:
            reason = "periodic_visual_refresh"
        else:
            reason = "near_duplicate_live_vlm_skipped"
        updated = {
            **state,
            "last_seen_signature": signature,
            "last_seen_signature_kind": kind,
            "last_seen_at_epoch": now_epoch,
            "last_decision_json": json_dumps({
                "analyze_live_vlm": analyze,
                "reason": reason,
                "signature_kind": kind,
                "distance_from_last_analyzed": distance,
                "distance_limit": distance_limit,
                "seconds_since_last_live_vlm": round(elapsed, 3),
                "source_event_id": descriptor.get("event_id") or descriptor.get("source_event_id"),
                "comparison_anchor": comparison_anchor,
            }),
            "updated_at": now_iso(),
        }
        upsert(con, "brainlive_live_visual_state_v188", updated, "live_session_id")
    return {
        "analyze_live_vlm": analyze,
        "reason": reason,
        "signature": signature,
        "signature_kind": kind,
        "distance_from_last_analyzed": distance,
        "comparison_anchor": comparison_anchor,
        "refresh_s": refresh_s,
    }


def annotate_captured_frame(*, frame_id: str | None, policy: Mapping[str, Any]) -> None:
    """Attach cheap capture-side visual evidence to the immutable frame row.

    The raw image remains authoritative.  This metadata is only a lightweight
    dHash/signature used by the assembler when live VLM was deliberately skipped.
    It lets post-stop split activities on visual change without pretending that a
    pixel hash is a semantic interpretation.
    """
    if not frame_id:
        return
    ensure_v18_8_live_policy_schema()
    with connect() as con, write_transaction(con):
        row = con.execute("SELECT metadata_json FROM vision_frames WHERE frame_id=?", (str(frame_id),)).fetchone()
        if not row:
            return
        metadata = json_loads(row["metadata_json"], {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update({
            "v188_image_signature": policy.get("signature"),
            "v188_image_signature_kind": policy.get("signature_kind"),
            "v188_live_vlm_policy": policy.get("reason"),
            "v188_capture_policy_version": "18.8.1",
        })
        con.execute(
            "UPDATE vision_frames SET metadata_json=? WHERE frame_id=?",
            (json_dumps(metadata), str(frame_id)),
        )


def plan_image_worker_dispatch(*, live_session_id: str, audio_pending: int, silence_seen: bool) -> dict[str, Any]:
    """Fair audio-first VLM scheduler.

    Audio is handled first in every service iteration. A frame gets a fast slot
    after a verified silence, when audio is idle, or after a bounded wait during
    continuous speech. This prevents both audio starvation and image starvation.
    """
    ensure_v18_8_live_policy_schema()
    now_epoch = time.time()
    min_vlm_interval = _image_float_env("MLOMEGA_BRAINLIVE_IMAGE_MIN_VLM_INTERVAL_S", 20.0, minimum=1.0)
    force_after = _image_float_env("MLOMEGA_BRAINLIVE_IMAGE_FORCE_AFTER_S", 90.0, minimum=min_vlm_interval)
    with connect() as con:
        state = _visual_state(con, live_session_id)
        row = con.execute(
            """SELECT * FROM brainlive_image_work_queue_v188
                 WHERE live_session_id=? AND status IN ('queued','retryable_error')
                   AND available_at_epoch<=?
                 ORDER BY created_at,image_work_id LIMIT 1""",
            (live_session_id, now_epoch),
        ).fetchone()
    if not row:
        return {"run": False, "reason": "no_due_image_work"}
    work = dict(row)
    try:
        queued_at = datetime_from_iso(str(work.get("created_at") or ""))
        age = max(0.0, now_epoch - queued_at)
    except Exception:
        age = force_after
    since_last = now_epoch - float(state.get("last_vlm_started_epoch") or 0.0)
    if since_last < min_vlm_interval:
        return {"run": False, "reason": "live_vlm_rate_limited", "image_work_id": work.get("image_work_id"), "seconds_until": round(min_vlm_interval - since_last, 3)}
    if audio_pending <= 0:
        return {"run": True, "reason": "audio_idle", "image_work_id": work.get("image_work_id"), "age_s": round(age, 3)}
    if silence_seen:
        return {"run": True, "reason": "silence_slot", "image_work_id": work.get("image_work_id"), "age_s": round(age, 3)}
    if age >= force_after:
        return {"run": True, "reason": "fair_share_max_wait", "image_work_id": work.get("image_work_id"), "age_s": round(age, 3)}
    return {"run": False, "reason": "audio_priority_wait", "image_work_id": work.get("image_work_id"), "age_s": round(age, 3), "force_after_s": force_after}


def datetime_from_iso(value: str) -> float:
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()

def enqueue_image_work(*, live_session_id: str, person_id: str, descriptor: Mapping[str, Any], path: str | Path) -> dict[str, Any]:
    """Persist an image VLM job after its immutable capture event is accepted."""
    ensure_v18_8_live_policy_schema()
    p = Path(path).expanduser().resolve()
    source_event_id = str(descriptor.get("event_id") or descriptor.get("source_event_id") or "").strip()
    if not source_event_id:
        raise ValueError("image work requires source event identity")
    work_id = stable_id("v188imagework", live_session_id, source_event_id)
    now = now_iso()
    with connect() as con, write_transaction(con):
        existing = con.execute("SELECT * FROM brainlive_image_work_queue_v188 WHERE live_session_id=? AND source_event_id=?", (live_session_id, source_event_id)).fetchone()
        if existing:
            return {"status": "deduplicated", "image_work_id": str(existing["image_work_id"])}
        try:
            max_pending = max(1, min(500, int(os.environ.get("MLOMEGA_BRAINLIVE_IMAGE_MAX_PENDING", "48"))))
        except (TypeError, ValueError):
            max_pending = 48
        queued_count = int(con.execute(
            "SELECT COUNT(*) AS n FROM brainlive_image_work_queue_v188 WHERE live_session_id=? AND status IN ('queued','running','retryable_error')",
            (live_session_id,),
        ).fetchone()["n"])
        # A visually dynamic game or video can produce a new dHash on every
        # capture.  The raw frames remain durable for deep vision, but the live
        # VLM needs only a short, current work set.  Coalesce the oldest queued
        # (never leased/running) job when that set is full, so a new person,
        # screen, activity or decor still reaches live context instead of being
        # stranded behind stale gameplay frames.
        try:
            queue_target = max(1, min(max_pending, int(os.environ.get("MLOMEGA_BRAINLIVE_IMAGE_QUEUE_TARGET", "4"))))
        except (TypeError, ValueError):
            queue_target = min(max_pending, 4)
        queued_only = con.execute(
            "SELECT image_work_id FROM brainlive_image_work_queue_v188 WHERE live_session_id=? AND status IN ('queued','retryable_error') ORDER BY created_at,image_work_id",
            (live_session_id,),
        ).fetchall()
        coalesced_id = None
        if len(queued_only) >= queue_target:
            coalesced_id = str(queued_only[0]["image_work_id"])
            con.execute(
                """UPDATE brainlive_image_work_queue_v188
                   SET status='superseded_live_only', lease_token=NULL, leased_until_epoch=0,
                       error_text='superseded by newer visual change; raw frame retained for deep vision',
                       updated_at=?
                 WHERE image_work_id=? AND status IN ('queued','retryable_error')""",
                (now, coalesced_id),
            )
            queued_count -= 1
        if queued_count >= max_pending:
            # The raw frame is already durable in vision_frames.  Let the
            # post-stop keyframe selector see it, but do not let a continuous
            # stream of visual changes consume unbounded live GPU work.
            return {"status": "deferred_deep_only", "reason": "live_vlm_queue_full", "pending": queued_count, "max_pending": max_pending}
        upsert(con, "brainlive_image_work_queue_v188", {
            "image_work_id": work_id,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "source_event_id": source_event_id,
            "source_path": str(p),
            "source_sha256": descriptor.get("source_sha256"),
            "source_occurred_at": descriptor.get("occurred_at"),
            "source_device": descriptor.get("source_device"),
            "descriptor_json": json_dumps(dict(descriptor)),
            "status": "queued",
            "attempts": 0,
            "available_at_epoch": 0.0,
            "lease_token": None,
            "leased_until_epoch": 0.0,
            "result_json": "{}",
            "error_text": None,
            "created_at": now,
            "updated_at": now,
        }, "image_work_id")
    return {"status": "queued_coalesced_latest" if coalesced_id else "queued", "image_work_id": work_id, "superseded_image_work_id": coalesced_id}


def claim_due_image_work(*, live_session_id: str, lease_seconds: float = 120.0) -> dict[str, Any] | None:
    """Claim one image job only when the audio-priority service allows it."""
    ensure_v18_8_live_policy_schema()
    now_epoch = time.time()
    token = uuid4().hex
    with connect() as con, write_transaction(con):
        # A process crash leaves a short lease; it becomes retryable, not lost.
        con.execute(
            """UPDATE brainlive_image_work_queue_v188
               SET status='retryable_error', lease_token=NULL, leased_until_epoch=0,
                   error_text=COALESCE(error_text,'image worker lease expired'), updated_at=?
             WHERE live_session_id=? AND status='running' AND leased_until_epoch<?""",
            (now_iso(), live_session_id, now_epoch),
        )
        row = con.execute(
            """SELECT * FROM brainlive_image_work_queue_v188
                 WHERE live_session_id=? AND status IN ('queued','retryable_error')
                   AND available_at_epoch<=?
                 ORDER BY created_at,image_work_id LIMIT 1""",
            (live_session_id, now_epoch),
        ).fetchone()
        if not row:
            return None
        work = dict(row)
        con.execute(
            """UPDATE brainlive_image_work_queue_v188
               SET status='running', attempts=attempts+1, lease_token=?, leased_until_epoch=?, updated_at=?
             WHERE image_work_id=? AND status IN ('queued','retryable_error')""",
            (token, now_epoch + max(5.0, lease_seconds), now_iso(), work["image_work_id"]),
        )
        changed = con.execute("SELECT * FROM brainlive_image_work_queue_v188 WHERE image_work_id=?", (work["image_work_id"],)).fetchone()
        if not changed or str(changed["lease_token"] or "") != token:
            return None
        out = dict(changed)
        out["lease_token"] = token
        state = _visual_state(con, live_session_id)
        state.update({
            "last_vlm_started_epoch": now_epoch,
            "last_decision_json": json_dumps({"image_work_id": out.get("image_work_id"), "reason": "live_vlm_claimed"}),
            "updated_at": now_iso(),
        })
        upsert(con, "brainlive_live_visual_state_v188", state, "live_session_id")
        return out


def finish_image_work(*, image_work_id: str, lease_token: str, status: str, result: Mapping[str, Any], error_text: str | None = None) -> None:
    ensure_v18_8_live_policy_schema()
    terminal = status in {"completed", "captured_no_vlm", "blocked"}
    delay = _float_env("MLOMEGA_BRAINLIVE_IMAGE_RETRY_DELAY_S", 20.0, minimum=1.0)
    with connect() as con, write_transaction(con):
        row = con.execute("SELECT * FROM brainlive_image_work_queue_v188 WHERE image_work_id=?", (image_work_id,)).fetchone()
        if not row or str(row["lease_token"] or "") != lease_token:
            return
        work = dict(row)
        next_status = "completed" if terminal else "retryable_error"
        con.execute(
            """UPDATE brainlive_image_work_queue_v188
               SET status=?, available_at_epoch=?, lease_token=NULL, leased_until_epoch=0,
                   result_json=?, error_text=?, updated_at=? WHERE image_work_id=?""",
            (
                next_status,
                0.0 if terminal else time.time() + delay,
                json_dumps(dict(result)),
                (error_text or "")[:2000] or None,
                now_iso(),
                image_work_id,
            ),
        )
        state = _visual_state(con, str(work["live_session_id"]))
        result_dict = dict(result)
        signature = result_dict.get("image_signature") or json_loads(work.get("descriptor_json"), {}).get("v188_image_signature")
        signature_kind = result_dict.get("image_signature_kind") or json_loads(work.get("descriptor_json"), {}).get("v188_image_signature_kind")
        state.update({
            "last_vlm_completed_epoch": time.time() if terminal else float(state.get("last_vlm_completed_epoch") or 0.0),
            "last_vlm_status": str(status),
            "last_analyzed_signature": signature if terminal and signature else state.get("last_analyzed_signature"),
            "last_analyzed_signature_kind": signature_kind if terminal and signature_kind else state.get("last_analyzed_signature_kind"),
            "last_decision_json": json_dumps({"image_work_id": image_work_id, "reason": "live_vlm_finished", "status": status}),
            "updated_at": now_iso(),
        })
        upsert(con, "brainlive_live_visual_state_v188", state, "live_session_id")

def pending_image_count(*, live_session_id: str) -> int:
    ensure_v18_8_live_policy_schema()
    with connect() as con:
        row = con.execute("SELECT COUNT(*) AS n FROM brainlive_image_work_queue_v188 WHERE live_session_id=? AND status IN ('queued','running','retryable_error')", (live_session_id,)).fetchone()
        return int(row["n"] if row else 0)


_FEEDBACK_STATES = {"delivered", "displayed", "seen", "acted", "dismissed", "ignored", "failed"}


def _delivery_row(con: Any, delivery_id: str) -> dict[str, Any]:
    row = con.execute("SELECT * FROM brainlive_intervention_delivery_queue WHERE delivery_id=?", (delivery_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown delivery_id: {delivery_id}")
    return dict(row)


def record_delivery_feedback(*, delivery_id: str, feedback_type: str, feedback_source: str, note: str | None = None, evidence: Mapping[str, Any] | None = None, observed_at: str | None = None) -> dict[str, Any]:
    """Record the actual delivery lifecycle without inventing causality."""
    ensure_v18_8_live_policy_schema()
    feedback_type = str(feedback_type or "").strip().lower()
    if feedback_type not in _FEEDBACK_STATES:
        raise ValueError(f"feedback_type must be one of {sorted(_FEEDBACK_STATES)}")
    now = observed_at or now_iso()
    with connect() as con, write_transaction(con):
        delivery = _delivery_row(con, delivery_id)
        feedback_id = stable_id("v188feedback", delivery_id, feedback_type, feedback_source, now, uuid4().hex)
        candidate_id = delivery.get("candidate_id")
        person_row = con.execute("SELECT person_id FROM brainlive_sessions WHERE live_session_id=?", (delivery["live_session_id"],)).fetchone()
        if not person_row:
            raise ValueError("delivery references missing live session")
        person_id = str(person_row["person_id"])
        upsert(con, "brainlive_intervention_feedback_events_v188", {
            "feedback_id": feedback_id,
            "delivery_id": delivery_id,
            "candidate_id": candidate_id,
            "live_session_id": delivery["live_session_id"],
            "person_id": person_id,
            "feedback_type": feedback_type,
            "feedback_source": str(feedback_source or "unknown"),
            "note": (note or "")[:4000] or None,
            "evidence_json": json_dumps(dict(evidence or {})),
            "observed_at": now,
            "created_at": now_iso(),
        }, "feedback_id")
        updates = {
            "delivery_status": feedback_type,
            "feedback_at": now,
            "feedback_type": feedback_type,
            "feedback_note": (note or "")[:4000] or None,
            "updated_at": now_iso(),
        }
        if feedback_type in {"delivered", "displayed"}:
            updates["delivered_at"] = delivery.get("delivered_at") or now
        if feedback_type in {"displayed", "seen", "acted", "dismissed", "ignored"}:
            updates["displayed_at"] = delivery.get("displayed_at") or now
        if feedback_type in {"seen", "acted", "dismissed", "ignored"}:
            updates["seen_at"] = delivery.get("seen_at") or now
        merged = {**delivery, **updates}
        upsert(con, "brainlive_intervention_delivery_queue", merged, "delivery_id")
    return {"feedback_id": feedback_id, "delivery_id": delivery_id, "feedback_type": feedback_type}


def materialize_intervention_outcome_observation(*, delivery_id: str, outcome_status: str, observed_later_summary: str | None = None, did_help: bool | None = None, evidence: Mapping[str, Any] | None = None, observed_at: str | None = None) -> dict[str, Any]:
    """Attach explicit feedback or later evidence to one delivery.

    This records a *traceable observation*, not an unsupported causal verdict.
    Brain2 receives the link plus source evidence and can reconcile it at
    post-stop. ``did_help`` stays NULL unless the user explicitly supplied it.
    """
    ensure_v18_8_live_policy_schema()
    allowed = {"observation_pending", "feedback_explicit", "reconciled_helped", "reconciled_not_helped", "unresolved"}
    if outcome_status not in allowed:
        raise ValueError(f"outcome_status must be one of {sorted(allowed)}")
    now = observed_at or now_iso()
    with connect() as con, write_transaction(con):
        delivery = _delivery_row(con, delivery_id)
        person_row = con.execute("SELECT person_id FROM brainlive_sessions WHERE live_session_id=?", (delivery["live_session_id"],)).fetchone()
        if not person_row:
            raise ValueError("delivery references missing live session")
        outcome_id = stable_id("v188outcome", delivery_id, outcome_status)
        upsert(con, "brainlive_intervention_outcomes_v188", {
            "intervention_outcome_id": outcome_id,
            "delivery_id": delivery_id,
            "candidate_id": delivery.get("candidate_id"),
            "live_session_id": delivery["live_session_id"],
            "person_id": str(person_row["person_id"]),
            "outcome_status": outcome_status,
            "did_help": None if did_help is None else (1 if did_help else 0),
            "observed_later_summary": (observed_later_summary or "")[:8000] or None,
            "evidence_json": json_dumps(dict(evidence or {})),
            "observed_at": now,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }, "intervention_outcome_id")
    return {"intervention_outcome_id": outcome_id, "delivery_id": delivery_id, "outcome_status": outcome_status}


def materialize_open_delivery_observations(*, live_session_id: str, person_id: str, limit: int = 50) -> list[str]:
    """Ensure every delivered/seen intervention reaches Brain2 as linked evidence."""
    ensure_v18_8_live_policy_schema()
    out: list[str] = []
    with connect() as con:
        # A purely audio/vision deployment can reach assembly before the
        # optional live-delivery subsystem has ever emitted a candidate.  This
        # must be a no-op, not a post-stop failure on a fresh database.
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brainlive_intervention_delivery_queue'"
        ).fetchone()
        if not exists:
            return out
        rows = con.execute(
            """SELECT q.* FROM brainlive_intervention_delivery_queue q
                 WHERE q.live_session_id=?
                   AND q.delivery_status IN ('delivered','displayed','seen','acted','dismissed','ignored')
                   AND NOT EXISTS(
                     SELECT 1 FROM brainlive_intervention_outcomes_v188 o
                     WHERE o.delivery_id=q.delivery_id AND o.outcome_status IN ('observation_pending','feedback_explicit','reconciled_helped','reconciled_not_helped')
                   )
                 ORDER BY q.created_at LIMIT ?""",
            (live_session_id, limit),
        ).fetchall()
    for row in rows:
        delivery = dict(row)
        status = "feedback_explicit" if delivery.get("delivery_status") in {"acted", "dismissed", "ignored"} else "observation_pending"
        outcome = materialize_intervention_outcome_observation(
            delivery_id=str(delivery["delivery_id"]),
            outcome_status=status,
            observed_later_summary=delivery.get("feedback_note"),
            did_help=True if delivery.get("delivery_status") == "acted" else False if delivery.get("delivery_status") in {"dismissed", "ignored"} else None,
            evidence={"delivery_status": delivery.get("delivery_status"), "feedback_type": delivery.get("feedback_type")},
            observed_at=delivery.get("feedback_at") or delivery.get("seen_at") or delivery.get("displayed_at") or delivery.get("delivered_at") or delivery.get("created_at"),
        )
        out.append(str(outcome["intervention_outcome_id"]))
    return out
