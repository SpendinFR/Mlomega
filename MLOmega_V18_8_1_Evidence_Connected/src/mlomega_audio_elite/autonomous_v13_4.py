from __future__ import annotations

"""V13.4 autonomous insight loop.

This layer closes the UX gap: after each new conversation, the Brain 2.0 does not
only store and wait for a manual `v13-predict next_*` request. It creates an
inbox of pending hypotheses, predictions, warnings, questions-to-confirm and
interventions by itself, using Qwen strict JSON and the already-built V13 model.

Manual `v13-predict` remains useful for targeted questions. Autonomous insights
are what the system thinks you should see without being asked.
"""

from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient, EliteLLMError
from .utils import json_dumps, json_loads, now_iso, stable_id, sha256_bytes
from .brain2_complete_v13 import COMPLETE_TARGETS
from .brain2_strict_v13_2 import ensure_strict_v13_schema

AUTONOMOUS_VERSION = "13.4.0-autonomous-insight-loop"

INSIGHT_SCHEMA: dict[str, Any] = {
    "insights": [
        {
            "insight_type": "prediction|hypothesis|warning|intervention|question_to_user|similarity|loop_risk",
            "priority": "low|medium|high|critical",
            "person_id": "me",
            "episode_id": None,
            "title": "",
            "summary": "",
            "prediction_target": "next_action|next_phrase|next_emotion|next_thought|next_choice|next_reaction|next_outcome|next_trajectory|next_loop|next_risk|none",
            "predicted_value": "",
            "probability": 0.0,
            "confidence": 0.0,
            "why": [],
            "similar_cases": [],
            "counter_evidence": [],
            "assumptions": [],
            "intervention": "",
            "watch_for": [],
            "verification_question": "",
        }
    ],
    "global_summary": "",
    "missing_context": [],
    "confidence": 0.0,
}

