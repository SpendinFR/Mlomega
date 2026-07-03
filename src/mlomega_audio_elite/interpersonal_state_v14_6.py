from __future__ import annotations

"""V14.6 Other Person Model + Interpersonal Emotional Coupling.

This layer is deliberately separate from V13/V14/V14.2/V14.3/V14.4/V14.5.
It models the social-emotional reality that the user's state and next actions can
be shaped by other people, including brief encounters.

Examples this layer is built to capture:
- a cashier's joyful tone lifts the user's day for the next hour;
- Max being tense makes the user more tense, more direct, or more avoidant;
- one person's vagueness triggers the user's need for clarity;
- the user's pressure changes the other person's state and creates an escalation loop;
- repeated micro-interactions have long-horizon effects even when they look trivial.

The module does not read minds. It stores hypotheses with evidence,
counter-evidence, confidence, time horizon and revision status. Cognitive
interpretation is Qwen/Ollama JSON-contract based. No regex or local keyword
rules are used for emotional or interpersonal conclusions.
"""

from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .people_openloops_v14_5 import ensure_v14_5_schema

V14_6_VERSION = "14.6.0-interpersonal-state-final"

V14_6_TABLES = {
    "v14_6_interpersonal_runs",
    "v14_6_other_person_state_snapshots",
    "v14_6_interpersonal_emotional_couplings",
    "v14_6_micro_interaction_impacts",
    "v14_6_social_aftereffects",
    "v14_6_relationship_state_models",
    "v14_6_interpersonal_loop_cards",
    "v14_6_intervention_suggestions",
    "v14_6_person_model_summaries",
    "v14_6_contract_checks",
}

