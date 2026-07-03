from __future__ import annotations

"""V14.5 People Identity Hypotheses + Personal Open Loop Solution Tracker.

This layer does not replace voice learning, V13 prediction, V14 Pattern Mirror,
V14.2 vector fusion, V14.3 self-model export or V14.4 auto-verification.

It adds two user-facing capabilities that make the memory more humanly useful:

1. The system may *hypothesize* that an unknown speaker is a known person (for
   example Max) from dialogue evidence, address patterns, family/proximity clues,
   repeated contexts and existing relationship models. It never confirms or
   rewrites speaker identity by itself. Confirmation still goes through
   `name-voice` / `enroll-voice`.
2. The system tracks the user's active desires, confusions, questions, blocks,
   expectations, unresolved problems and solution candidates over time. This
   turns casual sentences like "I would like to do X" or "I do not understand why
   Y happens" into active self-model objects that can be updated by later
   evidence, linked to outcomes, and exported in the readable self-model.

Cognitive interpretation is Qwen/Ollama JSON-contract based. This module does
not assign identities, relationships or psychological conclusions from local
keyword rules. If Qwen is unavailable, the run is recorded as an error and no
fake hypothesis is created.
"""

from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, stable_id
from .self_model_export_v14_3 import ensure_v14_3_schema
from .auto_verification_v14_4 import ensure_v14_4_schema

V14_5_VERSION = "14.5.0-people-openloops-final"

V14_5_TABLES = {
    "v14_5_people_identity_runs",
    "v14_5_people_identity_hypotheses",
    "v14_5_speaker_name_evidence",
    "v14_5_relationship_inference_cards",
    "v14_5_people_context_profiles",
    "v14_5_open_loop_runs",
    "v14_5_personal_open_loops",
    "v14_5_open_loop_updates",
    "v14_5_active_questions",
    "v14_5_solution_candidates",
    "v14_5_next_best_actions",
    "v14_5_contract_checks",
}

IDENTITY_SCHEMA: dict[str, Any] = {
    "speaker_identity_hypotheses": [
        {
            "speaker_label": "",
            "voice_cluster_id": None,
            "suspected_person_id": None,
            "suspected_display_name": "",
            "suspected_relation_to_user": "brother|sister|parent|partner|friend|colleague|client|family|close_person|unknown|other",
            "familiarity_level": "unknown|low|medium|high|very_high",
            "addressed_by_name": False,
            "address_evidence": [],
            "relationship_evidence": [],
            "conversation_contexts": [],
            "counter_evidence": [],
            "confidence": 0.0,
            "recommended_action": "confirm_with_name_voice|collect_more_audio|ignore|reject_hypothesis|enroll_voice_sample",
        }
    ],
    "relationship_inferences": [
        {
            "other_person_hint": "",
            "relationship_type_hypothesis": "brother|sister|parent|partner|friend|colleague|client|family|close_person|unknown|other",
            "familiarity_level": "unknown|low|medium|high|very_high",
            "communication_style": "",
            "topics_often_discussed": [],
            "emotional_dynamic": "",
            "roles_or_family_clues": [],
            "evidence": [],
            "counter_evidence": [],
            "confidence": 0.0,
        }
    ],
    "people_context_profiles": [
        {
            "person_hint": "",
            "known_person_id": None,
            "speaker_label": None,
            "what_you_often_talk_about": [],
            "how_user_behaves_with_them": "",
            "states_they_trigger": [],
            "recurring_loops": [],
            "open_questions_about_person": [],
            "confidence": 0.0,
        }
    ],
    "missing_context": [],
    "confidence": 0.0,
}

