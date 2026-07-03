from __future__ import annotations

"""V13 final Brain 2.0 complete layer.

This module is intentionally strict: the complete cognitive engines are LLM-first and
proof-bound. When Qwen/Ollama is disabled the code only records missing-engine
contract rows; it does not pretend to infer hidden thoughts or future behaviour.

The goal is to turn the V12/V13 foundation into the exact model discussed in the
project plan:

observed life -> situation -> internal state -> words/actions -> reactions/outcomes
-> patterns -> simulation -> prediction -> verification -> correction.
"""

import os
from collections import Counter, defaultdict
from typing import Any

from .db import connect, init_db, upsert
from .llm import EliteLLMError, OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, sha256_bytes, stable_id, tokenize, normalize_text

COMPLETE_VERSION = "13.1.0-brain2-complete-final"

COMPLETE_TARGETS = {
    "next_word", "next_phrase", "next_message", "next_emotion", "next_thought",
    "next_action", "next_choice", "next_reaction", "next_outcome", "next_loop",
    "next_risk", "next_relationship_move", "next_project_move", "next_client_outcome",
    "next_life_event", "next_trajectory", "next_state", "next_intervention",
    "next_contradiction", "next_trigger",
}

# Explicit plan requirements. These are not marketing labels: audit_complete_v13_plan
# persists them and verifies the physical tables/engines exist.
PLAN_TABLES = {
    # sacred evidence
    "raw_assets", "conversations", "turns", "source_spans", "source_items", "lifestream_segments",
    "speaker_profiles", "speaker_matches", "speaker_uncertainty_segments", "memory_evidence", "memory_revisions",
    # interpretation layer
    "utterance_analyses", "speech_acts", "word_signals", "expression_signals", "memory_frames", "memory_facets",
    "thought_hypotheses", "internal_state_snapshots", "emotion_evidence", "state_transitions", "audio_prosody_events",
    # episode layer
    "episodes", "episode_evidence", "episode_links", "episode_boundaries", "situation_episodes",
    "interaction_episodes", "choice_episodes", "choice_options", "choice_criteria", "action_intentions",
    "action_outcomes", "contradiction_events",
    # model layer
    "self_model_dimensions", "relationship_models", "social_roles", "trust_history", "conflict_loops", "repair_patterns",
    "behavior_signals", "candidate_patterns", "confirmed_patterns", "loop_patterns", "pattern_contexts", "pattern_counterexamples",
    "causal_edges", "causal_hypotheses", "counter_evidence_items", "personal_language_patterns", "phrase_templates",
    "next_phrase_cases", "style_state_snapshots", "language_ngrams",
    # prediction/simulation/calibration/intervention
    "prediction_cases", "similar_case_scores", "similar_case_retrieval_runs", "predictions", "prediction_results",
    "prediction_target_scores", "simulation_branches", "future_scenarios", "trajectory_warnings", "escape_conditions",
    "recommended_actions", "trajectory_interventions", "calibration_scores", "model_revisions",
    # V13 operating/audit layer
    "v13_cognitive_cycles", "v13_llm_extractions", "v13_dynamic_models", "v13_user_model_snapshots",
    "v13_case_clusters", "v13_prediction_explanations", "v13_memory_contract_checks", "v13_replay_events",
    "v13_intervention_plans", "v13_plan_audit_rows", "v13_plan_requirements", "v13_component_coverage",
    "v13_engine_runs", "v13_engine_outputs", "v13_complete_contract_checks",
}

ENGINE_ORDER = [
    "capture_engine",
    "language_signature_engine",
    "episode_builder",
    "context_resolver",
    "internal_state_engine",
    "social_model_engine",
    "causality_engine",
    "contradiction_engine",
    "pattern_miner",
    "choice_model_engine",
    "outcome_tracker",
    "similar_case_retrieval",
    "prediction_engine",
    "simulation_engine",
    "calibration_engine",
    "intervention_engine",
]

ENGINE_TABLES: dict[str, list[str]] = {
    "capture_engine": ["raw_assets", "conversations", "turns", "source_spans", "source_items", "speaker_matches", "audio_prosody_events"],
    "language_signature_engine": ["word_signals", "expression_signals", "personal_language_patterns", "phrase_templates", "next_phrase_cases", "style_state_snapshots", "language_ngrams"],
    "episode_builder": ["episodes", "episode_evidence", "episode_links", "episode_boundaries", "situation_episodes", "interaction_episodes"],
    "context_resolver": ["situation_episodes", "social_roles", "v13_dynamic_models", "v13_engine_outputs"],
    "internal_state_engine": ["internal_state_snapshots", "thought_hypotheses", "emotion_evidence", "state_transitions"],
    "social_model_engine": ["relationship_models", "social_roles", "trust_history", "conflict_loops", "repair_patterns", "interaction_episodes"],
    "causality_engine": ["causal_edges", "causal_hypotheses", "counter_evidence_items"],
    "contradiction_engine": ["contradiction_events", "counter_evidence_items", "model_revisions"],
    "pattern_miner": ["behavior_signals", "candidate_patterns", "confirmed_patterns", "loop_patterns", "pattern_contexts", "pattern_counterexamples"],
    "choice_model_engine": ["choice_episodes", "choice_options", "choice_criteria", "action_outcomes"],
    "outcome_tracker": ["action_intentions", "action_outcomes", "prediction_results", "v13_replay_events", "model_revisions"],
    "similar_case_retrieval": ["prediction_cases", "similar_case_scores", "similar_case_retrieval_runs", "v13_case_clusters"],
    "prediction_engine": ["predictions", "v13_prediction_explanations", "prediction_target_scores"],
    "simulation_engine": ["simulation_branches", "future_scenarios"],
    "calibration_engine": ["calibration_scores", "prediction_results", "prediction_target_scores", "v13_replay_events"],
    "intervention_engine": ["recommended_actions", "trajectory_warnings", "escape_conditions", "v13_intervention_plans", "trajectory_interventions"],
}

