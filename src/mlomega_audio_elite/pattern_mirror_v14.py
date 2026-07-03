from __future__ import annotations

"""V14 final Brain 2.0 Pattern Mirror / Long Horizon Self Model.

Purpose
-------
V13 made the system autonomous after a conversation. V14 makes the objective
explicit: the assistant must surface the hidden loops an owner of a limited human
memory usually misses. It is not a passive memory and not only a targeted
`next_*` predictor. It is an autonomous longitudinal mirror:

    evidence -> episodes -> states/actions/outcomes -> long-horizon threads
    -> hidden patterns/blindspots -> forecasts -> interventions -> revisions.

Cognitive content is Qwen/Ollama JSON-contract based. This module never creates
psychological conclusions from regex/keywords. When Qwen is unavailable, it
records the failed run and stores no fake insight.
"""

from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, sha256_bytes, stable_id
from .autonomous_v13_4 import ensure_autonomous_schema
from .v18_brain2_context import conversation_context_addenda

V14_VERSION = "14.0.0-pattern-mirror-final"

V14_TABLES = {
    "v14_mirror_runs",
    "v14_pattern_mirror_cards",
    "v14_blindspot_hypotheses",
    "v14_long_horizon_threads",
    "v14_trajectory_forecasts",
    "v14_self_model_readings",
    "v14_intervention_triggers",
    "v14_counterfactual_lessons",
    "v14_memory_horizon_index",
    "v14_open_questions",
    "v14_user_facing_reports",
    "v14_ask_runs",
    "v14_contract_checks",
    "v14_periodic_self_snapshots",
    "v14_people_trigger_maps",
    "v14_repetition_chains",
    "v14_forecast_watch_queue",
}

