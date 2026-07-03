from __future__ import annotations

"""V15.14 BrainLive Event Assembler.

BrainLive must stay fast during the day: short VAD/ASR/VLM windows, hot context,
H0/H1/H2 forecasts and intervention gates.  Brain2 must stay deep at night: it
needs complete events, not a lossy live summary.

V15.14 is the offline bridge between those two constraints:

- live code keeps writing short rows as it already does;
- end-of-day assembly reads those rows once from the database;
- it groups them into complete event bundles with transcripts, diarization,
  vision descriptions, world states, predictions, interventions, outcomes and
  raw evidence references;
- optionally it materializes each bundle as a normal Brain2 conversation/turns
  so the calibrated V13/V14 engines can analyze richer scenes without changing
  their core logic.

No psychology is inferred here.  This module only links timestamps, ids and raw
observations.  Meaning-making stays in Brain2.
"""

from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
from typing import Any

from .db import connect, init_db, upsert, insert_only
from .governance_v18 import (
    DataAccessError, GovernanceError, Scope, ensure_v18_schema, strict_many, strict_one,
    record_artifact_version, link_artifact, invalidate_descendants, canonical_time, register_conversation_scope,
)
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v18_8_live_policy import ensure_v18_8_live_policy_schema, materialize_open_delivery_observations