OPEN_LOOP_SCHEMA: dict[str, Any] = {
    "new_or_updated_open_loops": [
        {
            "loop_type": "desire|goal|confusion|problem|blockage|expectation|need|fear|decision|relationship_question|project_question|self_question|other",
            "title": "",
            "user_words": "",
            "canonical_summary": "",
            "why_it_matters": "",
            "current_status": "new|active|stalled|progressing|resolved|contradicted|unclear",
            "urgency": "low|medium|high|critical",
            "life_domain": "personal|relationship|professional|project|health|family|identity|money|learning|unknown|other",
            "related_people": [],
            "related_projects_or_topics": [],
            "suspected_blockers": [],
            "what_would_count_as_progress": "",
            "what_would_count_as_resolution": "",
            "evidence_turn_ids": [],
            "evidence_texts": [],
            "linked_existing_loop_id": None,
            "confidence": 0.0,
        }
    ],
    "loop_updates": [
        {
            "loop_id_or_title": "",
            "update_type": "progress|stalled|resolved|contradicted|new_evidence|new_blocker|solution_found|needs_followup|other",
            "update_summary": "",
            "evidence_text": "",
            "confidence": 0.0,
        }
    ],
    "active_questions": [
        {
            "question_text": "",
            "question_type": "why_is_this_happening|how_to_solve|what_do_i_want|what_should_i_do|why_am_i_blocked|relationship|project|self|other",
            "current_best_hypothesis": "",
            "what_to_watch_next": [],
            "confidence": 0.0,
        }
    ],
    "solution_candidates": [
        {
            "related_loop_title_or_id": "",
            "solution_type": "explanation|next_action|test|conversation|boundary|simplification|environment_change|habit|decision|information_needed|other",
            "solution_summary": "",
            "why_this_might_work": "",
            "evidence_from_history": [],
            "counter_risks": [],
            "confidence": 0.0,
        }
    ],
    "next_best_actions": [
        {
            "related_loop_title_or_id": "",
            "action_text": "",
            "time_horizon": "now|today|week|month|later|unknown",
            "expected_effect": "",
            "risk_if_not_done": "",
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


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any], timeout: int = 420) -> dict[str, Any]:
    data = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=timeout)
    if not isinstance(data, dict):
        raise RuntimeError("Brain2 V14.5 returned non-object JSON")
    return data


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
        d: dict[str, Any] = {}
        for k, v in row.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > 1800:
                d[k] = v[:1800] + "…"
            else:
                d[k] = v
        out.append(d)
    return out