# One schema per engine. This is the contract sent to Qwen. It avoids vague prose
# and forces explicit evidence/counter-evidence, confidence and materializable output.
ENGINE_SCHEMAS: dict[str, dict[str, Any]] = {
    "capture_engine": {
        "capture_quality": {"raw_preserved": True, "speaker_certainty": 0.0, "timestamp_certainty": 0.0, "missing_audio_signals": []},
        "prosody_events": [{"turn_id": "", "event_type": "pause|laughter|sigh|stress_voice|hesitation|overlap|volume_shift|unknown", "interpretation": "", "confidence": 0.0}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "language_signature_engine": {
        "word_predictions": [{"context": "", "next_word_candidates": [], "confidence": 0.0}],
        "phrase_templates": [{"template": "", "context_type": "", "speech_act_context": "", "emotion_context": "", "probability": 0.0, "examples": []}],
        "style_state": {"directness": 0.0, "detail_level": 0.0, "correction_tendency": 0.0, "validation_seeking": 0.0, "typical_phrases": []},
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "episode_builder": {
        "episode_boundaries": [{"boundary_type": "start|end|topic_shift|trigger|resolution", "turn_id": "", "reason": "", "confidence": 0.0}],
        "episode_summary_update": {"episode_type": "", "situation_summary": "", "trigger": "", "unresolved_tension": "", "outcome": ""},
        "links_to_other_episodes": [{"to_episode_id": "", "relation_type": "similar|causes|continues|contradicts|resolves", "confidence": 0.0}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "context_resolver": {
        "situation": {"situation_type": "", "life_domain": "", "participants": [], "main_person": "", "targets": [], "place_explicit": None, "place_inferred": None, "social_context": "", "power_balance": "", "stakes": "", "constraints": []},
        "resolved_references": [{"expression": "", "referent": "", "confidence": 0.0}],
        "missing_context": [], "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "internal_state_engine": {
        "state_before": {}, "state_during": {}, "state_after": {},
        "dominant_emotion": "", "secondary_emotions": [],
        "thought_hypotheses": [{"thought_type": "", "content": "", "trigger": "", "confidence": 0.0}],
        "state_transitions": [{"from": "", "to": "", "trigger": "", "confidence": 0.0}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "social_model_engine": {
        "relationship_updates": [{"other_person_id": "", "relationship_type": "", "trust_delta": 0.0, "tension_delta": 0.0, "common_trigger": "", "repair_action": "", "confidence": 0.0}],
        "social_roles": [{"person_id": "", "role_label": "", "role_context": "", "relation_to_user": "", "confidence": 0.0}],
        "conflict_loops": [{"summary": "", "trigger_pattern": "", "escalation_path": "", "deescalation_path": "", "confidence": 0.0}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "causality_engine": {
        "causal_hypotheses": [{"hypothesis": "", "cause": "", "effect": "", "causal_type": "triggered|increased|decreased|blocked|enabled|caused|explained_by|justified_after|correlated_with", "strength": 0.0, "confidence": 0.0}],
        "correlations_not_causes": [],
        "counter_evidence": [], "evidence": [], "confidence": 0.0,
    },
    "contradiction_engine": {
        "contradictions": [{"declared": "", "observed": "", "contradiction_type": "stated_vs_action|emotion_vs_language|intention_vs_outcome|value_vs_choice|confidence_vs_behavior|avoidance_vs_claim", "severity": 0.0, "possible_explanation": "", "confidence": 0.0}],
        "model_revisions_needed": [{"target": "", "reason": "", "new_view": ""}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "pattern_miner": {
        "signals": [{"signal_type": "", "signal_value": "", "episode_id": "", "strength": 0.0}],
        "candidate_patterns": [{"pattern_type": "", "pattern_key": "", "title": "", "evidence_count": 0, "activation_contexts": [], "counterexamples": []}],
        "confirmed_patterns": [{"pattern_type": "", "pattern_key": "", "title": "", "conditions": [], "usual_outcome": "", "escape_conditions": []}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "choice_model_engine": {
        "choices": [{"choice_context": "", "options": [], "criteria": [], "chosen_option": "", "rejected_options": [], "reason_given": "", "real_reason_hypothesis": "", "confidence": 0.0}],
        "predicted_choice_biases": [{"criterion": "", "weight": 0.0, "evidence": []}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "outcome_tracker": {
        "intention_outcome_links": [{"intention_id": "", "action_taken": "", "result": "", "success_level": 0.0, "delay": "", "lesson": "", "confidence": 0.0}],
        "open_loops": [{"item": "", "what_would_close_it": "", "risk_if_unclosed": ""}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "similar_case_retrieval": {
        "similar_cases": [{"case_id": "", "semantic_similarity": 0.0, "situation_similarity": 0.0, "state_similarity": 0.0, "relationship_similarity": 0.0, "outcome_similarity": 0.0, "language_similarity": 0.0, "final_score": 0.0, "why_similar": "", "why_not_identical": ""}],
        "clusters": [{"cluster_key": "", "cluster_type": "", "case_ids": [], "confidence": 0.0}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "prediction_engine": {
        "predictions": [{"prediction_target": "next_action", "horizon": "next", "predicted_value": "", "probability": 0.0, "confidence": 0.0, "why": [], "similar_cases": [], "counter_evidence": [], "assumptions": [], "interventions": [], "verification_plan": []}],
        "target_scores": [{"prediction_target": "", "reliability_label": "unproven|weak|promising|strong", "reason": ""}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "simulation_engine": {
        "branches": [{"branch_name": "", "if_condition": "", "probability": 0.0, "expected_path": "", "risk_level": 0.0, "opportunity_level": 0.0, "recommended_intervention": ""}],
        "future_scenarios": [{"scenario_type": "", "summary": "", "path": "", "risk": "", "opportunity": "", "confidence": 0.0}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "calibration_engine": {
        "calibration": [{"prediction_target": "", "expected_accuracy": 0.0, "overconfidence_risk": 0.0, "data_sufficiency": 0.0, "reliability_label": "unproven|weak|promising|strong"}],
        "model_updates": [],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
    "intervention_engine": {
        "trajectory_warnings": [{"warning_type": "", "summary": "", "risk_level": 0.0, "evidence": []}],
        "escape_conditions": [{"loop_key": "", "condition_text": "", "how_to_trigger": "", "confidence": 0.0}],
        "interventions": [{"goal": "", "current_path": "", "desired_path": "", "actions": [], "expected_effect": {}, "risk": {}, "verification_plan": [], "confidence": 0.0}],
        "evidence": [], "counter_evidence": [], "confidence": 0.0,
    },
}


def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        f = float(value)
    except Exception:
        f = 0.0
    return max(lo, min(hi, f))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _available_tables(con) -> set[str]:
    return {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _hash_payload(value: Any) -> str:
    return sha256_bytes(json_dumps(value).encode("utf-8"))


def _default_user(con, conversation_id: str | None = None) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return row["person_id"]
    if conversation_id:
        conv = con.execute("SELECT participants_json, speaker_map_json FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if conv:
            for v in (_as_dict(json_loads(conv["speaker_map_json"], {})) or {}).values():
                if str(v).lower() in {"me", "moi", "user", "utilisateur"}:
                    return str(v)
            for p in json_loads(conv["participants_json"], []) or []:
                if str(p).lower() in {"me", "moi", "user", "utilisateur"}:
                    return str(p)
    return "me"


def _episode_bundle(con, episode_id: str) -> dict[str, Any]:
    ep = con.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    if not ep:
        return {}
    evs = [dict(r) for r in con.execute("SELECT * FROM episode_evidence WHERE episode_id=? ORDER BY created_at", (episode_id,))]
    turn_ids = [r["turn_id"] for r in evs if r.get("turn_id")]
    turns = []
    if turn_ids:
        marks = ",".join("?" for _ in turn_ids)
        turns = [dict(r) for r in con.execute(f"SELECT * FROM turns WHERE turn_id IN ({marks}) ORDER BY idx", turn_ids)]
    def many(table: str) -> list[dict[str, Any]]:
        try:
            return [dict(r) for r in con.execute(f"SELECT * FROM {table} WHERE episode_id=?", (episode_id,))]
        except Exception:
            return []
    return {
        "episode": dict(ep),
        "evidence": evs[:120],
        "turns": turns[:120],
        "situation": many("situation_episodes"),
        "interactions": many("interaction_episodes"),
        "speech_acts": many("speech_acts"),
        "states": many("internal_state_snapshots"),
        "thoughts": many("thought_hypotheses"),
        "intentions": many("action_intentions"),
        "outcomes": many("action_outcomes"),
        "choices": many("choice_episodes"),
        "causes": many("causal_edges"),
        "contradictions": many("contradiction_events"),
        "cases": many("prediction_cases"),
    }


def _make_prompt(engine_name: str, bundle: dict[str, Any], prior_outputs: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> str:
    payload = {
        "engine_name": engine_name,
        "mission": "Build the Brain 2.0 dynamic user model exactly from evidence. Do not invent facts; label uncertainty; cite evidence strings.",
        "required_schema": ENGINE_SCHEMAS[engine_name],
        "episode_bundle": bundle,
        "prior_engine_outputs": prior_outputs or {},
        "extra": extra or {},
    }
    return json_dumps(payload)[:70000]


def _run_qwen_engine(engine_name: str, prompt: str, *, require_llm: bool) -> dict[str, Any] | None:
    if not require_llm:
        return None
    system = (
        "You are a strict local Brain 2.0 cognitive engine. Return only valid JSON matching the schema. "
        "Every inferred item must include confidence and evidence or counter_evidence. Never claim certainty. "
        "Do not use generic psychology; compare only the provided evidence and previous cases."
    )
    client = OllamaJsonClient()
    data = client.require_json(system, prompt, schema_hint=ENGINE_SCHEMAS[engine_name], timeout=float(os.environ.get("MLOMEGA_V13_ENGINE_TIMEOUT", "120")))
    if not isinstance(data, dict):
        raise EliteLLMError(f"{engine_name} returned non-object JSON")
    return data


def _persist_engine_run(con, *, engine_name: str, cycle_id: str | None, conversation_id: str | None, episode_id: str | None, person_id: str | None, prompt: str, require_llm: bool, status: str, output: dict[str, Any] | None, error: str | None = None) -> str:
    now = now_iso()
    run_id = stable_id("v13eng", COMPLETE_VERSION, engine_name, cycle_id, episode_id, _hash_payload(prompt)[:16])
    warnings = []
    missing = []
    if output is None:
        missing.append("qwen_engine_output")
        warnings.append("LLM not run; this is a contract record only, not a cognitive inference.")
    upsert(con, "v13_engine_runs", {
        "engine_run_id": run_id,
        "engine_name": engine_name,
        "engine_version": COMPLETE_VERSION,
        "cycle_id": cycle_id,
        "conversation_id": conversation_id,
        "episode_id": episode_id,
        "person_id": person_id,
        "input_hash": _hash_payload(prompt),
        "require_llm": 1 if require_llm else 0,
        "llm_model": os.environ.get("MLOMEGA_OLLAMA_MODEL", "qwen3:8b") if require_llm else None,
        "status": status,
        "stage": "finished" if status in {"ok", "evidence_only", "error"} else "running",
        "started_at": now,
        "finished_at": now,
        "counts_json": json_dumps({"output_keys": len(output or {})}),
        "warnings_json": json_dumps(warnings),
        "missing_json": json_dumps(missing),
        "error_text": error,
        "metadata_json": json_dumps({"complete_version": COMPLETE_VERSION}),
    }, "engine_run_id")
    out_id = stable_id("v13out", run_id, engine_name)
    upsert(con, "v13_engine_outputs", {
        "output_id": out_id,
        "engine_run_id": run_id,
        "engine_name": engine_name,
        "target_table": "episodes" if episode_id else None,
        "target_id": episode_id,
        "output_type": "engine_json" if output is not None else "missing_llm_contract",
        "output_json": json_dumps(output or {}),
        "confidence": _clamp((output or {}).get("confidence")),
        "evidence_json": json_dumps(_as_list((output or {}).get("evidence"))),
        "counter_evidence_json": json_dumps(_as_list((output or {}).get("counter_evidence"))),
        "validation_status": "valid" if output is not None else "missing_llm",
        "created_at": now,
    }, "output_id")
    return run_id


def seed_complete_plan_requirements(con) -> None:
    now = now_iso()
    for table in sorted(PLAN_TABLES):
        rid = stable_id("v13req", "table", table)
        upsert(con, "v13_plan_requirements", {
            "requirement_id": rid,
            "section": "tables",
            "item_key": table,
            "item_type": "table",
            "required_tables_json": json_dumps([table]),
            "required_engines_json": json_dumps([]),
            "rationale": "Physical table required by the complete Brain 2.0 plan.",
            "status": "declared",
            "created_at": now,
            "updated_at": now,
        }, "requirement_id")
    for engine in ENGINE_ORDER:
        rid = stable_id("v13req", "engine", engine)
        upsert(con, "v13_plan_requirements", {
            "requirement_id": rid,
            "section": "engines",
            "item_key": engine,
            "item_type": "engine",
            "required_tables_json": json_dumps(ENGINE_TABLES[engine]),
            "required_engines_json": json_dumps([engine]),
            "rationale": f"Complete cognitive engine: {engine}.",
            "status": "declared",
            "created_at": now,
            "updated_at": now,
        }, "requirement_id")


def audit_complete_v13_plan(*, persist: bool = True) -> dict[str, Any]:
    init_db()
    now = now_iso()
    rows: list[dict[str, Any]] = []
    with connect() as con:
        seed_complete_plan_requirements(con)
        tables = _available_tables(con)
        for table in sorted(PLAN_TABLES):
            ok = table in tables
            rows.append({"section": "tables", "item": table, "status": "ok" if ok else "missing", "missing": [] if ok else [table], "type": "table"})
        for engine in ENGINE_ORDER:
            missing = [t for t in ENGINE_TABLES[engine] if t not in tables]
            rows.append({"section": "engines", "item": engine, "status": "ok" if not missing else "partial", "missing": missing, "type": "engine"})
        if persist:
            for r in rows:
                req_id = stable_id("v13req", r["type"], r["item"])
                upsert(con, "v13_component_coverage", {
                    "coverage_id": stable_id("v13cov", r["section"], r["item"]),
                    "requirement_id": req_id,
                    "component_name": r["item"],
                    "component_type": r["type"],
                    "coverage_status": r["status"],
                    "evidence_json": json_dumps(["present in sqlite schema"] if r["status"] == "ok" else []),
                    "missing_json": json_dumps(r["missing"]),
                    "severity": "ok" if r["status"] == "ok" else "critical",
                    "created_at": now,
                    "updated_at": now,
                }, "coverage_id")
                upsert(con, "v13_complete_contract_checks", {
                    "check_id": stable_id("v13check", r["section"], r["item"]),
                    "check_group": r["section"],
                    "check_name": r["item"],
                    "required_status": "ok",
                    "actual_status": r["status"],
                    "detail": "" if r["status"] == "ok" else "missing: " + ", ".join(r["missing"]),
                    "severity": "info" if r["status"] == "ok" else "critical",
                    "created_at": now,
                }, "check_id")
            con.commit()
    return {"version": COMPLETE_VERSION, "total": len(rows), "ok": sum(1 for r in rows if r["status"] == "ok"), "missing_or_partial": [r for r in rows if r["status"] != "ok"], "rows": rows}


def _materialize_boundaries(con, episode_id: str) -> int:
    ep = con.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    if not ep or not ep["source_conversation_id"]:
        return 0
    now = now_iso()
    count = 0
    for btype, tid, reason in [("start", ep["start_turn_id"], "episode start_turn_id"), ("end", ep["end_turn_id"], "episode end_turn_id")]:
        if not tid:
            continue
        tr = con.execute("SELECT idx, text FROM turns WHERE turn_id=?", (tid,)).fetchone()
        upsert(con, "episode_boundaries", {
            "boundary_id": stable_id("epbound", episode_id, btype, tid),
            "conversation_id": ep["source_conversation_id"],
            "episode_id": episode_id,
            "boundary_type": btype,
            "turn_id": tid,
            "idx": tr["idx"] if tr else None,
            "reason": reason,
            "confidence": 0.9,
            "evidence_text": tr["text"] if tr else None,
            "created_at": now,
        }, "boundary_id")
        count += 1
    return count


def _materialize_choices(con, episode_id: str) -> int:
    now = now_iso(); count = 0
    for ch in con.execute("SELECT * FROM choice_episodes WHERE episode_id=?", (episode_id,)):
        for opt in json_loads(ch["options_json"], []) or []:
            text = str(opt if not isinstance(opt, dict) else opt.get("text") or opt.get("option") or opt)
            upsert(con, "choice_options", {"option_id": stable_id("choiceopt", ch["choice_id"], text), "choice_id": ch["choice_id"], "option_text": text, "option_status": "chosen" if text == ch["chosen_option"] else "available", "evidence_text": ch["reason_given"], "confidence": ch["confidence_after"] or ch["confidence_before"] or 0.5, "metadata_json": json_dumps({}), "created_at": now}, "option_id")
            count += 1
        for crit in json_loads(ch["criteria_json"], []) or []:
            if isinstance(crit, dict):
                key = str(crit.get("key") or crit.get("criterion") or crit.get("name") or "criterion")
                val = str(crit.get("value") or crit.get("text") or "")
                weight = _clamp(crit.get("weight"), 0, 1)
            else:
                key = normalize_text(str(crit))[:80] or "criterion"
                val = str(crit)
                weight = 0.5
            upsert(con, "choice_criteria", {"criterion_id": stable_id("choicecrit", ch["choice_id"], key, val), "choice_id": ch["choice_id"], "criterion_key": key, "criterion_value": val, "weight": weight, "evidence_text": ch["reason_given"], "confidence": ch["confidence_after"] or ch["confidence_before"] or 0.5, "metadata_json": json_dumps({}), "created_at": now}, "criterion_id")
            count += 1
    return count


def _materialize_language(con, conversation_id: str | None = None) -> int:
    now = now_iso(); count = 0
    params = ()
    where = ""
    if conversation_id:
        where = "WHERE conversation_id=?"; params=(conversation_id,)
    turns = [dict(r) for r in con.execute(f"SELECT * FROM turns {where} ORDER BY conversation_id, idx", params)]
    by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in turns:
        by_person[t.get("person_id") or t.get("speaker_label") or "unknown"].append(t)
    for person, rows in by_person.items():
        all_tokens: list[str] = []
        for t in rows:
            toks = tokenize(t["text"])
            all_tokens.extend(toks)
            # next phrase cases
            nxt = next((r for r in rows if r["conversation_id"] == t["conversation_id"] and r["idx"] == t["idx"] + 1), None)
            if nxt:
                upsert(con, "next_phrase_cases", {"next_phrase_case_id": stable_id("nextphrase", t["turn_id"], nxt["turn_id"]), "person_id": person, "episode_id": None, "turn_id": t["turn_id"], "previous_text": t["text"], "actual_next_text": nxt["text"], "predicted_next_text": None, "speech_act_context": None, "emotion_context": None, "interlocutor_context": nxt.get("person_id") or nxt.get("speaker_label"), "match_score": None, "usable_for_prediction": 1, "created_at": now}, "next_phrase_case_id")
                count += 1
        for n in (1, 2, 3):
            grams = Counter(" ".join(all_tokens[i:i+n]) for i in range(max(0, len(all_tokens)-n+1)))
            total = sum(grams.values()) or 1
            for gram, freq in grams.most_common(200):
                upsert(con, "language_ngrams", {"ngram_id": stable_id("ngram", person, n, gram), "person_id": person, "n": n, "ngram": gram, "context_type": "all", "frequency": freq, "examples_json": json_dumps([]), "probability": freq / total, "last_seen": now, "created_at": now, "updated_at": now}, "ngram_id")
                count += 1
        phrases = [r["text"] for r in rows[-20:]]
        upsert(con, "style_state_snapshots", {"style_state_id": stable_id("style", person, conversation_id or "all", now[:13]), "person_id": person, "episode_id": None, "context_type": "conversation" if conversation_id else "global", "directness": 0.5, "detail_level": min(1.0, sum(len(tokenize(p)) for p in phrases) / max(1, len(phrases)*40)), "correction_tendency": 0.5, "validation_seeking": 0.5, "emotional_charge": 0.5, "typical_phrases_json": json_dumps(phrases[-10:]), "evidence_json": json_dumps(phrases[-5:]), "confidence": 0.45, "created_at": now}, "style_state_id")
        count += 1
    return count


def _materialize_patterns_and_scores(con, person_id: str) -> int:
    now = now_iso(); count = 0
    for tbl, pid_col in [("candidate_patterns", "candidate_id"), ("confirmed_patterns", "confirmed_pattern_id"), ("loop_patterns", "loop_id")]:
        try:
            rows = [dict(r) for r in con.execute(f"SELECT * FROM {tbl} WHERE person_id=?", (person_id,))]
        except Exception:
            rows = []
        for r in rows:
            pid = r.get(pid_col) or r.get("pattern_id") or r.get("loop_id")
            if not pid: continue
            context_val = r.get("pattern_key") or r.get("trigger") or r.get("loop_type") or r.get("title") or "unknown"
            upsert(con, "pattern_contexts", {"pattern_context_id": stable_id("patctx", tbl, pid, context_val), "pattern_table": tbl, "pattern_id": pid, "context_type": "activation", "context_value": str(context_val), "activation_strength": _clamp(r.get("confidence")), "evidence_json": json_dumps([]), "confidence": _clamp(r.get("confidence")), "created_at": now}, "pattern_context_id")
            count += 1
            # explicit placeholder only if counterexamples_json exists and non-empty
            for ce in json_loads(r.get("counterexamples_json"), []) or []:
                upsert(con, "pattern_counterexamples", {"counterexample_id": stable_id("patce", tbl, pid, str(ce)), "pattern_table": tbl, "pattern_id": pid, "episode_id": None, "counterexample_summary": str(ce), "why_it_matters": "counter-evidence to pattern", "strength": 0.5, "evidence_json": json_dumps([ce]), "created_at": now}, "counterexample_id")
                count += 1
    # target score calibration
    targets = [r["prediction_target"] for r in con.execute("SELECT DISTINCT prediction_target FROM predictions WHERE person_id=?", (person_id,))]
    for target in targets:
        preds = [dict(r) for r in con.execute("SELECT * FROM predictions WHERE person_id=? AND prediction_target=?", (person_id, target))]
        results = [dict(r) for r in con.execute("SELECT pr.* FROM prediction_results pr JOIN predictions p ON p.prediction_id=pr.prediction_id WHERE p.person_id=? AND p.prediction_target=?", (person_id, target))]
        total = len(preds); verified = len(results); correct = sum(1 for r in results if r.get("was_correct"))
        mean_match = sum(float(r.get("match_score") or 0) for r in results) / max(1, verified)
        mean_conf = sum(float(p.get("confidence") or 0) for p in preds) / max(1, total)
        gap = abs(mean_conf - mean_match) if verified else mean_conf
        label = "unproven" if verified < 3 else ("strong" if mean_match >= .75 and gap <= .2 else "promising" if mean_match >= .55 else "weak")
        upsert(con, "prediction_target_scores", {"score_id": stable_id("targetscore", person_id, target), "person_id": person_id, "prediction_target": target, "total_predictions": total, "verified_predictions": verified, "correct_predictions": correct, "mean_match_score": mean_match, "mean_confidence": mean_conf, "calibration_gap": gap, "reliability_label": label, "updated_at": now}, "score_id")
        count += 1
    return count


def _materialize_social_and_causal(con, episode_id: str, person_id: str, outputs: dict[str, dict[str, Any]]) -> int:
    now = now_iso(); count = 0
    social = outputs.get("social_model_engine") or {}
    for role in _as_list(social.get("social_roles")):
        if not isinstance(role, dict): continue
        pid = str(role.get("person_id") or person_id)
        label = str(role.get("role_label") or role.get("relation_to_user") or "unknown")
        upsert(con, "social_roles", {"social_role_id": stable_id("socrole", pid, label), "person_id": pid, "role_label": label, "role_context": role.get("role_context"), "relation_to_user": role.get("relation_to_user"), "evidence_json": json_dumps(_as_list(role.get("evidence"))), "confidence": _clamp(role.get("confidence")), "status": "active", "created_at": now, "updated_at": now}, "social_role_id")
        count += 1
    for update in _as_list(social.get("relationship_updates")):
        if not isinstance(update, dict): continue
        other = str(update.get("other_person_id") or "unknown")
        rid = stable_id("rel", person_id, other)
        upsert(con, "trust_history", {"trust_history_id": stable_id("trust", rid, episode_id, now), "relationship_id": rid, "person_a": person_id, "person_b": other, "episode_id": episode_id, "trust_delta": float(update.get("trust_delta") or 0), "tension_delta": float(update.get("tension_delta") or 0), "reason": update.get("common_trigger") or update.get("reason"), "evidence_json": json_dumps(_as_list(update.get("evidence"))), "confidence": _clamp(update.get("confidence")), "created_at": now}, "trust_history_id")
        count += 1
    for loop in _as_list(social.get("conflict_loops")):
        if not isinstance(loop, dict) or not loop.get("summary"): continue
        upsert(con, "conflict_loops", {"conflict_loop_id": stable_id("conflictloop", person_id, episode_id, loop.get("summary")), "relationship_id": None, "person_a": person_id, "person_b": None, "loop_summary": loop.get("summary"), "trigger_pattern": loop.get("trigger_pattern"), "escalation_path": loop.get("escalation_path"), "deescalation_path": loop.get("deescalation_path"), "evidence_count": len(_as_list(social.get("evidence"))), "confidence": _clamp(loop.get("confidence")), "status": "candidate", "created_at": now, "updated_at": now}, "conflict_loop_id")
        count += 1
    for up in _as_list(outputs.get("causality_engine", {}).get("causal_hypotheses")):
        if not isinstance(up, dict) or not up.get("hypothesis"): continue
        hid = stable_id("caushyp", episode_id, up.get("hypothesis"))
        upsert(con, "causal_hypotheses", {"hypothesis_id": hid, "episode_id": episode_id, "person_id": person_id, "hypothesis_text": up.get("hypothesis"), "cause_table": None, "cause_id": str(up.get("cause") or ""), "effect_table": None, "effect_id": str(up.get("effect") or ""), "causal_type": up.get("causal_type"), "strength": _clamp(up.get("strength")), "evidence_json": json_dumps(_as_list(up.get("evidence"))), "counter_evidence_json": json_dumps(_as_list(up.get("counter_evidence"))), "status": "candidate", "confidence": _clamp(up.get("confidence")), "created_at": now, "updated_at": now}, "hypothesis_id")
        count += 1
    for engine, out in outputs.items():
        for ce in _as_list(out.get("counter_evidence")):
            upsert(con, "counter_evidence_items", {"counter_evidence_id": stable_id("counterev", engine, episode_id, str(ce)), "target_table": "episodes", "target_id": episode_id, "counter_evidence_type": engine, "counter_evidence_text": str(ce), "source_span_id": None, "strength": 0.5, "status": "active", "metadata_json": json_dumps({"engine": engine}), "created_at": now}, "counter_evidence_id")
            count += 1
    return count


def _materialize_predictions_from_outputs(con, episode_id: str | None, person_id: str, outputs: dict[str, dict[str, Any]], context: str) -> list[str]:
    now = now_iso(); pred_ids: list[str] = []
    pred_engine = outputs.get("prediction_engine") or {}
    sim_engine = outputs.get("simulation_engine") or {}
    intervention_engine = outputs.get("intervention_engine") or {}
    for i, p in enumerate(_as_list(pred_engine.get("predictions"))):
        if not isinstance(p, dict): continue
        target = str(p.get("prediction_target") or "next_action")
        if target not in COMPLETE_TARGETS: target = "next_action"
        value = str(p.get("predicted_value") or p.get("prediction") or "").strip()
        if not value: continue
        pid = stable_id("completepred", episode_id, person_id, target, value[:120])
        upsert(con, "predictions", {"prediction_id": pid, "created_at": now, "person_id": person_id, "prediction_target": target, "horizon": p.get("horizon") or "next", "current_context": context, "predicted_value": value, "probability": _clamp(p.get("probability")), "confidence": _clamp(p.get("confidence")), "alternatives_json": json_dumps([]), "evidence_cases_json": json_dumps(_as_list(p.get("similar_cases"))), "counter_evidence_json": json_dumps(_as_list(p.get("counter_evidence"))), "assumptions_json": json_dumps(_as_list(p.get("assumptions"))), "intervention_options_json": json_dumps(_as_list(p.get("interventions"))), "verification_due_at": None, "status": "open", "metadata_json": json_dumps({"complete_version": COMPLETE_VERSION, "episode_id": episode_id}), "updated_at": now}, "prediction_id")
        pred_ids.append(pid)
        for why in _as_list(p.get("why")):
            upsert(con, "v13_prediction_explanations", {"explanation_id": stable_id("predexp", pid, str(why)), "prediction_id": pid, "explanation_json": json_dumps({"text": str(why), "source": "brain2_complete_v13", "v15_18_contract_fix": True}), "why_json": json_dumps(_as_list(p.get("why"))), "similar_cases_json": json_dumps(_as_list(p.get("similar_cases"))), "counter_evidence_json": json_dumps(_as_list(p.get("counter_evidence"))), "assumptions_json": json_dumps(_as_list(p.get("assumptions"))), "intervention_json": json_dumps(_as_list(p.get("interventions"))), "uncertainty_json": json_dumps({"confidence": _clamp(p.get("confidence"))}), "created_at": now}, "explanation_id")
        for br in _as_list(sim_engine.get("branches")) + _as_list(p.get("branches")):
            if isinstance(br, dict) and (br.get("branch_name") or br.get("expected_path")):
                upsert(con, "simulation_branches", {"branch_id": stable_id("branch", pid, br.get("branch_name"), br.get("if_condition")), "prediction_id": pid, "branch_name": br.get("branch_name") or "branch", "if_condition": br.get("if_condition"), "probability": _clamp(br.get("probability")), "expected_path": br.get("expected_path"), "risk_level": _clamp(br.get("risk_level")), "opportunity_level": _clamp(br.get("opportunity_level")), "recommended_intervention": br.get("recommended_intervention"), "metadata_json": json_dumps({"complete_version": COMPLETE_VERSION}), "created_at": now}, "branch_id")
        for inter in _as_list(intervention_engine.get("interventions")):
            if isinstance(inter, dict) and (inter.get("goal") or inter.get("desired_path")):
                upsert(con, "trajectory_interventions", {"trajectory_intervention_id": stable_id("trajinter", pid, inter.get("goal"), inter.get("desired_path")), "prediction_id": pid, "person_id": person_id, "intervention_type": "trajectory_change", "current_path": inter.get("current_path"), "desired_path": inter.get("desired_path"), "action_plan_json": json_dumps(_as_list(inter.get("actions"))), "expected_effect_json": json_dumps(_as_dict(inter.get("expected_effect"))), "risk_json": json_dumps(_as_dict(inter.get("risk"))), "verification_plan_json": json_dumps(_as_list(inter.get("verification_plan"))), "status": "candidate", "confidence": _clamp(inter.get("confidence")), "created_at": now, "updated_at": now}, "trajectory_intervention_id")
    return pred_ids


def run_complete_engines_for_episode(con, episode_id: str, *, require_llm: bool = True) -> dict[str, Any]:
    ep = con.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    if not ep:
        return {"episode_id": episode_id, "error": "missing_episode"}
    person_id = _default_user(con, ep["source_conversation_id"])
    cycle_id = stable_id("v13completecycle", episode_id, now_iso()[:19])
    bundle = _episode_bundle(con, episode_id)
    outputs: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for engine in ENGINE_ORDER:
        prompt = _make_prompt(engine, bundle, prior_outputs=outputs, extra={"complete_version": COMPLETE_VERSION})
        try:
            out = _run_qwen_engine(engine, prompt, require_llm=require_llm)
            status = "ok" if out is not None else "evidence_only"
            error = None
        except Exception as exc:
            if require_llm:
                # Persist the failure before re-raising; the user gets a hard error instead of fake analysis.
                _persist_engine_run(con, engine_name=engine, cycle_id=cycle_id, conversation_id=ep["source_conversation_id"], episode_id=episode_id, person_id=person_id, prompt=prompt, require_llm=require_llm, status="error", output=None, error=str(exc)[:1000])
                raise
            out = None; status = "evidence_only"; error = str(exc)[:1000]
        _persist_engine_run(con, engine_name=engine, cycle_id=cycle_id, conversation_id=ep["source_conversation_id"], episode_id=episode_id, person_id=person_id, prompt=prompt, require_llm=require_llm, status=status, output=out, error=error)
        if out is not None:
            outputs[engine] = out
        counts[f"engine_{engine}"] += 1
    counts["episode_boundaries"] += _materialize_boundaries(con, episode_id)
    counts["choice_detail_rows"] += _materialize_choices(con, episode_id)
    counts["social_causal_rows"] += _materialize_social_and_causal(con, episode_id, person_id, outputs)
    pred_ids = _materialize_predictions_from_outputs(con, episode_id, person_id, outputs, ep["situation_summary"])
    counts["predictions"] += len(pred_ids)
    return {"cycle_id": cycle_id, "episode_id": episode_id, "person_id": person_id, "require_llm": require_llm, "engines_run": len(ENGINE_ORDER), "outputs": len(outputs), "prediction_ids": pred_ids, "counts": dict(counts)}


def build_complete_v13_for_conversation(conversation_id: str, *, require_llm: bool = True, max_episodes: int | None = None) -> dict[str, Any]:
    init_db()
    audit = audit_complete_v13_plan(persist=True)
    results = []
    with connect() as con:
        eps = [dict(r) for r in con.execute("SELECT episode_id FROM episodes WHERE source_conversation_id=? ORDER BY start_time, created_at", (conversation_id,))]
        if max_episodes is not None:
            eps = eps[:max_episodes]
        for ep in eps:
            results.append(run_complete_engines_for_episode(con, ep["episode_id"], require_llm=require_llm))
        # deterministic materialization that is not inference: language/choice/boundaries/calibration from stored facts
        lang_count = _materialize_language(con, conversation_id)
        person_id = _default_user(con, conversation_id)
        score_count = _materialize_patterns_and_scores(con, person_id)
        con.commit()
    return {"version": COMPLETE_VERSION, "conversation_id": conversation_id, "require_llm": require_llm, "episodes": len(results), "results": results, "language_rows": lang_count, "pattern_score_rows": score_count, "audit_ok": audit["ok"], "audit_total": audit["total"]}


def build_complete_v13_all(*, require_llm: bool = True, max_episodes_per_conversation: int | None = None) -> dict[str, Any]:
    init_db()
    with connect() as con:
        convs = [r["conversation_id"] for r in con.execute("SELECT conversation_id FROM conversations ORDER BY started_at, created_at")]
    results = [build_complete_v13_for_conversation(cid, require_llm=require_llm, max_episodes=max_episodes_per_conversation) for cid in convs]
    return {"version": COMPLETE_VERSION, "conversations": len(convs), "results": results}


def complete_v13_overview() -> dict[str, Any]:
    init_db()
    tables = sorted(PLAN_TABLES)
    with connect() as con:
        counts = {}
        for t in tables:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            except Exception:
                counts[t] = "missing"
        audit = audit_complete_v13_plan(persist=False)
    return {"version": COMPLETE_VERSION, "audit_ok": audit["ok"], "audit_total": audit["total"], "missing_or_partial": audit["missing_or_partial"], "counts": counts}


def complete_v13_verify_and_calibrate(prediction_id: str, observed_value: str, *, match_score: float | None = None, note: str | None = None) -> dict[str, Any]:
    from .behavior_v13 import verify_v13_prediction
    base = verify_v13_prediction(prediction_id, observed_value, match_score=match_score, note=note, require_llm=True)
    init_db()
    now = now_iso()
    with connect() as con:
        pred = con.execute("SELECT * FROM predictions WHERE prediction_id=?", (prediction_id,)).fetchone()
        if pred:
            person_id = pred["person_id"] or _default_user(con)
            _materialize_patterns_and_scores(con, person_id)
            upsert(con, "model_revisions", {"model_revision_id": stable_id("modelrev", prediction_id, observed_value[:120]), "target_table": "predictions", "target_id": prediction_id, "revision_type": "prediction_verification", "previous_json": json_dumps(dict(pred)), "new_json": json_dumps({"observed_value": observed_value, "match_score": match_score}), "reason": note or "prediction verified by observed value", "evidence_json": json_dumps([observed_value]), "created_at": now}, "model_revision_id")
            con.commit()
    return {"version": COMPLETE_VERSION, "base": base}
