from __future__ import annotations

"""V15.9 Brain2 -> BrainLive personal model feed.

BrainLive does not need a second Brain2. It needs Brain2 to expose the right
*live-ready* material: what William does, where, with whom, what he says, his
expressions, emotional/need patterns, preferences, expectations, routines,
short/medium/long forecasts and intervention principles.

This module builds that feed from the existing Brain2/V13/V14 tables and stores
it as a hot export BrainLive can preload. It does not infer psychological meaning
by regex or keywords. It either:

- copies already-extracted Brain2 structures; or
- asks the same strict local LLM JSON client used by Brain2 to synthesize a
  live-ready index from those structures.

If the LLM is unavailable, the raw feed is still available but interpretation is
marked llm_required.
"""

from typing import Any
from uuid import uuid4

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .v18_legacy_forecasts import active_legacy_forecasts as _active_v14_forecasts
from .integrity_v176 import active_forecast_sql
from .llm_contracts_v15_18 import memory_usability

VERSION = "15.9.0-brain2-live-personal-model"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS brainlive_personal_model_exports(
  export_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  active_people_json TEXT DEFAULT '[]',
  place_hint TEXT,
  topic_hint TEXT,
  source_counts_json TEXT DEFAULT '{}',
  raw_feed_json TEXT DEFAULT '{}',
  live_ready_json TEXT DEFAULT '{}',
  status TEXT NOT NULL,
  llm_model TEXT,
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brainlive_live_relevance_index(
  index_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  export_id TEXT,
  live_session_id TEXT,
  index_type TEXT NOT NULL,
  key TEXT NOT NULL,
  summary TEXT NOT NULL,
  activation_contexts_json TEXT DEFAULT '[]',
  live_use_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blpm_exports_person_time ON brainlive_personal_model_exports(person_id, created_at);
CREATE INDEX IF NOT EXISTS idx_blpm_index_person_type ON brainlive_live_relevance_index(person_id, index_type, status, confidence);
"""

LIVE_READY_SCHEMA: dict[str, Any] = {
    "identity_model": {"who_william_is_operationally": "", "stable_traits": [], "current_unknowns": [], "confidence": 0.0},
    "routines": [
        {"name": "", "when": "", "where": "", "trigger_contexts": [], "usual_actions": [], "likely_needs": [], "preferred_conditions": [], "future_prediction_use": "", "confidence": 0.0, "evidence": [], "counter_evidence": []}
    ],
    "places": [
        {"place": "", "meaning_for_william": "", "usual_actions_there": [], "preferred_affordances": [], "risks_or_opportunities": [], "confidence": 0.0, "evidence": []}
    ],
    "language_and_expressions": [
        {"expression_or_style": "", "personal_meaning": "", "contexts": [], "emotions_or_needs_often_linked": [], "response_implication": "", "confidence": 0.0, "evidence": []}
    ],
    "needs_expectations_preferences": [
        {"item": "", "kind": "need|expectation|preference|boundary|value|goal|avoidance", "activation_contexts": [], "how_it_shows_up": [], "what_helps": [], "what_hurts": [], "confidence": 0.0, "evidence": []}
    ],
    "emotional_state_patterns": [
        {"state_or_pattern": "", "past_signals": [], "current_live_signals_to_watch": [], "future_risk_or_need": "", "confidence": 0.0, "evidence": []}
    ],
    "relationship_live_packs": [
        {"person_or_group": "", "known_loops": [], "good_moves": [], "bad_moves": [], "watch_signals": [], "confidence": 0.0, "evidence": []}
    ],
    "forecast_hooks": [
        {"forecast": "", "horizon": "H0|H1|H2|day|week|long", "activation_conditions": [], "intervention_options": [], "confidence": 0.0, "evidence": []}
    ],
    "brainlive_operational_rules": [
        {"rule": "", "when_to_use": [], "when_not_to_use": [], "confidence": 0.0, "evidence": []}
    ],
    "missing_for_magic": [],
}


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _columns(con, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _many(con, table: str, where: str = "", params: tuple[Any, ...] = (), order_limit: str = "") -> list[dict[str, Any]]:
    if not _table_exists(con, table):
        return []
    try:
        sql = f"SELECT * FROM {table} {where} {order_limit}"
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def _active_live_prediction_hooks_for_feed(con, person_id: str, limit: int = 80) -> list[dict[str, Any]]:
    try:
        from .brainlive_brain2_coordination_v15_12 import _active_live_prediction_hooks
        return _active_live_prediction_hooks(con, person_id, limit)
    except Exception:
        # Fail closed-ish: if the lifecycle-aware helper is unavailable, still
        # refuse explicit do_not_use/forbidden hooks when the column exists.
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(brain2_live_prediction_hooks)").fetchall()}
            if "use_policy" in cols:
                return [dict(r) for r in con.execute("""
                    SELECT * FROM brain2_live_prediction_hooks
                    WHERE person_id=? AND status='active'
                      AND COALESCE(use_policy, 'silent_context') NOT IN ('do_not_use','forbidden')
                    ORDER BY confidence DESC, updated_at DESC LIMIT ?
                """, (person_id, limit)).fetchall()]
            return [dict(r) for r in con.execute("SELECT * FROM brain2_live_prediction_hooks WHERE person_id=? AND status='active' ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)).fetchall()]
        except Exception:
            return []


def _one(con, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    try:
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _compact(rows: list[dict[str, Any]], limit: int = 40, max_str: int = 900) -> list[dict[str, Any]]:
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
        if {"truth_status", "lifecycle_status", "confidence"}.issubset(set(nr.keys())):
            try:
                meta = json_loads(nr.get("metadata_json"), {}) if isinstance(nr.get("metadata_json"), str) else {}
            except Exception:
                meta = {}
            nr["v15_18_memory_usability"] = memory_usability(
                truth_status=nr.get("truth_status"),
                lifecycle_status=nr.get("lifecycle_status"),
                confidence=nr.get("confidence"),
                evidence_count=nr.get("evidence_count"),
                metadata=meta if isinstance(meta, dict) else {},
            )
        out.append(nr)
    return out


def ensure_personal_model_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(SCHEMA)
        con.commit()


def _count_section(feed: dict[str, Any]) -> dict[str, int]:
    counts = {}
    for k, v in feed.items():
        if isinstance(v, list):
            counts[k] = len(v)
        elif isinstance(v, dict):
            counts[k] = sum(len(x) if isinstance(x, list) else 1 for x in v.values())
        else:
            counts[k] = 1 if v else 0
    return counts




def _canonical_active_rows(con, table: str, pk: str, person_id: str, *, order: str, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(con, table):
        return []
    cols = _columns(con, table)
    lifecycle_clause = ""
    params: tuple[Any, ...]
    if _table_exists(con, "brain2_life_model_item_lifecycle"):
        lifecycle_clause = f"""
          AND NOT EXISTS (
            SELECT 1 FROM brain2_life_model_item_lifecycle lc
            WHERE lc.person_id=? AND lc.source_table=? AND lc.source_id=t.{pk}
              AND (
                COALESCE(lc.truth_status,'candidate') IN ('contradicted','obsolete','rejected','false','wrong')
                OR COALESCE(lc.use_policy,'watch_only') IN ('do_not_use','forbidden','never_use')
              )
          )
        """
        params = (person_id, person_id, table, limit)
    else:
        params = (person_id, limit)
    use_policy_clause = ""
    if "use_policy" in cols:
        use_policy_clause = "AND COALESCE(t.use_policy,'silent_context') NOT IN ('do_not_use','forbidden','never_use')"
    sql = f"""
        SELECT t.* FROM {table} t
        WHERE t.person_id=? AND COALESCE(t.status,'active')='active'
          {use_policy_clause}
          {lifecycle_clause}
        ORDER BY {order}
        LIMIT ?
    """
    return _query(con, sql, params)

def collect_brain2_life_feed(
    person_id: str,
    *,
    live_session_id: str | None = None,
    active_people: list[str] | None = None,
    place_hint: str | None = None,
    topic_hint: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Collect all Brain2 material that BrainLive needs for live prediction.

    This intentionally covers more than the previous BrainLive context: routines,
    places, life events, utterance analyses, personal language, emotion evidence,
    self facts, patterns, forecasts, relationship models and intervention policy.
    """
    active_people = active_people or []
    try:
        from .brain2_life_model_v15_10 import ensure_life_model_schema
        ensure_life_model_schema()
    except Exception:
        pass
    with connect() as con:
        feed: dict[str, Any] = {
            "person_id": person_id,
            "live_session_id": live_session_id,
            "active_people": active_people,
            "place_hint": place_hint,
            "topic_hint": topic_hint,
        }
        feed["self_model"] = {
            "dimensions": _compact(_query(con, "SELECT * FROM self_model_dimensions WHERE person_id=? ORDER BY confidence DESC, evidence_count DESC LIMIT ?", (person_id, limit)), limit),
            "facts": _compact(_query(con, "SELECT * FROM self_model_facts WHERE (scope=? OR scope IS NULL OR scope IN ('global','life','identity')) ORDER BY confidence DESC, evidence_count DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "v14_readings": _compact(_query(con, "SELECT * FROM v14_self_model_readings WHERE person_id=? ORDER BY confidence DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
            "exports": _compact(_query(con, "SELECT * FROM v14_3_self_model_exports WHERE person_id=? ORDER BY created_at DESC LIMIT 5", (person_id,)), 5),
        }
        feed["routines_places_actions"] = {
            "life_events": _compact(_query(con, "SELECT * FROM life_events WHERE (subject_person_id=? OR subject_person_id IS NULL) ORDER BY occurred_start DESC, created_at DESC LIMIT ?", (person_id, limit * 2)), limit * 2),
            "life_event_entities": _compact(_query(con, "SELECT lee.* FROM life_event_entities lee JOIN life_events le ON le.event_id=lee.event_id WHERE (le.subject_person_id=? OR le.subject_person_id IS NULL) ORDER BY lee.created_at DESC LIMIT ?", (person_id, limit * 2)), limit * 2),
            "memory_cards_routine_place": _compact(_query(con, "SELECT * FROM memory_cards WHERE person_id=? AND lifecycle_status='active' AND (card_type IN ('routine','preference','life_event','place','habit','goal','need') OR topic IS NOT NULL) ORDER BY importance_score DESC, confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "memory_facets": _compact(_query(con, "SELECT * FROM memory_facets WHERE target_table IN ('memory_cards','life_events','self_model_facts') ORDER BY weight DESC, confidence DESC, created_at DESC LIMIT ?", (limit * 2,)), limit * 2),
            "brainlive_routines": _compact(_query(con, "SELECT * FROM brainlive_routine_cards WHERE person_id=? AND status='active' ORDER BY confidence DESC, support_count DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "brainlive_routine_observations": _compact(_query(con, "SELECT * FROM brainlive_routine_observations WHERE person_id=? ORDER BY observed_at DESC LIMIT ?", (person_id, limit * 2)), limit * 2),
        }
        feed["language_expressions_parole"] = {
            "personal_language_patterns": _compact(_query(con, "SELECT * FROM personal_language_patterns WHERE person_id=? ORDER BY confidence DESC, frequency DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "phrase_templates": _compact(_query(con, "SELECT * FROM phrase_templates WHERE person_id=? ORDER BY confidence DESC, frequency DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "expression_signals": _compact(_query(con, "SELECT e.* FROM expression_signals e JOIN turns t ON t.turn_id=e.turn_id WHERE (t.person_id=? OR t.person_id IS NULL) ORDER BY e.created_at DESC LIMIT ?", (person_id, limit)), limit),
            "utterance_analyses": _compact(_query(con, "SELECT ua.* FROM utterance_analyses ua JOIN turns t ON t.turn_id=ua.turn_id WHERE (t.person_id=? OR t.person_id IS NULL) ORDER BY ua.created_at DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["emotions_needs_expectations"] = {
            "emotion_evidence": _compact(_query(con, "SELECT * FROM emotion_evidence WHERE (person_id=? OR person_id IS NULL) ORDER BY updated_at DESC, confidence DESC LIMIT ?", (person_id, limit)), limit),
            "memory_frames": _compact(_query(con, "SELECT * FROM memory_frames WHERE (actor_person_id=? OR actor_person_id IS NULL) ORDER BY frame_time DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
            "behavior_signals": _compact(_query(con, "SELECT * FROM behavior_signals WHERE (person_id=? OR person_id IS NULL) ORDER BY updated_at DESC, confidence DESC LIMIT ?", (person_id, limit)), limit),
            "confirmed_patterns": _compact(_query(con, "SELECT * FROM confirmed_patterns WHERE person_id=? ORDER BY confidence DESC, evidence_count DESC LIMIT ?", (person_id, limit)), limit),
            "candidate_patterns": _compact(_query(con, "SELECT * FROM candidate_patterns WHERE person_id=? ORDER BY confidence DESC, evidence_count DESC LIMIT ?", (person_id, limit)), limit),
            "loop_patterns": _compact(_query(con, "SELECT * FROM loop_patterns WHERE person_id=? ORDER BY confidence DESC, evidence_count DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["relationships"] = {
            "people_profiles": _compact(_query(con, "SELECT * FROM v14_5_people_context_profiles WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "open_loops": _compact(_query(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? AND current_status IN ('open','active','pending','watching') ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "relationship_states": _compact(_query(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "interpersonal_loops": _compact(_query(con, "SELECT * FROM v14_6_interpersonal_loop_cards WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "emotional_couplings": _compact(_query(con, "SELECT * FROM v14_6_interpersonal_emotional_couplings WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "active_people_focus": [],
        }
        if active_people:
            focus = []
            for other in active_people[:10]:
                focus.extend(_query(con, """SELECT * FROM v14_6_relationship_state_models
                    WHERE person_id=? AND (known_person_id=? OR person_hint=? OR person_hint LIKE ?)
                    ORDER BY confidence DESC, updated_at DESC LIMIT 5""", (person_id, other, other, f"%{other}%")))
                focus.extend(_query(con, """SELECT * FROM v14_5_people_context_profiles
                    WHERE person_id=? AND (known_person_id=? OR person_hint=? OR person_hint LIKE ? OR speaker_label=?)
                    ORDER BY confidence DESC, updated_at DESC LIMIT 5""", (person_id, other, other, f"%{other}%", other)))
            feed["relationships"]["active_people_focus"] = _compact(focus, 30)
        feed["forecasts_future"] = {
            "predictions": _compact(_query(con, "SELECT * FROM predictions WHERE (person_id=? OR person_id IS NULL) AND status IN ('open','active','watch') ORDER BY confidence DESC, created_at DESC LIMIT ?", (person_id, limit)), limit),
            "future_scenarios": _compact(_query(con, "SELECT * FROM future_scenarios WHERE (person_id=? OR person_id IS NULL) AND status IN ('open','active') ORDER BY probability DESC, opportunity_level DESC, risk_level DESC LIMIT ?", (person_id, limit)), limit),
            "trajectory_warnings": _compact(_query(con, "SELECT * FROM trajectory_warnings WHERE (person_id=? OR person_id IS NULL) AND status IN ('open','active') ORDER BY severity DESC, probability DESC LIMIT ?", (person_id, limit)), limit),
            "v14_trajectory_forecasts": _compact(_active_v14_forecasts(con, person_id, "v14_trajectory_forecasts", limit), limit),
            "v14_forecast_watch_queue": _compact(_active_v14_forecasts(con, person_id, "v14_forecast_watch_queue", limit), limit),
            "brainlive_short_horizon_forecasts": _compact(_query(con, f"SELECT * FROM brainlive_short_horizon_forecasts f WHERE f.person_id=? AND {active_forecast_sql('f')} ORDER BY COALESCE(f.epistemic_confidence, f.confidence) DESC, f.probability DESC, COALESCE(f.occurred_at, f.created_at) DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["intervention_learning"] = {
            "v14_7_policies": _compact(_query(con, "SELECT * FROM v14_7_intervention_policies WHERE person_id=? LIMIT 5", (person_id,)), 5),
            "v14_7_feedback": _compact(_query(con, "SELECT * FROM v14_7_intervention_feedback WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
            "brainlive_interventions": _compact(_query(con, "SELECT * FROM brainlive_intervention_candidates WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
            "brainlive_prediction_outcomes": _compact(_query(con, "SELECT * FROM brainlive_prediction_outcomes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
            "disagreements": _compact(_query(con, "SELECT * FROM brainlive_user_disagreement_events WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
        }
        feed["vision_live_material"] = {
            "vision_observations": _compact(_query(con, "SELECT * FROM vision_scene_observations ORDER BY created_at DESC LIMIT ?", (limit,)), limit),
            "vlm_observations": _compact(_query(con, "SELECT * FROM brainlive_vlm_observations_v154 ORDER BY created_at DESC LIMIT ?", (limit,)), limit),
            "affordance_matches": _compact(_query(con, "SELECT * FROM brainlive_affordance_matches WHERE person_id=? AND status='active' ORDER BY personal_fit DESC, time_sensitivity DESC LIMIT ?", (person_id, limit)), limit),
        }
        # V15.10 canonical Brain2 life model: official live-ready source when compiled.
        feed["brain2_canonical_life_model"] = {
            "exports": _compact(_query(con, "SELECT * FROM brain2_life_model_exports WHERE person_id=? ORDER BY created_at DESC LIMIT 3", (person_id,)), 3),
            "routines": _compact(_canonical_active_rows(con, "brain2_personal_routine_models", "routine_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "places": _compact(_canonical_active_rows(con, "brain2_place_preference_models", "place_model_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "actions": _compact(_canonical_active_rows(con, "brain2_action_preference_models", "action_model_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "needs_expectations": _compact(_canonical_active_rows(con, "brain2_need_expectation_models", "need_model_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "expressions_states": _compact(_canonical_active_rows(con, "brain2_expression_state_models", "expression_model_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "emotional_trajectories": _compact(_canonical_active_rows(con, "brain2_emotional_trajectory_models", "trajectory_model_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "contextual_self": _compact(_canonical_active_rows(con, "brain2_contextual_self_models", "contextual_model_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "live_prediction_hooks": _compact(_active_live_prediction_hooks_for_feed(con, person_id, limit), limit),
            "affordance_preferences": _compact(_canonical_active_rows(con, "brain2_live_affordance_preferences", "affordance_pref_id", person_id, order="confidence DESC, updated_at DESC", limit=limit), limit),
            "watch_bindings_v1512": _compact(_query(con, "SELECT * FROM brain2_live_watch_bindings WHERE person_id=? AND status='active' ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "lifecycle_v1512": _compact(_query(con, "SELECT * FROM brain2_life_model_lifecycle WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "recent_reconciliations_v1512": _compact(_query(con, "SELECT * FROM brainlive_brain2_reconciliations WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
            "strata_v1513": _compact(_query(con, "SELECT * FROM brain2_life_model_strata WHERE person_id=? ORDER BY updated_at DESC", (person_id,)), 10),
            "item_lifecycle_v1513": _compact(_query(con, "SELECT * FROM brain2_life_model_item_lifecycle WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
            "patch_runs_v1513": _compact(_query(con, "SELECT * FROM brain2_life_model_patch_runs WHERE person_id=? ORDER BY created_at DESC LIMIT 5", (person_id,)), 5),
        }
        feed["brainlive_brain2_coordination_v1512"] = {
            "day_packages": _compact(_query(con, "SELECT * FROM brainlive_day_packages WHERE person_id=? ORDER BY created_at DESC LIMIT 5", (person_id,)), 5),
            "watch_bindings": feed["brain2_canonical_life_model"].get("watch_bindings_v1512", []),
            "lifecycle": feed["brain2_canonical_life_model"].get("lifecycle_v1512", []),
            "reconciliations": feed["brain2_canonical_life_model"].get("recent_reconciliations_v1512", []),
        }
        return feed


def _query(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def synthesize_live_ready_model(raw_feed: dict[str, Any], *, timeout: float = 90.0) -> tuple[dict[str, Any], str | None]:
    """Ask the local Brain2-style LLM to transform raw Brain2 material into live-ready cognition.

    This is where Brain2 becomes operational for BrainLive: not just rows, but
    routines, places, parole, needs, expectations, future hooks and intervention
    principles. No fallback psychology is used.
    """
    try:
        client = OllamaJsonClient()
        system = (
            "Tu es Brain2→BrainLive Personal Model Compiler. "
            "Tu ne fais aucune déduction par mots-clés. Tu synthétises uniquement à partir des preuves Brain2 fournies. "
            "But: produire un modèle live exploitable pour anticiper William à H0/H1/H2: routines, lieux, actions, paroles, expressions, émotions passées/actuelles probables/futures, besoins, attentes, préférences, relations, scénarios et règles d'intervention. "
            "Chaque élément doit avoir preuves, contre-preuves si disponibles, confidence, et missing evidence. JSON strict."
        )
        prompt = json_dumps({
            "mission": "Compile Brain2 into a live-ready personal operating model for BrainLive. Think about 1000 situations, not one cigarette example: conversations, negotiation, work, fatigue, ideas, social, places, routines, needs, preferences, expectations, risks, opportunities.",
            "raw_brain2_feed": raw_feed,
            "horizons": {"H0": "0-10 seconds", "H1": "10 seconds-5 minutes", "H2": "5 minutes-2 hours", "day_week_long": "Brain2 trajectories that can become active now"},
            "rules": [
                "No generic advice.",
                "No regex or keyword psychology.",
                "Only infer from evidence in Brain2 feed.",
                "Prefer multiple hypotheses with missing evidence over certainty.",
                "Make this directly useful to a live system that must decide observe/speak/wait.",
            ],
        })
        data = client.require_json(system, prompt, schema_hint=LIVE_READY_SCHEMA, timeout=timeout)
        return data, None
    except Exception as exc:
        return {"llm_required": True, "error": str(exc), "raw_feed_available": True}, str(exc)


def build_brain2_live_personal_model(
    person_id: str,
    *,
    live_session_id: str | None = None,
    active_people: list[str] | None = None,
    place_hint: str | None = None,
    topic_hint: str | None = None,
    use_llm: bool = True,
    timeout: float = 90.0,
    limit: int = 50,
) -> dict[str, Any]:
    """Build and persist the full Brain2 feed BrainLive needs."""
    ensure_personal_model_schema()
    raw = collect_brain2_life_feed(person_id, live_session_id=live_session_id, active_people=active_people, place_hint=place_hint, topic_hint=topic_hint, limit=limit)
    live_ready: dict[str, Any]
    error: str | None = None
    status = "raw_only_llm_disabled"
    if use_llm:
        live_ready, error = synthesize_live_ready_model(raw, timeout=timeout)
        status = "llm_ready" if not error else "raw_ready_llm_required"
    else:
        live_ready = {"llm_required": True, "reason": "use_llm=false", "raw_feed_available": True}
    now = now_iso()
    export_id = stable_id("blpm", person_id, live_session_id or "global", now)
    with connect() as con:
        row = {
            "export_id": export_id,
            "person_id": person_id,
            "live_session_id": live_session_id,
            "active_people_json": json_dumps(active_people or []),
            "place_hint": place_hint,
            "topic_hint": topic_hint,
            "source_counts_json": json_dumps(_count_section(raw)),
            "raw_feed_json": json_dumps(raw),
            "live_ready_json": json_dumps(live_ready),
            "status": status,
            "llm_model": None,
            "error_text": error,
            "created_at": now,
        }
        upsert(con, "brainlive_personal_model_exports", row, "export_id")
        if isinstance(live_ready, dict) and not live_ready.get("llm_required"):
            _store_relevance_index(con, person_id, export_id, live_session_id, live_ready)
        con.commit()
    return {"version": VERSION, "export_id": export_id, "person_id": person_id, "status": status, "source_counts": _count_section(raw), "raw_feed": raw, "live_ready": live_ready}


def _store_relevance_index(con, person_id: str, export_id: str, live_session_id: str | None, live_ready: dict[str, Any]) -> None:
    now = now_iso()
    groups = {
        "routine": live_ready.get("routines") or [],
        "place": live_ready.get("places") or [],
        "language_expression": live_ready.get("language_and_expressions") or [],
        "need_expectation_preference": live_ready.get("needs_expectations_preferences") or [],
        "emotional_pattern": live_ready.get("emotional_state_patterns") or [],
        "relationship_live_pack": live_ready.get("relationship_live_packs") or [],
        "forecast_hook": live_ready.get("forecast_hooks") or [],
        "operational_rule": live_ready.get("brainlive_operational_rules") or [],
    }
    for index_type, items in groups.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("name") or item.get("place") or item.get("expression_or_style") or item.get("item") or item.get("state_or_pattern") or item.get("person_or_group") or item.get("forecast") or item.get("rule") or str(uuid4())
            summary = item.get("future_prediction_use") or item.get("meaning_for_william") or item.get("personal_meaning") or item.get("response_implication") or item.get("future_risk_or_need") or item.get("forecast") or item.get("rule") or key
            conf = _clamp(item.get("confidence"), 0.5)
            activation = item.get("trigger_contexts") or item.get("activation_contexts") or item.get("contexts") or item.get("current_live_signals_to_watch") or item.get("watch_signals") or item.get("activation_conditions") or item.get("when_to_use") or []
            upsert(con, "brainlive_live_relevance_index", {
                "index_id": stable_id("blidx", person_id, export_id, index_type, key),
                "person_id": person_id,
                "export_id": export_id,
                "live_session_id": live_session_id,
                "index_type": index_type,
                "key": str(key)[:500],
                "summary": str(summary)[:4000],
                "activation_contexts_json": json_dumps(activation),
                "live_use_json": json_dumps(item),
                "evidence_json": json_dumps(item.get("evidence") or []),
                "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                "confidence": conf,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }, "index_id")


def _clamp(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except Exception:
        x = default
    return max(0.0, min(1.0, x))


def latest_live_personal_model(person_id: str, *, live_session_id: str | None = None, max_age_hours: float = 12.0) -> dict[str, Any] | None:
    ensure_personal_model_schema()
    with connect() as con:
        if live_session_id:
            row = _one(con, "SELECT * FROM brainlive_personal_model_exports WHERE person_id=? AND (live_session_id=? OR live_session_id IS NULL) ORDER BY created_at DESC LIMIT 1", (person_id, live_session_id))
        else:
            row = _one(con, "SELECT * FROM brainlive_personal_model_exports WHERE person_id=? ORDER BY created_at DESC LIMIT 1", (person_id,))
        if not row:
            return None
        return {
            "export_id": row.get("export_id"),
            "status": row.get("status"),
            "source_counts": json_loads(row.get("source_counts_json"), {}),
            "raw_feed": json_loads(row.get("raw_feed_json"), {}),
            "live_ready": json_loads(row.get("live_ready_json"), {}),
            "created_at": row.get("created_at"),
        }


def brainlive_personal_model_audit(person_id: str) -> dict[str, Any]:
    ensure_personal_model_schema()
    raw = collect_brain2_life_feed(person_id, limit=25)
    counts = _count_section(raw)
    missing = []
    critical = {
        "self_model": counts.get("self_model", 0),
        "routines_places_actions": counts.get("routines_places_actions", 0),
        "language_expressions_parole": counts.get("language_expressions_parole", 0),
        "emotions_needs_expectations": counts.get("emotions_needs_expectations", 0),
        "relationships": counts.get("relationships", 0),
        "forecasts_future": counts.get("forecasts_future", 0),
        "intervention_learning": counts.get("intervention_learning", 0),
    }
    for k, v in critical.items():
        if v <= 0:
            missing.append(k)
    latest = latest_live_personal_model(person_id)
    return {"version": VERSION, "person_id": person_id, "critical_counts": critical, "missing_for_magic": missing, "latest_export": latest, "verdict": "ready" if not missing and latest else "needs_export_or_brain2_data"}

# V18: only owner-scoped, active, non-retracted material can enter BrainLive's personal model.
from .v18_personal_model import install as _install_v18_personal_model
_globals_v18_personal_model = _install_v18_personal_model(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_personal_model)
