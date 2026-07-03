"""V19 visual evidence store.

Append-only visual/XR evidence tables used by the V19 live memory bridge.
The module is intentionally SDK-free: callers pass already-normalized contract
payloads and the store persists owner-scoped evidence rows for V18 governance.
"""
from __future__ import annotations

from typing import Any, Mapping

from .db import connect, init_db, insert_only, upsert, write_transaction
from .utils import json_dumps, new_id, now_iso, stable_id

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS visual_evidence_assets_v19 (
  visual_asset_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  asset_kind TEXT NOT NULL,
  uri TEXT,
  sha256 TEXT,
  frame_id TEXT,
  clip_id TEXT,
  captured_at TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v19_visual_assets_owner_session ON visual_evidence_assets_v19(person_id, live_session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_v19_visual_assets_sha ON visual_evidence_assets_v19(sha256);

CREATE TABLE IF NOT EXISTS visual_events_v19 (
  visual_event_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  entity_json TEXT DEFAULT '{}',
  observation_json TEXT DEFAULT '{}',
  place_json TEXT DEFAULT '{}',
  truth_level TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_refs_json TEXT DEFAULT '[]',
  provenance_json TEXT DEFAULT '{}',
  asset_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(asset_id) REFERENCES visual_evidence_assets_v19(visual_asset_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_v19_visual_events_owner_time ON visual_events_v19(person_id, occurred_at, created_at);
CREATE INDEX IF NOT EXISTS idx_v19_visual_events_session ON visual_events_v19(live_session_id, occurred_at);

CREATE TABLE IF NOT EXISTS world_entity_links_v19 (
  world_entity_link_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_refs_json TEXT DEFAULT '[]',
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_v19_world_links_unique ON world_entity_links_v19(person_id, entity_id, source_kind, source_id, relation);

CREATE TABLE IF NOT EXISTS scene_session_summaries_v19 (
  scene_summary_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  summary_start TEXT NOT NULL,
  summary_end TEXT,
  place_hint TEXT,
  map_quality REAL,
  summary_json TEXT NOT NULL,
  evidence_refs_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v19_scene_summaries_owner_session ON scene_session_summaries_v19(person_id, live_session_id, summary_start);

CREATE TABLE IF NOT EXISTS ui_interaction_outcomes_v19 (
  ui_outcome_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  ui_intent_id TEXT NOT NULL,
  delivery_id TEXT,
  event TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  local_track_state_json TEXT DEFAULT '{}',
  user_action_json TEXT DEFAULT '{}',
  source TEXT NOT NULL,
  evidence_refs_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v19_ui_outcomes_owner_time ON ui_interaction_outcomes_v19(person_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_v19_ui_outcomes_delivery ON ui_interaction_outcomes_v19(delivery_id);

CREATE TABLE IF NOT EXISTS brain2_spatial_routine_models (
  routine_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  entity_key TEXT NOT NULL,
  place_key TEXT NOT NULL,
  time_slot TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0.0,
  evidence_refs_json TEXT DEFAULT '[]',
  first_observed TEXT,
  last_observed TEXT,
  updated_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brain2_spatial_routine_owner ON brain2_spatial_routine_models(person_id, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_brain2_spatial_routine_key ON brain2_spatial_routine_models(person_id, entity_key, place_key, time_slot);

CREATE TABLE IF NOT EXISTS brain2_visual_task_models (
  task_model_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  task_key TEXT NOT NULL,
  step_json TEXT DEFAULT '{}',
  occurrence_count INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0.0,
  evidence_refs_json TEXT DEFAULT '[]',
  updated_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brain2_visual_task_owner ON brain2_visual_task_models(person_id, updated_at);

CREATE TABLE IF NOT EXISTS brain2_ui_preference_models (
  ui_pref_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  component TEXT NOT NULL,
  preference_json TEXT DEFAULT '{}',
  occurrence_count INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0.0,
  evidence_refs_json TEXT DEFAULT '[]',
  updated_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brain2_ui_pref_owner ON brain2_ui_preference_models(person_id, updated_at);
"""


def ensure_v19_visual_schema(db_path=None) -> None:
    init_db(db_path)
    with connect(db_path) as con:
        with write_transaction(con):
            con.executescript(SCHEMA)


def _owner(payload: Mapping[str, Any]) -> str:
    owner = str(payload.get("memory_owner_id") or payload.get("person_id") or "").strip()
    if not owner:
        raise ValueError("memory_owner_id is required for V19 visual memory writes")
    return owner


def _session(payload: Mapping[str, Any]) -> str:
    session = str(payload.get("live_session_id") or payload.get("session_id") or "").strip()
    if not session:
        raise ValueError("live_session_id/session_id is required")
    return session


def store_visual_event(payload: Mapping[str, Any], *, db_path=None) -> str:
    ensure_v19_visual_schema(db_path)
    owner, session = _owner(payload), _session(payload)
    now = now_iso()
    evidence = list(payload.get("evidence") or payload.get("evidence_refs") or [])
    asset_id = None
    first_asset = evidence[0] if evidence and isinstance(evidence[0], Mapping) else None
    with connect(db_path) as con:
        with write_transaction(con):
            if first_asset:
                asset_id = str(first_asset.get("asset_id") or stable_id("v19asset", owner, session, first_asset.get("sha256"), first_asset.get("frame_id"), first_asset.get("clip_id")))
                upsert(con, "visual_evidence_assets_v19", {
                    "visual_asset_id": asset_id, "person_id": owner, "live_session_id": session,
                    "asset_kind": str(first_asset.get("kind") or first_asset.get("asset_kind") or "frame"),
                    "uri": first_asset.get("uri") or first_asset.get("path"), "sha256": first_asset.get("sha256"),
                    "frame_id": first_asset.get("frame_id"), "clip_id": first_asset.get("clip_id"),
                    "captured_at": first_asset.get("captured_at") or payload.get("occurred_at"),
                    "metadata_json": json_dumps(dict(first_asset)), "created_at": now,
                }, "visual_asset_id")
            event_id = str(payload.get("visual_event_id") or new_id("v19vevt"))
            insert_only(con, "visual_events_v19", {
                "visual_event_id": event_id, "person_id": owner, "live_session_id": session,
                "event_type": str(payload.get("event_type") or "visual_event"),
                "occurred_at": str(payload.get("occurred_at") or now),
                "entity_json": json_dumps(payload.get("entity") or {}),
                "observation_json": json_dumps(payload.get("observation") or {}),
                "place_json": json_dumps(payload.get("place") or {}),
                "truth_level": str(payload.get("truth_level") or "observed"),
                "confidence": float(payload.get("confidence") if payload.get("confidence") is not None else 1.0),
                "evidence_refs_json": json_dumps(evidence),
                "provenance_json": json_dumps(payload.get("provenance") or {}),
                "asset_id": asset_id, "created_at": now,
            })
            return event_id


def store_scene_summary(payload: Mapping[str, Any], *, db_path=None) -> str:
    ensure_v19_visual_schema(db_path)
    owner, session = _owner(payload), _session(payload)
    now = now_iso()
    summary_id = str(payload.get("scene_summary_id") or stable_id("v19scene", owner, session, payload.get("summary_start") or payload.get("started_at") or now))
    with connect(db_path) as con:
        with write_transaction(con):
            upsert(con, "scene_session_summaries_v19", {
                "scene_summary_id": summary_id, "person_id": owner, "live_session_id": session,
                "summary_start": str(payload.get("summary_start") or payload.get("started_at") or now),
                "summary_end": payload.get("summary_end") or payload.get("ended_at"),
                "place_hint": payload.get("place_hint"), "map_quality": payload.get("map_quality"),
                "summary_json": json_dumps(payload.get("summary") or payload),
                "evidence_refs_json": json_dumps(payload.get("evidence_refs") or []), "created_at": now,
            }, "scene_summary_id")
    return summary_id


def store_ui_outcome(payload: Mapping[str, Any], *, db_path=None) -> str:
    ensure_v19_visual_schema(db_path)
    owner, session = _owner(payload), _session(payload)
    now = now_iso()
    outcome_id = str(payload.get("ui_outcome_id") or new_id("v19uiout"))
    with connect(db_path) as con:
        with write_transaction(con):
            insert_only(con, "ui_interaction_outcomes_v19", {
                "ui_outcome_id": outcome_id, "person_id": owner, "live_session_id": session,
                "ui_intent_id": str(payload.get("ui_intent_id") or ""), "delivery_id": payload.get("delivery_id"),
                "event": str(payload.get("event") or "displayed"), "observed_at": str(payload.get("observed_at") or now),
                "local_track_state_json": json_dumps(payload.get("local_track_state") or {}),
                "user_action_json": json_dumps(payload.get("user_action") or {}),
                "source": str(payload.get("source") or "xr"),
                "evidence_refs_json": json_dumps(payload.get("evidence_refs") or []), "created_at": now,
            })
    return outcome_id
