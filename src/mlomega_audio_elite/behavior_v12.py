from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from .db import connect, init_db, upsert
from .utils import json_dumps, json_loads, normalize_text, now_iso, stable_id, tokenize

V12_VERSION = "12.1.0-complete-foundation"

PREDICTION_TARGETS = {
    "next_word",
    "next_phrase",
    "next_message",
    "next_emotion",
    "next_thought",
    "next_action",
    "next_choice",
    "next_reaction",
    "next_outcome",
    "next_loop",
    "next_risk",
    "next_relationship_move",
    "next_project_move",
    "next_client_outcome",
    "next_life_event",
    "next_trajectory",
}

CANONICAL_FACETS = {
    "need:clarity": ("need", "clarity", ["clarté", "comprendre", "explication", "concret"]),
    "need:control": ("need", "control", ["contrôle", "maîtrise", "fiable", "pas de fallback"]),
    "need:proof": ("need", "proof", ["preuve", "preuves", "sûr", "vérifier"]),
    "emotion:frustration": ("emotion", "frustration", ["énervé", "saoule", "frustration"]),
    "emotion:curiosity": ("emotion", "curiosity", ["curieux", "intéressant", "comprendre"]),
    "domain:project": ("life_domain", "project", ["projet", "moteur", "système"]),
    "domain:relationship": ("life_domain", "relationship", ["relation", "personne", "avec qui"]),
    "domain:client": ("life_domain", "client", ["client", "contrat", "demande"]),
}

WEAK_PATTERN_THRESHOLD = 2
CONFIRMED_PATTERN_THRESHOLD = 4


def _score_word(text: str, words: list[str]) -> float:
    norm = normalize_text(text)
    return min(1.0, sum(1 for w in words if normalize_text(w) in norm) / max(1, len(words)))


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _load_json(row: Any, key: str, default: Any) -> Any:
    try:
        return json_loads(row[key], default)
    except Exception:
        return default


def _person_is_user(con, person_id: str | None) -> bool:
    if not person_id:
        return False
    row = con.execute("SELECT is_user FROM speaker_profiles WHERE person_id=?", (person_id,)).fetchone()
    if row:
        return bool(row["is_user"])
    return person_id in {"me", "user", "utilisateur"}