def ensure_v14_5_schema() -> None:
    ensure_v14_4_schema()
    ensure_v14_3_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_5_people_identity_runs(
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error_text TEXT,
                qwen_output_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_people_identity_hypotheses(
                hypothesis_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                speaker_label TEXT,
                voice_cluster_id TEXT,
                suspected_person_id TEXT,
                suspected_display_name TEXT,
                suspected_relation_to_user TEXT,
                familiarity_level TEXT,
                status TEXT NOT NULL DEFAULT 'pending_confirmation',
                addressed_by_name INTEGER DEFAULT 0,
                confidence REAL DEFAULT 0,
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                recommended_action TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_speaker_name_evidence(
                evidence_id TEXT PRIMARY KEY,
                hypothesis_id TEXT,
                conversation_id TEXT,
                speaker_label TEXT,
                evidence_turn_id TEXT,
                evidence_text TEXT,
                evidence_type TEXT,
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_relationship_inference_cards(
                card_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                other_person_hint TEXT,
                known_person_id TEXT,
                relationship_type_hypothesis TEXT,
                familiarity_level TEXT,
                communication_style TEXT,
                topics_json TEXT DEFAULT '[]',
                emotional_dynamic TEXT,
                evidence_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                status TEXT DEFAULT 'hypothesis',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_people_context_profiles(
                profile_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL DEFAULT 'me',
                person_hint TEXT,
                known_person_id TEXT,
                speaker_label TEXT,
                what_you_often_talk_about_json TEXT DEFAULT '[]',
                how_user_behaves_with_them TEXT,
                states_they_trigger_json TEXT DEFAULT '[]',
                recurring_loops_json TEXT DEFAULT '[]',
                open_questions_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0,
                evidence_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_open_loop_runs(
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error_text TEXT,
                new_or_updated_count INTEGER DEFAULT 0,
                qwen_output_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_personal_open_loops(
                loop_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                loop_type TEXT NOT NULL,
                title TEXT NOT NULL,
                canonical_summary TEXT,
                user_words TEXT,
                why_it_matters TEXT,
                current_status TEXT NOT NULL DEFAULT 'active',
                urgency TEXT DEFAULT 'medium',
                life_domain TEXT DEFAULT 'unknown',
                related_people_json TEXT DEFAULT '[]',
                related_projects_or_topics_json TEXT DEFAULT '[]',
                suspected_blockers_json TEXT DEFAULT '[]',
                progress_definition TEXT,
                resolution_definition TEXT,
                evidence_json TEXT DEFAULT '[]',
                linked_existing_loop_id TEXT,
                confidence REAL DEFAULT 0,
                first_seen_at TEXT,
                last_seen_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_open_loop_updates(
                update_id TEXT PRIMARY KEY,
                loop_id TEXT,
                conversation_id TEXT,
                update_type TEXT NOT NULL,
                update_summary TEXT,
                evidence_text TEXT,
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_active_questions(
                question_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                loop_id TEXT,
                question_text TEXT NOT NULL,
                question_type TEXT,
                current_best_hypothesis TEXT,
                what_to_watch_next_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'open',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_solution_candidates(
                solution_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                loop_id TEXT,
                solution_type TEXT,
                solution_summary TEXT NOT NULL,
                why_this_might_work TEXT,
                evidence_from_history_json TEXT DEFAULT '[]',
                counter_risks_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'candidate',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_next_best_actions(
                action_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                loop_id TEXT,
                action_text TEXT NOT NULL,
                time_horizon TEXT,
                expected_effect TEXT,
                risk_if_not_done TEXT,
                status TEXT DEFAULT 'suggested',
                confidence REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_5_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v145_identity_speaker ON v14_5_people_identity_hypotheses(speaker_label, suspected_person_id, status);
            CREATE INDEX IF NOT EXISTS idx_v145_identity_cluster ON v14_5_people_identity_hypotheses(voice_cluster_id, status);
            CREATE INDEX IF NOT EXISTS idx_v145_loops_person ON v14_5_personal_open_loops(person_id, current_status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v145_questions_person ON v14_5_active_questions(person_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v145_solutions_person ON v14_5_solution_candidates(person_id, status, updated_at);
            """
        )
        # V16.0 migration: V14.5 people profiles originally had no owner column.
        # BrainLive needs owner-scoped retrieval so expressions/relations with X
        # cannot bleed across users or profiles.
        cols = {str(r[1]) for r in con.execute("PRAGMA table_info(v14_5_people_context_profiles)").fetchall()}
        if "person_id" not in cols:
            con.execute("ALTER TABLE v14_5_people_context_profiles ADD COLUMN person_id TEXT DEFAULT 'me'")
            con.execute("UPDATE v14_5_people_context_profiles SET person_id='me' WHERE person_id IS NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_v145_profiles_owner_hint ON v14_5_people_context_profiles(person_id, known_person_id, person_hint, updated_at)")
        now = now_iso()
        for table in sorted(V14_5_TABLES):
            upsert(con, "v14_5_contract_checks", {
                "check_id": stable_id("v145check", table),
                "check_name": f"table:{table}",
                "status": "declared",
                "detail": "V14.5 people identity hypotheses and personal open-loop solution tracker table.",
                "created_at": now,
            }, "check_id")
        con.commit()


def _conversation_payload(con, conversation_id: str, *, limit_turns: int = 160) -> dict[str, Any]:
    conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not conv:
        raise ValueError(f"conversation introuvable: {conversation_id}")
    turns = _many(con, "SELECT * FROM turns WHERE conversation_id=? ORDER BY idx LIMIT ?", (conversation_id, limit_turns))
    speaker_matches = _many(con, "SELECT * FROM speaker_matches WHERE conversation_id=? ORDER BY created_at", (conversation_id,))
    try:
        voice_observations = _many(con, "SELECT * FROM voice_observations WHERE conversation_id=? ORDER BY created_at", (conversation_id,))
    except Exception:
        voice_observations = []
    from .v18_brain2_context import conversation_context_addenda
    return {
        "conversation": dict(conv),
        "turns": _compact(turns, limit_turns),
        "context_addenda": conversation_context_addenda(con, conversation_id=conversation_id),
        "speaker_matches": _compact(speaker_matches, 80),
        "voice_observations": _compact(voice_observations, 80),
    }


def _background_for_person(con, person_id: str, *, limit: int = 60) -> dict[str, Any]:
    return {
        "speaker_profiles": _compact(_many(con, "SELECT * FROM speaker_profiles ORDER BY is_user DESC, created_at DESC LIMIT ?", (limit,)), limit),
        "voice_clusters": _compact(_many(con, "SELECT * FROM voice_clusters ORDER BY last_seen_at DESC LIMIT ?", (limit,)), limit),
        "relationship_models": _compact(_many(con, "SELECT * FROM relationship_models WHERE person_a=? OR person_b=? ORDER BY updated_at DESC LIMIT ?", (person_id, person_id, limit)), limit),
        "people_trigger_maps": _compact(_many(con, "SELECT * FROM v14_people_trigger_maps WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "existing_identity_hypotheses": _compact(_many(con, "SELECT * FROM v14_5_people_identity_hypotheses ORDER BY updated_at DESC LIMIT ?", (limit,)), limit),
        "existing_open_loops": _compact(_many(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "existing_questions": _compact(_many(con, "SELECT * FROM v14_5_active_questions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_intentions": _compact(_many(con, "SELECT * FROM action_intentions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_outcomes": _compact(_many(con, "SELECT * FROM action_outcomes WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_thoughts": _compact(_many(con, "SELECT * FROM thought_hypotheses WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "recent_states": _compact(_many(con, "SELECT * FROM internal_state_snapshots WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "latent_outcomes": _compact(_many(con, "SELECT * FROM latent_outcome_links ORDER BY created_at DESC LIMIT ?", (limit,)), limit),
    }


def analyze_people_identity_hypotheses(conversation_id: str, *, person_id: str | None = None) -> dict[str, Any]:
    """Create pending identity/relationship hypotheses for people in a conversation.

    It never updates speaker_profiles or speaker_matches. It only creates
    confirmable hypotheses and relationship context cards.
    """
    ensure_v14_5_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        payload = {
            "mission": (
                "Analyse l'identité relationnelle possible des personnes dans cette conversation. "
                "Tu peux proposer des hypothèses pending: prénom possible, lien familial/proche, familiarité, "
                "sujets et dynamique. Ne confirme jamais définitivement une identité vocale. "
                "Ne propose une identité que si des preuves conversationnelles existent, et donne aussi les contre-preuves."
            ),
            "user_person_id": person_id,
            "conversation_data": _conversation_payload(con, conversation_id),
            "background": _background_for_person(con, person_id, limit=60),
            "required_behavior": [
                "If the user talks ABOUT Max to another speaker, do not treat that alone as speaking TO Max.",
                "A suspected name/relation must remain pending_confirmation until the user confirms with name-voice/enroll-voice.",
                "Prefer uncertainty over a false identity.",
                "Use evidence_turn_ids/evidence text when available.",
            ],
            "schema": IDENTITY_SCHEMA,
        }
    run_id = stable_id("v145people", conversation_id, person_id, now)
    try:
        out = _llm_json("Tu es le People Identity Analyst strict. Réponds en JSON valide uniquement.", payload, IDENTITY_SCHEMA)
        status = "ok"
        error = None
    except Exception as exc:
        out = {"error": str(exc)[:2000]}
        status = "error"
        error = str(exc)[:2000]
    with connect() as con:
        upsert(con, "v14_5_people_identity_runs", {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "person_id": person_id,
            "status": status,
            "error_text": error,
            "qwen_output_json": json_dumps(out),
            "created_at": now,
        }, "run_id")
        if status == "ok":
            for item in out.get("speaker_identity_hypotheses") or []:
                if not isinstance(item, dict):
                    continue
                speaker_label = item.get("speaker_label")
                suspected_name = item.get("suspected_display_name") or item.get("suspected_person_id")
                if not speaker_label or not suspected_name:
                    continue
                hypothesis_id = stable_id("v145ident", speaker_label, item.get("voice_cluster_id"), suspected_name)
                evidence = {
                    "address_evidence": item.get("address_evidence") or [],
                    "relationship_evidence": item.get("relationship_evidence") or [],
                    "conversation_contexts": item.get("conversation_contexts") or [],
                }
                upsert(con, "v14_5_people_identity_hypotheses", {
                    "hypothesis_id": hypothesis_id,
                    "conversation_id": conversation_id,
                    "speaker_label": speaker_label,
                    "voice_cluster_id": item.get("voice_cluster_id"),
                    "suspected_person_id": item.get("suspected_person_id"),
                    "suspected_display_name": suspected_name,
                    "suspected_relation_to_user": item.get("suspected_relation_to_user") or "unknown",
                    "familiarity_level": item.get("familiarity_level") or "unknown",
                    "status": "pending_confirmation",
                    "addressed_by_name": 1 if item.get("addressed_by_name") else 0,
                    "confidence": _clamp(item.get("confidence")),
                    "evidence_json": json_dumps(evidence),
                    "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                    "recommended_action": item.get("recommended_action") or "collect_more_audio",
                    "created_at": now,
                    "updated_at": now,
                }, "hypothesis_id")
                for idx, ev in enumerate((item.get("address_evidence") or []) + (item.get("relationship_evidence") or [])):
                    evidence_text = ev.get("evidence_text") if isinstance(ev, dict) else str(ev)
                    turn_id = ev.get("turn_id") if isinstance(ev, dict) else None
                    evidence_id = stable_id("v145nameev", hypothesis_id, idx, turn_id, evidence_text)
                    upsert(con, "v14_5_speaker_name_evidence", {
                        "evidence_id": evidence_id,
                        "hypothesis_id": hypothesis_id,
                        "conversation_id": conversation_id,
                        "speaker_label": speaker_label,
                        "evidence_turn_id": turn_id,
                        "evidence_text": evidence_text,
                        "evidence_type": "address_or_relationship_clue",
                        "confidence": _clamp(item.get("confidence")),
                        "created_at": now,
                    }, "evidence_id")
            for item in out.get("relationship_inferences") or []:
                if not isinstance(item, dict) or not item.get("other_person_hint"):
                    continue
                card_id = stable_id("v145rel", item.get("other_person_hint"), item.get("relationship_type_hypothesis"), conversation_id)
                upsert(con, "v14_5_relationship_inference_cards", {
                    "card_id": card_id,
                    "conversation_id": conversation_id,
                    "other_person_hint": item.get("other_person_hint"),
                    "known_person_id": item.get("known_person_id"),
                    "relationship_type_hypothesis": item.get("relationship_type_hypothesis") or "unknown",
                    "familiarity_level": item.get("familiarity_level") or "unknown",
                    "communication_style": item.get("communication_style"),
                    "topics_json": json_dumps(item.get("topics_often_discussed") or []),
                    "emotional_dynamic": item.get("emotional_dynamic"),
                    "evidence_json": json_dumps(item.get("evidence") or []),
                    "counter_evidence_json": json_dumps(item.get("counter_evidence") or []),
                    "confidence": _clamp(item.get("confidence")),
                    "status": "hypothesis",
                    "created_at": now,
                    "updated_at": now,
                }, "card_id")
            for item in out.get("people_context_profiles") or []:
                if not isinstance(item, dict) or not item.get("person_hint"):
                    continue
                profile_id = stable_id("v145peopleprofile", item.get("known_person_id") or item.get("person_hint"), item.get("speaker_label"))
                evidence_count = len(item.get("what_you_often_talk_about") or []) + len(item.get("states_they_trigger") or []) + len(item.get("recurring_loops") or [])
                upsert(con, "v14_5_people_context_profiles", {
                    "profile_id": profile_id,
                    "person_id": person_id,
                    "person_hint": item.get("person_hint"),
                    "known_person_id": item.get("known_person_id"),
                    "speaker_label": item.get("speaker_label"),
                    "what_you_often_talk_about_json": json_dumps(item.get("what_you_often_talk_about") or []),
                    "how_user_behaves_with_them": item.get("how_user_behaves_with_them"),
                    "states_they_trigger_json": json_dumps(item.get("states_they_trigger") or []),
                    "recurring_loops_json": json_dumps(item.get("recurring_loops") or []),
                    "open_questions_json": json_dumps(item.get("open_questions_about_person") or []),
                    "confidence": _clamp(item.get("confidence")),
                    "evidence_count": evidence_count,
                    "created_at": now,
                    "updated_at": now,
                }, "profile_id")
        con.commit()
    return {"version": V14_5_VERSION, "run_id": run_id, "conversation_id": conversation_id, "status": status, "error": error, "raw": out}


def _match_existing_loop_id(item: dict[str, Any], person_id: str) -> str | None:
    linked = item.get("linked_existing_loop_id")
    if isinstance(linked, str) and linked.strip():
        return linked.strip()
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    candidate_id = stable_id("v145loop", person_id, title)
    with connect() as con:
        row = con.execute("SELECT loop_id FROM v14_5_personal_open_loops WHERE loop_id=?", (candidate_id,)).fetchone()
        return row["loop_id"] if row else None


def track_personal_open_loops(conversation_id: str, *, person_id: str | None = None) -> dict[str, Any]:
    """Track desires, questions, blocks and possible solutions from a conversation."""
    ensure_v14_5_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        payload = {
            "mission": (
                "Transforme les désirs, attentes, questions, incompréhensions, blocages, besoins et problèmes personnels exprimés "
                "dans la conversation en objets suivables. Cherche aussi si une nouvelle preuve résout, contredit ou explique un ancien blocage. "
                "Propose des solutions candidates uniquement si elles sont appuyées par l'historique ou par un test concret."
            ),
            "user_person_id": person_id,
            "conversation_data": _conversation_payload(con, conversation_id),
            "background": _background_for_person(con, person_id, limit=90),
            "required_behavior": [
                "Do not turn every casual sentence into a life-level issue.",
                "Track explicit desires and explicit confusion strongly; inferred desires must have lower confidence.",
                "A solution candidate must include why it might work and counter-risks.",
                "Keep unresolved questions open until later outcomes or user corrections close them.",
            ],
            "schema": OPEN_LOOP_SCHEMA,
        }
    run_id = stable_id("v145openlooprun", conversation_id, person_id, now)
    try:
        out = _llm_json("Tu es le Personal Open Loop Solution Tracker strict. Réponds en JSON valide uniquement.", payload, OPEN_LOOP_SCHEMA)
        status = "ok"
        error = None
    except Exception as exc:
        out = {"error": str(exc)[:2000]}
        status = "error"
        error = str(exc)[:2000]
    loop_title_to_id: dict[str, str] = {}
    created_or_updated = 0
    with connect() as con:
        if status == "ok":
            for item in out.get("new_or_updated_open_loops") or []:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                loop_id = _match_existing_loop_id(item, person_id) or stable_id("v145loop", person_id, item.get("title"))
                loop_title_to_id[str(item.get("title"))] = loop_id
                prev = con.execute("SELECT first_seen_at, created_at FROM v14_5_personal_open_loops WHERE loop_id=?", (loop_id,)).fetchone()
                first_seen = prev["first_seen_at"] if prev and prev["first_seen_at"] else now
                created_at = prev["created_at"] if prev and prev["created_at"] else now
                evidence = {
                    "evidence_turn_ids": item.get("evidence_turn_ids") or [],
                    "evidence_texts": item.get("evidence_texts") or [],
                    "source_conversation_id": conversation_id,
                }
                upsert(con, "v14_5_personal_open_loops", {
                    "loop_id": loop_id,
                    "person_id": person_id,
                    "loop_type": item.get("loop_type") or "other",
                    "title": item.get("title"),
                    "canonical_summary": item.get("canonical_summary"),
                    "user_words": item.get("user_words"),
                    "why_it_matters": item.get("why_it_matters"),
                    "current_status": item.get("current_status") or "active",
                    "urgency": item.get("urgency") or "medium",
                    "life_domain": item.get("life_domain") or "unknown",
                    "related_people_json": json_dumps(item.get("related_people") or []),
                    "related_projects_or_topics_json": json_dumps(item.get("related_projects_or_topics") or []),
                    "suspected_blockers_json": json_dumps(item.get("suspected_blockers") or []),
                    "progress_definition": item.get("what_would_count_as_progress"),
                    "resolution_definition": item.get("what_would_count_as_resolution"),
                    "evidence_json": json_dumps(evidence),
                    "linked_existing_loop_id": item.get("linked_existing_loop_id"),
                    "confidence": _clamp(item.get("confidence")),
                    "first_seen_at": first_seen,
                    "last_seen_at": now,
                    "created_at": created_at,
                    "updated_at": now,
                }, "loop_id")
                created_or_updated += 1
            for item in out.get("loop_updates") or []:
                if not isinstance(item, dict):
                    continue
                loop_ref = str(item.get("loop_id_or_title") or "").strip()
                loop_id = loop_title_to_id.get(loop_ref) or (loop_ref if loop_ref.startswith("v145loop_") else None)
                update_id = stable_id("v145loopupdate", conversation_id, loop_ref, item.get("update_type"), item.get("update_summary"), item.get("evidence_text"))
                upsert(con, "v14_5_open_loop_updates", {
                    "update_id": update_id,
                    "loop_id": loop_id,
                    "conversation_id": conversation_id,
                    "update_type": item.get("update_type") or "new_evidence",
                    "update_summary": item.get("update_summary"),
                    "evidence_text": item.get("evidence_text"),
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                }, "update_id")
                if loop_id and item.get("update_type") in {"progress", "stalled", "resolved", "contradicted"}:
                    con.execute("UPDATE v14_5_personal_open_loops SET current_status=?, updated_at=?, last_seen_at=? WHERE loop_id=?", (item.get("update_type"), now, now, loop_id))
            for item in out.get("active_questions") or []:
                if not isinstance(item, dict) or not item.get("question_text"):
                    continue
                loop_ref = str(item.get("loop_id_or_title") or "").strip()
                loop_id = loop_title_to_id.get(loop_ref)
                question_id = stable_id("v145question", person_id, item.get("question_text"))
                upsert(con, "v14_5_active_questions", {
                    "question_id": question_id,
                    "person_id": person_id,
                    "loop_id": loop_id,
                    "question_text": item.get("question_text"),
                    "question_type": item.get("question_type"),
                    "current_best_hypothesis": item.get("current_best_hypothesis"),
                    "what_to_watch_next_json": json_dumps(item.get("what_to_watch_next") or []),
                    "status": "open",
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "question_id")
            for item in out.get("solution_candidates") or []:
                if not isinstance(item, dict) or not item.get("solution_summary"):
                    continue
                loop_ref = str(item.get("related_loop_title_or_id") or "").strip()
                loop_id = loop_title_to_id.get(loop_ref) or (loop_ref if loop_ref.startswith("v145loop_") else None)
                solution_id = stable_id("v145solution", person_id, loop_id or loop_ref, item.get("solution_summary"))
                upsert(con, "v14_5_solution_candidates", {
                    "solution_id": solution_id,
                    "person_id": person_id,
                    "loop_id": loop_id,
                    "solution_type": item.get("solution_type"),
                    "solution_summary": item.get("solution_summary"),
                    "why_this_might_work": item.get("why_this_might_work"),
                    "evidence_from_history_json": json_dumps(item.get("evidence_from_history") or []),
                    "counter_risks_json": json_dumps(item.get("counter_risks") or []),
                    "status": "candidate",
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "solution_id")
            for item in out.get("next_best_actions") or []:
                if not isinstance(item, dict) or not item.get("action_text"):
                    continue
                loop_ref = str(item.get("related_loop_title_or_id") or "").strip()
                loop_id = loop_title_to_id.get(loop_ref) or (loop_ref if loop_ref.startswith("v145loop_") else None)
                action_id = stable_id("v145nextaction", person_id, loop_id or loop_ref, item.get("action_text"))
                upsert(con, "v14_5_next_best_actions", {
                    "action_id": action_id,
                    "person_id": person_id,
                    "loop_id": loop_id,
                    "action_text": item.get("action_text"),
                    "time_horizon": item.get("time_horizon"),
                    "expected_effect": item.get("expected_effect"),
                    "risk_if_not_done": item.get("risk_if_not_done"),
                    "status": "suggested",
                    "confidence": _clamp(item.get("confidence")),
                    "created_at": now,
                    "updated_at": now,
                }, "action_id")
        upsert(con, "v14_5_open_loop_runs", {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "person_id": person_id,
            "status": status,
            "error_text": error,
            "new_or_updated_count": created_or_updated,
            "qwen_output_json": json_dumps(out),
            "created_at": now,
        }, "run_id")
        con.commit()
    return {"version": V14_5_VERSION, "run_id": run_id, "conversation_id": conversation_id, "person_id": person_id, "status": status, "new_or_updated_count": created_or_updated, "error": error, "raw": out}


def run_v14_5_post_conversation(conversation_id: str, *, person_id: str | None = None) -> dict[str, Any]:
    ensure_v14_5_schema()
    people = analyze_people_identity_hypotheses(conversation_id, person_id=person_id)
    open_loops = track_personal_open_loops(conversation_id, person_id=person_id)
    return {"version": V14_5_VERSION, "conversation_id": conversation_id, "people_identity": people, "open_loops": open_loops}


def list_people_identity_hypotheses(*, status: str = "pending_confirmation", limit: int = 50) -> dict[str, Any]:
    ensure_v14_5_schema()
    with connect() as con:
        rows = _many(con, "SELECT * FROM v14_5_people_identity_hypotheses WHERE status=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (status, limit))
        cards = _many(con, "SELECT * FROM v14_5_relationship_inference_cards ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,))
        profiles = _many(con, "SELECT * FROM v14_5_people_context_profiles ORDER BY updated_at DESC LIMIT ?", (limit,))
    return {"version": V14_5_VERSION, "status": status, "hypotheses": rows, "relationship_cards": cards, "people_context_profiles": profiles}


def list_personal_open_loops(*, person_id: str | None = None, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    ensure_v14_5_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        if status:
            loops = _many(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? AND current_status=? ORDER BY urgency DESC, updated_at DESC LIMIT ?", (person_id, status, limit))
        else:
            loops = _many(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        questions = _many(con, "SELECT * FROM v14_5_active_questions WHERE person_id=? ORDER BY updated_at DESC LIMIT ?", (person_id, limit))
        solutions = _many(con, "SELECT * FROM v14_5_solution_candidates WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit))
        actions = _many(con, "SELECT * FROM v14_5_next_best_actions WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit))
    return {"version": V14_5_VERSION, "person_id": person_id, "loops": loops, "active_questions": questions, "solution_candidates": solutions, "next_best_actions": actions}


def audit_v14_5(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_5_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_5_TABLES - tables)
        counts: dict[str, int | None] = {}
        for table in sorted(V14_5_TABLES):
            try:
                counts[table] = int(con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            except Exception:
                counts[table] = None
    return {
        "version": V14_5_VERSION,
        "ok": not missing,
        "missing_tables": missing,
        "required_tables": sorted(V14_5_TABLES),
        "counts": counts,
        "capabilities": {
            "people_identity_hypotheses": "unknown voices can be linked to pending name/relation hypotheses without auto-confirming identity",
            "relationship_depth": "family/proximity/familiarity/topics/emotional dynamics are tracked as hypotheses",
            "open_loop_solution_tracker": "desires, confusions, blocks, expectations and solution candidates become active self-model objects",
            "flow_watch_autonomy": "flow-watch calls V14.5 after V14.4 and before export/consolidation",
            "trust_boundary": "name-voice/enroll-voice remain manual for identity confirmation",
        },
        "manual_commands": [
            "mlomega-audio v14-5-run <conversation_id>",
            "mlomega-audio v14-people-hypotheses",
            "mlomega-audio v14-open-loops --person-id me",
            "mlomega-audio v14-5-audit",
        ],
    }
