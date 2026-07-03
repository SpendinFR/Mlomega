from __future__ import annotations

"""V15 BrainLive: active short-horizon personal predictive layer.

BrainLive does not replace Brain2. Brain2 remains the deep/nightly consolidation
engine. BrainLive keeps a hot, auditable state of the present and connects it to
Brain2's existing people, relationship, pattern, forecast, open-loop, intervention
and clarification layers.

Core doctrine:
- observe the present as a personal world-state, not only as text;
- preload context before it is needed;
- predict H0/H1/H2 needs, actions, words, risks and opportunities;
- compare personal needs with world affordances, including vision;
- propose interventions only when expected value beats interruption cost;
- store predictions and outcomes so the system learns from real life;
- keep alternative hypotheses, evidence, counter-evidence and uncertainty.
"""

from dataclasses import dataclass
import os
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .db import connect, init_db, upsert, insert_only, write_transaction
from .governance_v18 import (
    GovernanceError, Scope, canonical_time, ensure_v18_schema, assert_live_session_owner,
    record_artifact_version, record_artifact_version_in_transaction, link_artifact, build_context_manifest, ContextItem,
)
from .integrity_v176 import new_id
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, sha256_file, stable_id
from .v18_legacy_forecasts import active_legacy_forecasts as _active_v14_forecasts
from .integrity_v176 import ContractValidationError, create_forecast, ensure_integrity_schema, quarantine, validate_brainlive_output

BRAINLIVE_VERSION = "15.1.0-brainlive-h0h1h2-vision-longitudinal-strict"

BRAINLIVE_TABLES = {
    "vision_frames",
    "vision_scene_observations",
    "vision_context_windows",
    "brainlive_sessions",
    "brainlive_turn_buffer",
    "brainlive_world_states",
    "brainlive_active_contexts",
    "brainlive_event_candidates",
    "brainlive_need_predictions",
    "brainlive_affordances",
    "brainlive_short_horizon_forecasts",
    "brainlive_analysis_runs",
    "brainlive_intervention_candidates",
    "brainlive_intervention_deliveries",
    "brainlive_prediction_outcomes",
    "brainlive_life_hypotheses",
    "brainlive_hypothesis_evidence",
    "brainlive_hypothesis_forecasts",
    "brainlive_user_disagreement_events",
    "brainlive_missed_opportunity_cards",
    "brainlive_routine_cards",
    "brainlive_routine_observations",
    "brainlive_longitudinal_comparisons",
    "brainlive_outcome_evaluations",
    "brainlive_affordance_matches",
    "brainlive_daily_scheduler_runs",
    "brainlive_offline_replay_bridges",
    "brainlive_nightly_consolidation_runs",
    "brainlive_contract_checks",
}