def _default_user(con, conversation_id: str | None = None) -> str | None:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return row["person_id"]
    if conversation_id:
        row = con.execute("SELECT speaker_map_json, participants_json FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row:
            speaker_map = json_loads(row["speaker_map_json"], {}) or {}
            for val in speaker_map.values():
                if str(val).lower() in {"me", "moi", "user", "utilisateur"}:
                    return val
            participants = json_loads(row["participants_json"], []) or []
            for val in participants:
                if str(val).lower() in {"me", "moi", "user", "utilisateur"}:
                    return val
    return "me"


def _classify_speech_act(text: str) -> dict[str, Any]:
    norm = normalize_text(text)
    has_question = "?" in text or norm.startswith(("est ce", "est-ce", "pourquoi", "comment", "quand", "ou ", "où", "qui", "quoi"))
    act = "statement"
    if has_question:
        act = "question"
    if any(k in norm for k in ["je veux", "j'aimerais", "j aimerais", "fais", "explique", "dis moi", "redis", "liste"]):
        act = "request"
    if any(k in norm for k in ["ok", "oui", "exactement", "c'est ça", "c est ca", "c est ça"]):
        act = "validation"
    if any(k in norm for k in ["non", "pas ça", "pas ca", "tu n'as pas compris", "j'ai rien compris", "c est faux"]):
        act = "correction"
    if any(k in norm for k in ["je vais", "on va", "il faudra", "je ferai", "je dois"]):
        act = "commitment_or_plan"
    directness = 0.75 if act in {"request", "correction", "commitment_or_plan"} else 0.5
    pressure = 0.75 if any(k in norm for k in ["absolument", "tout", "sans rien oublier", "ne laisse rien", "réponse longue", "ultra"] ) else 0.5
    certainty = 0.75 if any(k in norm for k in ["je sais", "c'est", "exactement", "sans problème"] ) else 0.45
    if has_question:
        certainty = min(certainty, 0.45)
    return {
        "act_type": act,
        "directness": directness,
        "politeness": 0.5,
        "pressure_level": pressure,
        "certainty_level": certainty,
        "emotional_charge": max(pressure, 0.65 if act == "correction" else 0.45),
        "implicit_request": "clarify_or_execute" if act in {"request", "question", "correction"} else None,
    }


def _infer_state(text: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    norm = normalize_text(text)
    emotion = (analysis or {}).get("emotion") or "unknown"
    intensity = float((analysis or {}).get("emotion_intensity") or 0.5)
    frustration = max(_score_word(norm, ["faux", "rien compris", "cassé", "mauvais", "dur", "bloque", "saoule"]), intensity if "frustr" in normalize_text(emotion) else 0.0)
    curiosity = max(_score_word(norm, ["intéressant", "explique", "comprendre", "pourquoi", "comment"]), intensity if "curios" in normalize_text(emotion) else 0.0)
    urgency = _score_word(norm, ["maintenant", "vite", "tout", "sans rien oublier", "ultra", "complet"])
    clarity_need = _score_word(norm, ["concret", "détail", "détaillé", "explique", "comprendre", "liste"])
    control_need = _score_word(norm, ["contrôle", "fiable", "sans fallback", "strict", "preuve", "sûr"])
    return {
        "energy": _clamp(0.55 + urgency * 0.25),
        "stress": _clamp(0.35 + frustration * 0.45 + control_need * 0.2),
        "motivation": _clamp(0.45 + curiosity * 0.35 + urgency * 0.2),
        "confidence_state": _clamp(0.45 + _score_word(norm, ["je sais", "exactement", "sans problème"]) * 0.35 - frustration * 0.15),
        "clarity": _clamp(0.55 - clarity_need * 0.25 + _score_word(norm, ["oui", "exactement", "c'est ça"]) * 0.2),
        "frustration": _clamp(frustration),
        "curiosity": _clamp(curiosity),
        "urgency": _clamp(urgency),
        "sense_of_control": _clamp(0.45 + control_need * 0.25 - frustration * 0.1),
        "feeling_understood": _clamp(0.55 + _score_word(norm, ["exactement", "tu as compris", "c est ça"]) * 0.35 - _score_word(norm, ["pas compris", "rien compris"]) * 0.45),
        "social_safety": 0.55,
        "dominant_emotion": emotion,
        "emotional_valence": "negative" if frustration > 0.45 else ("positive" if curiosity > 0.45 else "neutral"),
    }


def _episode_type_from_text(text: str, topic: str | None = None) -> str:
    norm = normalize_text(" ".join([topic or "", text]))
    if any(k in norm for k in ["client", "devis", "contrat"]):
        return "client_context"
    if any(k in norm for k in ["relation", "avec qui", "personne", "ami", "copine", "collègue"]):
        return "relationship_context"
    if any(k in norm for k in ["choix", "choisir", "option", "ou", "décider"]):
        return "decision_point"
    if any(k in norm for k in ["projet", "moteur", "système", "installer", "zip", "audio"]):
        return "project_work"
    return "conversation_segment"


def _situation_type_from_act(act_type: str, text: str) -> str:
    norm = normalize_text(text)
    if act_type == "correction":
        return "correction_or_alignment"
    if act_type == "request" and any(k in norm for k in ["concret", "liste", "plan", "étape"]):
        return "technical_validation"
    if act_type == "commitment_or_plan":
        return "planning"
    if any(k in norm for k in ["relation", "client", "personne"]):
        return "social_prediction"
    return act_type


def _extract_options(text: str) -> list[str]:
    parts = re.split(r"\bou\b|/|\bsoit\b", text, flags=re.IGNORECASE)
    cleaned = [p.strip(" .,:;!?\n\t") for p in parts if len(p.strip()) >= 3]
    return cleaned[:8]


def _action_type(text: str) -> str:
    norm = normalize_text(text)
    if any(k in norm for k in ["installer", "lancer", "run", "tester"]):
        return "technical_execution"
    if any(k in norm for k in ["demander", "répondre", "dire", "expliquer"]):
        return "communication"
    if any(k in norm for k in ["choisir", "décider"]):
        return "decision"
    if any(k in norm for k in ["faire", "ferai", "vais"]):
        return "generic_action"
    return "unspecified_action"


def _upsert_canonical_facets(con) -> None:
    now = now_iso()
    for key, (ftype, canonical, aliases) in CANONICAL_FACETS.items():
        upsert(con, "v12_canonical_facets", {
            "facet_key": key,
            "facet_type": ftype,
            "canonical_value": canonical,
            "aliases_json": json_dumps(aliases),
            "description": f"Canonical V12 facet {ftype}:{canonical}",
            "created_at": now,
        }, "facet_key")


def build_v12_for_conversation(conversation_id: str) -> dict[str, int]:
    """Materialize the V4→V12 foundation for one conversation.

    This is intentionally deterministic and evidence-first. LLM calls can be added
    later behind the same tables, but this first pass never invents without keeping
    a truth_status/confidence/evidence trail.
    """
    init_db()
    counts: Counter[str] = Counter()
    with connect() as con:
        _upsert_canonical_facets(con)
        conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if not conv:
            return {"missing_conversation": 1}
        turns = list(con.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,)))
        analyses = {r["turn_id"]: r for r in con.execute("SELECT * FROM utterance_analyses WHERE conversation_id=?", (conversation_id,))}
        spans = {r["turn_id"]: r for r in con.execute("SELECT * FROM source_spans WHERE conversation_id=? AND turn_id IS NOT NULL", (conversation_id,))}
        participants = json_loads(conv["participants_json"], []) or []
        speaker_map = json_loads(conv["speaker_map_json"], {}) or {}
        user_id = _default_user(con, conversation_id)
        now = now_iso()
        all_text = "\n".join([t["text"] for t in turns])
        start_turn = turns[0]["turn_id"] if turns else None
        end_turn = turns[-1]["turn_id"] if turns else None
        episode_id = stable_id("ep", conversation_id, "conversation")
        episode_type = _episode_type_from_text(all_text, conv["topic"])
        upsert(con, "episodes", {
            "episode_id": episode_id,
            "episode_type": episode_type,
            "source_conversation_id": conversation_id,
            "start_turn_id": start_turn,
            "end_turn_id": end_turn,
            "start_time": conv["started_at"],
            "end_time": conv["ended_at"],
            "participants_json": conv["participants_json"],
            "location_text": None,
            "channel": conv["channel"],
            "topic": conv["topic"],
            "situation_summary": conv["title"] or conv["topic"] or "Conversation observée",
            "trigger_summary": "conversation_ingested",
            "user_state_before_json": json_dumps({}),
            "speech_or_action_summary": (all_text[:500] if all_text else None),
            "target_person_id": None,
            "target_reaction_summary": None,
            "user_state_after_json": json_dumps({}),
            "outcome_summary": None,
            "unresolved_tension": None,
            "truth_status": "observed",
            "confidence": 0.8,
            "importance_score": 0.6,
            "lifecycle_status": "active",
            "metadata_json": json_dumps({"v12_version": V12_VERSION, "speaker_map": speaker_map}),
            "created_at": now,
            "updated_at": now,
        }, "episode_id")
        counts["episodes"] += 1
        for t in turns:
            span = spans.get(t["turn_id"])
            ev_id = stable_id("epev", episode_id, t["turn_id"])
            upsert(con, "episode_evidence", {
                "episode_evidence_id": ev_id,
                "episode_id": episode_id,
                "source_span_id": span["span_id"] if span else None,
                "turn_id": t["turn_id"],
                "evidence_role": "turn_in_episode",
                "evidence_text": t["text"],
                "confidence": 1.0,
                "metadata_json": json_dumps({"turn_idx": t["idx"]}),
                "created_at": now,
            }, "episode_evidence_id")
            counts["episode_evidence"] += 1

        # Per-topic thread episodes when discourse already created them.
        for th in con.execute("SELECT * FROM conversation_topic_threads WHERE conversation_id=?", (conversation_id,)):
            th_ep_id = stable_id("ep", conversation_id, "thread", th["thread_key"])
            upsert(con, "episodes", {
                "episode_id": th_ep_id,
                "episode_type": "topic_thread",
                "source_conversation_id": conversation_id,
                "start_turn_id": None,
                "end_turn_id": None,
                "start_time": conv["started_at"],
                "end_time": conv["ended_at"],
                "participants_json": th["participants_json"],
                "location_text": None,
                "channel": conv["channel"],
                "topic": th["label"],
                "situation_summary": th["summary"],
                "trigger_summary": "topic_thread_detected",
                "user_state_before_json": json_dumps({}),
                "speech_or_action_summary": th["summary"],
                "target_person_id": None,
                "target_reaction_summary": None,
                "user_state_after_json": json_dumps({}),
                "outcome_summary": None,
                "unresolved_tension": None,
                "truth_status": "inferred",
                "confidence": th["importance"] or 0.65,
                "importance_score": th["importance"] or 0.65,
                "lifecycle_status": "active",
                "metadata_json": json_dumps({"thread_key": th["thread_key"], "v12_version": V12_VERSION}),
                "created_at": now,
                "updated_at": now,
            }, "episode_id")
            upsert(con, "episode_links", {
                "episode_link_id": stable_id("eplink", episode_id, "contains_thread", th_ep_id),
                "from_episode_id": episode_id,
                "relation_type": "contains_thread",
                "to_episode_id": th_ep_id,
                "confidence": 0.8,
                "evidence_text": th["summary"],
                "metadata_json": json_dumps({"thread_key": th["thread_key"]}),
                "created_at": now,
            }, "episode_link_id")
            counts["episodes"] += 1
            counts["episode_links"] += 1

        situation_id = stable_id("sit", episode_id)
        first_act = _classify_speech_act(turns[0]["text"] if turns else all_text)
        upsert(con, "situation_episodes", {
            "situation_id": situation_id,
            "episode_id": episode_id,
            "situation_type": _situation_type_from_act(first_act["act_type"], all_text),
            "life_domain": "project" if "projet" in normalize_text(all_text + " " + (conv["topic"] or "")) else None,
            "participants_json": conv["participants_json"],
            "main_person_id": user_id,
            "secondary_people_json": json_dumps([p for p in participants if p != user_id]),
            "place_explicit": None,
            "place_inferred": None,
            "channel": conv["channel"],
            "social_context": "conversation",
            "power_balance": None,
            "stakes": conv["topic"],
            "constraints_json": json_dumps([]),
            "trigger_event_id": None,
            "related_project": conv["topic"],
            "related_relationship_id": None,
            "confidence": 0.6,
            "metadata_json": json_dumps({"v12_version": V12_VERSION}),
            "created_at": now,
            "updated_at": now,
        }, "situation_id")
        counts["situation_episodes"] += 1

        prev_state_id = None
        previous_turn = None
        for t in turns:
            person_id = t["person_id"] or t["speaker_label"] or user_id
            act = _classify_speech_act(t["text"])
            speech_act_id = stable_id("speech", t["turn_id"])
            upsert(con, "speech_acts", {
                "speech_act_id": speech_act_id,
                "turn_id": t["turn_id"],
                "episode_id": episode_id,
                "speaker_person_id": person_id,
                "target_person_id": None,
                "act_type": act["act_type"],
                "directness": act["directness"],
                "politeness": act["politeness"],
                "pressure_level": act["pressure_level"],
                "certainty_level": act["certainty_level"],
                "emotional_charge": act["emotional_charge"],
                "implicit_request": act["implicit_request"],
                "evidence_text": t["text"],
                "confidence": 0.65,
                "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                "created_at": now,
            }, "speech_act_id")
            counts["speech_acts"] += 1

            ana = analyses.get(t["turn_id"])
            ana_dict = dict(ana) if ana else {}
            state = _infer_state(t["text"], ana_dict)
            state_id = stable_id("state", t["turn_id"], state.get("dominant_emotion"))
            upsert(con, "internal_state_snapshots", {
                "state_id": state_id,
                "person_id": person_id,
                "episode_id": episode_id,
                "turn_id": t["turn_id"],
                "time_start": None,
                "time_end": None,
                "energy": state["energy"],
                "stress": state["stress"],
                "motivation": state["motivation"],
                "confidence_state": state["confidence_state"],
                "clarity": state["clarity"],
                "frustration": state["frustration"],
                "curiosity": state["curiosity"],
                "urgency": state["urgency"],
                "sense_of_control": state["sense_of_control"],
                "feeling_understood": state["feeling_understood"],
                "social_safety": state["social_safety"],
                "emotional_valence": state["emotional_valence"],
                "dominant_emotion": state["dominant_emotion"],
                "secondary_emotions_json": json_dumps([]),
                "evidence_text": t["text"],
                "source_type": "text_context",
                "truth_status": "inferred",
                "confidence": ana_dict.get("confidence") or 0.55,
                "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                "created_at": now,
                "updated_at": now,
            }, "state_id")
            counts["internal_state_snapshots"] += 1
            if prev_state_id:
                upsert(con, "state_transitions", {
                    "transition_id": stable_id("sttr", prev_state_id, state_id),
                    "person_id": person_id,
                    "from_state_id": prev_state_id,
                    "to_state_id": state_id,
                    "transition_type": "sequential",
                    "change_summary": f"Transition vers {state['dominant_emotion']}",
                    "trigger_summary": act["act_type"],
                    "confidence": 0.55,
                    "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                    "created_at": now,
                }, "transition_id")
                counts["state_transitions"] += 1
            prev_state_id = state_id

            if ana:
                for thought_type, value in [
                    ("deep_intent", ana["deep_intent"]),
                    ("hidden_expectation", ana["hidden_expectation"]),
                    ("why_now", ana["why_now"]),
                ]:
                    if value:
                        upsert(con, "thought_hypotheses", {
                            "thought_id": stable_id("thought", t["turn_id"], thought_type, value),
                            "person_id": person_id,
                            "episode_id": episode_id,
                            "turn_id": t["turn_id"],
                            "thought_type": thought_type,
                            "content": value,
                            "consciousness_level": "inferred",
                            "evidence_text": t["text"],
                            "trigger_summary": ana["trigger_summary"],
                            "related_need": None,
                            "related_fear": None,
                            "related_goal": conv["topic"],
                            "truth_status": "inferred",
                            "confidence": ana["confidence"] or 0.55,
                            "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                            "created_at": now,
                            "updated_at": now,
                        }, "thought_id")
                        counts["thought_hypotheses"] += 1

            norm = normalize_text(t["text"])
            if any(k in norm for k in ["je vais", "je veux", "il faut", "il faudra", "on va", "je ferai", "je dois"]):
                upsert(con, "action_intentions", {
                    "intention_id": stable_id("intent", t["turn_id"], t["text"][:120]),
                    "person_id": person_id,
                    "episode_id": episode_id,
                    "turn_id": t["turn_id"],
                    "intention_text": t["text"],
                    "action_type": _action_type(t["text"]),
                    "target": conv["topic"],
                    "deadline": None,
                    "strength": 0.75 if any(k in norm for k in ["il faut", "je veux", "je dois"] ) else 0.6,
                    "explicitness": "explicit",
                    "obstacles_json": json_dumps([]),
                    "required_conditions_json": json_dumps([]),
                    "status": "open",
                    "evidence_text": t["text"],
                    "confidence": 0.7,
                    "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                    "created_at": now,
                    "updated_at": now,
                }, "intention_id")
                counts["action_intentions"] += 1
            if any(k in norm for k in ["choix", "choisir", "option", " ou ", "soit", "décide", "décider"]):
                options = _extract_options(t["text"])
                upsert(con, "choice_episodes", {
                    "choice_id": stable_id("choice", t["turn_id"], t["text"][:120]),
                    "episode_id": episode_id,
                    "person_id": person_id,
                    "turn_id": t["turn_id"],
                    "choice_context": conv["topic"] or "choix détecté",
                    "options_json": json_dumps(options),
                    "criteria_json": json_dumps([]),
                    "preferred_option_before": None,
                    "chosen_option": None,
                    "rejected_options_json": json_dumps([]),
                    "decision_time": conv["started_at"],
                    "confidence_before": None,
                    "confidence_after": None,
                    "reason_given": None,
                    "real_reason_hypothesis": None,
                    "outcome_id": None,
                    "satisfaction_after": None,
                    "regret_after": None,
                    "evidence_text": t["text"],
                    "truth_status": "inferred",
                    "confidence": 0.55,
                    "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                    "created_at": now,
                    "updated_at": now,
                }, "choice_id")
                counts["choice_episodes"] += 1
            if any(k in norm for k in ["mais", "pourtant", "sauf", "alors que", "même si", "cependant"]):
                upsert(con, "contradiction_events", {
                    "contradiction_id": stable_id("contra", t["turn_id"], t["text"][:80]),
                    "person_id": person_id,
                    "episode_id": episode_id,
                    "declared_table": "turns",
                    "declared_id": t["turn_id"],
                    "observed_table": "turns",
                    "observed_id": t["turn_id"],
                    "contradiction_type": "linguistic_tension_marker",
                    "severity": 0.45,
                    "possible_explanation": "Connecteur d'opposition ou tension sémantique à vérifier.",
                    "resolved": 0,
                    "evidence_for": t["text"],
                    "evidence_against": None,
                    "confidence": 0.45,
                    "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                    "created_at": now,
                    "updated_at": now,
                }, "contradiction_id")
                counts["contradiction_events"] += 1
            if ana and ana["trigger_summary"] and ana["emotion"]:
                upsert(con, "causal_edges", {
                    "causal_edge_id": stable_id("cause", t["turn_id"], ana["trigger_summary"], ana["emotion"]),
                    "from_table": "turns",
                    "from_id": t["turn_id"],
                    "to_table": "internal_state_snapshots",
                    "to_id": state_id,
                    "causal_type": "triggered_or_explained_by",
                    "strength": min(0.8, float(ana["confidence"] or 0.55)),
                    "lag_time_text": "same_turn",
                    "evidence_text": ana["trigger_summary"],
                    "counter_evidence_text": None,
                    "truth_status": "hypothesis",
                    "confidence": ana["confidence"] or 0.55,
                    "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                    "created_at": now,
                    "updated_at": now,
                }, "causal_edge_id")
                counts["causal_edges"] += 1

            # Behavior signals.
            for sig_type, sig_value, strength in [
                ("speech_act", act["act_type"], 0.65),
                ("emotion", state["dominant_emotion"], float(ana_dict.get("confidence") or 0.55)),
            ]:
                upsert(con, "behavior_signals", {
                    "signal_id": stable_id("bsig", t["turn_id"], sig_type, sig_value),
                    "person_id": person_id,
                    "episode_id": episode_id,
                    "turn_id": t["turn_id"],
                    "signal_type": sig_type,
                    "signal_value": sig_value or "unknown",
                    "strength": strength,
                    "evidence_text": t["text"],
                    "status": "isolated_signal",
                    "confidence": strength,
                    "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                    "created_at": now,
                    "updated_at": now,
                }, "signal_id")
                counts["behavior_signals"] += 1

            # Prediction case: current turn -> next observed turn.
            if previous_turn is not None:
                prev_person = previous_turn["person_id"] or previous_turn["speaker_label"] or user_id
                upsert(con, "prediction_cases", {
                    "case_id": stable_id("pcase", previous_turn["turn_id"], "next_phrase", t["turn_id"]),
                    "case_type": "next_phrase",
                    "episode_id": episode_id,
                    "person_id": prev_person,
                    "context_summary": previous_turn["text"],
                    "situation_vector_json": json_dumps({"topic": conv["topic"], "channel": conv["channel"], "act": _classify_speech_act(previous_turn["text"])["act_type"]}),
                    "state_vector_json": json_dumps({}),
                    "action_taken": None,
                    "speech_next": t["text"],
                    "emotion_next": analyses[t["turn_id"]]["emotion"] if t["turn_id"] in analyses else None,
                    "thought_next_hypothesis": analyses[t["turn_id"]]["deep_intent"] if t["turn_id"] in analyses else None,
                    "outcome": None,
                    "usable_for_prediction": 1,
                    "quality_score": 0.65,
                    "evidence_json": json_dumps({"current_turn_id": previous_turn["turn_id"], "next_turn_id": t["turn_id"]}),
                    "created_at": now,
                    "updated_at": now,
                }, "case_id")
                counts["prediction_cases"] += 1
            previous_turn = t

        # Language patterns from personal expressions and frequent n-grams.
        by_person: dict[str, list[str]] = defaultdict(list)
        for t in turns:
            by_person[t["person_id"] or t["speaker_label"] or user_id].append(t["text"])
        for person_id, texts in by_person.items():
            phrases = Counter()
            for text in texts:
                tokens = tokenize(text)
                for n in (1, 2, 3):
                    for i in range(0, max(0, len(tokens) - n + 1)):
                        gram = " ".join(tokens[i:i+n])
                        if len(gram) >= 3:
                            phrases[gram] += 1
                for expr in re.findall(r"\b(concr[eè]tement|en gros|tu vois|c'?est ça|r[eé]ponse courte|r[eé]ponse longue|sois dur|sans rien oublier)\b", normalize_text(text)):
                    phrases[expr] += 3
            for expr, freq in phrases.most_common(40):
                if freq < 1:
                    continue
                upsert(con, "personal_language_patterns", {
                    "language_pattern_id": stable_id("lang", person_id, expr, "global"),
                    "person_id": person_id,
                    "expression": expr,
                    "normalized_expression": normalize_text(expr),
                    "context_type": "global",
                    "preceding_context": None,
                    "following_context": None,
                    "emotion_context": None,
                    "speech_act_context": None,
                    "frequency": int(freq),
                    "last_seen": conv["started_at"] or now,
                    "examples_json": json_dumps(texts[:3]),
                    "probability_boost": _clamp(math.log(freq + 1) / 5),
                    "confidence": _clamp(0.35 + min(freq, 10) / 20),
                    "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                    "created_at": now,
                    "updated_at": now,
                }, "language_pattern_id")
                counts["personal_language_patterns"] += 1

        # Relationship models for participants.
        people = list(dict.fromkeys([p for p in participants if p]))
        for other in people:
            if other == user_id:
                continue
            rel_id = stable_id("rel", sorted([user_id, other]))
            upsert(con, "relationship_models", {
                "relationship_id": rel_id,
                "person_a": user_id,
                "person_b": other,
                "relationship_type": "unknown",
                "trust_level": 0.5,
                "tension_level": 0.5,
                "attachment_level": 0.5,
                "dependency_level": 0.5,
                "power_balance": None,
                "conflict_frequency": 0.0,
                "repair_frequency": 0.0,
                "communication_style": "observed_in_conversation",
                "common_triggers_json": json_dumps([]),
                "common_loops_json": json_dumps([]),
                "current_status": "active",
                "evidence_count": sum(1 for t in turns if (t["person_id"] or t["speaker_label"]) == other),
                "confidence": 0.45,
                "metadata_json": json_dumps({"v12_version": V12_VERSION, "conversation_id": conversation_id}),
                "created_at": now,
                "updated_at": now,
            }, "relationship_id")
            counts["relationship_models"] += 1

        _build_self_dimensions(con, user_id, conversation_id)
        counts["self_model_dimensions"] = con.execute("SELECT COUNT(*) c FROM self_model_dimensions WHERE person_id=?", (user_id,)).fetchone()["c"]
        _promote_patterns(con, conversation_id=conversation_id)
        counts["candidate_patterns"] = con.execute("SELECT COUNT(*) c FROM candidate_patterns").fetchone()["c"]
        counts["confirmed_patterns"] = con.execute("SELECT COUNT(*) c FROM confirmed_patterns").fetchone()["c"]

        _create_interaction_episodes(con, episode_id, user_id, participants, turns, conversation_id)
        _create_emotion_evidence(con, episode_id)
        _create_phrase_templates(con, episode_id, user_id)
        _create_outcome_candidates(con, episode_id)
        _create_expanded_prediction_cases(con, episode_id)
        _create_loop_patterns(con, user_id, conversation_id)
        _create_future_scenarios_warnings_and_recommendations(con, episode_id, user_id)
        _create_quality_findings(con, conversation_id, episode_id)
        _record_engine_run(con, "v12_build_complete", conversation_id, "conversations", "ok", dict(counts))
        for _table in [
            "interaction_episodes", "emotion_evidence", "phrase_templates", "action_outcomes",
            "loop_patterns", "future_scenarios", "trajectory_warnings", "escape_conditions",
            "recommended_actions", "similar_case_scores", "v12_quality_findings", "v12_quarantine"
        ]:
            counts[_table] = con.execute(f"SELECT COUNT(*) c FROM {_table}").fetchone()["c"]
        con.commit()
    return dict(counts)