INTERPERSONAL_SCHEMA: dict[str, Any] = {
    "other_person_state_snapshots": [
        {
            "person_hint": "",
            "known_person_id": None,
            "speaker_label": None,
            "moment_state_summary": "",
            "probable_emotions": [],
            "probable_thoughts": [],
            "probable_needs": [],
            "probable_avoidances": [],
            "arousal_level": "unknown|low|medium|high",
            "valence": "unknown|positive|neutral|negative|mixed",
            "tension_level": "unknown|low|medium|high|critical",
            "openness_level": "unknown|closed|guarded|open|very_open",
            "social_intent_hypothesis": "support|avoid|clarify|challenge|repair|play|ask|control|deflect|connect|unknown|other",
            "evidence_turn_ids": [],
            "evidence_texts": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "emotional_couplings": [
        {
            "source_person_hint": "",
            "target_person_id": "me",
            "source_state": "",
            "target_state_before": "",
            "target_state_after": "",
            "coupling_type": "contagion|escalation|deescalation|regulation|reassurance|joy_lift|tension_transfer|fatigue|threat_response|avoidance|motivation|unknown|other",
            "impact_direction": "positive|negative|mixed|neutral|unknown",
            "impact_strength": "low|medium|high|critical|unknown",
            "latency": "instant|minutes|hours|days|long_term|unknown",
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "micro_interaction_impacts": [
        {
            "interaction_type": "cashier|stranger|short_call|message|family|friend|client|colleague|service|unknown|other",
            "other_person_hint": "",
            "moment_summary": "",
            "other_person_state": "",
            "user_shift": "",
            "possible_aftereffect": "",
            "life_domain_affected": "personal|relationship|professional|project|mood|energy|decision|unknown|other",
            "time_horizon": "minutes|hours|day|week|long_term|unknown",
            "evidence": [],
            "confidence": 0.0,
        }
    ],
    "social_aftereffects": [
        {
            "trigger_person_hint": "",
            "trigger_event_summary": "",
            "user_after_state": "",
            "next_actions_influenced": [],
            "risks_if_unnoticed": [],
            "positive_levers": [],
            "watch_until": "hour|day|week|month|unknown",
            "evidence": [],
            "confidence": 0.0,
        }
    ],
    "relationship_state_models": [
        {
            "person_hint": "",
            "known_person_id": None,
            "relationship_state_summary": "",
            "their_typical_states": [],
            "their_probable_needs_or_motives": [],
            "their_common_avoidances": [],
            "how_user_affects_them": [],
            "how_they_affect_user": [],
            "communication_style": "",
            "sensitive_topics": [],
            "easy_topics": [],
            "repair_conditions": [],
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "interpersonal_loops": [
        {
            "loop_title": "",
            "person_hint": "",
            "loop_sequence": [],
            "user_role_in_loop": "",
            "other_role_in_loop": "",
            "usual_outcome": "",
            "early_warning_signals": [],
            "escape_conditions": [],
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "intervention_suggestions": [
        {
            "person_hint": "",
            "situation": "",
            "suggestion": "",
            "why_it_might_help": "",
            "when_to_use": "",
            "risk_if_used_wrong": "",
            "confidence": 0.0,
        }
    ],
    "person_model_summaries": [
        {
            "person_hint": "",
            "known_person_id": None,
            "summary": "",
            "what_system_thinks_they_are_like": [],
            "what_system_thinks_they_often_think_or_seek": [],
            "what_is_uncertain": [],
            "evidence": [],
            "confidence": 0.0,
        }
    ],
    "missing_context": [],
    "confidence": 0.0,
}


def _clamp(v: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        f = float(v)
    except Exception:
        f = 0.0
    return max(lo, min(hi, f))


def _safe_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    return json_loads(str(value), default if default is not None else {})


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params)]
    except Exception:
        return []


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _compact(rows: list[dict[str, Any]], limit: int = 80) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        compact: dict[str, Any] = {}
        for k, v in row.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > 1800:
                compact[k] = v[:1800] + "…"
            else:
                compact[k] = v
        out.append(compact)
    return out


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any], timeout: int = 480) -> dict[str, Any]:
    data = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=timeout)
    if not isinstance(data, dict):
        raise RuntimeError("Brain2 V14.6 returned non-object JSON")
    return data


def ensure_v14_6_schema() -> None:
    ensure_v14_5_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_6_interpersonal_runs(
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error_text TEXT,
                qwen_output_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_other_person_state_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                person_hint TEXT,
                known_person_id TEXT,
                speaker_label TEXT,
                moment_state_summary TEXT,
                probable_emotions_json TEXT DEFAULT '[]',
                probable_thoughts_json TEXT DEFAULT '[]',
                probable_needs_json TEXT DEFAULT '[]',
                probable_avoidances_json TEXT DEFAULT '[]',
                arousal_level TEXT,
                valence TEXT,
                tension_level TEXT,
                openness_level TEXT,
                social_intent_hypothesis TEXT,
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_interpersonal_emotional_couplings(
                coupling_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                source_person_hint TEXT,
                target_person_id TEXT,
                source_state TEXT,
                target_state_before TEXT,
                target_state_after TEXT,
                coupling_type TEXT,
                impact_direction TEXT,
                impact_strength TEXT,
                latency TEXT,
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_micro_interaction_impacts(
                impact_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                interaction_type TEXT,
                other_person_hint TEXT,
                moment_summary TEXT,
                other_person_state TEXT,
                user_shift TEXT,
                possible_aftereffect TEXT,
                life_domain_affected TEXT,
                time_horizon TEXT,
                evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_social_aftereffects(
                aftereffect_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                trigger_person_hint TEXT,
                trigger_event_summary TEXT,
                user_after_state TEXT,
                next_actions_influenced_json TEXT DEFAULT '[]',
                risks_if_unnoticed_json TEXT DEFAULT '[]',
                positive_levers_json TEXT DEFAULT '[]',
                watch_until TEXT,
                evidence_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'open',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_relationship_state_models(
                model_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                person_hint TEXT,
                known_person_id TEXT,
                relationship_state_summary TEXT,
                their_typical_states_json TEXT DEFAULT '[]',
                their_probable_needs_or_motives_json TEXT DEFAULT '[]',
                their_common_avoidances_json TEXT DEFAULT '[]',
                how_user_affects_them_json TEXT DEFAULT '[]',
                how_they_affect_user_json TEXT DEFAULT '[]',
                communication_style TEXT,
                sensitive_topics_json TEXT DEFAULT '[]',
                easy_topics_json TEXT DEFAULT '[]',
                repair_conditions_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_interpersonal_loop_cards(
                loop_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                person_hint TEXT,
                loop_title TEXT,
                loop_sequence_json TEXT DEFAULT '[]',
                user_role_in_loop TEXT,
                other_role_in_loop TEXT,
                usual_outcome TEXT,
                early_warning_signals_json TEXT DEFAULT '[]',
                escape_conditions_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'hypothesis',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_intervention_suggestions(
                suggestion_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                person_hint TEXT,
                situation TEXT,
                suggestion TEXT,
                why_it_might_help TEXT,
                when_to_use TEXT,
                risk_if_used_wrong TEXT,
                status TEXT DEFAULT 'suggested',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_person_model_summaries(
                summary_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                person_hint TEXT,
                known_person_id TEXT,
                summary TEXT,
                what_system_thinks_they_are_like_json TEXT DEFAULT '[]',
                what_system_thinks_they_often_think_or_seek_json TEXT DEFAULT '[]',
                what_is_uncertain_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_6_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v146_state_person ON v14_6_other_person_state_snapshots(person_id, person_hint, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v146_coupling_person ON v14_6_interpersonal_emotional_couplings(person_id, source_person_hint, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v146_aftereffects_person ON v14_6_social_aftereffects(person_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v146_models_person ON v14_6_relationship_state_models(person_id, person_hint, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v146_loops_person ON v14_6_interpersonal_loop_cards(person_id, person_hint, updated_at);
            """
        )
        now = now_iso()
        for table in sorted(V14_6_TABLES):
            upsert(con, "v14_6_contract_checks", {
                "check_id": stable_id("v146check", table),
                "check_name": f"table:{table}",
                "status": "declared",
                "detail": "V14.6 other-person state, emotional coupling, micro-interaction impact and relationship loop table.",
                "created_at": now,
            }, "check_id")
        con.commit()


def _conversation_payload(con, conversation_id: str, *, limit_turns: int = 180) -> dict[str, Any]:
    conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not conv:
        raise ValueError(f"conversation introuvable: {conversation_id}")
    turns = _many(con, "SELECT * FROM turns WHERE conversation_id=? ORDER BY idx LIMIT ?", (conversation_id, limit_turns))
    utterance_analyses = _many(con, "SELECT * FROM utterance_analyses WHERE conversation_id=? ORDER BY created_at LIMIT ?", (conversation_id, limit_turns))
    speaker_matches = _many(con, "SELECT * FROM speaker_matches WHERE conversation_id=? ORDER BY created_at", (conversation_id,))
    from .v18_brain2_context import conversation_context_addenda
    return {
        "conversation": dict(conv),
        "turns": _compact(turns, limit_turns),
        "context_addenda": conversation_context_addenda(con, conversation_id=conversation_id),
        "utterance_analyses": _compact(utterance_analyses, 120),
        "speaker_matches": _compact(speaker_matches, 80),
    }


def _background(con, person_id: str, *, limit: int = 60) -> dict[str, Any]:
    return {
        "speaker_profiles": _compact(_many(con, "SELECT * FROM speaker_profiles ORDER BY is_user DESC, created_at DESC LIMIT ?", (limit,)), limit),
        "people_identity_hypotheses": _compact(_many(con, "SELECT * FROM v14_5_people_identity_hypotheses ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,)), limit),
        "relationship_cards": _compact(_many(con, "SELECT * FROM v14_5_relationship_inference_cards ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,)), limit),
        "people_context_profiles": _compact(_many(con, "SELECT * FROM v14_5_people_context_profiles ORDER BY updated_at DESC LIMIT ?", (limit,)), limit),
        "people_trigger_maps": _compact(_many(con, "SELECT * FROM v14_people_trigger_maps WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "relationship_models": _compact(_many(con, "SELECT * FROM relationship_models WHERE person_a=? OR person_b=? ORDER BY updated_at DESC LIMIT ?", (person_id, person_id, limit)), limit),
        "recent_user_states": _compact(_many(con, "SELECT * FROM internal_state_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_emotion_evidence": _compact(_many(con, "SELECT * FROM emotion_evidence WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)), limit),
        "existing_couplings": _compact(_many(con, "SELECT * FROM v14_6_interpersonal_emotional_couplings WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "existing_relationship_state_models": _compact(_many(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "existing_social_aftereffects": _compact(_many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
    }


def analyze_interpersonal_state(conversation_id: str, *, person_id: str | None = None) -> dict[str, Any]:
    ensure_v14_6_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        payload = {
            "conversation_payload": _conversation_payload(con, conversation_id),
            "background": _background(con, person_id),
            "person_id": person_id,
            "task": (
                "Model the interpersonal emotional system for this conversation. "
                "Infer other-person moment states, not as facts but as hypotheses. "
                "Detect how another person's state may shift the user's state, next actions, energy, choices or open loops. "
                "Include micro-interactions, short encounters and long-term relationship patterns. "
                "Use only provided evidence. Separate evidence from counter-evidence. "
                "Never confirm identity or relationship as fact; use person_hint/known_person_id and confidence. "
                "Prefer 'unknown' when evidence is weak."
            ),
        }
    run_id = stable_id("v146run", conversation_id, person_id, now_iso())
    now = now_iso()
    status = "ok"
    error = None
    out: dict[str, Any] = {}
    try:
        out = _llm_json(
            "You are Brain2 V14.6 Interpersonal State Mirror. Return strict JSON only. No diagnosis. No mind-reading. Hypotheses must include evidence, counter-evidence and confidence.",
            payload,
            INTERPERSONAL_SCHEMA,
            timeout=540,
        )
    except Exception as exc:
        status = "error"
        error = str(exc)[:2000]
        out = {"error": error}
    with connect() as con:
        if status == "ok":
            for item in out.get("other_person_state_snapshots") or []:
                if not isinstance(item, dict) or not item.get("moment_state_summary"):
                    continue
                person_hint = item.get("person_hint") or item.get("speaker_label") or "unknown"
                snapshot_id = stable_id("v146state", conversation_id, person_hint, item.get("moment_state_summary"))
                upsert(con, "v14_6_other_person_state_snapshots", {
                    "snapshot_id": snapshot_id,
                    "conversation_id": conversation_id,
                    "person_id": person_id,
                    "person_hint": person_hint,
                    "known_person_id": item.get("known_person_id"),
                    "speaker_label": item.get("speaker_label"),
                    "moment_state_summary": item.get("moment_state_summary"),
                    "probable_emotions_json": json_dumps(item.get("probable_emotions") or []),
                    "probable_thoughts_json": json_dumps(item.get("probable_thoughts") or []),
                    "probable_needs_json": json_dumps(item.get("probable_needs") or []),
                    "probable_avoidances_json": json_dumps(item.get("probable_avoidances") or []),
                    "arousal_level": item.get("arousal_level"),
                    "valence": item.get("valence"),
                    "tension_level": item.get("tension_level"),
                    "openness_level": item.get("openness_level"),
                    "social_intent_hypothesis": item.get("social_intent_hypothesis"),
                    "evidence_json": json_dumps((item.get("evidence_turn_ids") or []) + (item.get("evidence_texts") or [])),
                    "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "snapshot_id")
            for item in out.get("emotional_couplings") or []:
                if not isinstance(item, dict) or not item.get("coupling_type"):
                    continue
                source = item.get("source_person_hint") or "unknown"
                coupling_id = stable_id("v146coupling", conversation_id, person_id, source, item.get("source_state"), item.get("target_state_after"), item.get("coupling_type"))
                upsert(con, "v14_6_interpersonal_emotional_couplings", {
                    "coupling_id": coupling_id,
                    "conversation_id": conversation_id,
                    "person_id": person_id,
                    "source_person_hint": source,
                    "target_person_id": item.get("target_person_id") or person_id,
                    "source_state": item.get("source_state"),
                    "target_state_before": item.get("target_state_before"),
                    "target_state_after": item.get("target_state_after"),
                    "coupling_type": item.get("coupling_type"),
                    "impact_direction": item.get("impact_direction"),
                    "impact_strength": item.get("impact_strength"),
                    "latency": item.get("latency"),
                    "evidence_json": json_dumps(item.get("evidence") or []),
                    "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "coupling_id")
            for item in out.get("micro_interaction_impacts") or []:
                if not isinstance(item, dict) or not item.get("moment_summary"):
                    continue
                impact_id = stable_id("v146micro", conversation_id, person_id, item.get("interaction_type"), item.get("moment_summary"))
                upsert(con, "v14_6_micro_interaction_impacts", {
                    "impact_id": impact_id,
                    "conversation_id": conversation_id,
                    "person_id": person_id,
                    "interaction_type": item.get("interaction_type"),
                    "other_person_hint": item.get("other_person_hint"),
                    "moment_summary": item.get("moment_summary"),
                    "other_person_state": item.get("other_person_state"),
                    "user_shift": item.get("user_shift"),
                    "possible_aftereffect": item.get("possible_aftereffect"),
                    "life_domain_affected": item.get("life_domain_affected"),
                    "time_horizon": item.get("time_horizon"),
                    "evidence_json": json_dumps(item.get("evidence") or []),
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "impact_id")
            for item in out.get("social_aftereffects") or []:
                if not isinstance(item, dict) or not item.get("trigger_event_summary"):
                    continue
                aftereffect_id = stable_id("v146after", conversation_id, person_id, item.get("trigger_person_hint"), item.get("trigger_event_summary"))
                upsert(con, "v14_6_social_aftereffects", {
                    "aftereffect_id": aftereffect_id,
                    "conversation_id": conversation_id,
                    "person_id": person_id,
                    "trigger_person_hint": item.get("trigger_person_hint"),
                    "trigger_event_summary": item.get("trigger_event_summary"),
                    "user_after_state": item.get("user_after_state"),
                    "next_actions_influenced_json": json_dumps(item.get("next_actions_influenced") or []),
                    "risks_if_unnoticed_json": json_dumps(item.get("risks_if_unnoticed") or []),
                    "positive_levers_json": json_dumps(item.get("positive_levers") or []),
                    "watch_until": item.get("watch_until"),
                    "evidence_json": json_dumps(item.get("evidence") or []),
                    "status": "open",
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "aftereffect_id")
            for item in out.get("relationship_state_models") or []:
                if not isinstance(item, dict) or not item.get("relationship_state_summary"):
                    continue
                person_hint = item.get("person_hint") or item.get("known_person_id") or "unknown"
                model_id = stable_id("v146relmodel", person_id, person_hint)
                created_at = now
                prev = con.execute("SELECT created_at FROM v14_6_relationship_state_models WHERE model_id=?", (model_id,)).fetchone()
                if prev:
                    created_at = prev["created_at"]
                upsert(con, "v14_6_relationship_state_models", {
                    "model_id": model_id,
                    "person_id": person_id,
                    "person_hint": person_hint,
                    "known_person_id": item.get("known_person_id"),
                    "relationship_state_summary": item.get("relationship_state_summary"),
                    "their_typical_states_json": json_dumps(item.get("their_typical_states") or []),
                    "their_probable_needs_or_motives_json": json_dumps(item.get("their_probable_needs_or_motives") or []),
                    "their_common_avoidances_json": json_dumps(item.get("their_common_avoidances") or []),
                    "how_user_affects_them_json": json_dumps(item.get("how_user_affects_them") or []),
                    "how_they_affect_user_json": json_dumps(item.get("how_they_affect_user") or []),
                    "communication_style": item.get("communication_style"),
                    "sensitive_topics_json": json_dumps(item.get("sensitive_topics") or []),
                    "easy_topics_json": json_dumps(item.get("easy_topics") or []),
                    "repair_conditions_json": json_dumps(item.get("repair_conditions") or []),
                    "evidence_json": json_dumps(item.get("evidence") or []),
                    "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": created_at,
                    "updated_at": now,
                }, "model_id")
            for item in out.get("interpersonal_loops") or []:
                if not isinstance(item, dict) or not item.get("loop_title"):
                    continue
                loop_id = stable_id("v146loop", person_id, item.get("person_hint"), item.get("loop_title"))
                created_at = now
                prev = con.execute("SELECT created_at FROM v14_6_interpersonal_loop_cards WHERE loop_id=?", (loop_id,)).fetchone()
                if prev:
                    created_at = prev["created_at"]
                upsert(con, "v14_6_interpersonal_loop_cards", {
                    "loop_id": loop_id,
                    "person_id": person_id,
                    "person_hint": item.get("person_hint"),
                    "loop_title": item.get("loop_title"),
                    "loop_sequence_json": json_dumps(item.get("loop_sequence") or []),
                    "user_role_in_loop": item.get("user_role_in_loop"),
                    "other_role_in_loop": item.get("other_role_in_loop"),
                    "usual_outcome": item.get("usual_outcome"),
                    "early_warning_signals_json": json_dumps(item.get("early_warning_signals") or []),
                    "escape_conditions_json": json_dumps(item.get("escape_conditions") or []),
                    "evidence_json": json_dumps(item.get("evidence") or []),
                    "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                    "status": "hypothesis",
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": created_at,
                    "updated_at": now,
                }, "loop_id")
            for item in out.get("intervention_suggestions") or []:
                if not isinstance(item, dict) or not item.get("suggestion"):
                    continue
                suggestion_id = stable_id("v146suggest", person_id, item.get("person_hint"), item.get("situation"), item.get("suggestion"))
                upsert(con, "v14_6_intervention_suggestions", {
                    "suggestion_id": suggestion_id,
                    "person_id": person_id,
                    "person_hint": item.get("person_hint"),
                    "situation": item.get("situation"),
                    "suggestion": item.get("suggestion"),
                    "why_it_might_help": item.get("why_it_might_help"),
                    "when_to_use": item.get("when_to_use"),
                    "risk_if_used_wrong": item.get("risk_if_used_wrong"),
                    "status": "suggested",
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "suggestion_id")
            for item in out.get("person_model_summaries") or []:
                if not isinstance(item, dict) or not item.get("summary"):
                    continue
                person_hint = item.get("person_hint") or item.get("known_person_id") or "unknown"
                summary_id = stable_id("v146summary", person_id, person_hint)
                created_at = now
                prev = con.execute("SELECT created_at FROM v14_6_person_model_summaries WHERE summary_id=?", (summary_id,)).fetchone()
                if prev:
                    created_at = prev["created_at"]
                upsert(con, "v14_6_person_model_summaries", {
                    "summary_id": summary_id,
                    "person_id": person_id,
                    "person_hint": person_hint,
                    "known_person_id": item.get("known_person_id"),
                    "summary": item.get("summary"),
                    "what_system_thinks_they_are_like_json": json_dumps(item.get("what_system_thinks_they_are_like") or []),
                    "what_system_thinks_they_often_think_or_seek_json": json_dumps(item.get("what_system_thinks_they_often_think_or_seek") or []),
                    "what_is_uncertain_json": json_dumps(item.get("what_is_uncertain") or []),
                    "evidence_json": json_dumps(item.get("evidence") or []),
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": created_at,
                    "updated_at": now,
                }, "summary_id")
        upsert(con, "v14_6_interpersonal_runs", {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "person_id": person_id,
            "status": status,
            "error_text": error,
            "qwen_output_json": json_dumps(out),
            "created_at": now,
        }, "run_id")
        con.commit()
    return {"version": V14_6_VERSION, "run_id": run_id, "conversation_id": conversation_id, "person_id": person_id, "status": status, "error": error, "raw": out}


def run_v14_6_post_conversation(conversation_id: str, *, person_id: str | None = None) -> dict[str, Any]:
    return analyze_interpersonal_state(conversation_id, person_id=person_id)


def list_other_person_models(*, person_id: str | None = None, person_hint: str | None = None, limit: int = 50) -> dict[str, Any]:
    ensure_v14_6_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        params: tuple[Any, ...]
        if person_hint:
            params = (person_id, person_hint, limit)
            models = _many(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? AND person_hint=? ORDER BY updated_at DESC LIMIT ?", params)
            summaries = _many(con, "SELECT * FROM v14_6_person_model_summaries WHERE person_id=? AND person_hint=? ORDER BY updated_at DESC LIMIT ?", params)
        else:
            models = _many(con, "SELECT * FROM v14_6_relationship_state_models WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
            summaries = _many(con, "SELECT * FROM v14_6_person_model_summaries WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        states = _many(con, "SELECT * FROM v14_6_other_person_state_snapshots WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        couplings = _many(con, "SELECT * FROM v14_6_interpersonal_emotional_couplings WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        loops = _many(con, "SELECT * FROM v14_6_interpersonal_loop_cards WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        suggestions = _many(con, "SELECT * FROM v14_6_intervention_suggestions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
    return {"version": V14_6_VERSION, "person_id": person_id, "person_hint": person_hint, "models": models, "summaries": summaries, "recent_states": states, "couplings": couplings, "loops": loops, "intervention_suggestions": suggestions}


def list_social_aftereffects(*, person_id: str | None = None, status: str | None = "open", limit: int = 50) -> dict[str, Any]:
    ensure_v14_6_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        if status:
            aftereffects = _many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? AND status=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, status, limit))
        else:
            aftereffects = _many(con, "SELECT * FROM v14_6_social_aftereffects WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        micro = _many(con, "SELECT * FROM v14_6_micro_interaction_impacts WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
    return {"version": V14_6_VERSION, "person_id": person_id, "status": status, "social_aftereffects": aftereffects, "micro_interaction_impacts": micro}


def audit_v14_6(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_6_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_6_TABLES - tables)
        counts: dict[str, int | None] = {}
        for table in sorted(V14_6_TABLES):
            try:
                counts[table] = int(con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            except Exception:
                counts[table] = None
    return {
        "version": V14_6_VERSION,
        "ok": not missing,
        "missing_tables": missing,
        "required_tables": sorted(V14_6_TABLES),
        "counts": counts,
        "capabilities": {
            "other_person_state_at_t": "captures likely moment state of other people with evidence and uncertainty",
            "interpersonal_emotional_coupling": "tracks how another person's state may shift the user's emotion, energy, choices or next actions",
            "micro_interaction_impact": "short encounters such as cashier/stranger/service exchanges can be recorded as day-level aftereffects",
            "relationship_state_models": "builds evolving person models: what they are like, what they tend to seek/avoid, how the user affects them and how they affect the user",
            "interpersonal_loops": "detects repeated social loops and escape conditions",
            "trust_boundary": "no mind-reading, no identity confirmation, no regex/keyword rules; all conclusions are hypotheses with confidence and counter-evidence",
        },
        "manual_commands": [
            "mlomega-audio v14-6-run <conversation_id>",
            "mlomega-audio v14-people-models --person-id me",
            "mlomega-audio v14-social-aftereffects --person-id me",
            "mlomega-audio v14-6-audit",
        ],
    }