MIRROR_SCHEMA: dict[str, Any] = {
    "mirror_cards": [
        {
            "title": "",
            "pattern_type": "loop|blindspot|contradiction|weak_signal|self_model|relationship|choice|language|avoidance|trajectory|opportunity",
            "time_horizon": "immediate|short|medium|long|very_long|life_pattern",
            "summary": "",
            "why_user_may_not_see_it": "",
            "first_seen": None,
            "last_seen": None,
            "evidence_count": 0,
            "severity": "low|medium|high|critical",
            "confidence": 0.0,
            "linked_object_ids": [],
            "evidence": [],
            "counter_evidence": [],
            "watch_for": [],
            "possible_future_if_unchanged": "",
            "escape_condition": "",
        }
    ],
    "blindspots": [
        {
            "title": "",
            "blindspot_type": "memory_gap|rationalization|avoidance|repetition|relationship_trigger|choice_bias|emotion_shift|language_marker|unknown",
            "statement": "",
            "why_user_may_not_see_it": "",
            "related_card_title": "",
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "long_horizon_threads": [
        {
            "theme": "",
            "summary": "",
            "start_time": None,
            "latest_time": None,
            "linked_object_ids": [],
            "linked_conversation_ids": [],
            "recurrence_count": 0,
            "trajectory_summary": "",
            "confidence": 0.0,
        }
    ],
    "forecasts": [
        {
            "related_card_title": "",
            "current_situation": "",
            "probable_path": "",
            "probability": 0.0,
            "risk_level": "low|medium|high|critical",
            "opportunity_level": "low|medium|high",
            "time_horizon": "next_message|today|week|month|long_term|unknown",
            "early_warning_signals": [],
            "escape_options": [],
            "evidence": [],
            "confidence": 0.0,
        }
    ],
    "self_model_readings": [
        {
            "dimension": "need_for_clarity|need_for_proof|sensitivity_to_vagueness|validation_seeking|risk_tolerance|conflict_avoidance|directness|persistence|decision_style|relationship_trigger|language_marker|other",
            "statement": "",
            "score": 0.0,
            "confidence": 0.0,
            "active_contexts": [],
            "evidence": [],
            "counterexamples": [],
        }
    ],
    "intervention_triggers": [
        {
            "related_forecast_summary": "",
            "trigger_condition": "",
            "message_to_user": "",
            "urgency": "low|medium|high|critical",
            "should_interrupt": False,
            "confidence": 0.0,
        }
    ],
    "counterfactual_lessons": [
        {
            "past_situation": "",
            "avoidable_turning_point": "",
            "what_could_have_been_seen": "",
            "alternative_action": "",
            "evidence": [],
            "confidence": 0.0,
        }
    ],
    "open_questions": [
        {
            "question": "",
            "reason": "",
            "priority": "low|medium|high|critical",
            "related_card_title": "",
        }
    ],
    "user_facing_report": {
        "headline": "",
        "what_you_may_not_see": [],
        "likely_next_loops": [],
        "what_to_watch_now": [],
        "recommended_actions": [],
    },
    "missing_context": [],
    "confidence": 0.0,
}

ASK_SCHEMA: dict[str, Any] = {
    "answer": "",
    "question_type": "memory|prediction|simulation|blindspot|loop|relationship|choice|emotion|thought|action|unknown",
    "direct_reading": "",
    "probability": None,
    "confidence": 0.0,
    "hidden_patterns": [],
    "similar_cases": [],
    "counter_evidence": [],
    "forecast": "",
    "intervention": "",
    "what_to_verify_next": [],
    "missing_context": [],
}

PERIODIC_SCHEMA: dict[str, Any] = {
    "periodic_snapshot": {
        "period": "hour|day|week|month|quarter|year|all_time",
        "period_label": "",
        "overall_reading": "",
        "state_summary": "",
        "dominant_loops": [],
        "new_contradictions": [],
        "weak_signals_resembling_old_errors": [],
        "people_triggering_states": [],
        "decisions_resembling_old_choices": [],
        "phrases_predicting_action_or_blockage": [],
        "long_trajectory_updates": [],
        "forecasts_to_watch": [],
        "interventions": [],
        "confidence": 0.0,
    },
    "people_trigger_maps": [
        {
            "other_person_id": "",
            "other_display_name": "",
            "triggered_states": [],
            "common_loops": [],
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "repetition_chains": [
        {
            "chain_type": "language|choice|emotion|relationship|avoidance|project|action|reaction|trajectory|other",
            "title": "",
            "sequence": [],
            "first_seen": None,
            "last_seen": None,
            "recurrence_count": 0,
            "likely_next_step": "",
            "warning_signal": "",
            "escape_action": "",
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "forecast_watch_queue": [
        {
            "forecast_text": "",
            "watch_condition": "",
            "due_horizon": "hour|today|week|month|long_term|unknown",
            "probability": 0.0,
            "confidence": 0.0,
        }
    ],
    "missing_context": [],
}


def _clamp(v: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        f = float(v)
    except Exception:
        f = 0.0
    return max(lo, min(hi, f))


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _hash_payload(payload: Any) -> str:
    return sha256_bytes(json_dumps(payload).encode("utf-8"))


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    data = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=360)
    if not isinstance(data, dict):
        raise RuntimeError("V14 Pattern Mirror returned non-object JSON")
    return data


def ensure_v14_schema() -> None:
    ensure_autonomous_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_mirror_runs(
                run_id TEXT PRIMARY KEY,
                person_id TEXT,
                conversation_id TEXT,
                trigger_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                qwen_output_json TEXT DEFAULT '{}',
                error_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_pattern_mirror_cards(
                card_id TEXT PRIMARY KEY,
                run_id TEXT,
                person_id TEXT NOT NULL,
                conversation_id TEXT,
                title TEXT NOT NULL,
                pattern_type TEXT NOT NULL,
                time_horizon TEXT NOT NULL,
                hidden_pattern_summary TEXT NOT NULL,
                why_user_may_not_see_it TEXT,
                first_seen TEXT,
                last_seen TEXT,
                evidence_count INTEGER DEFAULT 0,
                severity TEXT DEFAULT 'medium',
                confidence REAL DEFAULT 0.5,
                linked_object_ids_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                watch_for_json TEXT DEFAULT '[]',
                possible_future_if_unchanged TEXT,
                escape_condition TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_blindspot_hypotheses(
                hypothesis_id TEXT PRIMARY KEY,
                card_id TEXT,
                person_id TEXT NOT NULL,
                blindspot_type TEXT NOT NULL,
                title TEXT,
                statement TEXT NOT NULL,
                why_user_may_not_see_it TEXT,
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_long_horizon_threads(
                thread_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                theme TEXT NOT NULL,
                summary TEXT NOT NULL,
                start_time TEXT,
                latest_time TEXT,
                linked_object_ids_json TEXT DEFAULT '[]',
                linked_conversation_ids_json TEXT DEFAULT '[]',
                recurrence_count INTEGER DEFAULT 0,
                trajectory_summary TEXT,
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_trajectory_forecasts(
                forecast_id TEXT PRIMARY KEY,
                card_id TEXT,
                person_id TEXT NOT NULL,
                current_situation TEXT NOT NULL,
                probable_path TEXT NOT NULL,
                probability REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.5,
                risk_level TEXT DEFAULT 'medium',
                opportunity_level TEXT DEFAULT 'medium',
                time_horizon TEXT,
                early_warning_signals_json TEXT DEFAULT '[]',
                escape_options_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_self_model_readings(
                reading_id TEXT PRIMARY KEY,
                run_id TEXT,
                person_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                statement TEXT NOT NULL,
                score REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.5,
                active_contexts_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                counterexamples_json TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_intervention_triggers(
                trigger_id TEXT PRIMARY KEY,
                forecast_id TEXT,
                person_id TEXT NOT NULL,
                trigger_condition TEXT NOT NULL,
                message_to_user TEXT NOT NULL,
                urgency TEXT DEFAULT 'medium',
                should_interrupt INTEGER DEFAULT 0,
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_counterfactual_lessons(
                lesson_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                related_card_id TEXT,
                past_situation TEXT NOT NULL,
                avoidable_turning_point TEXT,
                what_could_have_been_seen TEXT,
                alternative_action TEXT,
                evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_memory_horizon_index(
                index_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                target_table TEXT NOT NULL,
                target_id TEXT NOT NULL,
                horizon TEXT NOT NULL,
                theme TEXT,
                temporal_weight REAL DEFAULT 0.5,
                relevance_reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_open_questions(
                question_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                related_card_id TEXT,
                question TEXT NOT NULL,
                reason TEXT,
                priority TEXT DEFAULT 'medium',
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_user_facing_reports(
                report_id TEXT PRIMARY KEY,
                run_id TEXT,
                person_id TEXT NOT NULL,
                headline TEXT NOT NULL,
                what_you_may_not_see_json TEXT DEFAULT '[]',
                likely_next_loops_json TEXT DEFAULT '[]',
                what_to_watch_now_json TEXT DEFAULT '[]',
                recommended_actions_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_ask_runs(
                ask_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                qwen_answer_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_periodic_self_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                period TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT,
                period_label TEXT,
                overall_reading TEXT NOT NULL,
                state_summary TEXT,
                dominant_loops_json TEXT DEFAULT '[]',
                new_contradictions_json TEXT DEFAULT '[]',
                weak_signals_json TEXT DEFAULT '[]',
                people_triggers_json TEXT DEFAULT '[]',
                decision_echoes_json TEXT DEFAULT '[]',
                language_markers_json TEXT DEFAULT '[]',
                long_trajectory_updates_json TEXT DEFAULT '[]',
                forecasts_to_watch_json TEXT DEFAULT '[]',
                interventions_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_people_trigger_maps(
                map_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                other_person_id TEXT,
                other_display_name TEXT,
                triggered_states_json TEXT DEFAULT '[]',
                common_loops_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_repetition_chains(
                chain_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                chain_type TEXT NOT NULL,
                title TEXT NOT NULL,
                sequence_json TEXT DEFAULT '[]',
                first_seen TEXT,
                last_seen TEXT,
                recurrence_count INTEGER DEFAULT 0,
                likely_next_step TEXT,
                warning_signal TEXT,
                escape_action TEXT,
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_forecast_watch_queue(
                watch_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                snapshot_id TEXT,
                forecast_text TEXT NOT NULL,
                watch_condition TEXT,
                due_horizon TEXT,
                probability REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'watching',
                created_at TEXT NOT NULL,
                verified_at TEXT,
                observed_evidence TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_v14_cards_person_time ON v14_pattern_mirror_cards(person_id, updated_at, time_horizon);
            CREATE INDEX IF NOT EXISTS idx_v14_cards_type ON v14_pattern_mirror_cards(pattern_type, severity, status);
            CREATE INDEX IF NOT EXISTS idx_v14_forecasts_person ON v14_trajectory_forecasts(person_id, status, risk_level);
            CREATE INDEX IF NOT EXISTS idx_v14_threads_person ON v14_long_horizon_threads(person_id, latest_time);
            CREATE INDEX IF NOT EXISTS idx_v14_open_questions_person ON v14_open_questions(person_id, status, priority);
            CREATE INDEX IF NOT EXISTS idx_v14_snapshots_person_period ON v14_periodic_self_snapshots(person_id, period, created_at);
            CREATE INDEX IF NOT EXISTS idx_v14_trigger_maps_person ON v14_people_trigger_maps(person_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v14_repetition_chains_person ON v14_repetition_chains(person_id, chain_type, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v14_watch_queue_person ON v14_forecast_watch_queue(person_id, status, created_at);
            """
        )
        now = now_iso()
        for name in sorted(V14_TABLES):
            upsert(
                con,
                "v14_contract_checks",
                {
                    "check_id": stable_id("v14check", name),
                    "check_name": f"table:{name}",
                    "status": "declared",
                    "detail": "V14 Pattern Mirror table required by final Brain 2.0 objective.",
                    "created_at": now,
                },
                "check_id",
            )
        from .v18_legacy_forecasts import ensure_legacy_forecast_lifecycle_schema
        ensure_legacy_forecast_lifecycle_schema(con)
        con.commit()


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _many(con, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params)]
    except Exception:
        return []


def _bundle(con, *, conversation_id: str | None, person_id: str, limit: int = 120) -> dict[str, Any]:
    from .v18_brain2_context import active_brain2_conversation_ids
    conversations: list[dict[str, Any]]
    if conversation_id:
        active_ids = set(active_brain2_conversation_ids(con, person_id=person_id, limit=100000))
        if conversation_id not in active_ids:
            raise ValueError("conversation is superseded, inactive or outside the supplied Brain2 owner scope")
        conversations = _many(con, "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,))
        turns = _many(con, "SELECT turn_id, conversation_id, idx, person_id, speaker_label, start_s, end_s, text, metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,))
    else:
        active_ids = active_brain2_conversation_ids(con, person_id=person_id, limit=limit)
        if not active_ids:
            conversations = []
            turns = []
        else:
            marks = ",".join("?" for _ in active_ids)
            conversations = _many(con, f"SELECT * FROM conversations WHERE conversation_id IN ({marks}) ORDER BY started_at DESC, created_at DESC LIMIT ?", tuple(active_ids) + (limit,))
            turns = _many(con, f"SELECT turn_id, conversation_id, idx, person_id, speaker_label, start_s, end_s, text, metadata_json FROM turns WHERE conversation_id IN ({marks}) ORDER BY conversation_id, idx LIMIT ?", tuple(active_ids) + (limit * 3,))
    conv_ids = [c.get("conversation_id") for c in conversations if c.get("conversation_id")]
    conv_placeholders = ",".join("?" for _ in conv_ids) or "''"
    return {
        "objective": "Reveal hidden loops, weak signals, long-horizon repeating trajectories, and what the user does not see about themselves over time.",
        "evidence_role_rule": "Turns may include metadata_json.kind/evidence_role. System/context observations (vision/world/audio) are not user speech and must not be treated as William declaring a preference, intention or emotion.",
        "person_id": person_id,
        "conversation_id": conversation_id,
        "conversations": conversations,
        "turns": turns,
        "context_addenda": (
            conversation_context_addenda(con, conversation_id=conversation_id, person_id=person_id)
            if conversation_id else {"entries": [], "budget": {"context_incomplete": False}}
        ),
        "episodes": _many(con, f"SELECT * FROM episodes WHERE source_conversation_id IN ({conv_placeholders}) ORDER BY start_time, created_at LIMIT ?", tuple(conv_ids) + (limit,)) if conv_ids else [],
        "subtopics": _many(con, f"SELECT * FROM conversation_subtopic_segments WHERE conversation_id IN ({conv_placeholders}) ORDER BY created_at DESC LIMIT ?", tuple(conv_ids) + (limit,)) if conv_ids else [],
        "states": _many(con, "SELECT * FROM internal_state_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "thoughts": _many(con, "SELECT * FROM thought_hypotheses WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "speech_acts": _many(con, "SELECT * FROM speech_acts WHERE speaker_person_id=? OR target_person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, person_id, limit)),
        "intentions": _many(con, "SELECT * FROM action_intentions WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "outcomes": _many(con, "SELECT * FROM action_outcomes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "choices": _many(con, "SELECT * FROM choice_episodes WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "contradictions": _many(con, "SELECT * FROM contradiction_events WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "relationships": _many(con, "SELECT * FROM relationship_models WHERE person_a=? OR person_b=? ORDER BY updated_at DESC LIMIT ?", (person_id, person_id, limit)),
        "candidate_patterns": _many(con, "SELECT * FROM candidate_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)),
        "confirmed_patterns": _many(con, "SELECT * FROM confirmed_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)),
        "loops": _many(con, "SELECT * FROM loop_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)),
        "predictions": _many(con, "SELECT * FROM predictions WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "prediction_results": _many(con, "SELECT pr.* FROM prediction_results pr JOIN predictions p ON p.prediction_id=pr.prediction_id WHERE p.person_id=? ORDER BY pr.verified_at DESC LIMIT ?", (person_id, limit)),
        "latent_outcomes": _many(con, "SELECT * FROM latent_outcome_links ORDER BY created_at DESC LIMIT ?", (limit,)),
        "autonomous_insights": _many(con, "SELECT * FROM v13_autonomous_insights WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit)),
        "existing_v14_cards": _many(con, "SELECT * FROM v14_pattern_mirror_cards WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, 40)),
        "existing_v14_threads": _many(con, "SELECT * FROM v14_long_horizon_threads WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, 40)),
    }


def _card_title_map(cards: list[dict[str, Any]]) -> dict[str, str]:
    return {str(c.get("title") or "").strip().lower(): c.get("card_id") for c in cards if c.get("title") and c.get("card_id")}


def _persist_v14_output(con, *, run_id: str, person_id: str, conversation_id: str | None, out: dict[str, Any]) -> dict[str, Any]:
    now = now_iso()
    created: dict[str, list[str]] = {k: [] for k in ["cards", "blindspots", "threads", "forecasts", "readings", "interventions", "lessons", "questions", "reports"]}
    title_to_card: dict[str, str] = {}

    for idx, card in enumerate(out.get("mirror_cards") or []):
        if not isinstance(card, dict) or not card.get("title") or not card.get("summary"):
            continue
        card_id = stable_id("v14card", person_id, card.get("title"), card.get("pattern_type"), card.get("summary"))
        title_to_card[str(card.get("title")).strip().lower()] = card_id
        upsert(con, "v14_pattern_mirror_cards", {
            "card_id": card_id,
            "run_id": run_id,
            "person_id": person_id,
            "conversation_id": conversation_id,
            "title": str(card.get("title")),
            "pattern_type": str(card.get("pattern_type") or "loop"),
            "time_horizon": str(card.get("time_horizon") or "long"),
            "hidden_pattern_summary": str(card.get("summary")),
            "why_user_may_not_see_it": card.get("why_user_may_not_see_it"),
            "first_seen": card.get("first_seen"),
            "last_seen": card.get("last_seen"),
            "evidence_count": int(card.get("evidence_count") or len(_as_list(card.get("evidence")))) ,
            "severity": str(card.get("severity") or "medium"),
            "confidence": _clamp(card.get("confidence")),
            "linked_object_ids_json": json_dumps(_as_list(card.get("linked_object_ids"))),
            "evidence_json": json_dumps(_as_list(card.get("evidence"))),
            "counter_evidence_json": json_dumps(_as_list(card.get("counter_evidence"))),
            "watch_for_json": json_dumps(_as_list(card.get("watch_for"))),
            "possible_future_if_unchanged": card.get("possible_future_if_unchanged"),
            "escape_condition": card.get("escape_condition"),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "card_id")
        created["cards"].append(card_id)

        for obj in _as_list(card.get("linked_object_ids")):
            if isinstance(obj, dict):
                table = str(obj.get("table") or obj.get("target_table") or "unknown")
                target = str(obj.get("id") or obj.get("target_id") or "")
                theme = str(card.get("pattern_type") or "pattern")
            else:
                table = "unknown"; target = str(obj); theme = str(card.get("pattern_type") or "pattern")
            if not target:
                continue
            upsert(con, "v14_memory_horizon_index", {
                "index_id": stable_id("v14horizon", person_id, card_id, table, target),
                "person_id": person_id,
                "target_table": table,
                "target_id": target,
                "horizon": str(card.get("time_horizon") or "long"),
                "theme": theme,
                "temporal_weight": _clamp(card.get("confidence")),
                "relevance_reason": str(card.get("summary") or ""),
                "created_at": now,
            }, "index_id")

    # fallback for related-card titles after all cards are known
    title_to_card |= _card_title_map(_many(con, "SELECT card_id, title FROM v14_pattern_mirror_cards WHERE person_id=?", (person_id,)))

    for b in out.get("blindspots") or []:
        if not isinstance(b, dict) or not b.get("statement"):
            continue
        related = title_to_card.get(str(b.get("related_card_title") or "").strip().lower())
        hid = stable_id("v14blind", person_id, b.get("statement"), related)
        upsert(con, "v14_blindspot_hypotheses", {
            "hypothesis_id": hid,
            "card_id": related,
            "person_id": person_id,
            "blindspot_type": str(b.get("blindspot_type") or "unknown"),
            "title": b.get("title"),
            "statement": str(b.get("statement")),
            "why_user_may_not_see_it": b.get("why_user_may_not_see_it"),
            "evidence_json": json_dumps(_as_list(b.get("evidence"))),
            "counter_evidence_json": json_dumps(_as_list(b.get("counter_evidence"))),
            "confidence": _clamp(b.get("confidence")),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "hypothesis_id")
        created["blindspots"].append(hid)

    for th in out.get("long_horizon_threads") or []:
        if not isinstance(th, dict) or not th.get("theme") or not th.get("summary"):
            continue
        tid = stable_id("v14thread", person_id, th.get("theme"))
        upsert(con, "v14_long_horizon_threads", {
            "thread_id": tid,
            "person_id": person_id,
            "theme": str(th.get("theme")),
            "summary": str(th.get("summary")),
            "start_time": th.get("start_time"),
            "latest_time": th.get("latest_time"),
            "linked_object_ids_json": json_dumps(_as_list(th.get("linked_object_ids"))),
            "linked_conversation_ids_json": json_dumps(_as_list(th.get("linked_conversation_ids"))),
            "recurrence_count": int(th.get("recurrence_count") or 0),
            "trajectory_summary": th.get("trajectory_summary"),
            "confidence": _clamp(th.get("confidence")),
            "created_at": now,
            "updated_at": now,
        }, "thread_id")
        created["threads"].append(tid)

    forecast_summary_to_id: dict[str, str] = {}
    for f in out.get("forecasts") or []:
        if not isinstance(f, dict) or not f.get("probable_path"):
            continue
        related = title_to_card.get(str(f.get("related_card_title") or "").strip().lower())
        fid = stable_id("v14forecast", person_id, related, f.get("current_situation"), f.get("probable_path"))
        forecast_summary_to_id[str(f.get("probable_path") or "").strip().lower()] = fid
        upsert(con, "v14_trajectory_forecasts", {
            "forecast_id": fid,
            "card_id": related,
            "person_id": person_id,
            "current_situation": str(f.get("current_situation") or ""),
            "probable_path": str(f.get("probable_path")),
            "probability": _clamp(f.get("probability")),
            "confidence": _clamp(f.get("confidence")),
            "risk_level": str(f.get("risk_level") or "medium"),
            "opportunity_level": str(f.get("opportunity_level") or "medium"),
            "time_horizon": f.get("time_horizon"),
            "early_warning_signals_json": json_dumps(_as_list(f.get("early_warning_signals"))),
            "escape_options_json": json_dumps(_as_list(f.get("escape_options"))),
            "evidence_json": json_dumps(_as_list(f.get("evidence"))),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "forecast_id")
        created["forecasts"].append(fid)

    for r in out.get("self_model_readings") or []:
        if not isinstance(r, dict) or not r.get("dimension") or not r.get("statement"):
            continue
        rid = stable_id("v14reading", person_id, r.get("dimension"), r.get("statement"))
        upsert(con, "v14_self_model_readings", {
            "reading_id": rid,
            "run_id": run_id,
            "person_id": person_id,
            "dimension": str(r.get("dimension")),
            "statement": str(r.get("statement")),
            "score": _clamp(r.get("score")),
            "confidence": _clamp(r.get("confidence")),
            "active_contexts_json": json_dumps(_as_list(r.get("active_contexts"))),
            "evidence_json": json_dumps(_as_list(r.get("evidence"))),
            "counterexamples_json": json_dumps(_as_list(r.get("counterexamples"))),
            "created_at": now,
            "updated_at": now,
        }, "reading_id")
        created["readings"].append(rid)

    for it in out.get("intervention_triggers") or []:
        if not isinstance(it, dict) or not it.get("message_to_user"):
            continue
        forecast = forecast_summary_to_id.get(str(it.get("related_forecast_summary") or "").strip().lower())
        iid = stable_id("v14intervention", person_id, forecast, it.get("trigger_condition"), it.get("message_to_user"))
        upsert(con, "v14_intervention_triggers", {
            "trigger_id": iid,
            "forecast_id": forecast,
            "person_id": person_id,
            "trigger_condition": str(it.get("trigger_condition") or ""),
            "message_to_user": str(it.get("message_to_user")),
            "urgency": str(it.get("urgency") or "medium"),
            "should_interrupt": 1 if it.get("should_interrupt") else 0,
            "confidence": _clamp(it.get("confidence")),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }, "trigger_id")
        created["interventions"].append(iid)

    for l in out.get("counterfactual_lessons") or []:
        if not isinstance(l, dict) or not l.get("past_situation"):
            continue
        lid = stable_id("v14lesson", person_id, l.get("past_situation"), l.get("avoidable_turning_point"))
        upsert(con, "v14_counterfactual_lessons", {
            "lesson_id": lid,
            "person_id": person_id,
            "related_card_id": None,
            "past_situation": str(l.get("past_situation")),
            "avoidable_turning_point": l.get("avoidable_turning_point"),
            "what_could_have_been_seen": l.get("what_could_have_been_seen"),
            "alternative_action": l.get("alternative_action"),
            "evidence_json": json_dumps(_as_list(l.get("evidence"))),
            "confidence": _clamp(l.get("confidence")),
            "created_at": now,
        }, "lesson_id")
        created["lessons"].append(lid)

    for q in out.get("open_questions") or []:
        if not isinstance(q, dict) or not q.get("question"):
            continue
        related = title_to_card.get(str(q.get("related_card_title") or "").strip().lower())
        qid = stable_id("v14question", person_id, related, q.get("question"))
        upsert(con, "v14_open_questions", {
            "question_id": qid,
            "person_id": person_id,
            "related_card_id": related,
            "question": str(q.get("question")),
            "reason": q.get("reason"),
            "priority": str(q.get("priority") or "medium"),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "question_id")
        created["questions"].append(qid)

    rep = out.get("user_facing_report") or {}
    if isinstance(rep, dict) and rep.get("headline"):
        report_id = stable_id("v14report", run_id, rep.get("headline"))
        upsert(con, "v14_user_facing_reports", {
            "report_id": report_id,
            "run_id": run_id,
            "person_id": person_id,
            "headline": str(rep.get("headline")),
            "what_you_may_not_see_json": json_dumps(_as_list(rep.get("what_you_may_not_see"))),
            "likely_next_loops_json": json_dumps(_as_list(rep.get("likely_next_loops"))),
            "what_to_watch_now_json": json_dumps(_as_list(rep.get("what_to_watch_now"))),
            "recommended_actions_json": json_dumps(_as_list(rep.get("recommended_actions"))),
            "confidence": _clamp(out.get("confidence")),
            "created_at": now,
        }, "report_id")
        created["reports"].append(report_id)
    from .v18_legacy_forecasts import reconcile_legacy_forecasts
    reconcile_legacy_forecasts(person_id=person_id, con=con)
    return created


def run_pattern_mirror(conversation_id: str | None = None, *, person_id: str | None = None, trigger_type: str = "manual", scope: str = "long_horizon") -> dict[str, Any]:
    """Run the final V14 mirror over a conversation or the whole ingested life.

    If Qwen fails, no fake card is created. The failed run is persisted so the
    user can diagnose the missing brain dependency.
    """
    ensure_v14_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        run_id = stable_id("v14run", V14_VERSION, person_id, conversation_id or "all", trigger_type, now)
        payload = {
            "mission": (
                "Tu es Pattern Mirror / Long Horizon Self Model. Ton but est de montrer à l'humain ce qu'il ne voit pas lui-même: "
                "boucles cachées, contradictions longues, signaux faibles, répétitions de choix/mots/réactions, trajectoires probables, erreurs qui reviennent, et leviers pour ne pas répéter le futur. "
                "Ne fais pas un résumé. Lis la timeline comme un livre ouvert, mais chaque affirmation doit rester prouvée, probabiliste, et révisable."
            ),
            "strict_rules": [
                "Aucune psychologie générique.",
                "Chaque pattern doit citer des preuves ou dire que c'est une hypothèse faible.",
                "Distingue pattern court terme, moyen terme, long terme et très long terme.",
                "Cherche les patterns que l'utilisateur risque de ne pas voir lui-même.",
                "Cherche aussi les contre-exemples et conditions de sortie.",
                "Transforme les patterns importants en forecasts et interventions vérifiables.",
            ],
            "bundle": _bundle(con, conversation_id=conversation_id, person_id=person_id),
            "schema": MIRROR_SCHEMA,
        }
        try:
            out = _llm_json("Tu es le V14 Pattern Mirror strict. Réponds uniquement en JSON valide.", payload, MIRROR_SCHEMA)
            status = "ok"; err = None
        except Exception as exc:
            out = {"error": str(exc), "mirror_cards": [], "confidence": 0.0}
            status = "error"; err = str(exc)[:2000]
        upsert(con, "v14_mirror_runs", {
            "run_id": run_id,
            "person_id": person_id,
            "conversation_id": conversation_id,
            "trigger_type": trigger_type,
            "scope": scope,
            "status": status,
            "qwen_output_json": json_dumps(out),
            "error_text": err,
            "created_at": now,
            "updated_at": now_iso(),
        }, "run_id")
        created = _persist_v14_output(con, run_id=run_id, person_id=person_id, conversation_id=conversation_id, out=out) if status == "ok" else {k: [] for k in ["cards", "blindspots", "threads", "forecasts", "readings", "interventions", "lessons", "questions", "reports"]}
        con.commit()
    return {"version": V14_VERSION, "run_id": run_id, "status": status, "person_id": person_id, "conversation_id": conversation_id, "created": created, "raw": out}


def run_pattern_mirror_all(*, person_id: str | None = None) -> dict[str, Any]:
    return run_pattern_mirror(None, person_id=person_id, trigger_type="manual_all", scope="all_life")


def list_pattern_mirror_cards(*, person_id: str | None = None, status: str = "open", limit: int = 20) -> dict[str, Any]:
    ensure_v14_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        cards = _many(con, "SELECT * FROM v14_pattern_mirror_cards WHERE person_id=? AND status=? ORDER BY severity DESC, confidence DESC, updated_at DESC LIMIT ?", (person_id, status, limit))
        from .v18_legacy_forecasts import active_legacy_forecasts
        forecasts = active_legacy_forecasts(con, person_id=person_id, source_table="v14_trajectory_forecasts", limit=limit)
        interventions = _many(con, "SELECT * FROM v14_intervention_triggers WHERE person_id=? AND status='active' ORDER BY urgency DESC, confidence DESC, updated_at DESC LIMIT ?", (person_id, limit))
        questions = _many(con, "SELECT * FROM v14_open_questions WHERE person_id=? AND status='open' ORDER BY priority DESC, updated_at DESC LIMIT ?", (person_id, limit))
    return {"version": V14_VERSION, "person_id": person_id, "cards": cards, "forecasts": forecasts, "interventions": interventions, "open_questions": questions}


def pattern_mirror_digest(*, person_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    ensure_v14_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        reports = _many(con, "SELECT * FROM v14_user_facing_reports WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit))
        cards = _many(con, "SELECT title, pattern_type, time_horizon, hidden_pattern_summary, why_user_may_not_see_it, possible_future_if_unchanged, escape_condition, confidence FROM v14_pattern_mirror_cards WHERE person_id=? AND status='open' ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit))
        threads = _many(con, "SELECT theme, summary, recurrence_count, trajectory_summary, confidence FROM v14_long_horizon_threads WHERE person_id=? ORDER BY latest_time DESC, updated_at DESC LIMIT ?", (person_id, limit))
        lessons = _many(con, "SELECT past_situation, avoidable_turning_point, what_could_have_been_seen, alternative_action, confidence FROM v14_counterfactual_lessons WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit))
    return {"version": V14_VERSION, "person_id": person_id, "reports": reports, "mirror_cards": cards, "long_horizon_threads": threads, "counterfactual_lessons": lessons}


def ask_pattern_mirror(question: str, *, person_id: str | None = None) -> dict[str, Any]:
    """Natural-language V14 interface: no next_* target required."""
    ensure_v14_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        payload = {
            "mission": "Réponds comme le miroir longitudinal Brain 2.0: utilise toute la mémoire ingérée, les patterns V14, les preuves, contre-preuves, forecasts et interventions. Ne demande pas à l'utilisateur de choisir next_*.",
            "question": question,
            "bundle": _bundle(con, conversation_id=None, person_id=person_id, limit=160),
            "v14_digest": pattern_mirror_digest(person_id=person_id, limit=30),
            "schema": ASK_SCHEMA,
        }
        out = _llm_json("Tu es l'interface naturelle V14 Pattern Mirror. Réponds uniquement en JSON valide.", payload, ASK_SCHEMA)
        ask_id = stable_id("v14ask", person_id, question, _hash_payload(out), now)
        upsert(con, "v14_ask_runs", {"ask_id": ask_id, "person_id": person_id, "question": question, "qwen_answer_json": json_dumps(out), "created_at": now}, "ask_id")
        con.commit()
    return {"version": V14_VERSION, "ask_id": ask_id, "person_id": person_id, **out}


def _period_bounds(period: str, period_start: str | None = None, period_end: str | None = None) -> tuple[str | None, str | None, str]:
    # Keep parsing deliberately simple and non-cognitive: Qwen interprets the content;
    # these bounds only scope the snapshot.
    start = period_start
    end = period_end
    label = period
    if start or end:
        label = f"{period}:{start or '...'}->{end or '...'}"
    return start, end, label


def _bundle_for_period(con, *, person_id: str, period: str, period_start: str | None, period_end: str | None, limit: int = 220) -> dict[str, Any]:
    bundle = _bundle(con, conversation_id=None, person_id=person_id, limit=limit)
    bundle["period"] = period
    bundle["period_start"] = period_start
    bundle["period_end"] = period_end
    bundle["periodic_goal"] = (
        "Produire une consolidation périodique: aujourd'hui/cette semaine/ce mois, ce qui va bien, "
        "ce qui revient, contradictions récentes, signaux faibles similaires à d'anciennes erreurs, personnes qui déclenchent des états, "
        "décisions ressemblant à d'anciens choix, phrases qui annoncent action ou blocage, trajectoires longues, forecasts et interventions."
    )
    return bundle


def run_periodic_mirror(*, person_id: str | None = None, period: str = "day", period_start: str | None = None, period_end: str | None = None) -> dict[str, Any]:
    """Hourly/daily/weekly/monthly consolidation layer for the final objective.

    This is the command or scheduler job the system can run periodically. It does not wait for a
    specific question: it updates hidden loops, trigger maps, repeating chains,
    forecasts to watch and the human-readable periodic self snapshot.
    """
    ensure_v14_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        start, end, label = _period_bounds(period, period_start, period_end)
        run_id = stable_id("v14period", person_id, period, start or "", end or "", now)
        payload = {
            "mission": (
                "Tu es la consolidation périodique V14. Ne résume pas seulement. Dis ce que l'humain ne voit pas: "
                "heure/jour/semaine/mois, état dominant, boucles, contradictions, signaux faibles, personnes déclencheuses, décisions déjà vues, phrases qui annoncent blocage/action, trajectoires longues, forecasts à surveiller et interventions."
            ),
            "strict_rules": [
                "Aucune psychologie générique.",
                "Chaque affirmation doit être hypothèse/probabilité/preuve/contre-preuve.",
                "Relie court terme, moyen terme et long terme.",
                "Crée des forecasts vérifiables dans le futur.",
                "Quand les données sont insuffisantes, dis-le dans missing_context."
            ],
            "bundle": _bundle_for_period(con, person_id=person_id, period=period, period_start=start, period_end=end),
            "v14_digest": pattern_mirror_digest(person_id=person_id, limit=60),
            "schema": PERIODIC_SCHEMA,
        }
        out = _llm_json("Tu es V14 periodic Pattern Mirror. Réponds uniquement en JSON valide.", payload, PERIODIC_SCHEMA)
        snap = out.get("periodic_snapshot") or {}
        if not isinstance(snap, dict) or not snap.get("overall_reading"):
            raise RuntimeError("V14 periodic mirror returned no periodic_snapshot.overall_reading")
        snapshot_id = stable_id("v14snapshot", person_id, period, label, snap.get("overall_reading"), now)
        upsert(con, "v14_periodic_self_snapshots", {
            "snapshot_id": snapshot_id,
            "person_id": person_id,
            "period": str(snap.get("period") or period),
            "period_start": start,
            "period_end": end,
            "period_label": str(snap.get("period_label") or label),
            "overall_reading": str(snap.get("overall_reading")),
            "state_summary": snap.get("state_summary"),
            "dominant_loops_json": json_dumps(_as_list(snap.get("dominant_loops"))),
            "new_contradictions_json": json_dumps(_as_list(snap.get("new_contradictions"))),
            "weak_signals_json": json_dumps(_as_list(snap.get("weak_signals_resembling_old_errors"))),
            "people_triggers_json": json_dumps(_as_list(snap.get("people_triggering_states"))),
            "decision_echoes_json": json_dumps(_as_list(snap.get("decisions_resembling_old_choices"))),
            "language_markers_json": json_dumps(_as_list(snap.get("phrases_predicting_action_or_blockage"))),
            "long_trajectory_updates_json": json_dumps(_as_list(snap.get("long_trajectory_updates"))),
            "forecasts_to_watch_json": json_dumps(_as_list(snap.get("forecasts_to_watch"))),
            "interventions_json": json_dumps(_as_list(snap.get("interventions"))),
            "confidence": _clamp(snap.get("confidence")),
            "created_at": now,
        }, "snapshot_id")
        maps=[]; chains=[]; watches=[]
        for m in out.get("people_trigger_maps") or []:
            if not isinstance(m, dict):
                continue
            mid = stable_id("v14triggermap", person_id, m.get("other_person_id") or m.get("other_display_name") or "unknown")
            upsert(con, "v14_people_trigger_maps", {
                "map_id": mid, "person_id": person_id,
                "other_person_id": m.get("other_person_id"),
                "other_display_name": m.get("other_display_name"),
                "triggered_states_json": json_dumps(_as_list(m.get("triggered_states"))),
                "common_loops_json": json_dumps(_as_list(m.get("common_loops"))),
                "evidence_json": json_dumps(_as_list(m.get("evidence"))),
                "counter_evidence_json": json_dumps(_as_list(m.get("counter_evidence"))),
                "confidence": _clamp(m.get("confidence")),
                "created_at": now, "updated_at": now,
            }, "map_id")
            maps.append(mid)
        for c in out.get("repetition_chains") or []:
            if not isinstance(c, dict) or not c.get("title"):
                continue
            cid = stable_id("v14chain", person_id, c.get("chain_type"), c.get("title"))
            upsert(con, "v14_repetition_chains", {
                "chain_id": cid, "person_id": person_id,
                "chain_type": str(c.get("chain_type") or "other"),
                "title": str(c.get("title")),
                "sequence_json": json_dumps(_as_list(c.get("sequence"))),
                "first_seen": c.get("first_seen"), "last_seen": c.get("last_seen"),
                "recurrence_count": int(c.get("recurrence_count") or 0),
                "likely_next_step": c.get("likely_next_step"),
                "warning_signal": c.get("warning_signal"),
                "escape_action": c.get("escape_action"),
                "evidence_json": json_dumps(_as_list(c.get("evidence"))),
                "counter_evidence_json": json_dumps(_as_list(c.get("counter_evidence"))),
                "confidence": _clamp(c.get("confidence")),
                "created_at": now, "updated_at": now,
            }, "chain_id")
            chains.append(cid)
        for w in out.get("forecast_watch_queue") or []:
            if not isinstance(w, dict) or not w.get("forecast_text"):
                continue
            wid = stable_id("v14watch", person_id, snapshot_id, w.get("forecast_text"), w.get("watch_condition"))
            upsert(con, "v14_forecast_watch_queue", {
                "watch_id": wid, "person_id": person_id, "snapshot_id": snapshot_id,
                "forecast_text": str(w.get("forecast_text")),
                "watch_condition": w.get("watch_condition"),
                "due_horizon": w.get("due_horizon"),
                "probability": _clamp(w.get("probability")),
                "confidence": _clamp(w.get("confidence")),
                "status": "watching", "created_at": now,
                "verified_at": None, "observed_evidence": None,
            }, "watch_id")
            watches.append(wid)
        from .v18_legacy_forecasts import reconcile_legacy_forecasts
        reconcile_legacy_forecasts(person_id=person_id, con=con)
        con.commit()
    return {"version": V14_VERSION, "person_id": person_id, "period": period, "snapshot_id": snapshot_id, "trigger_maps": maps, "repetition_chains": chains, "forecast_watches": watches, "raw": out}


def list_periodic_snapshots(*, person_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    ensure_v14_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        snapshots = _many(con, "SELECT * FROM v14_periodic_self_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit))
        chains = _many(con, "SELECT * FROM v14_repetition_chains WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        trigger_maps = _many(con, "SELECT * FROM v14_people_trigger_maps WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        from .v18_legacy_forecasts import active_legacy_forecasts
        watches = active_legacy_forecasts(con, person_id=person_id, source_table="v14_forecast_watch_queue", limit=limit)
    return {"version": V14_VERSION, "person_id": person_id, "snapshots": snapshots, "repetition_chains": chains, "people_trigger_maps": trigger_maps, "forecast_watch_queue": watches}


def audit_v14(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_TABLES - tables)
        counts = {}
        for t in sorted(V14_TABLES):
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            except Exception:
                counts[t] = "missing"
        if persist:
            now = now_iso()
            for t in sorted(V14_TABLES):
                upsert(con, "v14_contract_checks", {
                    "check_id": stable_id("v14audit", t),
                    "check_name": f"exists:{t}",
                    "status": "ok" if t in tables else "missing",
                    "detail": "V14 final Pattern Mirror coverage check.",
                    "created_at": now,
                }, "check_id")
            con.commit()
    return {
        "version": V14_VERSION,
        "goal": "limited-human-memory -> hidden loops -> long-horizon self-model -> forecasts -> interventions -> revision",
        "required_tables": sorted(V14_TABLES),
        "missing": missing,
        "missing_tables": missing,
        "ok": not missing,
        "counts": counts,
        "capabilities": [
            "autonomous_hidden_pattern_detection",
            "long_horizon_self_model",
            "daily_weekly_monthly_consolidation",
            "people_trigger_mapping",
            "repetition_chain_detection",
            "forecast_watch_queue",
            "counterfactual_lessons",
            "natural_question_answering",
        ],
        "autonomous_flow": "flow-watch runs V13 then V14 Pattern Mirror after each conversation when Qwen is available.",
        "periodic_flow": "v14-today/v14-consolidate create day/week/month/all_time self snapshots: aujourd'hui, cette semaine, ce mois-ci, trajectoires longues.",
        "natural_interface": "v14-ask accepts natural questions without manually choosing next_*.",
    }