def _criteria_from_text(text: str) -> list[str]:
    norm = normalize_text(text)
    criteria: list[str] = []
    mapping = {
        "fiabilité": ["fiable", "marche", "solide", "cassé", "cassant"],
        "clarté": ["clair", "concret", "détail", "comprendre", "plan"],
        "contrôle": ["contrôle", "maîtrise", "preuve", "vérifier", "sans fallback"],
        "rapidité": ["vite", "maintenant", "direct", "tout de suite"],
        "complétude": ["complet", "tout", "sans rien oublier", "jusqu'au bout"],
    }
    for key, words in mapping.items():
        if any(w in norm for w in words):
            criteria.append(key)
    return criteria


def _create_interaction_episodes(con, episode_id: str, user_id: str | None, participants: list[str], turns: list[Any], conversation_id: str) -> None:
    now = now_iso()
    if not user_id:
        user_id = "me"
    other_people = [p for p in dict.fromkeys(participants) if p and p != user_id]
    # Add people actually found in turns even if participants_json is poor.
    for t in turns:
        p = t["person_id"] or t["speaker_label"]
        if p and p != user_id and p not in other_people:
            other_people.append(p)
    for other in other_people:
        user_acts = []
        other_acts = []
        for t in turns:
            p = t["person_id"] or t["speaker_label"]
            act = _classify_speech_act(t["text"])["act_type"]
            if p == user_id:
                user_acts.append(act)
            elif p == other:
                other_acts.append(act)
        upsert(con, "interaction_episodes", {
            "interaction_id": stable_id("inter", episode_id, user_id, other),
            "episode_id": episode_id,
            "user_person_id": user_id,
            "other_person_id": other,
            "relationship_type": "unknown",
            "trust_level": 0.5,
            "tension_level": 0.65 if "correction" in user_acts else 0.5,
            "dependency_level": 0.5,
            "message_direction": "multi_turn" if user_acts and other_acts else "observed_context",
            "user_speech_act": Counter(user_acts).most_common(1)[0][0] if user_acts else None,
            "other_reaction": Counter(other_acts).most_common(1)[0][0] if other_acts else None,
            "user_followup": user_acts[-1] if user_acts else None,
            "communication_result": "open_loop" if not other_acts else "exchange_observed",
            "confidence": 0.55,
            "metadata_json": json_dumps({"v12_version": V12_VERSION, "conversation_id": conversation_id}),
            "created_at": now,
            "updated_at": now,
        }, "interaction_id")