ASK_SCHEMA: dict[str, Any] = {
    "answer": "",
    "inferred_intent": "memory_question|prediction_question|simulation_question|model_question|unknown",
    "inferred_prediction_targets": [],
    "probability": None,
    "confidence": 0.0,
    "why": [],
    "similar_cases": [],
    "counter_evidence": [],
    "recommended_action": "",
    "verification_plan": [],
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
    client = OllamaJsonClient()
    data = client.require_json(system, json_dumps(payload), schema_hint=schema)
    if not isinstance(data, dict):
        raise EliteLLMError("V13.4 autonomous engine returned non-object JSON")
    return data


def ensure_autonomous_schema() -> None:
    ensure_strict_v13_schema()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v13_autonomous_runs(
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT,
                trigger_type TEXT NOT NULL,
                status TEXT NOT NULL,
                qwen_output_json TEXT DEFAULT '{}',
                error_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v13_autonomous_insights(
                insight_id TEXT PRIMARY KEY,
                run_id TEXT,
                conversation_id TEXT,
                episode_id TEXT,
                person_id TEXT,
                insight_type TEXT NOT NULL,
                priority TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                prediction_id TEXT,
                warning_id TEXT,
                recommendation_id TEXT,
                confidence REAL DEFAULT 0.5,
                probability REAL,
                why_json TEXT DEFAULT '[]',
                similar_cases_json TEXT DEFAULT '[]',
                counter_evidence_json TEXT DEFAULT '[]',
                watch_for_json TEXT DEFAULT '[]',
                verification_question TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v13_autonomous_ask_runs(
                ask_id TEXT PRIMARY KEY,
                person_id TEXT,
                question TEXT NOT NULL,
                inferred_intent TEXT,
                qwen_answer_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        con.commit()


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _bundle_for_autonomy(con, conversation_id: str, person_id: str) -> dict[str, Any]:
    conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not conv:
        raise ValueError(f"conversation_missing: {conversation_id}")

    def many(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            return [dict(r) for r in con.execute(sql, params)]
        except Exception:
            return []

    episode_ids = [r["episode_id"] for r in con.execute("SELECT episode_id FROM episodes WHERE source_conversation_id=? ORDER BY start_time, created_at", (conversation_id,))]
    placeholders = ",".join("?" for _ in episode_ids) or "''"
    ep_params = tuple(episode_ids)
    from .v18_brain2_context import conversation_context_addenda
    return {
        "conversation": dict(conv),
        "turns": many("SELECT turn_id, idx, person_id, speaker_label, start_s, end_s, text, metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,)),
        "context_addenda": conversation_context_addenda(con, conversation_id=conversation_id, person_id=person_id),
        "episodes": many("SELECT * FROM episodes WHERE source_conversation_id=? ORDER BY start_time, created_at", (conversation_id,)),
        "subtopics": many("SELECT * FROM conversation_subtopic_segments WHERE conversation_id=? ORDER BY start_time, created_at", (conversation_id,)),
        "states": many(f"SELECT * FROM internal_state_snapshots WHERE episode_id IN ({placeholders})", ep_params),
        "thoughts": many(f"SELECT * FROM thought_hypotheses WHERE episode_id IN ({placeholders})", ep_params),
        "speech_acts": many(f"SELECT * FROM speech_acts WHERE episode_id IN ({placeholders})", ep_params),
        "intentions": many(f"SELECT * FROM action_intentions WHERE episode_id IN ({placeholders})", ep_params),
        "outcomes": many(f"SELECT * FROM action_outcomes WHERE episode_id IN ({placeholders})", ep_params),
        "choices": many(f"SELECT * FROM choice_episodes WHERE episode_id IN ({placeholders})", ep_params),
        "contradictions": many(f"SELECT * FROM contradiction_events WHERE episode_id IN ({placeholders})", ep_params),
        "recent_predictions": many("SELECT * FROM predictions WHERE person_id=? ORDER BY created_at DESC LIMIT 50", (person_id,)),
        "open_patterns": many("SELECT * FROM candidate_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT 50", (person_id,)) + many("SELECT * FROM confirmed_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT 50", (person_id,)),
        "loops": many("SELECT * FROM loop_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT 50", (person_id,)),
        "relationships": many("SELECT * FROM relationship_models WHERE person_a=? OR person_b=? ORDER BY updated_at DESC LIMIT 50", (person_id, person_id)),
        "latent_outcomes": many("SELECT * FROM latent_outcome_links WHERE new_conversation_id=? ORDER BY created_at DESC LIMIT 50", (conversation_id,)),
    }


def run_autonomous_insights(conversation_id: str, *, trigger_type: str = "post_ingest") -> dict[str, Any]:
    """Generate the autonomous prediction/hypothesis queue after a conversation.

    This is the thing the user expected: William said X to Max -> likely Y,
    because similar loops happened before -> advice/intervention to avoid the
    repeated error. It is not a manual next_* query.
    """
    ensure_autonomous_schema()
    now = now_iso()
    with connect() as con:
        person_id = _default_user(con)
        bundle = _bundle_for_autonomy(con, conversation_id, person_id)
        payload = {
            "mission": (
                "Tu es le moteur autonome Brain 2.0. Après une conversation, tu dois créer sans attendre une question utilisateur: "
                "1) hypothèses en attente, 2) prédictions probables, 3) risques de boucle répétée, "
                "4) interventions conseillées, 5) questions de confirmation. "
                "Ne choisis pas une seule cible: propose toutes les cibles utiles (next_phrase, next_action, next_emotion, next_thought, next_choice, next_reaction, next_outcome, next_trajectory). "
                "Chaque insight doit citer pourquoi, cas similaires, contre-preuves, et ce qu'il faut surveiller."
            ),
            "rules": [
                "Aucune psychologie générique: utilise uniquement le bundle et les preuves.",
                "Si la preuve est insuffisante, crée une question_to_user ou une hypothèse faible, pas une certitude.",
                "Priorise les insights utiles sans que l'utilisateur demande next_*.",
                "Les prédictions restent probabilistes et vérifiables.",
                "Respecte metadata_json.kind/evidence_role: une observation capteur/contexte n’est jamais une parole ou préférence déclarée de William.",
            ],
            "bundle": bundle,
            "schema": INSIGHT_SCHEMA,
        }
        run_id = stable_id("autonrun", AUTONOMOUS_VERSION, conversation_id, now)
        try:
            out = _llm_json("Tu es le moteur autonome Brain 2.0 strict. Réponds uniquement en JSON valide.", payload, INSIGHT_SCHEMA)
            status = "ok"; err = None
        except Exception as exc:
            out = {"insights": [], "error": str(exc)}; status = "error"; err = str(exc)[:2000]
        upsert(con, "v13_autonomous_runs", {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "person_id": person_id,
            "trigger_type": trigger_type,
            "status": status,
            "qwen_output_json": json_dumps(out),
            "error_text": err,
            "created_at": now,
            "updated_at": now,
        }, "run_id")
        created: list[str] = []
        if status == "ok":
            for idx, ins in enumerate(_as_list(out.get("insights"))):
                if not isinstance(ins, dict):
                    continue
                title = str(ins.get("title") or ins.get("summary") or "Insight autonome")[:300]
                summary = str(ins.get("summary") or title)
                episode_id = ins.get("episode_id")
                target = ins.get("prediction_target") if ins.get("prediction_target") in COMPLETE_TARGETS else None
                predicted_value = str(ins.get("predicted_value") or "").strip()
                prediction_id = None
                if target and predicted_value and target != "none":
                    prediction_id = stable_id("autonpred", AUTONOMOUS_VERSION, person_id, conversation_id, target, predicted_value[:180])
                    upsert(con, "predictions", {
                        "prediction_id": prediction_id,
                        "created_at": now,
                        "person_id": person_id,
                        "prediction_target": target,
                        "horizon": "next_or_near_future",
                        "current_context": summary,
                        "predicted_value": predicted_value,
                        "probability": _clamp(ins.get("probability")),
                        "confidence": _clamp(ins.get("confidence")),
                        "alternatives_json": json_dumps([]),
                        "evidence_cases_json": json_dumps(_as_list(ins.get("similar_cases"))),
                        "counter_evidence_json": json_dumps(_as_list(ins.get("counter_evidence"))),
                        "assumptions_json": json_dumps(_as_list(ins.get("assumptions"))),
                        "intervention_options_json": json_dumps(_as_list(ins.get("intervention"))),
                        "verification_due_at": None,
                        "status": "open",
                        "metadata_json": json_dumps({"autonomous": True, "run_id": run_id, "why": _as_list(ins.get("why")), "watch_for": _as_list(ins.get("watch_for"))}),
                        "updated_at": now,
                    }, "prediction_id")
                    upsert(con, "v13_prediction_explanations", {
                        "explanation_id": stable_id("autonexp", prediction_id),
                        "prediction_id": prediction_id,
                        "explanation_json": json_dumps({"title": title, "summary": summary}),
                        "why_json": json_dumps(_as_list(ins.get("why"))),
                        "similar_cases_json": json_dumps(_as_list(ins.get("similar_cases"))),
                        "counter_evidence_json": json_dumps(_as_list(ins.get("counter_evidence"))),
                        "assumptions_json": json_dumps(_as_list(ins.get("assumptions"))),
                        "intervention_json": json_dumps(_as_list(ins.get("intervention"))),
                        "uncertainty_json": json_dumps({"confidence": _clamp(ins.get("confidence")), "probability": _clamp(ins.get("probability"))}),
                        "created_at": now,
                    }, "explanation_id")
                warning_id = None
                if str(ins.get("insight_type")) in {"warning", "loop_risk"}:
                    warning_id = stable_id("autonwarn", run_id, idx, title)
                    upsert(con, "trajectory_warnings", {
                        "warning_id": warning_id,
                        "person_id": person_id,
                        "episode_id": episode_id,
                        "prediction_id": prediction_id,
                        "warning_type": str(ins.get("insight_type") or "warning"),
                        "title": title,
                        "detail": summary,
                        "severity": _clamp(ins.get("probability") or ins.get("confidence")),
                        "probability": _clamp(ins.get("probability")),
                        "evidence_json": json_dumps(_as_list(ins.get("why")) + _as_list(ins.get("similar_cases"))),
                        "counter_evidence_json": json_dumps(_as_list(ins.get("counter_evidence"))),
                        "status": "open",
                        "created_at": now,
                        "updated_at": now,
                    }, "warning_id")
                recommendation_id = None
                if ins.get("intervention"):
                    recommendation_id = stable_id("autonrec", run_id, idx, str(ins.get("intervention"))[:180])
                    upsert(con, "recommended_actions", {
                        "recommendation_id": recommendation_id,
                        "person_id": person_id,
                        "prediction_id": prediction_id,
                        "episode_id": episode_id,
                        "recommendation_type": "autonomous_intervention",
                        "title": "Intervention proposée: " + title[:180],
                        "detail": str(ins.get("intervention")),
                        "expected_effect": "Changer ou surveiller la trajectoire prédite.",
                        "confidence": _clamp(ins.get("confidence")),
                        "status": "open",
                        "evidence_json": json_dumps(_as_list(ins.get("why")) + _as_list(ins.get("similar_cases"))),
                        "created_at": now,
                        "updated_at": now,
                    }, "recommendation_id")
                insight_id = stable_id("autoninsight", run_id, idx, title, summary[:120])
                upsert(con, "v13_autonomous_insights", {
                    "insight_id": insight_id,
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                    "episode_id": episode_id,
                    "person_id": person_id,
                    "insight_type": str(ins.get("insight_type") or "hypothesis"),
                    "priority": str(ins.get("priority") or "medium"),
                    "title": title,
                    "summary": summary,
                    "prediction_id": prediction_id,
                    "warning_id": warning_id,
                    "recommendation_id": recommendation_id,
                    "confidence": _clamp(ins.get("confidence")),
                    "probability": _clamp(ins.get("probability")),
                    "why_json": json_dumps(_as_list(ins.get("why"))),
                    "similar_cases_json": json_dumps(_as_list(ins.get("similar_cases"))),
                    "counter_evidence_json": json_dumps(_as_list(ins.get("counter_evidence"))),
                    "watch_for_json": json_dumps(_as_list(ins.get("watch_for"))),
                    "verification_question": ins.get("verification_question"),
                    "status": "open",
                    "created_at": now,
                    "updated_at": now,
                }, "insight_id")
                created.append(insight_id)
        con.commit()
    return {"version": AUTONOMOUS_VERSION, "conversation_id": conversation_id, "run_id": run_id, "status": status, "insights_created": len(created), "insight_ids": created, "raw": out}


def list_autonomous_insights(*, status: str = "open", limit: int = 20) -> dict[str, Any]:
    ensure_autonomous_schema()
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM v13_autonomous_insights WHERE status=? ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC LIMIT ?",
            (status, limit),
        )]
    return {"version": AUTONOMOUS_VERSION, "status": status, "count": len(rows), "insights": rows}


def ask_life(question: str, *, person_id: str | None = None) -> dict[str, Any]:
    """Natural-language access to the whole ingested life model.

    The user does not choose next_action/next_emotion. Qwen infers the question
    type and can answer with memory, prediction, simulation or model insight.
    """
    ensure_autonomous_schema()
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        def many(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            try:
                return [dict(r) for r in con.execute(sql, params)]
            except Exception:
                return []
        payload = {
            "mission": "Réponds naturellement à la question de l'utilisateur à partir de toute la mémoire ingérée. Si la question est prédictive, infère toi-même la cible utile; ne demande pas à l'utilisateur de choisir next_*.",
            "question": question,
            "person_id": person_id,
            "memory": {
                "recent_episodes": many("SELECT * FROM episodes ORDER BY created_at DESC LIMIT 80"),
                "open_insights": many("SELECT * FROM v13_autonomous_insights WHERE status='open' ORDER BY created_at DESC LIMIT 50"),
                "predictions": many("SELECT * FROM predictions WHERE person_id=? ORDER BY created_at DESC LIMIT 80", (person_id,)),
                "patterns": many("SELECT * FROM candidate_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT 50", (person_id,)) + many("SELECT * FROM confirmed_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT 50", (person_id,)),
                "loops": many("SELECT * FROM loop_patterns WHERE person_id=? ORDER BY updated_at DESC LIMIT 50", (person_id,)),
                "states": many("SELECT * FROM internal_state_snapshots WHERE person_id=? ORDER BY created_at DESC LIMIT 80", (person_id,)),
                "thoughts": many("SELECT * FROM thought_hypotheses WHERE person_id=? ORDER BY created_at DESC LIMIT 80", (person_id,)),
                "relationships": many("SELECT * FROM relationship_models WHERE person_a=? OR person_b=? ORDER BY updated_at DESC LIMIT 80", (person_id, person_id)),
                "warnings": many("SELECT * FROM trajectory_warnings WHERE person_id=? AND status='open' ORDER BY created_at DESC LIMIT 50", (person_id,)),
            },
            "schema": ASK_SCHEMA,
        }
        out = _llm_json("Tu es l'interface naturelle Brain 2.0. Réponds en JSON strict, avec preuves, incertitude et action recommandée.", payload, ASK_SCHEMA)
        ask_id = stable_id("v13ask", AUTONOMOUS_VERSION, person_id, question, now)
        upsert(con, "v13_autonomous_ask_runs", {
            "ask_id": ask_id,
            "person_id": person_id,
            "question": question,
            "inferred_intent": out.get("inferred_intent"),
            "qwen_answer_json": json_dumps(out),
            "created_at": now,
        }, "ask_id")
        con.commit()
    return {"version": AUTONOMOUS_VERSION, "ask_id": ask_id, "question": question, **out}

# V18: autonomous outputs are scoped candidates, not immediate predictions/mutations.
from .v18_autonomous import install_autonomous as _install_v18_autonomous
_globals_v18_autonomous = _install_v18_autonomous(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_autonomous)
