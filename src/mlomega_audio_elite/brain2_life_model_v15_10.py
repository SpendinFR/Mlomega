from __future__ import annotations

"""V15.10 Brain2 canonical life model compiler for BrainLive.

Purpose
-------
BrainLive should not invent William's routines/preferences/needs at runtime and
should not rediscover them from 20 raw tables on every tick. Brain2 must compile
its deep material into a canonical, evidence-led, live-ready life model during
nightly/manual consolidation. BrainLive then reads that model as the official
source of truth for H0/H1/H2 prediction.

Strict policy
-------------
- No regex/keyword psychology.
- Deterministic code may aggregate neutral facts: timestamps, places, counts,
  source rows, evidence links, observed outcomes.
- Any psychological meaning (need, emotion, intent, avoidance, expectation,
  relationship interpretation, intervention rule) is produced only by the local
  Brain2 LLM JSON client or copied from existing Brain2 LLM-backed tables with
  explicit evidence/confidence.
- If the LLM is missing, this module stores raw evidence bundles and marks them
  `llm_required`; it does not synthesize meaning.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v18_legacy_forecasts import active_legacy_forecasts as _active_v14_forecasts

VERSION = "15.10.0-brain2-canonical-life-model"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brain2_life_model_exports(
  export_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  status TEXT NOT NULL,
  source_counts_json TEXT DEFAULT '{}',
  raw_evidence_json TEXT DEFAULT '{}',
  canonical_model_json TEXT DEFAULT '{}',
  llm_model TEXT,
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_personal_routine_models(
  routine_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  routine_name TEXT NOT NULL,
  routine_type TEXT DEFAULT 'unknown',
  temporal_pattern_json TEXT DEFAULT '{}',
  place_pattern_json TEXT DEFAULT '{}',
  trigger_contexts_json TEXT DEFAULT '[]',
  observed_actions_json TEXT DEFAULT '[]',
  likely_needs_json TEXT DEFAULT '[]',
  preferred_conditions_json TEXT DEFAULT '[]',
  outcomes_json TEXT DEFAULT '[]',
  live_activation_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_place_preference_models(
  place_model_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  place_key TEXT NOT NULL,
  meaning_for_user TEXT,
  common_actions_json TEXT DEFAULT '[]',
  preferred_affordances_json TEXT DEFAULT '[]',
  avoided_conditions_json TEXT DEFAULT '[]',
  time_patterns_json TEXT DEFAULT '{}',
  live_use_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_action_preference_models(
  action_model_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  action_or_choice TEXT NOT NULL,
  context_conditions_json TEXT DEFAULT '[]',
  preference_or_tendency TEXT,
  why_it_matters TEXT,
  what_helps_json TEXT DEFAULT '[]',
  what_hurts_json TEXT DEFAULT '[]',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_need_expectation_models(
  need_model_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  need_or_expectation TEXT NOT NULL,
  kind TEXT DEFAULT 'need',
  activation_contexts_json TEXT DEFAULT '[]',
  surface_signals_json TEXT DEFAULT '[]',
  deeper_hypotheses_json TEXT DEFAULT '[]',
  good_responses_json TEXT DEFAULT '[]',
  bad_responses_json TEXT DEFAULT '[]',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_expression_state_models(
  expression_model_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  expression_or_style TEXT NOT NULL,
  contexts_json TEXT DEFAULT '[]',
  possible_meanings_json TEXT DEFAULT '[]',
  state_links_json TEXT DEFAULT '[]',
  response_implications_json TEXT DEFAULT '[]',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_emotional_trajectory_models(
  trajectory_model_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  trajectory_name TEXT NOT NULL,
  starting_conditions_json TEXT DEFAULT '[]',
  live_signals_to_watch_json TEXT DEFAULT '[]',
  likely_next_states_json TEXT DEFAULT '[]',
  risks_json TEXT DEFAULT '[]',
  opportunities_json TEXT DEFAULT '[]',
  intervention_windows_json TEXT DEFAULT '[]',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_contextual_self_models(
  contextual_model_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  context_key TEXT NOT NULL,
  self_state_summary TEXT,
  strengths_json TEXT DEFAULT '[]',
  vulnerabilities_json TEXT DEFAULT '[]',
  needs_json TEXT DEFAULT '[]',
  best_moves_json TEXT DEFAULT '[]',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_live_prediction_hooks(
  hook_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  hook_name TEXT NOT NULL,
  horizon TEXT NOT NULL DEFAULT 'H1',
  domain TEXT,
  active_person_hint TEXT,
  risk_type TEXT,
  user_common_bad_move TEXT,
  recommended_micro_move TEXT,
  do_not_say_json TEXT DEFAULT '[]',
  intervention_mode TEXT DEFAULT 'watch',
  outcome_success_count INTEGER DEFAULT 0,
  outcome_failure_count INTEGER DEFAULT 0,
  calibration_score REAL DEFAULT 0.0,
  use_policy TEXT DEFAULT 'silent_context',
  activation_conditions_json TEXT DEFAULT '[]',
  predicts_json TEXT DEFAULT '{}',
  watch_signals_json TEXT DEFAULT '[]',
  proactive_options_json TEXT DEFAULT '[]',
  silence_policy_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_live_affordance_preferences(
  affordance_pref_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  affordance_type TEXT NOT NULL,
  preferred_when_json TEXT DEFAULT '[]',
  personal_fit_criteria_json TEXT DEFAULT '[]',
  live_detection_needs_json TEXT DEFAULT '[]',
  intervention_value_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain2_heuristic_audit(
  audit_id TEXT PRIMARY KEY,
  module TEXT NOT NULL,
  function_name TEXT,
  issue_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  description TEXT NOT NULL,
  action_taken TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_b2lm_exports_person ON brain2_life_model_exports(person_id, created_at);
CREATE INDEX IF NOT EXISTS idx_b2lm_hooks_person ON brain2_live_prediction_hooks(person_id, status, confidence);
CREATE INDEX IF NOT EXISTS idx_b2lm_routines_person ON brain2_personal_routine_models(person_id, status, confidence);
CREATE INDEX IF NOT EXISTS idx_b2lm_places_person ON brain2_place_preference_models(person_id, status, confidence);
"""