def _create_emotion_evidence(con, episode_id: str) -> None:
    now = now_iso()
    rows = list(con.execute("SELECT * FROM internal_state_snapshots WHERE episode_id=?", (episode_id,)))
    for r in rows:
        text = r["evidence_text"] or ""
        norm = normalize_text(text)
        signals = []
        if any(k in norm for k in ["pas compris", "rien compris", "cassé", "mauvais", "flou", "dur"]):
            signals.append(("frustration_or_control_need", 0.7))
        if any(k in norm for k in ["intéressant", "explique", "comprendre", "pourquoi", "comment"]):
            signals.append(("curiosity_or_clarity_need", 0.65))
        if any(k in norm for k in ["parfait", "exactement", "oui", "c'est ça", "c est ça"]):
            signals.append(("validation_or_alignment", 0.6))
        if not signals:
            signals.append((r["dominant_emotion"] or "unknown", 0.45))
        for label, strength in signals:
            upsert(con, "emotion_evidence", {
                "emotion_evidence_id": stable_id("emev", r["state_id"], label),
                "state_id": r["state_id"],
                "person_id": r["person_id"],
                "episode_id": episode_id,
                "turn_id": r["turn_id"],
                "source_type": "words_and_context",
                "emotion_label": label,
                "signal_text": text,
                "signal_strength": strength,
                "missing_evidence_json": json_dumps(["voice_prosody_not_measured", "physiological_state_not_measured"]),
                "confidence": min(0.8, strength),
                "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                "created_at": now,
                "updated_at": now,
            }, "emotion_evidence_id")


def _create_phrase_templates(con, episode_id: str, user_id: str | None) -> None:
    now = now_iso()
    rows = list(con.execute("SELECT t.*, s.act_type FROM turns t LEFT JOIN speech_acts s ON s.turn_id=t.turn_id WHERE s.episode_id=? ORDER BY t.idx", (episode_id,)))
    counts: Counter[tuple[str, str, str]] = Counter()
    examples: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for r in rows:
        person = r["person_id"] or r["speaker_label"] or user_id or "unknown"
        text = r["text"].strip()
        act = r["act_type"] or _classify_speech_act(text)["act_type"]
        norm = normalize_text(text)
        template = None
        if norm.startswith("je veux"):
            template = "je veux <objectif>"
        elif norm.startswith("est ce") or norm.startswith("est-ce"):
            template = "est-ce que <validation/question>"
        elif "concrètement" in norm:
            template = "concrètement <demande>"
        elif "en gros" in norm:
            template = "en gros <synthèse/validation>"
        elif act == "correction":
            template = "non / correction <recadrage>"
        elif act == "request":
            template = "demande directe <action attendue>"
        if template:
            key = (person, template, act)
            counts[key] += 1
            if len(examples[key]) < 5:
                examples[key].append(text)
    for (person, template, act), freq in counts.items():
        upsert(con, "phrase_templates", {
            "template_id": stable_id("tpl", person, template, act),
            "person_id": person,
            "template_text": template,
            "template_type": act,
            "context_type": "episode_observed",
            "frequency": freq,
            "confidence": _clamp(0.4 + freq / 10),
            "examples_json": json_dumps(examples[(person, template, act)]),
            "metadata_json": json_dumps({"v12_version": V12_VERSION, "episode_id": episode_id}),
            "created_at": now,
            "updated_at": now,
        }, "template_id")


def _create_outcome_candidates(con, episode_id: str) -> None:
    now = now_iso()
    turns = list(con.execute("SELECT * FROM turns WHERE conversation_id=(SELECT source_conversation_id FROM episodes WHERE episode_id=?) ORDER BY idx", (episode_id,)))
    intentions = list(con.execute("SELECT * FROM action_intentions WHERE episode_id=?", (episode_id,)))
    if not intentions:
        return
    done_markers = ["c'est fait", "c est fait", "j'ai fait", "j ai fait", "j'ai testé", "j ai testé", "ça marche", "ca marche", "ça a marché", "terminé", "réussi"]
    for intent in intentions:
        later = [t for t in turns if t["idx"] > (con.execute("SELECT idx FROM turns WHERE turn_id=?", (intent["turn_id"],)).fetchone() or {"idx": -1})["idx"]]
        observed = None
        for t in later:
            if any(m in normalize_text(t["text"]) for m in done_markers):
                observed = t
                break
        if observed:
            upsert(con, "action_outcomes", {
                "outcome_id": stable_id("out", intent["intention_id"], observed["turn_id"]),
                "intention_id": intent["intention_id"],
                "episode_id": episode_id,
                "person_id": intent["person_id"],
                "action_taken": observed["text"],
                "result": "observed_completion_or_success_signal",
                "success_level": 0.75,
                "delay_text": "later_in_same_conversation",
                "obstacle_encountered": None,
                "emotion_after": None,
                "lesson": "Intention suivie d'un signal d'action/résultat dans la même conversation.",
                "evidence_text": observed["text"],
                "truth_status": "observed_or_inferred",
                "confidence": 0.65,
                "metadata_json": json_dumps({"v12_version": V12_VERSION, "source_turn_id": observed["turn_id"]}),
                "created_at": now,
                "updated_at": now,
            }, "outcome_id")
        else:
            upsert(con, "recommended_actions", {
                "recommendation_id": stable_id("rec", intent["intention_id"], "track_outcome"),
                "person_id": intent["person_id"],
                "prediction_id": None,
                "episode_id": episode_id,
                "recommendation_type": "track_outcome",
                "title": "Vérifier si l'intention devient une action réelle",
                "detail": f"Intention ouverte à suivre: {intent['intention_text']}",
                "expected_effect": "Permettre au moteur de prédiction d'apprendre intention → action → résultat.",
                "confidence": 0.7,
                "status": "open",
                "evidence_json": json_dumps([{ "intention_id": intent["intention_id"], "evidence": intent["evidence_text"] }]),
                "created_at": now,
                "updated_at": now,
            }, "recommendation_id")