BRAINLIVE_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS vision_frames(
  frame_id TEXT PRIMARY KEY,
  source_asset_id TEXT,
  conversation_id TEXT,
  live_session_id TEXT,
  captured_at TEXT NOT NULL,
  image_path TEXT,
  image_sha256 TEXT,
  width INTEGER,
  height INTEGER,
  device_source TEXT,
  capture_mode TEXT DEFAULT 'manual',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vision_scene_observations(
  observation_id TEXT PRIMARY KEY,
  frame_id TEXT NOT NULL,
  live_session_id TEXT,
  conversation_id TEXT,
  model TEXT NOT NULL,
  scene_summary TEXT,
  location_hint TEXT,
  people_count INTEGER,
  spatial_context TEXT,
  social_context_hint TEXT,
  visible_text_json TEXT DEFAULT '[]',
  objects_json TEXT DEFAULT '[]',
  risks_json TEXT DEFAULT '[]',
  affordances_json TEXT DEFAULT '[]',
  possible_user_activities_json TEXT DEFAULT '[]',
  personal_relevance_json TEXT DEFAULT '{}',
  confidence REAL DEFAULT 0.0,
  raw_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(frame_id) REFERENCES vision_frames(frame_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS vision_context_windows(
  window_id TEXT PRIMARY KEY,
  live_session_id TEXT,
  conversation_id TEXT,
  start_time TEXT,
  end_time TEXT,
  dominant_location TEXT,
  stable_context_summary TEXT,
  people_presence_summary TEXT,
  environment_changes_json TEXT DEFAULT '[]',
  important_frames_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_sessions(
  live_session_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  session_title TEXT,
  active_location_hint TEXT,
  active_people_json TEXT DEFAULT '[]',
  active_conversation_id TEXT,
  current_mode TEXT DEFAULT 'unknown',
  h0_goal TEXT,
  h1_goal TEXT,
  h2_goal TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_turn_buffer(
  live_turn_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  conversation_id TEXT,
  timestamp_start TEXT,
  timestamp_end TEXT,
  speaker_label TEXT,
  speaker_person_id TEXT,
  speaker_confidence REAL DEFAULT 0.0,
  text_partial TEXT,
  text_final TEXT,
  asr_confidence REAL DEFAULT 0.0,
  is_final INTEGER DEFAULT 0,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(live_session_id) REFERENCES brainlive_sessions(live_session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS brainlive_world_states(
  world_state_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  state_time TEXT NOT NULL,
  where_am_i TEXT,
  who_is_active_json TEXT DEFAULT '[]',
  what_is_happening TEXT,
  probable_activity_json TEXT DEFAULT '[]',
  active_emotional_state TEXT,
  active_mode TEXT,
  audio_context_json TEXT DEFAULT '{}',
  visual_context_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(live_session_id) REFERENCES brainlive_sessions(live_session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS brainlive_active_contexts(
  active_context_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  active_people_json TEXT DEFAULT '[]',
  relationship_packs_json TEXT DEFAULT '[]',
  recent_turns_summary TEXT,
  recent_turns_json TEXT DEFAULT '[]',
  visual_context_json TEXT DEFAULT '{}',
  world_state_json TEXT DEFAULT '{}',
  open_loops_json TEXT DEFAULT '[]',
  pattern_cards_json TEXT DEFAULT '[]',
  interpersonal_loops_json TEXT DEFAULT '[]',
  forecast_watch_json TEXT DEFAULT '[]',
  personal_routines_json TEXT DEFAULT '[]',
  life_hypotheses_json TEXT DEFAULT '[]',
  intervention_policy_json TEXT DEFAULT '{}',
  risk_state_json TEXT DEFAULT '{}',
  loaded_at TEXT NOT NULL,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(live_session_id) REFERENCES brainlive_sessions(live_session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS brainlive_event_candidates(
  event_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_summary TEXT NOT NULL,
  source_turn_ids_json TEXT DEFAULT '[]',
  source_frame_ids_json TEXT DEFAULT '[]',
  related_person_ids_json TEXT DEFAULT '[]',
  urgency_score REAL DEFAULT 0.0,
  novelty_score REAL DEFAULT 0.0,
  tension_score REAL DEFAULT 0.0,
  relationship_relevance_score REAL DEFAULT 0.0,
  opportunity_score REAL DEFAULT 0.0,
  needs_llm_analysis INTEGER DEFAULT 1,
  status TEXT DEFAULT 'candidate',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(live_session_id) REFERENCES brainlive_sessions(live_session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS brainlive_need_predictions(
  need_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  event_id TEXT,
  person_id TEXT NOT NULL,
  need_label TEXT NOT NULL,
  need_type TEXT NOT NULL,
  horizon TEXT NOT NULL,
  why_now TEXT,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_affordances(
  affordance_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  frame_id TEXT,
  event_id TEXT,
  person_id TEXT NOT NULL,
  affordance_label TEXT NOT NULL,
  world_element TEXT,
  position_hint TEXT,
  personal_relevance TEXT,
  matched_need_id TEXT,
  personal_fit REAL DEFAULT 0.0,
  time_sensitivity REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_short_horizon_forecasts(
  forecast_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  event_id TEXT,
  person_id TEXT NOT NULL,
  horizon TEXT NOT NULL,
  forecast_type TEXT NOT NULL,
  predicted_need TEXT,
  predicted_action TEXT,
  predicted_words TEXT,
  predicted_emotion TEXT,
  predicted_risk TEXT,
  predicted_opportunity TEXT,
  if_intervene_future TEXT,
  if_silent_future TEXT,
  expected_gain REAL DEFAULT 0.0,
  probability REAL DEFAULT 0.0,
  confidence REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_analysis_runs(
  run_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  event_id TEXT,
  active_context_id TEXT,
  person_id TEXT NOT NULL,
  analysis_mode TEXT NOT NULL,
  model TEXT,
  prompt_context_json TEXT DEFAULT '{}',
  qwen_json TEXT DEFAULT '{}',
  latency_ms INTEGER,
  status TEXT NOT NULL,
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_intervention_candidates(
  candidate_id TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  event_id TEXT,
  run_id TEXT,
  person_id TEXT NOT NULL,
  message TEXT NOT NULL,
  intervention_type TEXT NOT NULL,
  recommended_timing TEXT NOT NULL,
  urgency REAL DEFAULT 0.0,
  confidence REAL DEFAULT 0.0,
  expected_gain REAL DEFAULT 0.0,
  risk_if_silent REAL DEFAULT 0.0,
  risk_if_said REAL DEFAULT 0.0,
  intrusion_score REAL DEFAULT 0.0,
  autonomy_risk REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'candidate',
  gate_decision TEXT DEFAULT 'undecided',
  gate_reason TEXT,
  cooldown_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_intervention_deliveries(
  delivery_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  delivered_text TEXT NOT NULL,
  delivered_at TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  feedback_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_prediction_outcomes(
  outcome_id TEXT PRIMARY KEY,
  live_session_id TEXT,
  forecast_id TEXT,
  candidate_id TEXT,
  person_id TEXT NOT NULL,
  observed_after TEXT NOT NULL,
  outcome_window TEXT,
  was_prediction_correct INTEGER,
  match_score REAL,
  user_feedback TEXT,
  lesson_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_life_hypotheses(
  hypothesis_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  hypothesis_type TEXT NOT NULL,
  statement TEXT NOT NULL,
  scope TEXT DEFAULT 'life',
  time_pattern TEXT,
  location_pattern TEXT,
  people_pattern_json TEXT DEFAULT '[]',
  trigger_contexts_json TEXT DEFAULT '[]',
  candidate_needs_json TEXT DEFAULT '[]',
  candidate_emotions_json TEXT DEFAULT '[]',
  candidate_risks_json TEXT DEFAULT '[]',
  candidate_opportunities_json TEXT DEFAULT '[]',
  predicted_next_window TEXT,
  confidence REAL DEFAULT 0.0,
  evidence_count INTEGER DEFAULT 0,
  counter_evidence_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_hypothesis_evidence(
  evidence_id TEXT PRIMARY KEY,
  hypothesis_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  evidence_role TEXT NOT NULL,
  source_table TEXT,
  source_id TEXT,
  evidence_text TEXT NOT NULL,
  weight REAL DEFAULT 0.5,
  observed_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(hypothesis_id) REFERENCES brainlive_life_hypotheses(hypothesis_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS brainlive_hypothesis_forecasts(
  hypothesis_forecast_id TEXT PRIMARY KEY,
  hypothesis_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  expected_window_start TEXT,
  expected_window_end TEXT,
  forecast_statement TEXT NOT NULL,
  watch_signals_json TEXT DEFAULT '[]',
  possible_interventions_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(hypothesis_id) REFERENCES brainlive_life_hypotheses(hypothesis_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS brainlive_user_disagreement_events(
  disagreement_id TEXT PRIMARY KEY,
  live_session_id TEXT,
  candidate_id TEXT,
  person_id TEXT NOT NULL,
  system_claim TEXT NOT NULL,
  user_response TEXT NOT NULL,
  possible_meanings_json TEXT DEFAULT '[]',
  next_policy TEXT DEFAULT 'observe',
  watch_next_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_missed_opportunity_cards(
  missed_id TEXT PRIMARY KEY,
  live_session_id TEXT,
  person_id TEXT NOT NULL,
  situation_summary TEXT NOT NULL,
  missed_intervention TEXT,
  why_missed TEXT,
  future_rule_learned TEXT,
  evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_nightly_consolidation_runs(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  run_date TEXT NOT NULL,
  status TEXT NOT NULL,
  brain2_period TEXT DEFAULT 'day',
  live_sessions_json TEXT DEFAULT '[]',
  counts_json TEXT DEFAULT '{}',
  notes TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_contract_checks(
  check_id TEXT PRIMARY KEY,
  check_name TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brainlive_routine_cards(
  routine_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  routine_label TEXT NOT NULL,
  routine_kind TEXT DEFAULT 'observed_pattern',
  temporal_signature_json TEXT DEFAULT '{}',
  location_signature_json TEXT DEFAULT '{}',
  people_signature_json TEXT DEFAULT '[]',
  action_signature_json TEXT DEFAULT '{}',
  trigger_contexts_json TEXT DEFAULT '[]',
  inferred_needs_json TEXT DEFAULT '[]',
  inferred_emotions_json TEXT DEFAULT '[]',
  preferred_affordances_json TEXT DEFAULT '[]',
  observed_outcomes_json TEXT DEFAULT '[]',
  support_count INTEGER DEFAULT 0,
  counter_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.0,
  interpretation_source TEXT DEFAULT 'statistical_or_llm',
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_routine_observations(
  observation_id TEXT PRIMARY KEY,
  routine_id TEXT,
  person_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT,
  observed_at TEXT,
  weekday INTEGER,
  hour_bucket INTEGER,
  location_hint TEXT,
  active_people_json TEXT DEFAULT '[]',
  activity_json TEXT DEFAULT '{}',
  visual_context_json TEXT DEFAULT '{}',
  audio_context_json TEXT DEFAULT '{}',
  outcome_hint_json TEXT DEFAULT '{}',
  evidence_weight REAL DEFAULT 0.5,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_longitudinal_comparisons(
  comparison_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  comparison_scope TEXT NOT NULL,
  anchor_signature_json TEXT DEFAULT '{}',
  compared_periods_json TEXT DEFAULT '[]',
  repeated_elements_json TEXT DEFAULT '[]',
  changed_elements_json TEXT DEFAULT '[]',
  generated_hypotheses_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  llm_run_id TEXT,
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_outcome_evaluations(
  evaluation_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  forecast_id TEXT,
  candidate_id TEXT,
  live_session_id TEXT,
  prediction_json TEXT DEFAULT '{}',
  observed_window_json TEXT DEFAULT '{}',
  evaluation_json TEXT DEFAULT '{}',
  match_score REAL,
  verdict TEXT DEFAULT 'unscored',
  learning_update_json TEXT DEFAULT '{}',
  evaluation_source TEXT DEFAULT 'llm_required',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_affordance_matches(
  match_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  frame_id TEXT,
  need_id TEXT,
  routine_id TEXT,
  hypothesis_id TEXT,
  world_element TEXT,
  affordance_label TEXT,
  personal_fit REAL DEFAULT 0.0,
  time_sensitivity REAL DEFAULT 0.0,
  reason_json TEXT DEFAULT '{}',
  intervention_candidate_json TEXT DEFAULT '{}',
  match_source TEXT DEFAULT 'llm_required',
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_daily_scheduler_runs(
  run_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  run_date TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  tasks_json TEXT DEFAULT '[]',
  results_json TEXT DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error_text TEXT
);
CREATE TABLE IF NOT EXISTS brainlive_offline_replay_bridges(
  replay_bridge_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT,
  replay_scope TEXT DEFAULT 'day',
  brain2_replay_refs_json TEXT DEFAULT '[]',
  brainlive_prediction_refs_json TEXT DEFAULT '[]',
  summary_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'prepared',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vision_frames_session_time ON vision_frames(live_session_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_vision_obs_session ON vision_scene_observations(live_session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_sessions_person_status ON brainlive_sessions(person_id, status, started_at);
CREATE INDEX IF NOT EXISTS idx_bl_turns_session_time ON brainlive_turn_buffer(live_session_id, timestamp_start, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_world_session_time ON brainlive_world_states(live_session_id, state_time);
CREATE INDEX IF NOT EXISTS idx_bl_context_session ON brainlive_active_contexts(live_session_id, loaded_at);
CREATE INDEX IF NOT EXISTS idx_bl_events_session_status ON brainlive_event_candidates(live_session_id, status, urgency_score);
CREATE INDEX IF NOT EXISTS idx_bl_need_session ON brainlive_need_predictions(live_session_id, horizon, status);
CREATE INDEX IF NOT EXISTS idx_bl_affordances_session ON brainlive_affordances(live_session_id, status, time_sensitivity);
CREATE INDEX IF NOT EXISTS idx_bl_forecasts_person ON brainlive_short_horizon_forecasts(person_id, horizon, status, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_candidates_person ON brainlive_intervention_candidates(person_id, status, gate_decision, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_hypotheses_person ON brainlive_life_hypotheses(person_id, hypothesis_type, status, confidence);
CREATE INDEX IF NOT EXISTS idx_bl_routines_person ON brainlive_routine_cards(person_id, routine_kind, status, confidence);
CREATE INDEX IF NOT EXISTS idx_bl_routine_obs_person_time ON brainlive_routine_observations(person_id, observed_at, weekday, hour_bucket);
CREATE INDEX IF NOT EXISTS idx_bl_outcome_eval_person ON brainlive_outcome_evaluations(person_id, verdict, created_at);
CREATE INDEX IF NOT EXISTS idx_bl_affordance_matches_session ON brainlive_affordance_matches(live_session_id, status, time_sensitivity);
"""

BRAINLIVE_LLM_SCHEMA: dict[str, Any] = {
    "world_state": {
        "where_am_i": "",
        "what_is_happening": "",
        "active_mode": "conversation|work|routine|transition|social|rest|unknown|other",
        "probable_activity": [],
        "active_emotional_state": "",
        "confidence": 0.0,
    },
    "events": [
        {
            "event_type": "relationship_tension|opportunity|need_emerging|routine_active|risk|idea_capture|decision_window|affordance_match|fatigue_flow_conflict|unknown",
            "summary": "",
            "urgency_score": 0.0,
            "novelty_score": 0.0,
            "tension_score": 0.0,
            "relationship_relevance_score": 0.0,
            "opportunity_score": 0.0,
            "evidence": [],
            "counter_evidence": [],
        }
    ],
    "need_predictions": [
        {"need_label": "", "need_type": "social|physical|cognitive|emotional|professional|routine|safety|creative|unknown", "horizon": "H0|H1|H2", "why_now": "", "confidence": 0.0, "evidence": [], "counter_evidence": []}
    ],
    "affordances": [
        {"affordance_label": "", "world_element": "", "position_hint": "", "personal_relevance": "", "matched_need_label": "", "personal_fit": 0.0, "time_sensitivity": 0.0, "confidence": 0.0, "evidence": [], "counter_evidence": []}
    ],
    "forecasts": [
        {"horizon": "H0|H1|H2", "forecast_type": "need|action|words|emotion|risk|opportunity|trajectory", "predicted_need": "", "predicted_action": "", "predicted_words": "", "predicted_emotion": "", "predicted_risk": "", "predicted_opportunity": "", "if_intervene_future": "", "if_silent_future": "", "expected_gain": 0.0, "probability": 0.0, "confidence": 0.0, "evidence": [], "counter_evidence": []}
    ],
    "life_hypotheses": [
        {"hypothesis_type": "routine|preference|need|avoidance|flow|fatigue|relationship|weekly_cycle|emotional_cycle|opportunity|contradiction|unknown", "statement": "", "scope": "short|day|week|life", "time_pattern": "", "location_pattern": "", "trigger_contexts": [], "candidate_needs": [], "candidate_emotions": [], "candidate_risks": [], "candidate_opportunities": [], "predicted_next_window": "", "confidence": 0.0, "evidence": [], "counter_evidence": []}
    ],
    "interventions": [
        {"message": "", "intervention_type": "say_now|watch|ask_question|reminder|idea_capture|affordance_hint|relationship_move|energy_guard|silence|other", "recommended_timing": "now|soon|after_pause|later|watch_only", "urgency": 0.0, "confidence": 0.0, "expected_gain": 0.0, "risk_if_silent": 0.0, "risk_if_said": 0.0, "intrusion_score": 0.0, "autonomy_risk": 0.0, "cooldown_key": "", "evidence": [], "counter_evidence": []}
    ],
    "watch_next": [],
    "notes_for_brain2": [],
}


def _clamp(v: Any, default: float = 0.0) -> float:
    """Legacy compatibility clamp that never turns NaN into certainty.

    Strict V17.6 LLM contracts reject invalid numeric values before persistence.
    This helper remains for historical/manual paths and falls back explicitly.
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return max(0.0, min(1.0, x))


def _safe_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return json_loads(str(value) if value is not None else None, default)


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    try:
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _compact(rows: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        nr: dict[str, Any] = {}
        for k, v in r.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > 1200:
                nr[k] = v[:1200] + "…"
            else:
                nr[k] = v
        out.append(nr)
    return out


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = _one(con, "SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1")
    return str(row["person_id"]) if row and row.get("person_id") else "me"


def ensure_brainlive_schema() -> None:
    ensure_v18_schema()
    init_db()
    with connect() as con:
        con.executescript(BRAINLIVE_SCHEMA)
        now = now_iso()
        for table in sorted(BRAINLIVE_TABLES):
            upsert(con, "brainlive_contract_checks", {
                "check_id": stable_id("blcheck", table),
                "check_name": f"exists:{table}",
                "status": "ok",
                "details_json": json_dumps({"table": table, "version": BRAINLIVE_VERSION}),
                "created_at": now,
            }, "check_id")
        con.commit()
    # Additive V17.6 migration: lifecycle/contract safeguards must exist before
    # any live writer persists a forecast or outcome.
    ensure_integrity_schema()


def audit_brainlive(*, persist: bool = True) -> dict[str, Any]:
    ensure_brainlive_schema()
    with connect() as con:
        existing = set()
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            existing.add(row["name"])
        missing = sorted(BRAINLIVE_TABLES - existing)
        counts = {}
        for table in sorted(BRAINLIVE_TABLES & existing):
            try:
                counts[table] = int(con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            except Exception:
                counts[table] = None
    return {"version": BRAINLIVE_VERSION, "status": "ok" if not missing else "missing", "missing_tables": missing, "counts": counts}


def start_live_session(*, person_id: str | None = None, title: str | None = None, active_people: list[str] | None = None, location_hint: str | None = None, mode: str = "unknown") -> dict[str, Any]:
    if not person_id:
        raise GovernanceError("V18 live session creation requires explicit person_id")
    ensure_brainlive_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        session_id = stable_id("blsess", person_id, now, title or "live", uuid4().hex)
        upsert(con, "brainlive_sessions", {
            "live_session_id": session_id,
            "person_id": person_id,
            "started_at": now,
            "ended_at": None,
            "status": "active",
            "session_title": title,
            "active_location_hint": location_hint,
            "active_people_json": json_dumps(active_people or []),
            "active_conversation_id": None,
            "current_mode": mode,
            "h0_goal": "percevoir les signaux immédiats et fenêtres fugaces",
            "h1_goal": "anticiper besoins/actions/mots dans les prochaines minutes",
            "h2_goal": "surveiller routines, décisions et trajectoires de la journée",
            "metadata_json": json_dumps({"created_by": "brainlive_v15"}),
            "created_at": now,
            "updated_at": now,
        }, "live_session_id")
        con.commit()
    return {"live_session_id": session_id, "person_id": person_id, "status": "active"}


def end_live_session(live_session_id: str, *, notes: str | None = None) -> dict[str, Any]:
    ensure_brainlive_schema()
    now = now_iso()
    with connect() as con:
        con.execute("UPDATE brainlive_sessions SET status='ended', ended_at=?, updated_at=? WHERE live_session_id=?", (now, now, live_session_id))
        con.commit()
    return {"live_session_id": live_session_id, "ended_at": now, "notes": notes}


def ingest_live_turn(
    live_session_id: str,
    text: str,
    *,
    speaker_label: str | None = None,
    speaker_person_id: str | None = None,
    speaker_confidence: float = 0.0,
    is_final: bool = True,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert/revise one source-addressable live turn.

    V17.4 derived IDs from second+text and overwrote real repeated utterances.
    V18 maps a retryable source identity to one mutable live representation,
    while distinct occurrences get distinct IDs even when their words match.
    """
    ensure_brainlive_schema()
    if not isinstance(text, str) or not text.strip():
        raise GovernanceError("live turn text is required")
    metadata = dict(metadata or {})
    if timestamp_start is None:
        if os.environ.get("MLOMEGA_ALLOW_UNTIMED_LEGACY", "false").lower() not in {"1","true","yes"}:
            raise GovernanceError("timestamp_start is mandatory for V18 live turns")
        timestamp_start = now_iso()
    from .integrity_v176 import iso_utc, parse_iso_utc
    start_at = iso_utc(parse_iso_utc(timestamp_start))
    end_at = iso_utc(parse_iso_utc(timestamp_end)) if timestamp_end else start_at
    if parse_iso_utc(end_at) < parse_iso_utc(start_at):
        raise GovernanceError("live turn end precedes start")
    now = now_iso()
    source_event_id = metadata.get("event_id") or metadata.get("source_event_id")
    source_part = metadata.get("segment_id")
    if source_part is None:
        source_part = metadata.get("raw_turn_index")
    # Source-addressable retries update the same logical turn.  A lack of a
    # source identity is permitted only for manual input and gets a UUID so it
    # can never collide with another genuine repetition.
    source_identity = (
        stable_id("v18turnsource", live_session_id, source_event_id, source_part, start_at)
        if source_event_id is not None else f"manual:{uuid4().hex}"
    )
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        # A stopped/post-stop session is immutable.  Late workers must retry
        # against an explicitly resumed/new session; they may never silently
        # append evidence to a closed historical scene.
        if str(sess.get("status") or "") != "active":
            raise GovernanceError(f"cannot ingest turn into non-active live session: {live_session_id}")
        person_id = str(sess["person_id"])
        existing = con.execute("SELECT * FROM v18_turn_source_map WHERE source_identity=?", (source_identity,)).fetchone()
        if existing:
            if str(existing["live_session_id"]) != live_session_id or str(existing["person_id"]) != person_id:
                raise GovernanceError("source identity was already bound to a different scope")
            turn_id = str(existing["live_turn_id"])
        else:
            turn_id = new_id("blturn")
        row = {
            "live_turn_id": turn_id,
            "live_session_id": live_session_id,
            "conversation_id": sess.get("active_conversation_id"),
            "timestamp_start": start_at,
            "timestamp_end": end_at,
            "speaker_label": speaker_label,
            "speaker_person_id": speaker_person_id,
            "speaker_confidence": _clamp(speaker_confidence),
            "text_partial": None if is_final else text,
            "text_final": text if is_final else None,
            "asr_confidence": _clamp(metadata.get("asr_confidence", 0.0)),
            "is_final": 1 if is_final else 0,
            "metadata_json": json_dumps({**metadata, "source_identity": source_identity, "time_quality": "canonical"}),
            "created_at": now,
        }
        # This is a mutable *representation* of an immutable source.  The
        # source mapping prevents a retry from becoming a second fact; a final
        # transcript deliberately supersedes its earlier partial state.
        upsert(con, "brainlive_turn_buffer", row, "live_turn_id")
        con.execute(
            """INSERT INTO v18_turn_source_map(source_identity,live_session_id,person_id,live_turn_id,state,source_event_id,occurred_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_identity) DO UPDATE SET live_turn_id=excluded.live_turn_id,state=excluded.state,updated_at=excluded.updated_at""",
            (source_identity, live_session_id, person_id, turn_id, "final" if is_final else "partial", str(source_event_id) if source_event_id is not None else None, start_at, now, now),
        )
        con.commit()
    record_artifact_version(artifact_table="brainlive_turn_buffer", artifact_id=turn_id, identity_key=source_identity, scope=Scope(person_id=person_id, live_session_id=live_session_id, mode="live"), source_payload={"text":text,"start":start_at,"end":end_at,"metadata":metadata}, metadata={"is_final":is_final})
    return {"live_turn_id": turn_id, "live_session_id": live_session_id, "source_identity": source_identity, "revised": bool(existing)}

def ingest_vision_frame(
    image_path: str | Path,
    *,
    live_session_id: str | None = None,
    conversation_id: str | None = None,
    captured_at: str | None = None,
    device_source: str | None = None,
    observation: dict[str, Any] | None = None,
    model: str = "manual_or_external_vlm",
    source_event_id: str | None = None,
    con: Any | None = None,
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Record one source-addressable vision occurrence exactly once.

    V18.1 allocated random frame/asset/source IDs before checking whether the
    source had already been seen.  A retry therefore created visual duplicates
    and then collided when the immutable sensor event was written.  V18.4 uses
    one stable occurrence map and lets the caller participate in one transaction
    with the VLM projection/sensor event.
    """
    if not schema_ready:
        ensure_brainlive_schema()
    from .v18_runtime_hardening import (
        complete_vision_occurrence,
        ensure_runtime_hardening_schema,
        reserve_vision_occurrence,
        vision_occurrence_key,
    )
    if not schema_ready:
        ensure_runtime_hardening_schema()
    p = Path(image_path).expanduser().resolve()
    now = now_iso()
    if captured_at is None:
        if os.environ.get("MLOMEGA_ALLOW_UNTIMED_LEGACY", "false").lower() not in {"1","true","yes"}:
            raise GovernanceError("captured_at is mandatory for V18 vision frames")
        captured_at = now
    from .integrity_v176 import iso_utc, parse_iso_utc
    captured_at = iso_utc(parse_iso_utc(captured_at))
    sha = sha256_file(p) if p.exists() and p.is_file() else "missing"

    def _write(tx: Any) -> dict[str, Any]:
        person_id: str | None = None
        if live_session_id:
            sess = _one(tx, "SELECT person_id,status FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
            if not sess:
                raise GovernanceError("vision frame references missing live session")
            if str(sess.get("status") or "") != "active":
                raise GovernanceError("cannot ingest vision frame into non-active live session")
            person_id = str(sess["person_id"])
        # A direct/manual frame without session is still isolated by a synthetic
        # explicit owner.  It cannot be used by live semantic validation until it
        # has a real session scope.
        owner = person_id or "unscoped_manual"
        occurrence_key = vision_occurrence_key(
            live_session_id=live_session_id,
            source_event_id=source_event_id,
            captured_at=captured_at,
            source_sha256=sha,
            source_path=str(p),
        )
        frame_id = stable_id("vframe", occurrence_key)
        asset_id = stable_id("assetvision", occurrence_key)
        source_item_id = stable_id("srcvision", occurrence_key)
        reserved = reserve_vision_occurrence(
            tx,
            occurrence_key=occurrence_key,
            person_id=owner,
            live_session_id=live_session_id,
            source_event_id=source_event_id,
            source_sha256=sha,
            captured_at=captured_at,
            source_path=str(p),
            frame_id=frame_id,
            asset_id=asset_id,
            source_item_id=source_item_id,
        )
        frame_id = str(reserved["frame_id"])
        asset_id = str(reserved["asset_id"])
        source_item_id = str(reserved["source_item_id"])
        obs_id = reserved.get("observation_id")
        if reserved.get("reused"):
            exists = tx.execute("SELECT 1 FROM vision_frames WHERE frame_id=?", (frame_id,)).fetchone()
            if not exists:
                raise GovernanceError("vision occurrence map references a missing frame")
            # A first VLM attempt may have failed.  Later success is an explicit
            # enrichment of the same immutable frame, never a new visual fact.
            if observation and not obs_id:
                obs_id = _store_vision_observation(
                    tx, frame_id=frame_id, live_session_id=live_session_id,
                    conversation_id=conversation_id, observation=observation,
                    model=model, now=now,
                )
                complete_vision_occurrence(tx, occurrence_key=occurrence_key, observation_id=obs_id)
            return {
                "frame_id": frame_id, "observation_id": obs_id,
                "source_asset_id": asset_id, "source_item_id": source_item_id,
                "captured_at": captured_at, "occurrence_key": occurrence_key,
                "reused": True,
            }

        insert_only(tx, "raw_assets", {
            "asset_id": asset_id, "type": "vision_frame", "path": str(p), "sha256": sha,
            "captured_at": captured_at, "source": device_source or "unknown",
            "metadata_json": json_dumps({"live_session_id": live_session_id, "conversation_id": conversation_id, "source_event_id": source_event_id, "occurrence_key": occurrence_key}),
            "created_at": now,
        }, on_conflict="error")
        insert_only(tx, "vision_frames", {
            "frame_id": frame_id, "source_asset_id": asset_id, "conversation_id": conversation_id,
            "live_session_id": live_session_id, "captured_at": captured_at, "image_path": str(p),
            "image_sha256": sha, "width": None, "height": None, "device_source": device_source,
            "capture_mode": "source_event", "metadata_json": json_dumps({"source_asset_id": asset_id, "source_event_id": source_event_id, "occurrence_key": occurrence_key}),
            "created_at": now,
        }, on_conflict="error")
        if observation:
            obs_id = _store_vision_observation(
                tx, frame_id=frame_id, live_session_id=live_session_id,
                conversation_id=conversation_id, observation=observation,
                model=model, now=now,
            )
        summary = (observation or {}).get("scene_summary") or (observation or {}).get("summary") or f"Vision frame captured: {p.name}"
        insert_only(tx, "source_items", {
            "source_item_id": source_item_id, "source_type": "vision_frame", "external_id": frame_id,
            "conversation_id": conversation_id, "turn_id": None, "source_asset_id": asset_id,
            "author_person_id": None, "channel": "vision", "direction": "observed",
            "title": f"Vision context {captured_at}", "content_text": summary, "content_sha256": sha,
            "captured_at": captured_at, "metadata_json": json_dumps({"live_session_id": live_session_id, "observation_id": obs_id, "source_event_id": source_event_id, "occurrence_key": occurrence_key}),
            "created_at": now,
        }, on_conflict="error")
        if conversation_id:
            insert_only(tx, "lifestream_segments", {
                "segment_id": stable_id("segvision", occurrence_key), "conversation_id": conversation_id,
                "turn_id": None, "source_item_id": source_item_id, "source_asset_id": asset_id,
                "segment_kind": "visual_context", "channel": "vision", "speaker_person_id": None,
                "start_s": None, "end_s": None, "captured_start": captured_at, "captured_end": captured_at,
                "transcript_text": None, "observed_summary": summary,
                "importance_score": _clamp((observation or {}).get("importance"), 0.5),
                "novelty_score": _clamp((observation or {}).get("novelty"), 0.5),
                "density_score": 0.4, "keep_level": "context", "compression_status": "raw_kept",
                "metadata_json": json_dumps({"frame_id": frame_id, "observation_id": obs_id, "live_session_id": live_session_id}), "created_at": now,
            }, on_conflict="error")
        complete_vision_occurrence(tx, occurrence_key=occurrence_key, observation_id=obs_id)
        if person_id:
            record_artifact_version_in_transaction(
                tx, artifact_table="vision_frames", artifact_id=frame_id, identity_key=occurrence_key,
                scope=Scope(person_id=person_id, live_session_id=live_session_id, mode="live"),
                source_payload={"asset": asset_id, "sha256": sha, "captured_at": captured_at, "source_event_id": source_event_id},
                metadata={"observation_id": obs_id},
            )
        return {
            "frame_id": frame_id, "observation_id": obs_id, "source_asset_id": asset_id,
            "source_item_id": source_item_id, "captured_at": captured_at,
            "occurrence_key": occurrence_key, "reused": False,
        }

    if con is not None:
        return _write(con)
    with connect() as own_con, write_transaction(own_con):
        return _write(own_con)

def _store_vision_observation(con, *, frame_id: str, live_session_id: str | None, conversation_id: str | None, observation: dict[str, Any], model: str, now: str) -> str:
    obs_id = stable_id("vobs", frame_id, model, observation)
    upsert(con, "vision_scene_observations", {
        "observation_id": obs_id,
        "frame_id": frame_id,
        "live_session_id": live_session_id,
        "conversation_id": conversation_id,
        "model": model,
        "scene_summary": observation.get("scene_summary") or observation.get("summary") or observation.get("scene"),
        "location_hint": observation.get("location_hint") or observation.get("place"),
        "people_count": observation.get("people_count"),
        "spatial_context": observation.get("spatial_context"),
        "social_context_hint": observation.get("social_context_hint"),
        "visible_text_json": json_dumps(observation.get("visible_text") or []),
        "objects_json": json_dumps(observation.get("objects") or observation.get("visible_objects") or []),
        "risks_json": json_dumps(observation.get("risks") or []),
        "affordances_json": json_dumps(observation.get("affordances") or observation.get("available_affordances") or []),
        "possible_user_activities_json": json_dumps(observation.get("possible_user_activities") or []),
        "personal_relevance_json": json_dumps(observation.get("personal_relevance") or {}),
        "confidence": _clamp(observation.get("confidence"), 0.0),
        "raw_json": json_dumps(observation),
        "created_at": now,
    }, "observation_id")
    return obs_id


def _recent_visual_context(con, live_session_id: str, limit: int = 6) -> dict[str, Any]:
    obs = _many(con, "SELECT * FROM vision_scene_observations WHERE live_session_id=? ORDER BY created_at DESC LIMIT ?", (live_session_id, limit))
    frames = _many(con, "SELECT * FROM vision_frames WHERE live_session_id=? ORDER BY captured_at DESC LIMIT ?", (live_session_id, limit))
    return {"recent_observations": _compact(obs, limit), "recent_frames": _compact(frames, limit)}


def _collect_brain2_context(con, *, person_id: str, active_people: list[str], limit: int = 20) -> dict[str, Any]:
    people = active_people or []
    ctx: dict[str, Any] = {
        "person_id": person_id,
        "active_people": people,
        "self_model": _compact(_many(con, "SELECT * FROM self_model_dimensions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "memory_cards": _compact(_many(con, "SELECT * FROM memory_cards WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_predictions": _compact(_many(con, "SELECT * FROM predictions WHERE person_id=? AND status IN ('open','active','watch') ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
        "future_scenarios": _compact(_many(con, "SELECT * FROM future_scenarios WHERE person_id=? AND status IN ('open','active') ORDER BY probability DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "trajectory_warnings": _compact(_many(con, "SELECT * FROM trajectory_warnings WHERE person_id=? AND status IN ('open','active') ORDER BY probability DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_pattern_cards": _compact(_many(con, "SELECT * FROM v14_pattern_mirror_cards WHERE person_id=? AND status='open' ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_forecasts": _compact(_active_v14_forecasts(con, person_id, "v14_trajectory_forecasts", limit), limit),
        "v14_forecast_watch_queue": _compact(_active_v14_forecasts(con, person_id, "v14_forecast_watch_queue", limit), limit),
        "v14_open_loops": _compact(_many(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? AND current_status IN ('open','active','pending','watching') ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_active_questions": _compact(_many(con, "SELECT * FROM v14_5_active_questions WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_next_best_actions": _compact(_many(con, "SELECT * FROM v14_5_next_best_actions WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_interpersonal_loops": _compact(_many(con, "SELECT * FROM v14_6_interpersonal_loop_cards WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_relationship_models": _compact(_many(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_social_aftereffects": _compact(_many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? AND status IN ('open','active') ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_intervention_suggestions": _compact(_many(con, "SELECT * FROM v14_6_intervention_suggestions WHERE person_id=? ORDER BY confidence DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_intervention_policy": _compact(_many(con, "SELECT * FROM v14_7_intervention_policies WHERE person_id=? LIMIT 1", (person_id,)), 1),
        "v14_intervention_feedback": _compact(_many(con, "SELECT * FROM v14_7_intervention_feedback WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
        "v14_clarifications": _compact(_many(con, "SELECT * FROM v14_8_clarification_items WHERE person_id=? AND status IN ('queued','watching','needs_followup') ORDER BY priority DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
        "brainlive_life_hypotheses": _compact(_many(con, "SELECT * FROM brainlive_life_hypotheses WHERE person_id=? AND status='active' ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v17_global_life_patterns": _compact(_many(con, "SELECT * FROM brain2_global_life_patterns_v17 WHERE person_id=? AND status IN ('candidate','confirmed','active') ORDER BY confidence DESC, recurrence_count DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "v17_recent_observed_cases": _compact(_many(con, "SELECT * FROM brain2_observed_cases_v17 WHERE person_id=? AND status='active' ORDER BY COALESCE(observed_at, created_at) DESC LIMIT ?", (person_id, limit)), limit),
    }
    # V15.9: enrich BrainLive with the full Brain2 personal operating model.
    # This covers routines, places, actions, speech/expression habits, emotions,
    # needs, expectations, preferences, relationships, future hooks and policies.
    # It does not replace Brain2; it packages Brain2 for live use.
    try:
        from .brainlive_personal_model_v15_9 import latest_live_personal_model, collect_brain2_life_feed
        latest = latest_live_personal_model(person_id)
        if latest:
            ctx["brain2_live_personal_model"] = latest
        else:
            ctx["brain2_live_personal_model"] = {
                "status": "raw_feed_no_export_yet",
                "raw_feed": collect_brain2_life_feed(person_id, active_people=people, limit=max(15, limit)),
                "live_ready": {"llm_required": True, "reason": "run brainlive-personal-model-build or nightly bridge"},
            }
    except Exception as exc:
        ctx["brain2_live_personal_model"] = {"status": "unavailable", "error": str(exc)}
    # V15.10: add the canonical Brain2 Life Model if compiled. This is now the
    # preferred live-ready source for routines, places, needs, expressions,
    # contextual self, prediction hooks and affordance preferences.
    try:
        from .brain2_life_model_v15_10 import latest_canonical_life_model
        canonical = latest_canonical_life_model(person_id)
        if canonical:
            ctx["brain2_canonical_life_model"] = canonical
        else:
            ctx["brain2_canonical_life_model"] = {"status": "missing", "reason": "run brain2-life-model-build or nightly scheduler"}
        try:
            from .brain2_life_model_updater_v15_13 import latest_life_model_strata
            ctx["brain2_life_model_strata_v1513"] = latest_life_model_strata(person_id)
        except Exception as exc:
            ctx["brain2_life_model_strata_v1513"] = {"status": "unavailable", "error": str(exc)}
    except Exception as exc:
        ctx["brain2_canonical_life_model"] = {"status": "unavailable", "error": str(exc)}

    if people:
        rel_pack: list[dict[str, Any]] = []
        for other in people[:8]:
            rel_pack.extend(_many(con, """SELECT * FROM v14_6_relationship_state_models
                WHERE person_id=? AND (known_person_id=? OR person_hint=? OR person_hint LIKE ?)
                ORDER BY confidence DESC, updated_at DESC LIMIT 5""", (person_id, other, other, f"%{other}%")))
            rel_pack.extend(_many(con, """SELECT * FROM v14_5_people_context_profiles
                WHERE person_id=? AND (known_person_id=? OR person_hint=? OR person_hint LIKE ? OR speaker_label=?)
                ORDER BY confidence DESC, updated_at DESC LIMIT 5""", (person_id, other, other, f"%{other}%", other)))
        ctx["active_relationship_packs"] = _compact(rel_pack, 30)
    return ctx


def build_active_context(live_session_id: str, *, active_people: list[str] | None = None, refresh_minutes: int = 10, limit: int = 20) -> dict[str, Any]:
    ensure_brainlive_schema()
    now = now_iso()
    with connect() as con:
        sess = _one(con, "SELECT * FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,))
        if not sess:
            raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
        person_id = sess["person_id"]
        turns = _many(con, "SELECT * FROM brainlive_turn_buffer WHERE live_session_id=? ORDER BY created_at DESC LIMIT 40", (live_session_id,))
        recent_people: list[str] = []
        for t in turns:
            speaker_person_id = t.get("speaker_person_id")
            if not speaker_person_id:
                continue
            try:
                speaker_confidence = float(t.get("speaker_confidence") or 0.0)
            except (TypeError, ValueError):
                speaker_confidence = 0.0
            if speaker_confidence >= 0.65 and speaker_person_id not in recent_people:
                recent_people.append(str(speaker_person_id))
        known_people = active_people or _safe_json(sess.get("active_people_json"), []) or recent_people[:3]
        turns_chrono = list(reversed(turns))
        recent_summary = "\n".join([f"{t.get('speaker_label') or t.get('speaker_person_id') or '?'}: {t.get('text_final') or t.get('text_partial') or ''}" for t in turns_chrono[-20:]])[-6000:]
        visual = _recent_visual_context(con, live_session_id)
        brain2 = _collect_brain2_context(con, person_id=person_id, active_people=known_people, limit=limit)
        world = _one(con, "SELECT * FROM brainlive_world_states WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1", (live_session_id,)) or {}
        routine_cards = _many(con, "SELECT * FROM brainlive_routine_cards WHERE person_id=? AND status='active' ORDER BY confidence DESC, support_count DESC LIMIT ?", (person_id, limit))
        affordance_matches = _many(con, "SELECT * FROM brainlive_affordance_matches WHERE person_id=? AND (live_session_id=? OR live_session_id IS NULL) AND status='active' ORDER BY time_sensitivity DESC, personal_fit DESC LIMIT ?", (person_id, live_session_id, limit))
        context_payload = {
            "session": sess,
            "recent_turns_summary": recent_summary,
            "recent_turns": _compact(turns_chrono, 40),
            "visual_context": visual,
            "world_state": world,
            "brain2_context": brain2,
            "brain2_live_personal_model": brain2.get("brain2_live_personal_model", {}),
            "brainlive_routine_cards": _compact(routine_cards, limit),
            "brainlive_affordance_matches": _compact(affordance_matches, limit),
            "horizons": {"H0": "0-10s", "H1": "10s-5min", "H2": "5min-2h"},
            "doctrine": "preload context, predict short-horizon needs, compare with world affordances, intervene only if expected value beats interruption cost, store outcomes for learning.",
        }
        active_context_id = stable_id("blctx", live_session_id, now)
        upsert(con, "brainlive_active_contexts", {
            "active_context_id": active_context_id,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "active_people_json": json_dumps(known_people),
            "relationship_packs_json": json_dumps(brain2.get("active_relationship_packs", [])),
            "recent_turns_summary": recent_summary,
            "recent_turns_json": json_dumps(_compact(turns_chrono, 40)),
            "visual_context_json": json_dumps(visual),
            "world_state_json": json_dumps(world),
            "open_loops_json": json_dumps(brain2.get("v14_open_loops", [])),
            "pattern_cards_json": json_dumps(brain2.get("v14_pattern_cards", []) + brain2.get("v17_global_life_patterns", [])),
            "interpersonal_loops_json": json_dumps(brain2.get("v14_interpersonal_loops", [])),
            "forecast_watch_json": json_dumps(brain2.get("v14_forecast_watch_queue", []) + brain2.get("v14_forecasts", [])),
            "personal_routines_json": json_dumps(_compact(routine_cards, limit)),
            "life_hypotheses_json": json_dumps({
                "brainlive_life_hypotheses": brain2.get("brainlive_life_hypotheses", []),
                "v17_recent_observed_cases": brain2.get("v17_recent_observed_cases", []),
                "v17_global_life_patterns": brain2.get("v17_global_life_patterns", []),
                "brain2_live_personal_model": brain2.get("brain2_live_personal_model", {}),
            }),
            "intervention_policy_json": json_dumps({
                "v14_policy": brain2.get("v14_intervention_policy", []),
                "brain2_operational_rules": (brain2.get("brain2_live_personal_model", {}).get("live_ready", {}) or {}).get("brainlive_operational_rules", []),
            }),
            "risk_state_json": json_dumps({"trajectory_warnings": brain2.get("trajectory_warnings", []), "future_scenarios": brain2.get("future_scenarios", [])}),
            "loaded_at": now,
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }, "active_context_id")
        con.commit()
    return {"active_context_id": active_context_id, "context": context_payload}



def _empty_llm_required_output(reason: str) -> dict[str, Any]:
    """Strict BrainLive fallback.

    V15.1 deliberately refuses fake psychological inference. If the LLM is
    unavailable, BrainLive records the failure and returns no inferred events,
    needs, emotions, intentions, hypotheses or interventions. Statistical
    longitudinal mining lives in brainlive_longitudinal_v15_1 and is explicitly
    non-cognitive unless calibrated by LLM.
    """
    return {
        "world_state": {},
        "events": [],
        "need_predictions": [],
        "affordances": [],
        "forecasts": [],
        "life_hypotheses": [],
        "interventions": [],
        "watch_next": ["llm_required"],
        "notes_for_brain2": [{"type": "llm_required", "reason": reason}],
    }


def run_brainlive(live_session_id: str, *, mode: str = "deep_live", use_llm: bool = True, timeout: float = 480.0, active_people: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    ensure_brainlive_schema()
    ctx_result = build_active_context(live_session_id, active_people=active_people, limit=limit)
    active_context_id = ctx_result["active_context_id"]
    context = ctx_result["context"]
    now = now_iso()
    run_id = stable_id("blrun", live_session_id, mode, now)
    person_id = context["session"]["person_id"]
    status = "ok"
    error_text = None
    qwen_json: dict[str, Any]
    invalid_llm_payload: dict[str, Any] | None = None
    import time
    started = time.time()
    # A bounded manifest is deliberately honest about omitted evidence. By
    # default an incomplete context may be stored/audited but cannot create new
    # forecasts, hypotheses or interventions. Operators can explicitly opt in
    # for exploratory runs, which remains visible in the analysis status.
    allow_incomplete = os.environ.get("MLOMEGA_V18_ALLOW_INCOMPLETE_CONTEXT_INFERENCE", "false").strip().lower() in {"1", "true", "yes", "on"}
    if bool(context.get("context_incomplete")) and not allow_incomplete:
        status = "context_incomplete"
        error_text = "V18 refused inference from an incomplete context manifest; retrieve or summarize missing evidence first."
        qwen_json = _empty_llm_required_output(error_text)
    elif use_llm:
        try:
            client = OllamaJsonClient()
            system = "Tu es BrainLive V15, moteur personnel prédictif H0/H1/H2. Tu ne remplaces pas Brain2: tu utilises son contexte pour lire le présent, anticiper besoins/actions/mots/risques/opportunités, détecter affordances personnelles, garder hypothèses concurrentes, et proposer une intervention seulement si elle améliore la trajectoire sans casser l'autonomie. Si un speaker est unknown/other/hypothesis_only, tu n'attribues jamais ses paroles à William comme vérité; tu peux utiliser lieu, vision, sujet, routines et historique de William pour comprendre la situation et proposer une aide prudente. Réponds en JSON strict. Ne donne pas de conseils de violence physique."
            prompt = json_dumps({"mission": "Analyse le contexte actif BrainLive. Produit world_state, events, need_predictions, affordances, forecasts H0/H1/H2, life_hypotheses, interventions, watch_next, notes_for_brain2.", "mode": mode, "context": context})
            raw_output = client.require_json(system, prompt, schema_hint=BRAINLIVE_LLM_SCHEMA, timeout=timeout)
            # JSON parseable is not enough: reject missing fields, invalid enums,
            # NaN, illegal horizons, and extra keys before any derived row exists.
            qwen_json = validate_brainlive_output(raw_output)
        except ContractValidationError as exc:
            status = "quarantined_invalid_llm_output"
            error_text = str(exc)[:2000]
            invalid_llm_payload = raw_output if 'raw_output' in locals() and isinstance(raw_output, dict) else None
            qwen_json = _empty_llm_required_output(error_text)
        except Exception as exc:
            status = "error"
            error_text = str(exc)[:2000]
            qwen_json = _empty_llm_required_output(error_text or "ollama_json_error")
    else:
        status = "llm_required"
        error_text = "BrainLive strict mode: --no-llm stores context only; no cognitive inference was generated."
        qwen_json = _empty_llm_required_output(error_text)
    latency_ms = int((time.time() - started) * 1000)
    with connect() as con:
        upsert(con, "brainlive_analysis_runs", {
            "run_id": run_id,
            "live_session_id": live_session_id,
            "event_id": None,
            "active_context_id": active_context_id,
            "person_id": person_id,
            "analysis_mode": mode,
            "model": "ollama" if use_llm else "none_strict_llm_required",
            "prompt_context_json": json_dumps({"active_context_id": active_context_id, "context_keys": list(context.keys())}),
            "qwen_json": json_dumps(qwen_json),
            "latency_ms": latency_ms,
            "status": status,
            "error_text": error_text,
            "created_at": now,
        }, "run_id")
        counts = _persist_brainlive_output(con, live_session_id=live_session_id, run_id=run_id, person_id=person_id, q=qwen_json, now=now) if status == "ok" else {"world_states": 0, "events": 0, "needs": 0, "affordances": 0, "forecasts": 0, "hypotheses": 0, "interventions": 0}
        con.commit()
    if status == "quarantined_invalid_llm_output":
        quarantine(category="invalid_llm_contract", reason=error_text or "invalid BrainLive LLM payload", raw_payload=invalid_llm_payload, run_id=run_id, source_table="brainlive_analysis_runs", source_id=run_id, person_id=person_id)
    return {"run_id": run_id, "live_session_id": live_session_id, "active_context_id": active_context_id, "status": status, "error_text": error_text, "latency_ms": latency_ms, "counts": counts, "output": qwen_json}


def _persist_brainlive_output(con, *, live_session_id: str, run_id: str, person_id: str, q: dict[str, Any], now: str) -> dict[str, int]:
    counts = {"world_states": 0, "events": 0, "needs": 0, "affordances": 0, "forecasts": 0, "hypotheses": 0, "interventions": 0}
    ws = q.get("world_state") if isinstance(q.get("world_state"), dict) else {}
    if ws:
        world_state_id = stable_id("blws", live_session_id, now, ws)
        upsert(con, "brainlive_world_states", {
            "world_state_id": world_state_id,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "state_time": now,
            "where_am_i": ws.get("where_am_i"),
            "who_is_active_json": json_dumps(ws.get("who_is_active") or []),
            "what_is_happening": ws.get("what_is_happening"),
            "probable_activity_json": json_dumps(ws.get("probable_activity") or []),
            "active_emotional_state": ws.get("active_emotional_state"),
            "active_mode": ws.get("active_mode"),
            "audio_context_json": json_dumps(ws.get("audio_context") or {}),
            "visual_context_json": json_dumps(ws.get("visual_context") or {}),
            "evidence_json": json_dumps(ws.get("evidence") or []),
            "counter_evidence_json": json_dumps(ws.get("counter_evidence") or []),
            "confidence": _clamp(ws.get("confidence")),
            "created_at": now,
        }, "world_state_id")
        counts["world_states"] += 1
    event_ids: list[str] = []
    for ev in q.get("events") or []:
        if not isinstance(ev, dict) or not (ev.get("summary") or ev.get("event_summary")):
            continue
        eid = stable_id("blevent", live_session_id, ev.get("event_type"), ev.get("summary"), now)
        event_ids.append(eid)
        upsert(con, "brainlive_event_candidates", {
            "event_id": eid,
            "live_session_id": live_session_id,
            "person_id": person_id,
            "event_type": ev.get("event_type") or "unknown",
            "event_summary": ev.get("summary") or ev.get("event_summary"),
            "source_turn_ids_json": json_dumps(ev.get("source_turn_ids") or []),
            "source_frame_ids_json": json_dumps(ev.get("source_frame_ids") or []),
            "related_person_ids_json": json_dumps(ev.get("related_person_ids") or []),
            "urgency_score": _clamp(ev.get("urgency_score")),
            "novelty_score": _clamp(ev.get("novelty_score")),
            "tension_score": _clamp(ev.get("tension_score")),
            "relationship_relevance_score": _clamp(ev.get("relationship_relevance_score")),
            "opportunity_score": _clamp(ev.get("opportunity_score")),
            "needs_llm_analysis": 1,
            "status": "candidate",
            "evidence_json": json_dumps(ev.get("evidence") or []),
            "counter_evidence_json": json_dumps(ev.get("counter_evidence") or []),
            "created_at": now,
            "updated_at": now,
        }, "event_id")
        counts["events"] += 1
    primary_event = event_ids[0] if event_ids else None
    need_map: dict[str, str] = {}
    for n in q.get("need_predictions") or []:
        if not isinstance(n, dict) or not n.get("need_label"):
            continue
        nid = stable_id("blneed", live_session_id, n.get("need_label"), n.get("horizon"), now)
        need_map[str(n.get("need_label"))] = nid
        upsert(con, "brainlive_need_predictions", {
            "need_id": nid,
            "live_session_id": live_session_id,
            "event_id": primary_event,
            "person_id": person_id,
            "need_label": n.get("need_label"),
            "need_type": n.get("need_type") or "unknown",
            "horizon": n.get("horizon") or "H1",
            "why_now": n.get("why_now"),
            "evidence_json": json_dumps(n.get("evidence") or []),
            "counter_evidence_json": json_dumps(n.get("counter_evidence") or []),
            "confidence": _clamp(n.get("confidence")),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }, "need_id")
        counts["needs"] += 1
    for a in q.get("affordances") or []:
        if not isinstance(a, dict) or not a.get("affordance_label"):
            continue
        aid = stable_id("blaff", live_session_id, a.get("affordance_label"), a.get("world_element"), now)
        upsert(con, "brainlive_affordances", {
            "affordance_id": aid,
            "live_session_id": live_session_id,
            "frame_id": a.get("frame_id"),
            "event_id": primary_event,
            "person_id": person_id,
            "affordance_label": a.get("affordance_label"),
            "world_element": a.get("world_element"),
            "position_hint": a.get("position_hint"),
            "personal_relevance": a.get("personal_relevance"),
            "matched_need_id": need_map.get(str(a.get("matched_need_label"))),
            "personal_fit": _clamp(a.get("personal_fit")),
            "time_sensitivity": _clamp(a.get("time_sensitivity")),
            "evidence_json": json_dumps(a.get("evidence") or []),
            "counter_evidence_json": json_dumps(a.get("counter_evidence") or []),
            "confidence": _clamp(a.get("confidence")),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }, "affordance_id")
        counts["affordances"] += 1
    for f in q.get("forecasts") or []:
        # ``run_brainlive`` validates the complete output before this writer is
        # reached.  The canonical writer still validates defensively and writes
        # an append-only, lifecycle-aware record rather than a destructive upsert.
        create_forecast(
            con,
            live_session_id=live_session_id,
            person_id=person_id,
            event_id=primary_event,
            run_id=run_id,
            payload=f,
            occurred_at=now,
            source="brainlive_run",
        )
        counts["forecasts"] += 1
    for h in q.get("life_hypotheses") or []:
        if not isinstance(h, dict) or not h.get("statement"):
            continue
        hid = stable_id("blhyp", person_id, h.get("hypothesis_type"), h.get("statement"))
        upsert(con, "brainlive_life_hypotheses", {
            "hypothesis_id": hid,
            "person_id": person_id,
            "hypothesis_type": h.get("hypothesis_type") or "unknown",
            "statement": h.get("statement"),
            "scope": h.get("scope") or "life",
            "time_pattern": h.get("time_pattern"),
            "location_pattern": h.get("location_pattern"),
            "people_pattern_json": json_dumps(h.get("people_pattern") or []),
            "trigger_contexts_json": json_dumps(h.get("trigger_contexts") or []),
            "candidate_needs_json": json_dumps(h.get("candidate_needs") or []),
            "candidate_emotions_json": json_dumps(h.get("candidate_emotions") or []),
            "candidate_risks_json": json_dumps(h.get("candidate_risks") or []),
            "candidate_opportunities_json": json_dumps(h.get("candidate_opportunities") or []),
            "predicted_next_window": h.get("predicted_next_window"),
            "confidence": _clamp(h.get("confidence")),
            "evidence_count": len(h.get("evidence") or []),
            "counter_evidence_count": len(h.get("counter_evidence") or []),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }, "hypothesis_id")
        for role, vals in (("support", h.get("evidence") or []), ("counter", h.get("counter_evidence") or [])):
            for i, txt in enumerate(vals):
                eid = stable_id("blhypev", hid, role, i, txt)
                upsert(con, "brainlive_hypothesis_evidence", {"evidence_id": eid, "hypothesis_id": hid, "person_id": person_id, "evidence_role": role, "source_table": "brainlive_analysis_runs", "source_id": run_id, "evidence_text": str(txt), "weight": 0.6 if role == "support" else -0.4, "observed_at": now, "created_at": now}, "evidence_id")
        counts["hypotheses"] += 1
    for it in q.get("interventions") or []:
        if not isinstance(it, dict) or not it.get("message"):
            continue
        cid = stable_id("blcand", live_session_id, it.get("message"), it.get("recommended_timing"), now)
        expected_gain = _clamp(it.get("expected_gain"))
        intrusion = _clamp(it.get("intrusion_score"))
        conf = _clamp(it.get("confidence"))
        if str(it.get("recommended_timing")) == "watch_only" or expected_gain < 0.35 or intrusion > 0.75 or conf < 0.35:
            gate = "watch"
            reason = "expected_gain/intrusion/confidence gate"
        else:
            gate = "speak_now" if str(it.get("recommended_timing")) == "now" and expected_gain >= 0.55 and conf >= 0.55 else "queue_or_wait"
            reason = "value positive but timing may need host confirmation"
        upsert(con, "brainlive_intervention_candidates", {
            "candidate_id": cid,
            "live_session_id": live_session_id,
            "event_id": primary_event,
            "run_id": run_id,
            "person_id": person_id,
            "message": it.get("message"),
            "intervention_type": it.get("intervention_type") or "other",
            "recommended_timing": it.get("recommended_timing") or "watch_only",
            "urgency": _clamp(it.get("urgency")),
            "confidence": conf,
            "expected_gain": expected_gain,
            "risk_if_silent": _clamp(it.get("risk_if_silent")),
            "risk_if_said": _clamp(it.get("risk_if_said")),
            "intrusion_score": intrusion,
            "autonomy_risk": _clamp(it.get("autonomy_risk")),
            "evidence_json": json_dumps(it.get("evidence") or []),
            "counter_evidence_json": json_dumps(it.get("counter_evidence") or []),
            "status": "candidate",
            "gate_decision": gate,
            "gate_reason": reason,
            "cooldown_key": it.get("cooldown_key"),
            "created_at": now,
            "updated_at": now,
        }, "candidate_id")
        counts["interventions"] += 1
    return counts


def list_live_inbox(*, person_id: str | None = None, status: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    if not person_id:
        raise GovernanceError("V18 live inbox requires explicit person_id")
    ensure_brainlive_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        params: list[Any] = [person_id]
        sql = "SELECT * FROM brainlive_intervention_candidates WHERE person_id=?"
        if status and status != "all":
            sql += " AND gate_decision=?"
            params.append(status)
        sql += " ORDER BY expected_gain DESC, urgency DESC, created_at DESC LIMIT ?"
        params.append(limit)
        return _many(con, sql, tuple(params))


def record_prediction_outcome(forecast_id: str | None, observed_after: str, *, person_id: str | None = None, live_session_id: str | None = None, candidate_id: str | None = None, match_score: float | None = None, user_feedback: str | None = None) -> dict[str, Any]:
    """Record one owner-scoped terminal outcome and close its forecast atomically."""
    ensure_brainlive_schema()
    if not forecast_id:
        raise ValueError("forecast_id is required for a forecast outcome")
    from .integrity_v176 import record_forecast_outcome
    with connect() as con:
        forecast = _one(con, "SELECT * FROM brainlive_short_horizon_forecasts WHERE forecast_id=?", (forecast_id,))
        if not forecast:
            raise ValueError(f"Forecast introuvable: {forecast_id}")
        resolved_person_id = person_id or (str(forecast.get("person_id")) if forecast.get("person_id") else None)
        if not resolved_person_id:
            raise GovernanceError("V18 prediction outcome requires explicit person_id or an owned forecast")
        if person_id and str(forecast.get("person_id") or "") != str(person_id):
            raise GovernanceError("V18 prediction outcome owner mismatch")
        result = record_forecast_outcome(
            con,
            forecast_id=forecast_id,
            person_id=resolved_person_id,
            observed_after=observed_after,
            was_prediction_correct=None if match_score is None else bool(match_score >= 0.6),
            match_score=match_score,
            outcome_window="manual",
            actor="manual_outcome",
            user_feedback=user_feedback,
            candidate_id=candidate_id,
        )
        con.commit()
    return {"outcome_id": result["outcome_id"], "forecast_id": forecast_id, "lifecycle_state": (result.get("forecast") or {}).get("lifecycle_state")}



def record_user_disagreement(candidate_id: str | None, system_claim: str, user_response: str, *, person_id: str | None = None, live_session_id: str | None = None) -> dict[str, Any]:
    """Record raw disagreement only.

    V15.1 no longer assigns hardcoded meanings to disagreement. Use
    brainlive-interpret-disagreement to run the LLM interpreter.
    """
    if not person_id:
        raise GovernanceError("V18 disagreement feedback requires explicit person_id")
    ensure_brainlive_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        did = stable_id("bldis", candidate_id, system_claim, user_response, now)
        upsert(con, "brainlive_user_disagreement_events", {
            "disagreement_id": did,
            "live_session_id": live_session_id,
            "candidate_id": candidate_id,
            "person_id": person_id,
            "system_claim": system_claim,
            "user_response": user_response,
            "possible_meanings_json": json_dumps([]),
            "next_policy": "llm_required",
            "watch_next_json": json_dumps(["interpret_with_brainlive_disagreement_llm"]),
            "created_at": now,
            "updated_at": now,
        }, "disagreement_id")
        con.commit()
    return {"disagreement_id": did, "status": "stored_raw", "next_step": "brainlive-interpret-disagreement"}


def run_nightly_bridge(*, person_id: str | None = None, run_date: str | None = None, force: bool = False) -> dict[str, Any]:
    """Record a nightly Brain2 bridge and, when available, call V14 day consolidation.

    This keeps BrainLive as the day/live layer and Brain2 as a nightly deep layer.
    It intentionally does not replace existing v14-auto-consolidate; it gives a
    single command that summarizes live sessions and then triggers day-level Brain2.
    """
    if not person_id:
        raise GovernanceError("V18 nightly bridge requires explicit person_id")
    ensure_brainlive_schema()
    now = now_iso()
    run_date = run_date or now[:10]
    with connect() as con:
        person_id = person_id or _default_user(con)
        sessions = _many(con, "SELECT * FROM brainlive_sessions WHERE person_id=? AND started_at LIKE ? ORDER BY started_at", (person_id, f"{run_date}%"))
        run_id = stable_id("blnight", person_id, run_date)
        upsert(con, "brainlive_nightly_consolidation_runs", {
            "run_id": run_id,
            "person_id": person_id,
            "run_date": run_date,
            "status": "started",
            "brain2_period": "day",
            "live_sessions_json": json_dumps(_compact(sessions, 200)),
            "counts_json": json_dumps({"sessions": len(sessions)}),
            "notes": "BrainLive day bridge prepared. Brain2 V14 day consolidation should run after this.",
            "started_at": now,
            "finished_at": None,
            "error_text": None,
        }, "run_id")
        con.commit()
    brain2_result: dict[str, Any] | None = None
    error_text = None
    try:
        from .pattern_mirror_v14 import run_periodic_mirror
        brain2_result = run_periodic_mirror(person_id=person_id, period="day")
    except Exception as exc:
        error_text = str(exc)[:2000]
    finished = now_iso()
    with connect() as con:
        con.execute("UPDATE brainlive_nightly_consolidation_runs SET status=?, finished_at=?, error_text=?, counts_json=? WHERE run_id=?", ("ok" if not error_text else "partial", finished, error_text, json_dumps({"sessions": len(sessions), "brain2_called": brain2_result is not None}), run_id))
        con.commit()
    return {"run_id": run_id, "person_id": person_id, "run_date": run_date, "sessions": len(sessions), "brain2_result": brain2_result, "error_text": error_text}

# V15.1 compatibility exports. Longitudinal engines live in their own module to
# keep BrainLive base small and avoid reintroducing shortcut inference here.
def mine_routines(*args, **kwargs):
    from .brainlive_longitudinal_v15_1 import mine_routines as _fn
    return _fn(*args, **kwargs)


def evaluate_prediction_outcomes(*args, **kwargs):
    from .brainlive_longitudinal_v15_1 import evaluate_prediction_outcomes as _fn
    return _fn(*args, **kwargs)

# V18 Context Gateway: LLM receives a bounded manifest with source references,
# owner/as_of scope and explicit incompleteness instead of raw truncation.
from .v18_context import install as _install_v18_context
_globals_v18_context = _install_v18_context(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_context)

# V18 live decomposition: observation, forecast and intervention have distinct
# contracts/provenance.  The legacy monolithic call is diagnostic-only.
from .v18_live_decomposition import install as _install_v18_live_decomposition
_globals_v18_live_decomposition = _install_v18_live_decomposition(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_live_decomposition)