CANONICAL_SCHEMA: dict[str, Any] = {
    "personal_routine_models": [
        {"routine_name": "", "routine_type": "routine|cycle|habit|transition|social|work|regulation|place|unknown", "temporal_pattern": {}, "place_pattern": {}, "trigger_contexts": [], "observed_actions": [], "likely_needs": [], "preferred_conditions": [], "outcomes": [], "live_activation": {}, "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "place_preference_models": [
        {"place_key": "", "meaning_for_user": "", "common_actions": [], "preferred_affordances": [], "avoided_conditions": [], "time_patterns": {}, "live_use": {}, "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "action_preference_models": [
        {"action_or_choice": "", "context_conditions": [], "preference_or_tendency": "", "why_it_matters": "", "what_helps": [], "what_hurts": [], "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "need_expectation_models": [
        {"need_or_expectation": "", "kind": "need|expectation|preference|boundary|value|goal|avoidance|unknown", "activation_contexts": [], "surface_signals": [], "deeper_hypotheses": [], "good_responses": [], "bad_responses": [], "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "expression_state_models": [
        {"expression_or_style": "", "contexts": [], "possible_meanings": [], "state_links": [], "response_implications": [], "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "emotional_trajectory_models": [
        {"trajectory_name": "", "starting_conditions": [], "live_signals_to_watch": [], "likely_next_states": [], "risks": [], "opportunities": [], "intervention_windows": [], "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "contextual_self_models": [
        {"context_key": "", "self_state_summary": "", "strengths": [], "vulnerabilities": [], "needs": [], "best_moves": [], "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "live_prediction_hooks": [
        {
            "hook_name": "",
            "horizon": "H0|H1|H2|day|week|long",
            "domain": "negotiation|conflict|client|relationship|routine|project|regulation|health|unknown",
            "active_person_hint": "optional person name/id when hook is person-specific",
            "risk_type": "avoidance|escalation|misread|delay|overcommit|missed_opportunity|unknown",
            "user_common_bad_move": "what William often does that worsens this case",
            "recommended_micro_move": "one short live move, e.g. ask a two-option question",
            "do_not_say": [],
            "intervention_mode": "watch|silent_context|queue|speak_now|avoid_intervention",
            "outcome_success_count": 0,
            "outcome_failure_count": 0,
            "calibration_score": 0.0,
            "use_policy": "watch_only|silent_context|proactive_allowed|strong_live_hook|do_not_use",
            "activation_conditions": [],
            "predicts": {},
            "watch_signals": [],
            "proactive_options": [],
            "silence_policy": {},
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0
        }
    ],
    "live_affordance_preferences": [
        {"affordance_type": "", "preferred_when": [], "personal_fit_criteria": [], "live_detection_needs": [], "intervention_value": {}, "evidence": [], "counter_evidence": [], "confidence": 0.0}
    ],
    "missing_evidence_for_magic": [],
    "do_not_infer_live_without": [],
}


def _ensure_columns(con, table: str, columns: dict[str, str]) -> None:
    try:
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return
    for name, ddl in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def ensure_life_model_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(SCHEMA)
        # V16.2: operational/tactical live hooks. Old databases created before
        # this version keep the generic hook columns; migrate them in-place so
        # BrainLive can route negotiation/conflict/client/routine hooks without
        # relying on opaque JSON only.
        _ensure_columns(con, "brain2_live_prediction_hooks", {
            "domain": "TEXT",
            "active_person_hint": "TEXT",
            "risk_type": "TEXT",
            "user_common_bad_move": "TEXT",
            "recommended_micro_move": "TEXT",
            "do_not_say_json": "TEXT DEFAULT '[]'",
            "intervention_mode": "TEXT DEFAULT 'watch'",
            "outcome_success_count": "INTEGER DEFAULT 0",
            "outcome_failure_count": "INTEGER DEFAULT 0",
            "calibration_score": "REAL DEFAULT 0.0",
            "use_policy": "TEXT DEFAULT 'silent_context'",
        })
        con.commit()


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _query(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _compact(rows: list[dict[str, Any]], limit: int = 80, max_str: int = 1200) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        nr: dict[str, Any] = {}
        for k, v in r.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > max_str:
                nr[k] = v[:max_str] + "…"
            else:
                nr[k] = v
        out.append(nr)
    return out


def _count(feed: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for k, v in feed.items():
        if isinstance(v, dict):
            counts[k] = sum(len(x) if isinstance(x, list) else (1 if x else 0) for x in v.values())
        elif isinstance(v, list):
            counts[k] = len(v)
        else:
            counts[k] = 1 if v else 0
    return counts


def _clamp(v: Any, default: float = 0.5) -> float:
    try:
        x = float(v)
    except Exception:
        x = default
    return max(0.0, min(1.0, x))




def _parse_dt(value: Any):
    from datetime import datetime, timezone
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _row_in_window(row: dict[str, Any], start: str | None, end: str | None, *keys: str) -> bool:
    if not start and not end:
        return True
    sdt = _parse_dt(start) if start else None
    edt = _parse_dt(end) if end else None
    for k in keys:
        if row.get(k):
            dt = _parse_dt(row.get(k))
            if dt is None:
                continue
            if sdt and dt < sdt:
                return False
            if edt and dt >= edt:
                return False
            return True
    # With an explicit delta window, rows without any time anchor should not be
    # treated as fresh evidence. They can still enter all-time builds.
    return False


def _filter_window(rows: list[dict[str, Any]], start: str | None, end: str | None, *keys: str) -> list[dict[str, Any]]:
    return [r for r in rows if _row_in_window(r, start, end, *keys)]

def collect_canonical_evidence(person_id: str, *, period_start: str | None = None, period_end: str | None = None, limit: int = 120) -> dict[str, Any]:
    """Collect Brain2 evidence without making psychological interpretations."""
    time_filter = ""
    params_time: list[Any] = []
    if period_start:
        time_filter += " AND COALESCE(occurred_start, created_at) >= ?"
        params_time.append(period_start)
    if period_end:
        time_filter += " AND COALESCE(occurred_start, created_at) <= ?"
        params_time.append(period_end)

    with connect() as con:
        feed: dict[str, Any] = {"person_id": person_id, "period_start": period_start, "period_end": period_end}
        feed["observed_life"] = {
            "episodes": _compact(_filter_window(_query(con, "SELECT * FROM episodes WHERE 1=1 ORDER BY COALESCE(start_time, created_at) DESC LIMIT ?", (limit * 4,)), period_start, period_end, "start_time", "created_at", "updated_at"), limit),
            "life_events": _compact(_query(con, f"SELECT * FROM life_events WHERE (subject_person_id=? OR subject_person_id IS NULL) {time_filter} ORDER BY COALESCE(occurred_start, created_at) DESC LIMIT ?", tuple([person_id] + params_time + [limit])), limit),
            "life_event_entities": _compact(_query(con, "SELECT lee.* FROM life_event_entities lee JOIN life_events le ON le.event_id=lee.event_id WHERE (le.subject_person_id=? OR le.subject_person_id IS NULL) ORDER BY lee.created_at DESC LIMIT ?", (person_id, limit)), limit),
            "situation_episodes": _compact(_filter_window(_query(con, "SELECT * FROM situation_episodes ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "interaction_episodes": _compact(_filter_window(_query(con, "SELECT * FROM interaction_episodes ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "choice_episodes": _compact(_filter_window(_query(con, "SELECT * FROM choice_episodes ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "action_intentions": _compact(_filter_window(_query(con, "SELECT * FROM action_intentions ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "action_outcomes": _compact(_filter_window(_query(con, "SELECT * FROM action_outcomes ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
        }
        feed["self_and_internal"] = {
            "self_model_dimensions": _compact(_query(con, "SELECT * FROM self_model_dimensions WHERE person_id=? ORDER BY confidence DESC, evidence_count DESC LIMIT ?", (person_id, limit)), limit),
            "self_model_facts": _compact(_query(con, "SELECT * FROM self_model_facts WHERE (scope=? OR scope IS NULL OR scope IN ('global','life','identity')) ORDER BY confidence DESC, evidence_count DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "internal_state_snapshots": _compact(_filter_window(_query(con, "SELECT * FROM internal_state_snapshots ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "emotion_evidence": _compact(_filter_window(_query(con, "SELECT * FROM emotion_evidence ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "thought_hypotheses": _compact(_filter_window(_query(con, "SELECT * FROM thought_hypotheses ORDER BY confidence DESC, created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "behavior_signals": _compact(_filter_window(_query(con, "SELECT * FROM behavior_signals ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
        }
        feed["language"] = {
            "turns_recent": _compact(_query(con, "SELECT t.* FROM turns t JOIN conversations c ON c.conversation_id=t.conversation_id WHERE (t.person_id=? OR t.person_id IS NULL) AND (? IS NULL OR COALESCE(c.started_at, c.created_at) >= ?) AND (? IS NULL OR COALESCE(c.started_at, c.created_at) <= ?) ORDER BY COALESCE(c.started_at, c.created_at) DESC, t.idx DESC LIMIT ?", (person_id, period_start, period_start, period_end, period_end, limit)), limit),
            "speech_acts": _compact(_filter_window(_query(con, "SELECT * FROM speech_acts ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at"), limit),
            "utterance_analyses": _compact(_query(con, "SELECT ua.* FROM utterance_analyses ua JOIN turns t ON t.turn_id=ua.turn_id WHERE (t.person_id=? OR t.person_id IS NULL) ORDER BY ua.created_at DESC LIMIT ?", (person_id, limit)), limit),
            "personal_language_patterns": _compact(_query(con, "SELECT * FROM personal_language_patterns WHERE person_id=? ORDER BY confidence DESC, frequency DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "phrase_templates": _compact(_query(con, "SELECT * FROM phrase_templates WHERE person_id=? ORDER BY confidence DESC, frequency DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "expression_signals": _compact(_query(con, "SELECT e.* FROM expression_signals e JOIN turns t ON t.turn_id=e.turn_id WHERE (t.person_id=? OR t.person_id IS NULL) ORDER BY e.created_at DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["memory_and_patterns"] = {
            "memory_cards": _compact(_query(con, "SELECT * FROM memory_cards WHERE person_id=? AND lifecycle_status='active' ORDER BY importance_score DESC, confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "memory_facets": _compact(_query(con, "SELECT * FROM memory_facets ORDER BY weight DESC, confidence DESC, created_at DESC LIMIT ?", (limit,)), limit),
            "candidate_patterns": _compact(_filter_window(_query(con, "SELECT * FROM candidate_patterns ORDER BY confidence DESC, created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "confirmed_patterns": _compact(_filter_window(_query(con, "SELECT * FROM confirmed_patterns ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "loop_patterns": _compact(_query(con, "SELECT * FROM loop_patterns ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,)), limit),
            "v14_pattern_mirror_cards": _compact(_query(con, "SELECT * FROM v14_pattern_mirror_cards WHERE person_id=? ORDER BY confidence DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["relationships"] = {
            "relationship_models": _compact(_filter_window(_query(con, "SELECT * FROM relationship_models ORDER BY updated_at DESC, confidence DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "updated_at"), limit),
            "v14_5_people_context_profiles": _compact(_query(con, "SELECT * FROM v14_5_people_context_profiles WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "v14_6_relationship_state_models": _compact(_query(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "v14_6_interpersonal_loop_cards": _compact(_query(con, "SELECT * FROM v14_6_interpersonal_loop_cards WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["future_and_interventions"] = {
            "prediction_cases": _compact(_query(con, "SELECT * FROM prediction_cases WHERE (person_id=? OR person_id IS NULL) AND COALESCE(usable_for_prediction,1)=1 ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
            "predictions": _compact(_query(con, "SELECT * FROM predictions WHERE (person_id=? OR person_id IS NULL) AND status IN ('open','active','watch') ORDER BY confidence DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
            "future_scenarios": _compact(_filter_window(_query(con, "SELECT * FROM future_scenarios WHERE (person_id=? OR person_id IS NULL) ORDER BY probability DESC, opportunity_level DESC, risk_level DESC LIMIT ?", (person_id, limit * 4)), period_start, period_end, "created_at", "updated_at"), limit),
            "trajectory_warnings": _compact(_filter_window(_query(con, "SELECT * FROM trajectory_warnings WHERE (person_id=? OR person_id IS NULL) ORDER BY severity DESC, probability DESC LIMIT ?", (person_id, limit * 4)), period_start, period_end, "created_at", "updated_at"), limit),
            "v14_trajectory_forecasts": _compact(_active_v14_forecasts(con, person_id, "v14_trajectory_forecasts", limit), limit),
            "v14_forecast_watch_queue": _compact(_active_v14_forecasts(con, person_id, "v14_forecast_watch_queue", limit), limit),
            "v14_7_intervention_policies": _compact(_query(con, "SELECT * FROM v14_7_intervention_policies WHERE person_id=? LIMIT ?", (person_id, 20)), 20),
            "v14_7_intervention_feedback": _compact(_query(con, "SELECT * FROM v14_7_intervention_feedback WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
            "v14_8_clarification_items": _compact(_query(con, "SELECT * FROM v14_8_clarification_items WHERE person_id=? ORDER BY priority DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["live_short_term_learning"] = {
            "brainlive_routine_cards": _compact(_query(con, "SELECT * FROM brainlive_routine_cards WHERE person_id=? AND status='active' ORDER BY confidence DESC, support_count DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "brainlive_prediction_outcomes": _compact(_filter_window(_query(con, "SELECT * FROM brainlive_prediction_outcomes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit * 4)), period_start, period_end, "created_at", "updated_at"), limit),
            "brainlive_life_hypotheses": _compact(_filter_window(_query(con, "SELECT * FROM brainlive_life_hypotheses WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit * 4)), period_start, period_end, "created_at", "updated_at"), limit),
            "brainlive_affordance_matches": _compact(_filter_window(_query(con, "SELECT * FROM brainlive_affordance_matches WHERE person_id=? ORDER BY personal_fit DESC, time_sensitivity DESC LIMIT ?", (person_id, limit * 4)), period_start, period_end, "created_at", "updated_at"), limit),
            "vision_scene_observations": _compact(_filter_window(_query(con, "SELECT * FROM vision_scene_observations ORDER BY created_at DESC LIMIT ?", (limit * 4,)), period_start, period_end, "created_at", "captured_at"), limit),
        }
        return feed


def synthesize_canonical_life_model(raw_evidence: dict[str, Any], *, timeout: float = 180.0) -> tuple[dict[str, Any], str | None]:
    try:
        client = OllamaJsonClient()
        system = (
            "Tu es le Brain2 Life Model Compiler canonique. Tu construis le modèle de vie officiel de William pour BrainLive. "
            "Tu n'utilises aucune regex, aucun mot-clé, aucune psychologie générique. "
            "Tu dois produire des objets stables, testables, utilisables en live H0/H1/H2: routines, lieux, actions, besoins, attentes, expressions, émotions/trajectoires, contextes du self, hooks prédictifs et affordances. "
            "Chaque élément doit citer les preuves fournies, les contre-preuves si présentes, les contextes où c'est faux/incertain, et une confidence. "
            "Si une conclusion n'a pas assez de preuves, place-la dans missing_evidence_for_magic ou do_not_infer_live_without. JSON strict uniquement."
        )
        prompt = json_dumps({
            "mission": "Compile Brain2 raw/deep evidence into canonical life-model tables for BrainLive. Think beyond examples: cigarette, negotiation, relationships, work, fatigue, ideas, routines, places, social timing, needs, future hooks.",
            "raw_evidence": raw_evidence,
            "hard_rules": [
                "No generic human advice.",
                "No keyword inference.",
                "No certainty without evidence.",
                "Separate observed action from inferred need/emotion/intention.",
                "Prefer multiple competing hypotheses when needed.",
                "Make outputs directly actionable for BrainLive H0/H1/H2.",
            ],
        })
        data = client.require_json(system, prompt, schema_hint=CANONICAL_SCHEMA, timeout=timeout)
        return data, None
    except Exception as exc:
        return {"llm_required": True, "raw_evidence_available": True, "error": str(exc)}, str(exc)


def _j(v: Any) -> str:
    return json_dumps(v if v is not None else ([] if isinstance(v, list) else {}))


def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else ([] if v in (None, "") else [v])


def store_canonical_life_model(person_id: str, export_id: str, model: dict[str, Any]) -> None:
    now = now_iso()
    with connect() as con:
        for item in _list(model.get("personal_routine_models")):
            if not isinstance(item, dict):
                continue
            name = str(item.get("routine_name") or item.get("name") or "unknown_routine")[:500]
            upsert(con, "brain2_personal_routine_models", {
                "routine_id": stable_id("b2routine", person_id, name),
                "person_id": person_id, "export_id": export_id, "routine_name": name,
                "routine_type": item.get("routine_type") or "unknown",
                "temporal_pattern_json": json_dumps(item.get("temporal_pattern") or {}),
                "place_pattern_json": json_dumps(item.get("place_pattern") or {}),
                "trigger_contexts_json": json_dumps(item.get("trigger_contexts") or []),
                "observed_actions_json": json_dumps(item.get("observed_actions") or []),
                "likely_needs_json": json_dumps(item.get("likely_needs") or []),
                "preferred_conditions_json": json_dumps(item.get("preferred_conditions") or []),
                "outcomes_json": json_dumps(item.get("outcomes") or []),
                "live_activation_json": json_dumps(item.get("live_activation") or {}),
                "evidence_json": json_dumps(item.get("evidence") or []),
                "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "routine_id")
        for item in _list(model.get("place_preference_models")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("place_key") or item.get("place") or "unknown_place")[:500]
            upsert(con, "brain2_place_preference_models", {
                "place_model_id": stable_id("b2place", person_id, key),
                "person_id": person_id, "export_id": export_id, "place_key": key,
                "meaning_for_user": item.get("meaning_for_user"),
                "common_actions_json": json_dumps(item.get("common_actions") or []),
                "preferred_affordances_json": json_dumps(item.get("preferred_affordances") or []),
                "avoided_conditions_json": json_dumps(item.get("avoided_conditions") or []),
                "time_patterns_json": json_dumps(item.get("time_patterns") or {}),
                "live_use_json": json_dumps(item.get("live_use") or {}),
                "evidence_json": json_dumps(item.get("evidence") or []),
                "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "place_model_id")
        for item in _list(model.get("action_preference_models")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("action_or_choice") or "unknown_action")[:500]
            upsert(con, "brain2_action_preference_models", {
                "action_model_id": stable_id("b2action", person_id, key), "person_id": person_id, "export_id": export_id,
                "action_or_choice": key, "context_conditions_json": json_dumps(item.get("context_conditions") or []),
                "preference_or_tendency": item.get("preference_or_tendency"), "why_it_matters": item.get("why_it_matters"),
                "what_helps_json": json_dumps(item.get("what_helps") or []), "what_hurts_json": json_dumps(item.get("what_hurts") or []),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "action_model_id")
        for item in _list(model.get("need_expectation_models")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("need_or_expectation") or item.get("item") or "unknown_need")[:500]
            upsert(con, "brain2_need_expectation_models", {
                "need_model_id": stable_id("b2need", person_id, key), "person_id": person_id, "export_id": export_id,
                "need_or_expectation": key, "kind": item.get("kind") or "need",
                "activation_contexts_json": json_dumps(item.get("activation_contexts") or []),
                "surface_signals_json": json_dumps(item.get("surface_signals") or []),
                "deeper_hypotheses_json": json_dumps(item.get("deeper_hypotheses") or []),
                "good_responses_json": json_dumps(item.get("good_responses") or []), "bad_responses_json": json_dumps(item.get("bad_responses") or []),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "need_model_id")
        for item in _list(model.get("expression_state_models")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("expression_or_style") or "unknown_expression")[:500]
            upsert(con, "brain2_expression_state_models", {
                "expression_model_id": stable_id("b2expr", person_id, key), "person_id": person_id, "export_id": export_id,
                "expression_or_style": key, "contexts_json": json_dumps(item.get("contexts") or []),
                "possible_meanings_json": json_dumps(item.get("possible_meanings") or []), "state_links_json": json_dumps(item.get("state_links") or []),
                "response_implications_json": json_dumps(item.get("response_implications") or []),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "expression_model_id")
        for item in _list(model.get("emotional_trajectory_models")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("trajectory_name") or "unknown_trajectory")[:500]
            upsert(con, "brain2_emotional_trajectory_models", {
                "trajectory_model_id": stable_id("b2traj", person_id, key), "person_id": person_id, "export_id": export_id,
                "trajectory_name": key, "starting_conditions_json": json_dumps(item.get("starting_conditions") or []),
                "live_signals_to_watch_json": json_dumps(item.get("live_signals_to_watch") or []),
                "likely_next_states_json": json_dumps(item.get("likely_next_states") or []), "risks_json": json_dumps(item.get("risks") or []),
                "opportunities_json": json_dumps(item.get("opportunities") or []), "intervention_windows_json": json_dumps(item.get("intervention_windows") or []),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "trajectory_model_id")
        for item in _list(model.get("contextual_self_models")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("context_key") or "unknown_context")[:500]
            upsert(con, "brain2_contextual_self_models", {
                "contextual_model_id": stable_id("b2ctxself", person_id, key), "person_id": person_id, "export_id": export_id,
                "context_key": key, "self_state_summary": item.get("self_state_summary"),
                "strengths_json": json_dumps(item.get("strengths") or []), "vulnerabilities_json": json_dumps(item.get("vulnerabilities") or []),
                "needs_json": json_dumps(item.get("needs") or []), "best_moves_json": json_dumps(item.get("best_moves") or []),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "contextual_model_id")
        for item in _list(model.get("live_prediction_hooks")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("hook_name") or "unknown_hook")[:500]
            upsert(con, "brain2_live_prediction_hooks", {
                "hook_id": stable_id("b2hook", person_id, key), "person_id": person_id, "export_id": export_id,
                "hook_name": key, "horizon": item.get("horizon") or "H1",
                "domain": item.get("domain") or item.get("situation_domain"),
                "active_person_hint": item.get("active_person_hint") or item.get("person_hint") or item.get("known_person_hint"),
                "risk_type": item.get("risk_type") or item.get("risk_category"),
                "user_common_bad_move": item.get("user_common_bad_move") or item.get("common_bad_move"),
                "recommended_micro_move": item.get("recommended_micro_move") or item.get("micro_move"),
                "do_not_say_json": json_dumps(item.get("do_not_say") or item.get("avoid_phrases") or []),
                "intervention_mode": item.get("intervention_mode") or item.get("brainlive_action") or "watch",
                "outcome_success_count": int(item.get("outcome_success_count") or 0),
                "outcome_failure_count": int(item.get("outcome_failure_count") or 0),
                "calibration_score": _clamp(item.get("calibration_score"), 0.0),
                "use_policy": item.get("use_policy") or "silent_context",
                "activation_conditions_json": json_dumps(item.get("activation_conditions") or []),
                "predicts_json": json_dumps(item.get("predicts") or {}), "watch_signals_json": json_dumps(item.get("watch_signals") or []),
                "proactive_options_json": json_dumps(item.get("proactive_options") or []), "silence_policy_json": json_dumps(item.get("silence_policy") or {}),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "hook_id")
        for item in _list(model.get("live_affordance_preferences")):
            if not isinstance(item, dict):
                continue
            key = str(item.get("affordance_type") or "unknown_affordance")[:500]
            upsert(con, "brain2_live_affordance_preferences", {
                "affordance_pref_id": stable_id("b2affpref", person_id, key), "person_id": person_id, "export_id": export_id,
                "affordance_type": key, "preferred_when_json": json_dumps(item.get("preferred_when") or []),
                "personal_fit_criteria_json": json_dumps(item.get("personal_fit_criteria") or []),
                "live_detection_needs_json": json_dumps(item.get("live_detection_needs") or []), "intervention_value_json": json_dumps(item.get("intervention_value") or {}),
                "evidence_json": json_dumps(item.get("evidence") or []), "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": _clamp(item.get("confidence")), "status": "active", "created_at": now, "updated_at": now,
            }, "affordance_pref_id")
        con.commit()


def build_brain2_canonical_life_model(person_id: str, *, period_start: str | None = None, period_end: str | None = None, use_llm: bool = True, timeout: float = 180.0, limit: int = 120) -> dict[str, Any]:
    ensure_life_model_schema()
    raw = collect_canonical_evidence(person_id, period_start=period_start, period_end=period_end, limit=limit)
    error: str | None = None
    if use_llm:
        model, error = synthesize_canonical_life_model(raw, timeout=timeout)
        status = "llm_ready" if not error else "raw_ready_llm_required"
    else:
        model = {"llm_required": True, "raw_evidence_available": True, "reason": "use_llm=false"}
        status = "raw_only_llm_disabled"
    now = now_iso()
    export_id = stable_id("b2life", person_id, period_start or "all", period_end or "now", now)
    with connect() as con:
        upsert(con, "brain2_life_model_exports", {
            "export_id": export_id, "person_id": person_id, "period_start": period_start, "period_end": period_end,
            "status": status, "source_counts_json": json_dumps(_count(raw)), "raw_evidence_json": json_dumps(raw),
            "canonical_model_json": json_dumps(model), "llm_model": None, "error_text": error, "created_at": now,
        }, "export_id")
        con.commit()
    if isinstance(model, dict) and not model.get("llm_required"):
        store_canonical_life_model(person_id, export_id, model)
    return {"version": VERSION, "export_id": export_id, "person_id": person_id, "status": status, "source_counts": _count(raw), "canonical_model": model}


def latest_canonical_life_model(person_id: str) -> dict[str, Any] | None:
    ensure_life_model_schema()
    with connect() as con:
        row = con.execute("SELECT * FROM brain2_life_model_exports WHERE person_id=? ORDER BY created_at DESC LIMIT 1", (person_id,)).fetchone()
        if not row:
            return None
        return {"export_id": row["export_id"], "status": row["status"], "source_counts": json_loads(row["source_counts_json"], {}), "canonical_model": json_loads(row["canonical_model_json"], {}), "created_at": row["created_at"]}


def brain2_life_model_audit(person_id: str) -> dict[str, Any]:
    ensure_life_model_schema()
    raw = collect_canonical_evidence(person_id, limit=40)
    counts = _count(raw)
    latest = latest_canonical_life_model(person_id)
    with connect() as con:
        canonical_counts = {}
        for table in [
            "brain2_personal_routine_models", "brain2_place_preference_models", "brain2_action_preference_models",
            "brain2_need_expectation_models", "brain2_expression_state_models", "brain2_emotional_trajectory_models",
            "brain2_contextual_self_models", "brain2_live_prediction_hooks", "brain2_live_affordance_preferences",
        ]:
            if _table_exists(con, table):
                row = con.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE person_id=? AND status='active'", (person_id,)).fetchone()
                canonical_counts[table] = int(row["c"] if row else 0)
            else:
                canonical_counts[table] = 0
    missing = [k for k, v in canonical_counts.items() if v == 0]
    return {"version": VERSION, "person_id": person_id, "raw_source_counts": counts, "canonical_counts": canonical_counts, "missing_canonical_layers": missing, "latest_export": latest, "verdict": "ready" if latest and not missing else "needs_compile_or_data"}


def audit_brain2_heuristics() -> dict[str, Any]:
    """Record known legacy heuristic locations and how V15.10 treats them.

    We do not delete legacy V12 functions because older tests/flows may still use
    them. Instead, Brain2 canonical life model and BrainLive ignore heuristic-only
    psychological inference and require LLM canonical exports for live use.
    """
    ensure_life_model_schema()
    now = now_iso()
    issues = [
        ("behavior_v12.py", "_classify_speech_act", "keyword_rule", "medium", "Legacy deterministic speech-act tagging uses keywords; acceptable only as surface metadata, not deep psychology.", "Do not use for BrainLive psychological inference; canonical compiler uses LLM/evidence."),
        ("behavior_v12.py", "_infer_state", "keyword_psychology", "high", "Legacy internal-state scoring uses keyword/score heuristics.", "Excluded from canonical life meaning unless confirmed by Brain2 LLM/evidence; use LLM canonical models."),
        ("behavior_v12.py", "_episode_type_from_text/_situation_type_from_act", "keyword_context", "medium", "Legacy context classification uses keywords.", "Use only as low-trust routing metadata; canonical life model requires evidence/LLM synthesis."),
        ("behavior_v12.py", "_extract_options/_action_type", "regex_or_keyword", "medium", "Legacy option/action extraction uses regex/keywords.", "Not used as final live need/intent; LLM canonical compiler must interpret actions."),
    ]
    with connect() as con:
        for module, func, issue_type, severity, desc, action in issues:
            upsert(con, "brain2_heuristic_audit", {
                "audit_id": stable_id("heur", module, func, issue_type), "module": module, "function_name": func,
                "issue_type": issue_type, "severity": severity, "description": desc, "action_taken": action, "created_at": now,
            }, "audit_id")
        con.commit()
    return {"version": VERSION, "issues": [{"module": m, "function": f, "issue_type": t, "severity": s, "action_taken": a} for m, f, t, s, _d, a in issues], "policy": "BrainLive/Brain2 canonical live model uses LLM/evidence only for psychology; legacy heuristics are quarantined as low-trust metadata."}

# V18 remediation overrides: owner-scoped evidence, explicit truncation,
# evidence-validated canonical rows, and versioned life-model projections.
from .v18_life_model import install_canonical as _install_v18_life_model_canonical
_globals_v18_life_model_canonical = _install_v18_life_model_canonical(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_life_model_canonical)