def _create_expanded_prediction_cases(con, episode_id: str) -> None:
    now = now_iso()
    turns = list(con.execute("SELECT * FROM turns WHERE conversation_id=(SELECT source_conversation_id FROM episodes WHERE episode_id=?) ORDER BY idx", (episode_id,)))
    if len(turns) < 2:
        return
    states = {r["turn_id"]: r for r in con.execute("SELECT * FROM internal_state_snapshots WHERE episode_id=?", (episode_id,))}
    thoughts = defaultdict(list)
    for r in con.execute("SELECT * FROM thought_hypotheses WHERE episode_id=?", (episode_id,)):
        thoughts[r["turn_id"]].append(r)
    for a, b in zip(turns, turns[1:]):
        person = a["person_id"] or a["speaker_label"] or "unknown"
        next_tokens = tokenize(b["text"])
        st_b = states.get(b["turn_id"])
        th_b = thoughts.get(b["turn_id"], [])
        base = {
            "episode_id": episode_id,
            "person_id": person,
            "context_summary": a["text"],
            "situation_vector_json": json_dumps({"act": _classify_speech_act(a["text"])["act_type"], "next_act": _classify_speech_act(b["text"])["act_type"]}),
            "state_vector_json": json_dumps(_infer_state(a["text"])),
            "action_taken": None,
            "outcome": None,
            "usable_for_prediction": 1,
            "quality_score": 0.66,
            "evidence_json": json_dumps({"current_turn_id": a["turn_id"], "next_turn_id": b["turn_id"]}),
            "created_at": now,
            "updated_at": now,
        }
        for case_type, speech_next, emotion_next, thought_next in [
            ("next_word", next_tokens[0] if next_tokens else None, None, None),
            ("next_phrase", b["text"], st_b["dominant_emotion"] if st_b else None, th_b[0]["content"] if th_b else None),
            ("next_message", b["text"], st_b["dominant_emotion"] if st_b else None, th_b[0]["content"] if th_b else None),
            ("next_emotion", st_b["dominant_emotion"] if st_b else None, st_b["dominant_emotion"] if st_b else None, None),
            ("next_thought", th_b[0]["content"] if th_b else None, st_b["dominant_emotion"] if st_b else None, th_b[0]["content"] if th_b else None),
            ("next_reaction", b["text"], st_b["dominant_emotion"] if st_b else None, None),
        ]:
            vals = dict(base)
            vals.update({
                "case_id": stable_id("pcase", a["turn_id"], case_type, b["turn_id"]),
                "case_type": case_type,
                "speech_next": speech_next,
                "emotion_next": emotion_next,
                "thought_next_hypothesis": thought_next,
            })
            upsert(con, "prediction_cases", vals, "case_id")
    for intent in con.execute("SELECT * FROM action_intentions WHERE episode_id=?", (episode_id,)):
        vals = {
            "case_id": stable_id("pcase", intent["intention_id"], "next_action"),
            "case_type": "next_action",
            "episode_id": episode_id,
            "person_id": intent["person_id"],
            "context_summary": intent["evidence_text"] or intent["intention_text"],
            "situation_vector_json": json_dumps({"action_type": intent["action_type"], "strength": intent["strength"]}),
            "state_vector_json": json_dumps({}),
            "action_taken": intent["intention_text"],
            "speech_next": intent["intention_text"],
            "emotion_next": None,
            "thought_next_hypothesis": None,
            "outcome": intent["status"],
            "usable_for_prediction": 1,
            "quality_score": 0.62,
            "evidence_json": json_dumps({"intention_id": intent["intention_id"]}),
            "created_at": now,
            "updated_at": now,
        }
        upsert(con, "prediction_cases", vals, "case_id")
    for choice in con.execute("SELECT * FROM choice_episodes WHERE episode_id=?", (episode_id,)):
        vals = {
            "case_id": stable_id("pcase", choice["choice_id"], "next_choice"),
            "case_type": "next_choice",
            "episode_id": episode_id,
            "person_id": choice["person_id"],
            "context_summary": choice["evidence_text"] or choice["choice_context"],
            "situation_vector_json": json_dumps({"options": json_loads(choice["options_json"], [])}),
            "state_vector_json": json_dumps({"criteria": json_loads(choice["criteria_json"], [])}),
            "action_taken": choice["chosen_option"],
            "speech_next": choice["chosen_option"] or choice["choice_context"],
            "emotion_next": None,
            "thought_next_hypothesis": choice["real_reason_hypothesis"],
            "outcome": None,
            "usable_for_prediction": 1,
            "quality_score": 0.58,
            "evidence_json": json_dumps({"choice_id": choice["choice_id"]}),
            "created_at": now,
            "updated_at": now,
        }
        upsert(con, "prediction_cases", vals, "case_id")


def _create_loop_patterns(con, user_id: str | None, conversation_id: str) -> None:
    now = now_iso()
    if not user_id:
        user_id = "me"
    dims = {r["dimension_key"]: float(r["score"] or 0) for r in con.execute("SELECT * FROM self_model_dimensions WHERE person_id=?", (user_id,))}
    # A concrete loop for the project-style behavior explicitly required by the V12 doctrine.
    confirmed_count = con.execute("SELECT COUNT(*) c FROM confirmed_patterns WHERE person_id=?", (user_id,)).fetchone()["c"]
    candidate_count = con.execute("SELECT COUNT(*) c FROM candidate_patterns WHERE person_id=?", (user_id,)).fetchone()["c"]
    if dims.get("need_for_clarity", 0) > 0.45 or dims.get("need_for_control", 0) > 0.45 or confirmed_count or candidate_count:
        loop_id = stable_id("loop", user_id, "clarity_control_action")
        upsert(con, "loop_patterns", {
            "loop_id": loop_id,
            "person_id": user_id,
            "loop_type": "clarity_control_action_loop",
            "trigger_summary": "système complexe, enjeu élevé ou réponse floue",
            "phase_1": "intérêt fort / vision ambitieuse",
            "phase_2": "demande de détails, preuves et cadrage concret",
            "phase_3": "détection des trous, corrections, exigence de complétude",
            "phase_4": "passage à l'action si test minimal clair, sinon nouvelle boucle de précision",
            "usual_outcome": "progression si procédure concrète; friction si réponse abstraite",
            "escape_conditions_json": json_dumps(["commande testable", "preuve exacte", "résultat attendu", "scope réduit"]),
            "evidence_count": int(sum(r["evidence_count"] or 0 for r in con.execute("SELECT evidence_count FROM self_model_dimensions WHERE person_id=?", (user_id,)))) or 1,
            "confidence": _clamp(0.45 + max(dims.values() or [0]) * 0.4),
            "metadata_json": json_dumps({"v12_version": V12_VERSION, "conversation_id": conversation_id}),
            "created_at": now,
            "updated_at": now,
        }, "loop_id")
        for cond, effect in [
            ("réduire la prochaine étape à un test observable", "diminue le risque de boucle de précision"),
            ("lier chaque conclusion à une preuve exacte", "augmente la confiance sans surinterprétation"),
            ("vérifier les outcomes après action", "transforme l'intuition en apprentissage prédictif"),
        ]:
            upsert(con, "escape_conditions", {
                "escape_id": stable_id("esc", loop_id, cond),
                "person_id": user_id,
                "loop_id": loop_id,
                "prediction_id": None,
                "condition_text": cond,
                "expected_effect": effect,
                "confidence": 0.65,
                "evidence_json": json_dumps([{ "conversation_id": conversation_id }]),
                "status": "candidate",
                "created_at": now,
                "updated_at": now,
            }, "escape_id")