VERSION = "15.14.1-v18.8.1-evidence-connected"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_raw_timeline_v1514(
  raw_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  live_session_id TEXT,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  event_time TEXT,
  modality TEXT NOT NULL,
  row_kind TEXT NOT NULL,
  speaker_label TEXT,
  speaker_person_id TEXT,
  text TEXT,
  summary TEXT,
  payload_json TEXT DEFAULT '{}',
  linked_event_id TEXT,
  linked_forecast_id TEXT,
  linked_candidate_id TEXT,
  frame_id TEXT,
  conversation_id TEXT,
  evidence_role TEXT DEFAULT 'raw_observation',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_event_bundles_v1514(
  bundle_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  live_session_id TEXT,
  start_time TEXT,
  end_time TEXT,
  bundle_kind TEXT NOT NULL,
  title TEXT,
  participants_json TEXT DEFAULT '[]',
  place_json TEXT DEFAULT '{}',
  transcript_json TEXT DEFAULT '[]',
  diarization_json TEXT DEFAULT '[]',
  vision_timeline_json TEXT DEFAULT '[]',
  audio_timeline_json TEXT DEFAULT '[]',
  world_state_timeline_json TEXT DEFAULT '[]',
  prediction_timeline_json TEXT DEFAULT '[]',
  intervention_timeline_json TEXT DEFAULT '[]',
  outcome_timeline_json TEXT DEFAULT '[]',
  affordance_timeline_json TEXT DEFAULT '[]',
  raw_timeline_json TEXT DEFAULT '[]',
  source_counts_json TEXT DEFAULT '{}',
  brain2_conversation_id TEXT,
  status TEXT DEFAULT 'assembled',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_brain2_event_exports_v1514(
  export_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  bundle_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_ids_json TEXT DEFAULT '[]',
  export_status TEXT DEFAULT 'exported',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_event_assembly_runs_v1514(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  package_date TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  raw_rows INTEGER DEFAULT 0,
  bundles INTEGER DEFAULT 0,
  exports INTEGER DEFAULT 0,
  status TEXT NOT NULL,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bl_raw_v1514_person_date ON brainlive_raw_timeline_v1514(person_id, package_date, event_time);
CREATE INDEX IF NOT EXISTS idx_bl_raw_v1514_session ON brainlive_raw_timeline_v1514(live_session_id, event_time);
CREATE INDEX IF NOT EXISTS idx_bl_bundle_v1514_person_date ON brainlive_event_bundles_v1514(person_id, package_date, start_time);
CREATE INDEX IF NOT EXISTS idx_bl_export_v1514_bundle ON brainlive_brain2_event_exports_v1514(bundle_id, conversation_id);
"""


def ensure_event_assembler_schema() -> None:
    ensure_v18_schema()
    ensure_v18_8_live_policy_schema()
    init_db()
    with connect() as con:
        con.executescript(SCHEMA)
        con.commit()


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _columns(con, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _rows(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return strict_many(con, sql, params, purpose="event assembly query")


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _period_bounds(package_date: str | None = None, *, days_back: int = 0) -> tuple[str, str, str]:
    """Return the lived local day for William/me, persisted as UTC instants.

    BrainLive packages are daily life packages, not UTC log shards.  Default to
    Europe/Paris unless MLOMEGA_LOCAL_TZ is set.
    """
    tz_name = os.environ.get("MLOMEGA_LOCAL_TZ", "Europe/Paris")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    if package_date:
        local_day = datetime.fromisoformat(package_date[:10]).replace(tzinfo=tz)
    else:
        local_day = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back)
    start_dt = local_day.astimezone(timezone.utc)
    end_dt = (local_day + timedelta(days=1)).astimezone(timezone.utc)
    return local_day.date().isoformat(), start_dt.isoformat(), end_dt.isoformat()


def _in_period(row: dict[str, Any], start: str, end: str, *keys: str) -> bool:
    sdt = _parse_dt(start)
    edt = _parse_dt(end)
    if sdt is None or edt is None:
        return True
    for k in keys:
        if row.get(k):
            dt = _parse_dt(row.get(k))
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return sdt <= dt < edt
    return False


def _row_in_package_period(row: dict[str, Any], start: str, end: str) -> bool:
    """True when the row itself belongs to the package window.

    Session overlap selects candidate sessions; this second gate prevents long
    sessions from leaking yesterday/tomorrow rows into today's Brain2 bundle.
    """
    return _in_period(
        row, start, end,
        "timestamp_start", "timestamp_end", "event_time", "state_time", "window_start",
        "loaded_at", "processed_at", "captured_at", "frame_captured_at", "created_at",
        "updated_at",
    )


def _source_pk(row: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v:
            return str(v)
    return stable_id("row", row)


def _json_field(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    return json_loads(value if isinstance(value, str) else None, default)


def _as_text(value: Any, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    s = value.strip() if isinstance(value, str) else json_dumps(value)
    if not s:
        return None
    return s if max_len is None else s[:max_len]


def _compact_payload(row: dict[str, Any], max_str: int = 2500) -> dict[str, Any]:
    """Keep a compact retrieval reference plus the minimum non-lossy evidence.

    Most raw rows only need a source id and a digest.  Vision is different: the
    assembler must be able to recover the original frame path and dHash even when
    V18.8 intentionally skipped its *live* VLM call.  World-state location is
    retained as a boundary signal, not as an inferred activity.
    """
    import hashlib
    source_id = _source_pk(row, "event_id", "live_turn_id", "segment_id", "observation_id", "frame_id", "world_state_id", "fused_id", "forecast_id", "candidate_id")
    compact: dict[str, Any] = {
        "source_id": source_id,
        "source_payload_sha256": hashlib.sha256(json_dumps(row).encode("utf-8")).hexdigest(),
        "source_columns": sorted(row.keys()),
        "retrieval_required": True,
    }
    source_table = str(row.get("source_table") or "")
    if source_table in {"vision_frames", "vision_scene_observations"} or row.get("frame_id"):
        metadata = _json_field(row.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        vision: dict[str, Any] = {
            "frame_id": row.get("frame_id"),
            "image_path": row.get("image_path") or row.get("frame_image_path"),
            "image_sha256": row.get("image_sha256") or row.get("frame_image_sha256"),
            "captured_at": row.get("captured_at") or row.get("frame_captured_at"),
            "metadata": metadata,
            "scene_summary": row.get("scene_summary"),
            "location_hint": row.get("location_hint"),
            "spatial_context": row.get("spatial_context"),
            "objects_json": row.get("objects_json"),
            "affordances_json": row.get("affordances_json"),
            "possible_user_activities_json": row.get("possible_user_activities_json"),
            "visible_text_json": row.get("visible_text_json"),
            "people_count": row.get("people_count"),
        }
        compact["vision_evidence"] = {k: v for k, v in vision.items() if v not in (None, "", [], {})}
    if source_table == "brainlive_world_states":
        compact["context_evidence"] = {
            "where_am_i": row.get("where_am_i"),
            "what_is_happening": row.get("what_is_happening"),
            "probable_activity_json": row.get("probable_activity_json"),
            "visual_context_json": row.get("visual_context_json"),
        }
    return compact

def _append(out: list[dict[str, Any]], *, person_id: str, package_date: str, source_table: str, source_id: str, event_time: str | None, modality: str, row_kind: str, payload: dict[str, Any], live_session_id: str | None = None, speaker_label: str | None = None, speaker_person_id: str | None = None, text: str | None = None, summary: str | None = None, linked_event_id: str | None = None, linked_forecast_id: str | None = None, linked_candidate_id: str | None = None, frame_id: str | None = None, conversation_id: str | None = None, evidence_role: str = "raw_observation") -> None:
    raw_id = stable_id("blraw1514", person_id, package_date, source_table, source_id)
    out.append({
        "raw_id": raw_id,
        "person_id": person_id,
        "package_date": package_date,
        "live_session_id": live_session_id,
        "source_table": source_table,
        "source_id": source_id,
        "event_time": event_time,
        "modality": modality,
        "row_kind": row_kind,
        "speaker_label": speaker_label,
        "speaker_person_id": speaker_person_id,
        "text": text,
        "summary": summary,
        "payload_json": json_dumps(_compact_payload(payload)),
        "linked_event_id": linked_event_id,
        "linked_forecast_id": linked_forecast_id,
        "linked_candidate_id": linked_candidate_id,
        "frame_id": frame_id,
        "conversation_id": conversation_id,
        "evidence_role": evidence_role,
        "created_at": now_iso(),
    })


def collect_live_raw_timeline(
    person_id: str,
    *,
    package_date: str | None = None,
    live_session_id: str | None = None,
    limit_per_table: int = 5000,
) -> dict[str, Any]:
    """Collect a closed, canonical timeline for one owner/session/day.

    Canonical human speech comes only from ``brainlive_turn_buffer``.  Audio
    segments/chunks remain source evidence and can no longer become duplicate
    pseudo-dialogue.  A requested limit is an explicit incomplete result; it is
    never silently used to call a day complete.
    """
    if not person_id:
        raise GovernanceError("person_id is required for event assembly")
    ensure_event_assembler_schema()
    day, start, end = _period_bounds(package_date)
    items: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    with connect() as con:
        if live_session_id:
            session = strict_one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=? AND person_id=?", (live_session_id, person_id), purpose="assembly session scope")
            if not session:
                raise GovernanceError("requested live_session_id is missing or owned by another person")
            sessions=[session]
        else:
            sessions=strict_many(con, "SELECT * FROM brainlive_sessions WHERE person_id=? AND ((started_at>=? AND started_at<?) OR (ended_at>=? AND ended_at<?) OR (ended_at IS NULL AND started_at<?)) ORDER BY started_at", (person_id,start,end,start,end,end), purpose="assembly sessions")
        for session in sessions:
            _append(items,person_id=person_id,package_date=day,source_table="brainlive_sessions",source_id=str(session["live_session_id"]),event_time=session.get("started_at"),modality="session",row_kind="live_session",payload=session,live_session_id=session.get("live_session_id"),summary=session.get("session_title") or session.get("active_location_hint"),evidence_role="session_anchor")
        session_ids=[str(x["live_session_id"]) for x in sessions]
        if not session_ids:
            return {"version":VERSION,"person_id":person_id,"package_date":day,"period_start":start,"period_end":end,"live_session_id":live_session_id,"raw_rows":0,"timeline":[],"incomplete":False,"incomplete_reasons":[]}
        ph=','.join('?' for _ in session_ids)
        # Explicitly materialize an observation-pending record for every delivered
        # intervention before the closed package is collected for Brain2.
        for _sid in session_ids:
            materialize_open_delivery_observations(live_session_id=_sid, person_id=person_id)
        def checked(sql: str, params: tuple[Any,...], *, label: str) -> list[dict[str,Any]]:
            rows=strict_many(con,sql,params,purpose=label)
            if len(rows)>limit_per_table:
                incomplete.append({"table":label,"count":len(rows),"limit":limit_per_table})
                rows=rows[:limit_per_table]
            return rows
        # One canonical speech record per source turn.
        turns=checked(f"SELECT * FROM brainlive_turn_buffer WHERE live_session_id IN ({ph}) AND timestamp_start>=? AND timestamp_start<? ORDER BY timestamp_start,live_turn_id", tuple(session_ids)+(start,end),label="brainlive_turn_buffer")
        for r in turns:
            text=_as_text(r.get("text_final") or r.get("text_partial"))
            if text:
                _append(items,person_id=person_id,package_date=day,source_table="brainlive_turn_buffer",source_id=str(r["live_turn_id"]),event_time=r.get("timestamp_start"),modality="audio_text",row_kind="transcript_turn",payload=r,live_session_id=r.get("live_session_id"),speaker_label=r.get("speaker_label"),speaker_person_id=r.get("speaker_person_id"),text=text,summary=text,conversation_id=r.get("conversation_id"),evidence_role="transcript")
        # Source-level audio evidence only; no duplicate transcript text.
        if _table_exists(con,"brainlive_sensor_events"):
            evs=checked(f"SELECT * FROM brainlive_sensor_events WHERE person_id=? AND live_session_id IN ({ph}) AND event_time>=? AND event_time<? ORDER BY event_time,event_id", (person_id,*session_ids,start,end),label="brainlive_sensor_events")
            for r in evs:
                payload=_json_field(r.get("payload_json"),{})
                modality=str(r.get("modality") or "unknown")
                kind=str(r.get("event_type") or "sensor_event")
                # Audio speech text is represented by turn_buffer, not here.
                summary=None if modality=="audio" else _as_text(payload.get("normalized") or payload.get("summary") or payload)
                _append(items,person_id=person_id,package_date=day,source_table="brainlive_sensor_events",source_id=str(r["event_id"]),event_time=r.get("event_time"),modality=modality,row_kind=kind,payload=r,live_session_id=r.get("live_session_id"),summary=summary,linked_event_id=payload.get("linked_event_id") if isinstance(payload,dict) else None,evidence_role="sensor_observation")
        # Vision only if the observation has a frame in this exact session and
        # was captured in the requested period.  VLM errors are source errors,
        # never context evidence.
        if _table_exists(con,"vision_scene_observations"):
            vision=checked(f"""SELECT o.*,f.captured_at AS frame_captured_at,f.live_session_id AS frame_live_session_id,
                       f.image_path AS frame_image_path,f.image_sha256 AS frame_image_sha256,
                       f.metadata_json AS frame_metadata_json
                FROM vision_scene_observations o JOIN vision_frames f ON f.frame_id=o.frame_id
                WHERE f.live_session_id IN ({ph}) AND f.captured_at>=? AND f.captured_at<?
                ORDER BY f.captured_at,o.observation_id""", tuple(session_ids)+(start,end),label="vision_scene_observations")
            for r in vision:
                summary=r.get("scene_summary") or _as_text({"location_hint":r.get("location_hint"),"spatial_context":r.get("spatial_context")})
                _append(items,person_id=person_id,package_date=day,source_table="vision_scene_observations",source_id=str(r["observation_id"]),event_time=r.get("frame_captured_at"),modality="vision",row_kind="vision_scene",payload=r,live_session_id=r.get("frame_live_session_id"),summary=summary,frame_id=r.get("frame_id"),conversation_id=r.get("conversation_id"),evidence_role="vision_description")
        # Every captured frame is carried through to post-stop. V18.8 may skip
        # redundant *live* VLM calls, but it never removes evidence needed by
        # deep vision or activity-aware bundling.
        if _table_exists(con, "vision_frames"):
            frames = checked(f"""SELECT f.* FROM vision_frames f
                WHERE f.live_session_id IN ({ph}) AND f.captured_at>=? AND f.captured_at<?
                ORDER BY f.captured_at,f.frame_id""", tuple(session_ids)+(start,end), label="vision_frames")
            for r in frames:
                _append(items, person_id=person_id, package_date=day,
                    source_table="vision_frames", source_id=str(r["frame_id"]),
                    event_time=r.get("captured_at"), modality="vision", row_kind="vision_frame",
                    payload=r, live_session_id=r.get("live_session_id"),
                    summary=f"Raw visual frame: {Path(str(r.get('image_path') or '')).name}",
                    frame_id=r.get("frame_id"), conversation_id=r.get("conversation_id"),
                    evidence_role="raw_visual_frame")
        # Candidate -> delivery -> feedback -> outcome is included as linked
        # metadata for Brain2; none of these rows become fake dialogue.
        if _table_exists(con, "brainlive_intervention_delivery_queue"):
            deliveries = checked(f"SELECT * FROM brainlive_intervention_delivery_queue WHERE live_session_id IN ({ph}) AND created_at>=? AND created_at<? ORDER BY created_at", tuple(session_ids)+(start,end), label="brainlive_intervention_delivery_queue")
            for r in deliveries:
                _append(items,person_id=person_id,package_date=day,source_table="brainlive_intervention_delivery_queue",source_id=str(r.get("delivery_id")),event_time=r.get("created_at"),modality="intervention",row_kind="intervention_delivery",payload=r,live_session_id=r.get("live_session_id"),summary=r.get("message") or r.get("candidate_message") or r.get("feedback_note"),linked_candidate_id=r.get("candidate_id"),evidence_role="model_output")
        if _table_exists(con, "brainlive_intervention_feedback_events_v188"):
            feedback_rows = checked(f"SELECT * FROM brainlive_intervention_feedback_events_v188 WHERE live_session_id IN ({ph}) AND observed_at>=? AND observed_at<? ORDER BY observed_at", tuple(session_ids)+(start,end), label="brainlive_intervention_feedback_events_v188")
            for r in feedback_rows:
                _append(items,person_id=person_id,package_date=day,source_table="brainlive_intervention_feedback_events_v188",source_id=str(r.get("feedback_id")),event_time=r.get("observed_at"),modality="feedback",row_kind="intervention_feedback",payload=r,live_session_id=r.get("live_session_id"),summary=r.get("feedback_type") or r.get("note"),linked_candidate_id=r.get("candidate_id"),evidence_role="user_feedback")
        if _table_exists(con, "brainlive_intervention_outcomes_v188"):
            outcome_rows = checked(f"SELECT * FROM brainlive_intervention_outcomes_v188 WHERE live_session_id IN ({ph}) AND observed_at>=? AND observed_at<? ORDER BY observed_at", tuple(session_ids)+(start,end), label="brainlive_intervention_outcomes_v188")
            for r in outcome_rows:
                _append(items,person_id=person_id,package_date=day,source_table="brainlive_intervention_outcomes_v188",source_id=str(r.get("intervention_outcome_id")),event_time=r.get("observed_at"),modality="outcome",row_kind="intervention_outcome",payload=r,live_session_id=r.get("live_session_id"),summary=r.get("observed_later_summary") or r.get("outcome_status"),linked_candidate_id=r.get("candidate_id"),evidence_role="outcome_observation")
        for table,time_col,row_kind,modality,summary_col in [
            ("brainlive_world_states","state_time","world_state","world_state","what_is_happening"),
            ("brainlive_fused_situations","window_start","fused_situation","fused_situation",None),
            ("brainlive_event_candidates","created_at","event_candidate","event_candidate","event_summary"),
            ("brainlive_short_horizon_forecasts","occurred_at","forecast","forecast",None),
            ("brainlive_intervention_candidates","created_at","intervention","intervention","message"),
            ("brainlive_prediction_outcomes","observed_after","outcome","outcome",None),
        ]:
            if not _table_exists(con,table):
                continue
            rows=checked(f"SELECT * FROM {table} WHERE live_session_id IN ({ph}) AND {time_col}>=? AND {time_col}<? ORDER BY {time_col}", tuple(session_ids)+(start,end),label=table)
            for r in rows:
                pk=_source_pk(r,"world_state_id","fused_id","event_id","forecast_id","candidate_id","outcome_id")
                summary=r.get(summary_col) if summary_col else _as_text(r)
                _append(items,person_id=person_id,package_date=day,source_table=table,source_id=pk,event_time=r.get(time_col),modality=modality,row_kind=row_kind,payload=r,live_session_id=r.get("live_session_id"),summary=_as_text(summary),linked_event_id=r.get("event_id"),linked_forecast_id=r.get("forecast_id"),linked_candidate_id=r.get("candidate_id"),evidence_role="model_output" if modality in {"forecast","intervention"} else "system_observation")
    # Exact time sort; untimeable rows cannot enter a publishable package.
    invalid=[r for r in items if not canonical_time(r,"event_time","created_at")]
    if invalid:
        incomplete.append({"reason":"untimed_raw_rows","count":len(invalid)})
        items=[r for r in items if r not in invalid]
    items.sort(key=lambda r:(canonical_time(r,"event_time","created_at") or "",r["source_table"],r["source_id"]))
    with connect() as con:
        for item in items:
            insert_only(con,"brainlive_raw_timeline_v1514",item,on_conflict="ignore")
        con.commit()
    return {"version":VERSION,"person_id":person_id,"package_date":day,"period_start":start,"period_end":end,"live_session_id":live_session_id,"raw_rows":len(items),"timeline":items,"incomplete":bool(incomplete),"incomplete_reasons":incomplete}

def _window_rows(rows: list[dict[str, Any]], start_dt: datetime | None, end_dt: datetime | None, anchor_event_id: str | None = None) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for r in rows:
        if anchor_event_id and r.get("linked_event_id") == anchor_event_id:
            selected.append(r); continue
        dt = _parse_dt(r.get("event_time") or r.get("created_at"))
        if dt and start_dt and end_dt and start_dt <= dt <= end_dt:
            selected.append(r)
    # de-duplicate while preserving order
    seen: set[str] = set(); out: list[dict[str, Any]] = []
    for r in sorted(selected, key=lambda x: (x.get("event_time") or x.get("created_at") or "", x.get("source_table") or "")):
        rid = r.get("raw_id") or stable_id("raw", r)
        if rid not in seen:
            seen.add(rid); out.append(r)
    return out


def _bundle_payload(person_id: str, package_date: str, live_session_id: str | None, bundle_kind: str, title: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    times = [_parse_dt(r.get("event_time") or r.get("created_at")) for r in rows]
    times2 = [t for t in times if t is not None]
    start_time = min(times2).isoformat() if times2 else None
    end_time = max(times2).isoformat() if times2 else None
    participants: dict[str, dict[str, Any]] = {}
    transcript: list[dict[str, Any]] = []
    diar: list[dict[str, Any]] = []
    vision: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    world: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    interventions: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    affordances: list[dict[str, Any]] = []
    place_votes: list[str] = []
    raw_refs: list[dict[str, Any]] = []

    for r in rows:
        payload = json_loads(r.get("payload_json"), {}) or {}
        raw_refs.append({"source_table": r.get("source_table"), "source_id": r.get("source_id"), "raw_id": r.get("raw_id"), "event_time": r.get("event_time"), "evidence_role": r.get("evidence_role")})
        if r.get("speaker_person_id"):
            pid = str(r.get("speaker_person_id"))
            participants[pid] = {"person_id": pid, "speaker_label": r.get("speaker_label"), "source": "diarization_or_live_identity"}
        if r.get("modality") in {"audio_text", "audio"} and r.get("text"):
            transcript.append({"time": r.get("event_time"), "speaker_label": r.get("speaker_label"), "speaker_person_id": r.get("speaker_person_id"), "text": r.get("text"), "source_table": r.get("source_table"), "source_id": r.get("source_id")})
            diar.append({"time": r.get("event_time"), "speaker_label": r.get("speaker_label"), "speaker_person_id": r.get("speaker_person_id"), "confidence": payload.get("speaker_confidence") or payload.get("speaker_resolution_json"), "source_id": r.get("source_id")})
            audio.append({"time": r.get("event_time"), "kind": r.get("row_kind"), "summary": r.get("summary"), "source_table": r.get("source_table"), "source_id": r.get("source_id")})
        elif r.get("modality") == "audio_prosody":
            audio.append({"time": r.get("event_time"), "kind": r.get("row_kind"), "summary": r.get("summary"), "payload": payload, "source_table": r.get("source_table"), "source_id": r.get("source_id")})
        elif r.get("modality") == "vision":
            evidence = payload.get("vision_evidence") if isinstance(payload.get("vision_evidence"), dict) else payload
            metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
            image_path = evidence.get("image_path") or evidence.get("frame_image_path")
            location_hint = evidence.get("location_hint")
            vision.append({
                "time": r.get("event_time"),
                "summary": evidence.get("scene_summary") or r.get("summary") or r.get("text"),
                "location_hint": location_hint,
                "spatial_context": evidence.get("spatial_context"),
                "objects": json_loads(evidence.get("objects_json"), []) if isinstance(evidence.get("objects_json"), str) else evidence.get("objects_json"),
                "affordances": json_loads(evidence.get("affordances_json"), []) if isinstance(evidence.get("affordances_json"), str) else evidence.get("affordances_json"),
                "possible_user_activities": json_loads(evidence.get("possible_user_activities_json"), []) if isinstance(evidence.get("possible_user_activities_json"), str) else evidence.get("possible_user_activities_json"),
                "visible_text": json_loads(evidence.get("visible_text_json"), []) if isinstance(evidence.get("visible_text_json"), str) else evidence.get("visible_text_json"),
                "personal_relevance": json_loads(evidence.get("personal_relevance_json"), {}) if isinstance(evidence.get("personal_relevance_json"), str) else evidence.get("personal_relevance_json"),
                "frame_id": evidence.get("frame_id") or r.get("frame_id"),
                "image_path": image_path,
                "image_sha256": evidence.get("image_sha256"),
                "image_signature": metadata.get("v188_image_signature"),
                "image_signature_kind": metadata.get("v188_image_signature_kind"),
                "live_vlm_policy": metadata.get("v188_live_vlm_policy"),
                "source_table": r.get("source_table"), "source_id": r.get("source_id"),
            })
            if location_hint:
                place_votes.append(str(location_hint))
        elif r.get("modality") in {"world_state", "fused_context", "context", "event", "session"}:
            context_evidence = payload.get("context_evidence") if isinstance(payload.get("context_evidence"), dict) else payload
            world.append({"time": r.get("event_time"), "kind": r.get("row_kind"), "summary": r.get("summary"), "payload": context_evidence, "source_table": r.get("source_table"), "source_id": r.get("source_id")})
            if context_evidence.get("where_am_i"):
                place_votes.append(str(context_evidence.get("where_am_i")))
            if context_evidence.get("active_location_hint"):
                place_votes.append(str(context_evidence.get("active_location_hint")))
        elif r.get("modality") == "prediction":
            predictions.append({"time": r.get("event_time"), "kind": r.get("row_kind"), "summary": r.get("summary"), "payload": payload, "forecast_id": r.get("linked_forecast_id"), "source_table": r.get("source_table"), "source_id": r.get("source_id")})
        elif r.get("modality") == "intervention":
            interventions.append({"time": r.get("event_time"), "kind": r.get("row_kind"), "text": r.get("text"), "summary": r.get("summary"), "payload": payload, "candidate_id": r.get("linked_candidate_id"), "source_table": r.get("source_table"), "source_id": r.get("source_id")})
        elif r.get("modality") in {"outcome", "feedback"}:
            outcomes.append({"time": r.get("event_time"), "kind": r.get("row_kind"), "summary": r.get("summary"), "payload": payload, "source_table": r.get("source_table"), "source_id": r.get("source_id")})
        elif r.get("modality") == "affordance":
            affordances.append({"time": r.get("event_time"), "kind": r.get("row_kind"), "summary": r.get("summary"), "payload": payload, "frame_id": r.get("frame_id"), "image_path": payload.get("frame_image_path") or payload.get("image_path"), "source_table": r.get("source_table"), "source_id": r.get("source_id")})

    counts = Counter([str(r.get("row_kind")) for r in rows])
    source_counts = Counter([str(r.get("source_table")) for r in rows])
    place_json = {"dominant_hint": Counter(place_votes).most_common(1)[0][0] if place_votes else None, "all_hints": sorted(set(place_votes))}
    bundle_id = stable_id("blevent1514", person_id, package_date, live_session_id, bundle_kind, title, start_time, end_time, [r.get("raw_id") for r in rows[:20]])
    now = now_iso()
    return {
        "bundle_id": bundle_id,
        "person_id": person_id,
        "package_date": package_date,
        "live_session_id": live_session_id,
        "start_time": start_time,
        "end_time": end_time,
        "bundle_kind": bundle_kind,
        "title": title[:240],
        "participants_json": json_dumps(list(participants.values())),
        "place_json": json_dumps(place_json),
        "transcript_json": json_dumps(transcript),
        "diarization_json": json_dumps(diar),
        "vision_timeline_json": json_dumps(vision),
        "audio_timeline_json": json_dumps(audio),
        "world_state_timeline_json": json_dumps(world),
        "prediction_timeline_json": json_dumps(predictions),
        "intervention_timeline_json": json_dumps(interventions),
        "outcome_timeline_json": json_dumps(outcomes),
        "affordance_timeline_json": json_dumps(affordances),
        "raw_timeline_json": json_dumps(raw_refs),
        "source_counts_json": json_dumps({"by_kind": dict(counts), "by_source_table": dict(source_counts), "raw_rows": len(rows)}),
        "brain2_conversation_id": None,
        "status": "assembled",
        "created_at": now,
        "updated_at": now,
    }


def _row_vision_evidence(row: dict[str, Any]) -> dict[str, Any]:
    payload = json_loads(row.get("payload_json"), {}) or {}
    evidence = payload.get("vision_evidence") if isinstance(payload.get("vision_evidence"), dict) else payload
    return evidence if isinstance(evidence, dict) else {}


def _vision_scene_signature(row: dict[str, Any]) -> str | None:
    """Structured VLM signal for a genuine activity/context transition."""
    if str(row.get("source_table") or "") != "vision_scene_observations":
        return None
    payload = _row_vision_evidence(row)
    def unpack(key: str) -> Any:
        value = payload.get(key)
        return json_loads(value, []) if isinstance(value, str) else (value or [])
    def labels(value: Any, limit: int = 8) -> list[str]:
        out: list[str] = []
        if not isinstance(value, list):
            return out
        for item in value[:limit]:
            text = (item.get("activity") or item.get("label") or item.get("name") or item.get("text")) if isinstance(item, dict) else item
            if text not in (None, ""):
                out.append(str(text).strip().lower())
        return sorted(set(out))
    signature = {
        "activities": labels(unpack("possible_user_activities_json")),
        "people_count": payload.get("people_count"),
        "objects": labels(unpack("objects_json")),
        "visible_text": labels(unpack("visible_text_json")),
    }
    return stable_id("v188_scene", signature) if any(signature.values()) else None


def _pixel_scene_signature(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return capture-side dHash only for raw visual frame rows.

    A dHash is a boundary hint, never a semantic label.  It is used only when
    live VLM was skipped and only with a higher threshold/minimum dwell time.
    """
    if str(row.get("source_table") or "") != "vision_frames":
        return None
    metadata = _row_vision_evidence(row).get("metadata")
    if not isinstance(metadata, dict):
        return None
    kind = metadata.get("v188_image_signature_kind")
    value = metadata.get("v188_image_signature")
    if kind and value:
        return str(kind), str(value)
    return None


def _pixel_change_significant(previous: tuple[str, str] | None, incoming: tuple[str, str] | None) -> bool:
    if not previous or not incoming or previous[0] != incoming[0]:
        return False
    if previous[0] != "dhash64":
        # SHA fallback means bytes differ, not necessarily a scene changed.
        return False
    try:
        distance = (int(previous[1], 16) ^ int(incoming[1], 16)).bit_count()
        threshold = max(1, min(64, int(os.environ.get("MLOMEGA_BRAINLIVE_BUNDLE_DHASH_SPLIT_BITS", "14"))))
        return distance >= threshold
    except Exception:
        return False


def _place_scene_signature(row: dict[str, Any]) -> str | None:
    if str(row.get("source_table") or "") != "brainlive_world_states":
        return None
    payload = json_loads(row.get("payload_json"), {}) or {}
    context = payload.get("context_evidence") if isinstance(payload.get("context_evidence"), dict) else payload
    place = context.get("where_am_i") or context.get("active_location_hint") or context.get("place_name") or context.get("label")
    return stable_id("v188_place", str(place).strip().lower()) if place else None


def _bundle_max_minutes() -> int:
    try:
        return max(10, min(360, int(os.environ.get("MLOMEGA_BRAINLIVE_MAX_BUNDLE_MINUTES", "25"))))
    except Exception:
        return 25


def _visual_boundary_allowed(current: list[dict[str, Any]], incoming: dict[str, Any], *, pixel: bool = False) -> bool:
    if not current:
        return False
    previous = _parse_dt(canonical_time(current[-1], "event_time", "created_at"))
    now = _parse_dt(canonical_time(incoming, "event_time", "created_at"))
    if not previous or not now:
        return True
    try:
        default = "90" if pixel else "45"
        env_name = "MLOMEGA_BRAINLIVE_PIXEL_SPLIT_MIN_SEPARATION_S" if pixel else "MLOMEGA_BRAINLIVE_VISUAL_SPLIT_MIN_SEPARATION_S"
        min_separation = float(os.environ.get(env_name, default))
    except Exception:
        min_separation = 90.0 if pixel else 45.0
    return (now - previous).total_seconds() >= max(0.0, min_separation)

def assemble_event_bundles(
    person_id: str,
    *,
    package_date: str | None = None,
    raw_timeline: list[dict[str, Any]] | None = None,
    gap_minutes: int = 20,
    context_before_minutes: int = 4,
    context_after_minutes: int = 8,
    live_session_id: str | None = None,
) -> dict[str, Any]:
    """Partition a closed timeline into non-overlapping bundles.

    An evidence item has exactly one bundle owner.  Explicit event candidates
    influence a title/type but never create overlapping windows that reuse a
    sentence as proof in multiple scenes.
    """
    ensure_event_assembler_schema()
    if raw_timeline is None:
        collected=collect_live_raw_timeline(person_id,package_date=package_date,live_session_id=live_session_id)
        if collected.get("incomplete"):
            raise GovernanceError(f"cannot assemble incomplete raw timeline: {collected.get('incomplete_reasons')}")
        raw_timeline=collected["timeline"]
    day=(package_date or (raw_timeline[0].get("package_date") if raw_timeline else None) or _period_bounds(None)[0])[:10]
    by_session:dict[str,list[dict[str,Any]]]=defaultdict(list)
    for row in raw_timeline:
        if row.get("person_id")!=person_id:
            raise GovernanceError("cross-owner raw row passed to assembler")
        sid=str(row.get("live_session_id") or "no_session")
        if live_session_id and sid!=live_session_id:
            continue
        by_session[sid].append(row)
    bundles=[]
    max_bundle_minutes = _bundle_max_minutes()
    for sid,rows in by_session.items():
        rows.sort(key=lambda r:(canonical_time(r,"event_time","created_at") or "",r["raw_id"]))
        # A live-session row is scope metadata, not an evidence item.  It must
        # never form a standalone scene or split a late-arriving, correctly
        # timestamped capture from its actual evidence.  The session remains on
        # the bundle itself via ``live_session_id``.
        rows = [row for row in rows if row.get("row_kind") != "live_session"]
        if not rows:
            continue
        current=[]; last=None; current_anchor=None; current_start=None
        current_visual_signature=None; last_pixel_signature=None; current_place_signature=None
        for row in rows:
            dt=_parse_dt(canonical_time(row,"event_time","created_at"))
            is_anchor=row.get("row_kind")=="event_candidate"
            split=bool(current and dt and last and (dt-last)>timedelta(minutes=gap_minutes))
            visual_signature = _vision_scene_signature(row)
            pixel_signature = _pixel_scene_signature(row)
            place_signature = _place_scene_signature(row)
            if current and visual_signature and current_visual_signature and visual_signature != current_visual_signature and _visual_boundary_allowed(current, row):
                split=True
            # When live VLM was skipped for near-duplicates, a substantial dHash
            # change is still evidence of a visual transition at the same place.
            if current and not split and _pixel_change_significant(last_pixel_signature, pixel_signature) and _visual_boundary_allowed(current, row, pixel=True):
                split=True
            # Place changes are independent evidence. Small GPS jitter remains
            # grouped because the signature is based on the resolved place label.
            if current and not split and place_signature and current_place_signature and place_signature != current_place_signature and _visual_boundary_allowed(current, row):
                split=True
            if current and dt and current_start and (dt-current_start)>timedelta(minutes=max_bundle_minutes):
                split=True
            # A new anchor begins a new scene only if it is materially separated
            # from the current one. It never steals rows already owned.
            if split:
                title=(current_anchor or _title_from_rows(current))
                bundles.append(_bundle_payload(person_id,day,None if sid=="no_session" else sid,"anchored_event" if current_anchor else "timeline_scene",str(title),current))
                current=[]; current_anchor=None; current_start=None
                current_visual_signature=None; last_pixel_signature=None; current_place_signature=None
            current.append(row)
            if current_start is None and dt:
                current_start=dt
            if is_anchor and not current_anchor:
                current_anchor=row.get("summary") or f"BrainLive event {row.get('source_id')}"
            if visual_signature:
                current_visual_signature=visual_signature
            if pixel_signature:
                last_pixel_signature=pixel_signature
            if place_signature:
                current_place_signature=place_signature
            if dt: last=dt
        if current:
            title=current_anchor or _title_from_rows(current)
            bundles.append(_bundle_payload(person_id,day,None if sid=="no_session" else sid,"anchored_event" if current_anchor else "timeline_scene",str(title),current))
    scope=Scope(person_id=person_id,live_session_id=live_session_id,mode="post_stop")
    invalidated=[]
    with connect() as con:
        old_rows=strict_many(con,"SELECT bundle_id FROM brainlive_event_bundles_v1514 WHERE person_id=? AND package_date=? AND COALESCE(status,'assembled')='assembled'"+(" AND live_session_id=?" if live_session_id else ""),(person_id,day,*((live_session_id,) if live_session_id else ())),purpose="old bundles")
        new_ids={str(b["bundle_id"]) for b in bundles}
        for old in old_rows:
            bid=str(old["bundle_id"])
            if bid not in new_ids:
                con.execute("UPDATE brainlive_event_bundles_v1514 SET status='superseded',updated_at=? WHERE bundle_id=?",(now_iso(),bid))
                con.execute("UPDATE brainlive_brain2_event_exports_v1514 SET export_status='superseded',updated_at=? WHERE bundle_id=?",(now_iso(),bid))
                invalidated.append(bid)
        for b in bundles:
            upsert(con,"brainlive_event_bundles_v1514",b,"bundle_id")
        con.commit()
    for bid in invalidated:
        invalidate_descendants(root_table="brainlive_event_bundles_v1514",root_id=bid,scope=scope,reason="bundle superseded by non-overlapping V18 assembly")
    for b in bundles:
        record_artifact_version(artifact_table="brainlive_event_bundles_v1514",artifact_id=str(b["bundle_id"]),identity_key=str(b["bundle_id"]),scope=Scope(person_id=person_id,live_session_id=b.get("live_session_id"),mode="post_stop"),source_payload=json_loads(b["raw_timeline_json"],[]),metadata={"status":"assembled"})
    return {"version":VERSION,"person_id":person_id,"package_date":day,"live_session_id":live_session_id,"bundles_created":len(bundles),"bundles":bundles,"superseded_bundle_ids":invalidated}

def _title_from_rows(rows: list[dict[str, Any]]) -> str:
    for r in rows:
        if r.get("summary"):
            return f"BrainLive scene: {str(r.get('summary'))[:120]}"
        if r.get("text"):
            return f"BrainLive scene: {str(r.get('text'))[:120]}"
    if rows:
        return f"BrainLive scene {rows[0].get('event_time') or rows[0].get('created_at')}"
    return "BrainLive scene"


def _pseudo_turns_for_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Return raw Brain2 turns for the assembled event.

    V15.17 rule: do not turn BrainLive's own predictions/interventions/outcomes
    into conversation text.  Those stay in conversation.raw_json as side-channel
    evidence for reconciliation.  Brain2 receives only the human transcript plus
    raw non-dialogue context observations that help it understand the scene:
    vision descriptions, place/world-state hints and audio/prosody observations.
    No LLM reconstruction is performed here.
    """
    timeline: list[dict[str, Any]] = []
    transcript = json_loads(bundle.get("transcript_json"), []) or []
    for t in transcript:
        timeline.append({
            "time": t.get("time"),
            "speaker_label": t.get("speaker_label") or "speaker",
            "speaker_person_id": t.get("speaker_person_id"),
            "text": t.get("text"),
            "kind": "transcript",
            "evidence_role": "human_or_audio_transcript",
            "metadata": t,
        })
    for v in json_loads(bundle.get("vision_timeline_json"), []) or []:
        parts = []
        if v.get("summary"):
            parts.append(str(v.get("summary")))
        if v.get("location_hint"):
            parts.append(f"lieu_probable={v.get('location_hint')}")
        if v.get("spatial_context"):
            parts.append(f"spatial={v.get('spatial_context')}")
        if v.get("objects"):
            parts.append(f"objets={json_dumps(v.get('objects'))}")
        if v.get("affordances"):
            parts.append(f"affordances={json_dumps(v.get('affordances'))}")
        if v.get("possible_user_activities"):
            parts.append(f"activites_possibles={json_dumps(v.get('possible_user_activities'))}")
        text = "[CONTEXT_VISION_RAW] " + " | ".join(parts) if parts else "[CONTEXT_VISION_RAW] observation vision sans description textuelle"
        timeline.append({
            "time": v.get("time"),
            "speaker_label": "context_vision_raw",
            "speaker_person_id": None,
            "text": text[:8000],
            "kind": "vision_context",
            "evidence_role": "system_observation_not_user_speech",
            "metadata": v,
        })
    for w in json_loads(bundle.get("world_state_timeline_json"), []) or []:
        payload = w.get("payload") or {}
        summary = str(w.get("summary") or payload.get("where_am_i") or payload.get("what_is_happening") or "")
        if not summary and payload:
            # Keep only context-ish fields; avoid injecting BrainLive reasoning as a fact.
            safe = {k: payload.get(k) for k in ("where_am_i", "active_location_hint", "place_key", "people_present_json", "topic_hint", "state_time") if payload.get(k) is not None}
            summary = json_dumps(safe) if safe else ""
        if not summary:
            continue
        timeline.append({
            "time": w.get("time"),
            "speaker_label": "context_world_raw",
            "speaker_person_id": None,
            "text": ("[CONTEXT_WORLD_RAW] " + summary),
            "kind": "world_context",
            "evidence_role": "system_observation_not_user_speech",
            "metadata": w,
        })
    for a in json_loads(bundle.get("audio_timeline_json"), []) or []:
        if a.get("kind") != "prosody":
            continue
        text = str(a.get("summary") or "").strip()
        if not text:
            continue
        timeline.append({
            "time": a.get("time"),
            "speaker_label": "context_audio_raw",
            "speaker_person_id": None,
            "text": ("[CONTEXT_AUDIO_RAW] " + text),
            "kind": "audio_context",
            "evidence_role": "system_observation_not_user_speech",
            "metadata": a,
        })
    timeline = [x for x in timeline if x.get("text")]
    timeline.sort(key=lambda x: x.get("time") or "")
    return timeline

def export_event_bundles_to_brain2(
    person_id: str,
    *,
    package_date: str | None = None,
    bundle_ids: list[str] | None = None,
    limit: int = 200,
    live_session_id: str | None = None,
) -> dict[str, Any]:
    """Export active bundle versions as immutable Brain2 conversations.

    A changed bundle gets a new conversation version.  Previous conversations
    are invalidated through lineage rather than being partly overwritten with
    new turn indices while old descendants remain alive.
    """
    ensure_event_assembler_schema()
    day=(package_date or _period_bounds(None)[0])[:10]
    exports=[]
    invalidations=[]
    with connect() as con:
        where=["person_id=?","package_date=?","status='assembled'"]
        params:[Any]=[person_id,day]
        if live_session_id:
            where.append("live_session_id=?"); params.append(live_session_id)
        if bundle_ids:
            where.append("bundle_id IN (%s)" % ','.join('?' for _ in bundle_ids)); params.extend(bundle_ids)
        bundles=strict_many(con,"SELECT * FROM brainlive_event_bundles_v1514 WHERE "+" AND ".join(where)+" ORDER BY start_time LIMIT ?",tuple(params+[limit]),purpose="active bundle exports")
        for b in bundles:
            pseudo=_pseudo_turns_for_bundle(b)
            source_payload={"bundle_id":b["bundle_id"],"raw":json_loads(b.get("raw_timeline_json"),[]),"pseudo":pseudo}
            import hashlib
            digest=hashlib.sha256(json_dumps(source_payload).encode()).hexdigest()
            conv_id=stable_id("conv_blbundle_v18",b["bundle_id"],digest)
            previous=str(b.get("brain2_conversation_id") or "")
            raw_payload={"source":"brainlive_event_bundle_v18","bundle_id":b.get("bundle_id"),"bundle_digest":digest,"bundle_kind":b.get("bundle_kind"),"source_counts":json_loads(b.get("source_counts_json"),{}),"place":json_loads(b.get("place_json"),{}),"raw_timeline_refs":json_loads(b.get("raw_timeline_json"),[]),"side_channel_note":"prediction/intervention/outcome remain metadata, never dialogue"}
            insert_only(con,"conversations",{"conversation_id":conv_id,"title":b.get("title") or "BrainLive event bundle","started_at":b.get("start_time"),"ended_at":b.get("end_time"),"topic":"brainlive_event_bundle","channel":"brainlive_event_bundle_v18","participants_json":b.get("participants_json") or "[]","speaker_map_json":"{}","relationship_context_json":"{}","source_asset_id":None,"raw_json":json_dumps(raw_payload),"created_at":now_iso()},on_conflict="ignore")
            turn_ids=[]
            for idx,t in enumerate(pseudo):
                tid=stable_id("turn_blbundle_v18",conv_id,idx,t.get("time"),t.get("kind"),t.get("metadata",{}).get("source_id") if isinstance(t.get("metadata"),dict) else None)
                turn_ids.append(tid)
                insert_only(con,"turns",{"turn_id":tid,"conversation_id":conv_id,"idx":idx,"speaker_label":t.get("speaker_label"),"person_id":t.get("speaker_person_id"),"start_s":None,"end_s":None,"text":str(t.get("text") or ""),"previous_turn_id":turn_ids[idx-1] if idx else None,"metadata_json":json_dumps({"brainlive_bundle_id":b.get("bundle_id"),"bundle_digest":digest,"kind":t.get("kind"),"time":t.get("time"),"evidence_role":t.get("evidence_role"),"source":t.get("metadata")})},on_conflict="ignore")
            export_id=stable_id("blexport_v18",b.get("bundle_id"),digest)
            export={"export_id":export_id,"person_id":person_id,"bundle_id":b.get("bundle_id"),"conversation_id":conv_id,"turn_ids_json":json_dumps(turn_ids),"export_status":"exported","created_at":now_iso(),"updated_at":now_iso()}
            upsert(con,"brainlive_brain2_event_exports_v1514",export,"export_id")
            b2=dict(b); b2["brain2_conversation_id"]=conv_id; b2["updated_at"]=now_iso(); upsert(con,"brainlive_event_bundles_v1514",b2,"bundle_id")
            exports.append(export)
            if previous and previous!=conv_id:
                invalidations.append((str(b["bundle_id"]),previous))
        con.commit()
    for bundle_id,old_conv in invalidations:
        invalidate_descendants(root_table="conversations",root_id=old_conv,scope=Scope(person_id=person_id,live_session_id=live_session_id,mode="post_stop"),reason=f"bundle {bundle_id} re-exported as a new immutable conversation version")
    for export in exports:
        scope=Scope(person_id=person_id,live_session_id=live_session_id,mode="post_stop")
        record_artifact_version(artifact_table="conversations",artifact_id=export["conversation_id"],identity_key=f"bundle:{export['bundle_id']}",scope=scope,source_payload=json_loads(export["turn_ids_json"],[]),metadata={"export_id":export["export_id"]})
        # A BrainLive export is the authoritative conversation-owner proof.
        # Downstream V13/V14/V17 readers must not fall back to a global default.
        register_conversation_scope(conversation_id=export["conversation_id"],person_id=person_id,evidence_kind="explicit_export",evidence={"export_id":export["export_id"],"bundle_id":export["bundle_id"]})
        link_artifact(child_table="conversations",child_id=export["conversation_id"],parent_table="brainlive_event_bundles_v1514",parent_id=export["bundle_id"],scope=scope,relation_type="exported_from_bundle")
    return {"version":VERSION,"person_id":person_id,"package_date":day,"live_session_id":live_session_id,"exports_created":len(exports),"exports":exports,"invalidated_conversations":[x[1] for x in invalidations]}

def run_brainlive_event_assembly(
    person_id: str,
    *,
    package_date: str | None = None,
    export_to_brain2: bool = True,
    limit_per_table: int = 5000,
    gap_minutes: int = 20,
    live_session_id: str | None = None,
) -> dict[str, Any]:
    ensure_event_assembler_schema()
    day,start,end=_period_bounds(package_date)
    run_id=stable_id("blassembly_v18",person_id,live_session_id or "day",day,now_iso())
    status="failed"; error=None; raw_rows=0; bundle_count=0; export_count=0
    try:
        raw=collect_live_raw_timeline(person_id,package_date=day,live_session_id=live_session_id,limit_per_table=limit_per_table)
        if raw.get("incomplete"):
            raise GovernanceError(f"assembly blocked by incomplete source collection: {raw.get('incomplete_reasons')}")
        raw_rows=int(raw["raw_rows"])
        bundles=assemble_event_bundles(person_id,package_date=day,raw_timeline=raw["timeline"],gap_minutes=gap_minutes,live_session_id=live_session_id)
        bundle_count=int(bundles["bundles_created"])
        if raw_rows and not bundle_count:
            raise GovernanceError("assembly produced zero bundles from non-empty timeline")
        exports={"exports_created":0}
        if export_to_brain2:
            exports=export_event_bundles_to_brain2(person_id,package_date=day,live_session_id=live_session_id)
            export_count=int(exports["exports_created"])
            if bundle_count and export_count!=bundle_count:
                raise GovernanceError("not every assembled bundle was exported")
        status="ok"
    except Exception as exc:
        error=str(exc)[:2000]
        raise
    finally:
        with connect() as con:
            upsert(con,"brainlive_event_assembly_runs_v1514",{"run_id":run_id,"person_id":person_id,"package_date":day,"period_start":start,"period_end":end,"raw_rows":raw_rows,"bundles":bundle_count,"exports":export_count,"status":status,"error_text":error,"created_at":now_iso(),"updated_at":now_iso()},"run_id")
            con.commit()
    return {"version":VERSION,"run_id":run_id,"person_id":person_id,"live_session_id":live_session_id,"package_date":day,"period_start":start,"period_end":end,"raw_rows":raw_rows,"bundles":bundle_count,"exports":export_count,"status":status}

def event_assembly_audit(person_id: str = "me", *, package_date: str | None = None) -> dict[str, Any]:
    ensure_event_assembler_schema()
    day, _, _ = _period_bounds(package_date)
    with connect() as con:
        counts: dict[str, int] = {}
        for table in ["brainlive_raw_timeline_v1514", "brainlive_event_bundles_v1514", "brainlive_brain2_event_exports_v1514", "brainlive_event_assembly_runs_v1514"]:
            try:
                if table == "brainlive_brain2_event_exports_v1514":
                    row = con.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE person_id=?", (person_id,)).fetchone()
                else:
                    row = con.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE person_id=? AND package_date=?", (person_id, day)).fetchone()
                counts[table] = int(row["n"] if row else 0)
            except Exception:
                counts[table] = 0
        latest = con.execute("SELECT * FROM brainlive_event_assembly_runs_v1514 WHERE person_id=? ORDER BY created_at DESC LIMIT 1", (person_id,)).fetchone()
    return {"version": VERSION, "person_id": person_id, "package_date": day, "counts": counts, "latest_run": dict(latest) if latest else None, "verdict": "ready" if counts.get("brainlive_event_bundles_v1514", 0) else "needs_live_data_or_assembly_run"}