def _create_future_scenarios_warnings_and_recommendations(con, episode_id: str, user_id: str | None) -> None:
    now = now_iso()
    user_id = user_id or "me"
    episode = con.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    if not episode:
        return
    states = list(con.execute("SELECT * FROM internal_state_snapshots WHERE episode_id=? AND person_id=?", (episode_id, user_id)))
    avg_stress = sum(float(s["stress"] or 0.0) for s in states) / max(1, len(states))
    avg_clarity = sum(float(s["clarity"] or 0.0) for s in states) / max(1, len(states))
    scenarios = [
        ("concrete_path", "si la prochaine étape est concrète et vérifiable", "progression rapide ou validation du plan", 0.55, 0.25, 0.75),
        ("abstract_path", "si la suite reste abstraite ou trop large", "nouvelle demande de précision, correction ou friction", 0.35, 0.65, 0.35),
        ("no_action_path", "si aucune action n'est définie", "report ou boucle de réflexion", 0.25, 0.55, 0.25),
    ]
    for stype, cond, future, prob, risk, opp in scenarios:
        upsert(con, "future_scenarios", {
            "scenario_id": stable_id("fsc", episode_id, stype),
            "person_id": user_id,
            "episode_id": episode_id,
            "prediction_id": None,
            "scenario_type": stype,
            "horizon": "short_term",
            "if_condition": cond,
            "expected_future": future,
            "probability": prob,
            "risk_level": risk,
            "opportunity_level": opp,
            "evidence_json": json_dumps([{ "episode_id": episode_id, "topic": episode["topic"] }]),
            "counter_evidence_json": json_dumps([]),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "scenario_id")
    if avg_stress > 0.45 or avg_clarity < 0.45:
        warning_type = "clarity_or_overcomplexity_risk"
        upsert(con, "trajectory_warnings", {
            "warning_id": stable_id("warn", episode_id, warning_type),
            "person_id": user_id,
            "episode_id": episode_id,
            "prediction_id": None,
            "warning_type": warning_type,
            "title": "Risque de boucle de précision / surcomplexité",
            "detail": "Le contexte montre un besoin élevé de clarté ou de contrôle; sans outcome observable, le moteur peut accumuler des analyses au lieu d'apprendre.",
            "severity": _clamp(0.45 + avg_stress * 0.4 + (0.5 - avg_clarity) * 0.2),
            "probability": _clamp(0.45 + avg_stress * 0.3),
            "evidence_json": json_dumps([{ "episode_id": episode_id, "avg_stress": avg_stress, "avg_clarity": avg_clarity }]),
            "counter_evidence_json": json_dumps([]),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "warning_id")
    upsert(con, "recommended_actions", {
        "recommendation_id": stable_id("rec", episode_id, "v12_next_step"),
        "person_id": user_id,
        "prediction_id": None,
        "episode_id": episode_id,
        "recommendation_type": "build_outcome_loop",
        "title": "Créer une boucle résultat pour cet épisode",
        "detail": "Associer toute intention ou prédiction à un résultat observé futur afin que le moteur apprenne vraiment.",
        "expected_effect": "Augmente la qualité des prédictions next_action/next_outcome/next_choice.",
        "confidence": 0.75,
        "status": "open",
        "evidence_json": json_dumps([{ "episode_id": episode_id }]),
        "created_at": now,
        "updated_at": now,
    }, "recommendation_id")


def _create_quality_findings(con, conversation_id: str, episode_id: str) -> None:
    now = now_iso()
    conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    turns = list(con.execute("SELECT * FROM turns WHERE conversation_id=?", (conversation_id,)))
    if not conv:
        return
    checks: list[tuple[str, str, str, str, str]] = []
    if not json_loads(conv["speaker_map_json"], {}) and any(t["speaker_label"] for t in turns):
        checks.append(("conversations", conversation_id, "speaker_identity_uncertain", "medium", "Speaker map absent ou incomplet"))
    if not list(con.execute("SELECT 1 FROM source_spans WHERE conversation_id=? LIMIT 1", (conversation_id,))):
        checks.append(("episodes", episode_id, "missing_precise_source_spans", "low", "Pas de source_spans précis pour certains turns"))
    one_proof_patterns = list(con.execute("SELECT * FROM candidate_patterns WHERE evidence_count < 2"))
    for p in one_proof_patterns:
        checks.append(("candidate_patterns", p["candidate_pattern_id"], "pattern_too_weak", "medium", "Pattern candidat avec moins de 2 preuves"))
    for table, tid, ftype, severity, title in checks:
        fid = stable_id("qfind", table, tid, ftype)
        upsert(con, "v12_quality_findings", {
            "finding_id": fid,
            "target_table": table,
            "target_id": tid,
            "finding_type": ftype,
            "severity": severity,
            "title": title,
            "detail": "Garde-fou V12: ne pas consolider cette information sans preuve supplémentaire.",
            "status": "open",
            "evidence_json": json_dumps([{ "conversation_id": conversation_id, "episode_id": episode_id }]),
            "created_at": now,
            "updated_at": now,
        }, "finding_id")
        if severity in {"medium", "high"}:
            upsert(con, "v12_quarantine", {
                "quarantine_id": stable_id("quar", table, tid, ftype),
                "target_table": table,
                "target_id": tid,
                "reason": ftype,
                "confidence": 0.65,
                "status": "pending_review",
                "evidence_json": json_dumps([{ "finding_id": fid }]),
                "created_at": now,
                "updated_at": now,
            }, "quarantine_id")


def _record_engine_run(con, engine_name: str, target_id: str | None, target_table: str | None, status: str, counts: dict[str, Any], warnings: list[str] | None = None) -> None:
    now = now_iso()
    upsert(con, "v12_engine_runs", {
        "run_id": stable_id("v12run", engine_name, target_table or "", target_id or "", now[:19]),
        "engine_name": engine_name,
        "target_id": target_id,
        "target_table": target_table,
        "status": status,
        "counts_json": json_dumps(counts),
        "warnings_json": json_dumps(warnings or []),
        "started_at": now,
        "finished_at": now,
        "metadata_json": json_dumps({"v12_version": V12_VERSION}),
    }, "run_id")

def build_v12_all() -> dict[str, Any]:
    init_db()
    with connect() as con:
        conversations = [r["conversation_id"] for r in con.execute("SELECT conversation_id FROM conversations ORDER BY created_at")]
    total: Counter[str] = Counter()
    for conv_id in conversations:
        total.update(build_v12_for_conversation(conv_id))
    return {"conversations": len(conversations), "counts": dict(total)}


def _build_self_dimensions(con, person_id: str | None, conversation_id: str) -> None:
    if not person_id:
        return
    now = now_iso()
    rows = list(con.execute("SELECT * FROM turns WHERE conversation_id=?", (conversation_id,)))
    text = "\n".join(r["text"] for r in rows if (r["person_id"] or r["speaker_label"] or person_id) == person_id)
    dims = {
        "need_for_clarity": _score_word(text, ["concret", "explique", "détail", "comprendre", "clair", "plan"]),
        "need_for_control": _score_word(text, ["fiable", "strict", "contrôle", "sans fallback", "preuve", "sûr"]),
        "sensitivity_to_vagueness": _score_word(text, ["flou", "léger", "pas compris", "rien compris", "mauvais"]),
        "validation_seeking": _score_word(text, ["tu penses", "c'est ça", "ok", "sûr", "vérifier"]),
        "curiosity": _score_word(text, ["pourquoi", "comment", "explique", "intéressant", "comprendre"]),
        "persistence": _score_word(text, ["encore", "tout", "complet", "jusqu", "continue", "fini"]),
        "directness": _score_word(text, ["je veux", "réponse courte", "réponse longue", "sois dur", "fais"]),
    }
    for key, score in dims.items():
        score = _clamp(0.35 + score * 0.6)
        upsert(con, "self_model_dimensions", {
            "dimension_id": stable_id("smdim", person_id, key),
            "person_id": person_id,
            "dimension_key": key,
            "score": score,
            "confidence": _clamp(0.4 + min(len(rows), 20) / 40),
            "evidence_count": len(rows),
            "active_contexts_json": json_dumps(["conversation", "project"]),
            "counterexamples_json": json_dumps([]),
            "validity_status": "candidate" if len(rows) < 4 else "probable",
            "metadata_json": json_dumps({"v12_version": V12_VERSION, "conversation_id": conversation_id}),
            "created_at": now,
            "updated_at": now,
        }, "dimension_id")


def _promote_patterns(con, conversation_id: str | None = None) -> None:
    now = now_iso()
    group_rows = con.execute("""
        SELECT person_id, signal_type, signal_value, COUNT(*) c, MIN(created_at) first_seen, MAX(created_at) last_seen
        FROM behavior_signals
        GROUP BY person_id, signal_type, signal_value
        HAVING c >= ?
    """, (WEAK_PATTERN_THRESHOLD,)).fetchall()
    for r in group_rows:
        person_id = r["person_id"] or "unknown"
        key = f"{r['signal_type']}:{normalize_text(r['signal_value'])}"
        status = "candidate" if r["c"] < CONFIRMED_PATTERN_THRESHOLD else "ready_to_confirm"
        cp_id = stable_id("candpat", person_id, key)
        upsert(con, "candidate_patterns", {
            "candidate_pattern_id": cp_id,
            "person_id": person_id,
            "pattern_type": r["signal_type"],
            "pattern_key": key,
            "title": f"Répétition détectée: {r['signal_value']}",
            "description": f"Signal {r['signal_type']}={r['signal_value']} observé {r['c']} fois.",
            "evidence_count": r["c"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "activation_contexts_json": json_dumps([conversation_id] if conversation_id else []),
            "counterexamples_json": json_dumps([]),
            "status": status,
            "confidence": _clamp(0.35 + r["c"] / 10),
            "metadata_json": json_dumps({"v12_version": V12_VERSION}),
            "created_at": now,
            "updated_at": now,
        }, "candidate_pattern_id")
        if r["c"] >= CONFIRMED_PATTERN_THRESHOLD:
            upsert(con, "confirmed_patterns", {
                "confirmed_pattern_id": stable_id("confpat", person_id, key),
                "candidate_pattern_id": cp_id,
                "person_id": person_id,
                "pattern_type": r["signal_type"],
                "pattern_key": key,
                "title": f"Pattern confirmé: {r['signal_value']}",
                "description": f"Signal répété et promu avec {r['c']} preuves.",
                "evidence_count": r["c"],
                "counterexample_count": 0,
                "activation_conditions_json": json_dumps([]),
                "escape_conditions_json": json_dumps([]),
                "usual_outcome": None,
                "confidence": _clamp(0.55 + r["c"] / 20),
                "validity_status": "active",
                "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                "created_at": now,
                "updated_at": now,
            }, "confirmed_pattern_id")


def prediction_context_fingerprint(text: str) -> dict[str, Any]:
    act = _classify_speech_act(text)
    state = _infer_state(text)
    return {
        "act_type": act["act_type"],
        "pressure_level": act["pressure_level"],
        "certainty_level": act["certainty_level"],
        "dominant_emotion": state["dominant_emotion"],
        "frustration": state["frustration"],
        "curiosity": state["curiosity"],
        "tokens": tokenize(text)[:80],
    }


def _similarity(a: str, b: str) -> float:
    ta = set(tokenize(a))
    tb = set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def predict(target: str, context: str, *, person_id: str | None = None, horizon: str = "next") -> dict[str, Any]:
    if target not in PREDICTION_TARGETS:
        raise ValueError(f"prediction target inconnu: {target}")
    init_db()
    with connect() as con:
        person_id = person_id or _default_user(con)
        case_type = target if target in PREDICTION_TARGETS else "next_phrase"
        cases = list(con.execute("SELECT * FROM prediction_cases WHERE case_type=? AND (person_id=? OR ? IS NULL) AND usable_for_prediction=1 ORDER BY quality_score DESC LIMIT 200", (case_type, person_id, person_id)))
        if not cases and target not in {"next_phrase", "next_message"}:
            cases = list(con.execute("SELECT * FROM prediction_cases WHERE case_type IN ('next_phrase','next_message') AND (person_id=? OR ? IS NULL) AND usable_for_prediction=1 ORDER BY quality_score DESC LIMIT 200", (person_id, person_id)))
        scored = []
        for c in cases:
            sim = _similarity(context, c["context_summary"])
            scored.append((sim * float(c["quality_score"] or 0.5), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [(s, c) for s, c in scored[:8] if s > 0.03]
        lang = list(con.execute("SELECT * FROM personal_language_patterns WHERE person_id=? ORDER BY frequency DESC, confidence DESC LIMIT 12", (person_id,)))
        dims = list(con.execute("SELECT * FROM self_model_dimensions WHERE person_id=? ORDER BY confidence DESC LIMIT 8", (person_id,)))
        now = now_iso()
        prediction_value = _prediction_value_from_target(target, context, top, lang, dims)
        probability = _clamp(0.45 + sum(s for s, _ in top[:5]) / max(1, min(len(top), 5)) * 0.45 + (0.05 if lang else 0.0))
        confidence = _clamp(0.35 + min(len(top), 8) / 16 + min(len(lang), 12) / 40)
        alternatives = _alternatives_for_target(target, top, lang)
        evidence_cases = [{"case_id": c["case_id"], "score": round(s, 3), "context": c["context_summary"], "observed_next": c["speech_next"]} for s, c in top[:5]]
        pred_id = stable_id("pred", person_id, target, horizon, context[:250], now[:19])
        upsert(con, "predictions", {
            "prediction_id": pred_id,
            "created_at": now,
            "person_id": person_id,
            "prediction_target": target,
            "horizon": horizon,
            "current_context": context,
            "predicted_value": prediction_value,
            "probability": probability,
            "confidence": confidence,
            "alternatives_json": json_dumps(alternatives),
            "evidence_cases_json": json_dumps(evidence_cases),
            "counter_evidence_json": json_dumps([]),
            "assumptions_json": json_dumps(["prediction probabiliste basée sur cas similaires et langage personnel", "à vérifier par outcome futur"]),
            "intervention_options_json": json_dumps(_interventions_for_target(target, prediction_value)),
            "verification_due_at": None,
            "status": "open",
            "metadata_json": json_dumps({"v12_version": V12_VERSION, "fingerprint": prediction_context_fingerprint(context)}),
            "updated_at": now,
        }, "prediction_id")
        for i, branch in enumerate(_branches_for_prediction(target, prediction_value)):
            upsert(con, "simulation_branches", {
                "branch_id": stable_id("branch", pred_id, i, branch["branch_name"]),
                "prediction_id": pred_id,
                "branch_name": branch["branch_name"],
                "if_condition": branch["if_condition"],
                "probability": branch["probability"],
                "expected_path": branch["expected_path"],
                "risk_level": branch["risk_level"],
                "opportunity_level": branch["opportunity_level"],
                "recommended_intervention": branch["recommended_intervention"],
                "metadata_json": json_dumps({"v12_version": V12_VERSION}),
                "created_at": now,
                "updated_at": now,
            }, "branch_id")
        _persist_prediction_support(con, pred_id, person_id, target, context, top, prediction_value)
        con.commit()
        return {
            "prediction_id": pred_id,
            "target": target,
            "prediction": prediction_value,
            "probability": round(probability, 3),
            "confidence": round(confidence, 3),
            "evidence_cases": evidence_cases,
            "alternatives": alternatives,
            "interventions": _interventions_for_target(target, prediction_value),
        }


def _prediction_value_from_target(target: str, context: str, top: list[tuple[float, Any]], lang: list[Any], dims: list[Any]) -> str:
    if target in {"next_word", "next_phrase", "next_message"}:
        if top and top[0][1]["speech_next"]:
            return top[0][1]["speech_next"]
        if lang:
            return f"Formulation probable autour de: {lang[0]['expression']}"
        return "Prochaine formulation incertaine; pas encore assez de cas similaires."
    if target == "next_emotion":
        emotions = [c["emotion_next"] for _, c in top if c["emotion_next"]]
        if emotions:
            return Counter(emotions).most_common(1)[0][0]
        state = _infer_state(context)
        return state["dominant_emotion"] or "émotion incertaine"
    if target == "next_thought":
        thoughts = [c["thought_next_hypothesis"] for _, c in top if c["thought_next_hypothesis"]]
        if thoughts:
            return thoughts[0]
        return "Préoccupation probable liée à la clarté, au contrôle ou à la prochaine action."
    if target == "next_action":
        actions = [c["action_taken"] or c["speech_next"] for _, c in top if c["action_taken"] or c["speech_next"]]
        if actions:
            return actions[0]
        norm_context = normalize_text(context)
        if any(k in norm_context for k in ["concret", "plan", "sans rien oublier", "preuve", "test"]):
            return "Demander ou exécuter une étape concrète, vérifiable et liée à un résultat observable."
        if any(d["dimension_key"] in {"need_for_clarity", "need_for_control"} and d["score"] > 0.55 for d in dims):
            return "Demander une clarification concrète ou transformer le sujet en plan/action vérifiable."
        return "Action suivante incertaine; attendre plus de contexte ou un choix explicite."
    if target == "next_choice":
        return "Choix probable vers l'option la plus contrôlable, explicable et vérifiable."
    if target == "next_reaction":
        return "Réaction probable: clarification directe si le contexte est flou; validation si le plan est concret."
    if target == "next_outcome":
        return "Issue probable dépendante de la clarté des prochaines étapes: progression si procédure claire, boucle de questions si flou."
    if target == "next_loop":
        return "Boucle probable: exigence élevée → demande de précision → validation → action ou correction."
    if target == "next_risk":
        return "Risque probable: surcomplexité, bruit mémoire ou fausse certitude si les preuves et résultats ne sont pas vérifiés."
    if target == "next_relationship_move":
        return "Mouvement relationnel probable: clarification, test de compréhension ou ajustement de limite selon le niveau de tension."
    if target == "next_project_move":
        return "Mouvement projet probable: demander une version plus complète, un test exécutable ou une preuve que le système couvre tout le plan."
    if target == "next_client_outcome":
        return "Issue client probable: verrouiller le périmètre si la demande est floue; accepter seulement si les critères sont explicites."
    if target == "next_life_event":
        return "Événement probable: retour du même sujet ouvert sous forme de demande plus précise ou de décision à valider."
    if target == "next_trajectory":
        return "Trajectoire probable: si rien ne change, poursuite de la boucle analyse→exigence→plan; si preuve concrète, passage à l'action."
    return "Prédiction non spécialisée."


def _alternatives_for_target(target: str, top: list[tuple[float, Any]], lang: list[Any]) -> list[str]:
    alts: list[str] = []
    for _, c in top[:3]:
        if c["speech_next"]:
            alts.append(c["speech_next"])
    for r in lang[:3]:
        alts.append(f"expression fréquente: {r['expression']}")
    return list(dict.fromkeys(alts))[:5]


def _interventions_for_target(target: str, prediction_value: str) -> list[str]:
    if target in {"next_risk", "next_outcome", "next_action"}:
        return ["Demander une preuve exacte.", "Réduire la prochaine étape à un test observable.", "Vérifier après coup si la prédiction était correcte."]
    return ["Garder la prédiction comme hypothèse, pas comme vérité.", "Comparer au résultat réel dès qu'il apparaît."]


def _branches_for_prediction(target: str, prediction_value: str) -> list[dict[str, Any]]:
    return [
        {
            "branch_name": "trajectoire_si_concret",
            "if_condition": "La prochaine réponse/action est concrète, vérifiable et courte.",
            "probability": 0.45,
            "expected_path": "Progression ou validation plus rapide.",
            "risk_level": 0.25,
            "opportunity_level": 0.75,
            "recommended_intervention": "Donner une étape testable et une preuve.",
        },
        {
            "branch_name": "trajectoire_si_flou",
            "if_condition": "La prochaine réponse/action reste abstraite ou trop générale.",
            "probability": 0.35,
            "expected_path": "Nouvelle demande de précision ou correction de cadrage.",
            "risk_level": 0.65,
            "opportunity_level": 0.35,
            "recommended_intervention": "Revenir au contexte, aux preuves et à l'action minimale.",
        },
        {
            "branch_name": "trajectoire_si_non_action",
            "if_condition": "Aucune action claire n'est prise.",
            "probability": 0.20,
            "expected_path": "Boucle de réflexion ou report.",
            "risk_level": 0.55,
            "opportunity_level": 0.25,
            "recommended_intervention": "Créer un outcome attendu et une vérification future.",
        },
    ]




def _persist_prediction_support(con, pred_id: str, person_id: str | None, target: str, context: str, top: list[tuple[float, Any]], prediction_value: str) -> None:
    now = now_iso()
    for score, c in top[:12]:
        semantic = _similarity(context, c["context_summary"])
        situation = 0.5
        try:
            sv = json_loads(c["situation_vector_json"], {}) or {}
            fp = prediction_context_fingerprint(context)
            situation = 0.75 if sv.get("act") == fp.get("act_type") else 0.45
        except Exception:
            situation = 0.45
        language = _similarity(prediction_value, c["speech_next"] or "")
        final = _clamp(score * 0.7 + semantic * 0.15 + language * 0.15)
        upsert(con, "similar_case_scores", {
            "similar_case_id": stable_id("simcase", pred_id, c["case_id"]),
            "prediction_id": pred_id,
            "case_id": c["case_id"],
            "person_id": person_id,
            "prediction_target": target,
            "semantic_similarity": semantic,
            "situation_similarity": situation,
            "state_similarity": 0.5,
            "relationship_similarity": 0.5,
            "outcome_similarity": 0.5 if c["outcome"] else 0.25,
            "language_similarity": language,
            "final_score": final,
            "explanation": "Score mixte V12: texte + situation + langage + outcome.",
            "metadata_json": json_dumps({"v12_version": V12_VERSION}),
            "created_at": now,
        }, "similar_case_id")
    for branch in _branches_for_prediction(target, prediction_value):
        upsert(con, "future_scenarios", {
            "scenario_id": stable_id("fsc", pred_id, branch["branch_name"]),
            "person_id": person_id,
            "episode_id": None,
            "prediction_id": pred_id,
            "scenario_type": branch["branch_name"],
            "horizon": "prediction_horizon",
            "if_condition": branch["if_condition"],
            "expected_future": branch["expected_path"],
            "probability": branch["probability"],
            "risk_level": branch["risk_level"],
            "opportunity_level": branch["opportunity_level"],
            "evidence_json": json_dumps([{ "prediction_id": pred_id }]),
            "counter_evidence_json": json_dumps([]),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "scenario_id")
    if target in {"next_risk", "next_outcome", "next_trajectory"}:
        upsert(con, "trajectory_warnings", {
            "warning_id": stable_id("warn", pred_id, target),
            "person_id": person_id,
            "episode_id": None,
            "prediction_id": pred_id,
            "warning_type": target,
            "title": "Attention trajectoire prédite à vérifier",
            "detail": prediction_value,
            "severity": 0.62,
            "probability": 0.62,
            "evidence_json": json_dumps([{ "prediction_id": pred_id }]),
            "counter_evidence_json": json_dumps([]),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }, "warning_id")
    for i, action in enumerate(_interventions_for_target(target, prediction_value)):
        upsert(con, "recommended_actions", {
            "recommendation_id": stable_id("rec", pred_id, i, action),
            "person_id": person_id,
            "prediction_id": pred_id,
            "episode_id": None,
            "recommendation_type": "prediction_intervention",
            "title": "Intervention possible",
            "detail": action,
            "expected_effect": "Changer ou vérifier la trajectoire prédite.",
            "confidence": 0.6,
            "status": "open",
            "evidence_json": json_dumps([{ "prediction_id": pred_id, "target": target }]),
            "created_at": now,
            "updated_at": now,
        }, "recommendation_id")

def verify_prediction(prediction_id: str, observed_value: str, *, match_score: float | None = None, note: str | None = None) -> dict[str, Any]:
    init_db()
    with connect() as con:
        pred = con.execute("SELECT * FROM predictions WHERE prediction_id=?", (prediction_id,)).fetchone()
        if not pred:
            return {"error": "prediction_not_found", "prediction_id": prediction_id}
        if match_score is None:
            match_score = _similarity(pred["predicted_value"], observed_value)
        was_correct = 1 if match_score >= 0.45 else 0
        now = now_iso()
        result_id = stable_id("predres", prediction_id, observed_value[:200])
        upsert(con, "prediction_results", {
            "result_id": result_id,
            "prediction_id": prediction_id,
            "observed_value": observed_value,
            "match_score": match_score,
            "was_correct": was_correct,
            "why_correct": note if was_correct else None,
            "why_wrong": note if not was_correct else None,
            "model_update": "renforcer" if was_correct else "réduire confiance / chercher contexte manquant",
            "verified_at": now,
            "metadata_json": json_dumps({"v12_version": V12_VERSION}),
        }, "result_id")
        con.execute("UPDATE predictions SET status=?, updated_at=? WHERE prediction_id=?", ("verified_correct" if was_correct else "verified_wrong", now, prediction_id))
        _recompute_calibration(con, pred["person_id"], pred["prediction_target"])
        con.commit()
        return {"prediction_id": prediction_id, "result_id": result_id, "match_score": round(match_score, 3), "was_correct": bool(was_correct)}


def _recompute_calibration(con, person_id: str | None, target: str) -> None:
    rows = list(con.execute("""
        SELECT p.confidence, r.was_correct
        FROM predictions p JOIN prediction_results r ON r.prediction_id=p.prediction_id
        WHERE p.prediction_target=? AND (p.person_id IS ? OR p.person_id = ?)
    """, (target, person_id, person_id)))
    if not rows:
        return
    sample = len(rows)
    accuracy = sum(1 for r in rows if r["was_correct"]) / sample
    mean_conf = sum(float(r["confidence"] or 0.5) for r in rows) / sample
    now = now_iso()
    upsert(con, "calibration_scores", {
        "calibration_id": stable_id("cal", person_id or "unknown", target),
        "person_id": person_id,
        "prediction_target": target,
        "sample_size": sample,
        "accuracy": accuracy,
        "mean_confidence": mean_conf,
        "calibration_gap": mean_conf - accuracy,
        "notes": "Calibration automatique V12 fondation.",
        "calculated_at": now,
        "metadata_json": json_dumps({"v12_version": V12_VERSION}),
    }, "calibration_id")


def v12_overview() -> dict[str, Any]:
    init_db()
    tables = [
        "episodes", "situation_episodes", "interaction_episodes", "speech_acts", "internal_state_snapshots",
        "thought_hypotheses", "action_intentions", "action_outcomes", "choice_episodes", "causal_edges",
        "contradiction_events", "relationship_models", "self_model_dimensions", "behavior_signals",
        "candidate_patterns", "confirmed_patterns", "loop_patterns", "personal_language_patterns",
        "prediction_cases", "predictions", "prediction_results", "simulation_branches", "calibration_scores",
        "recommended_actions", "emotion_evidence", "similar_case_scores", "future_scenarios",
        "trajectory_warnings", "escape_conditions", "v12_engine_runs",
    ]
    with connect() as con:
        return {t: con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
