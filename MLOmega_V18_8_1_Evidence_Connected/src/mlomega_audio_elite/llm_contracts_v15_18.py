from __future__ import annotations

"""V15.18 LLM contract hardening helpers.

This module is intentionally boring: it translates common LLM JSON variants into
one canonical DB-facing contract and computes whether a memory is safe enough to
influence live behaviour.  It does not infer psychology; it normalizes and gates.
"""

from typing import Any

V15_18_VERSION = "15.18.0-llm-contract-hardening"


def clamp(value: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        f = float(value)
    except Exception:
        f = default
    return max(lo, min(hi, f))


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def first_present(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        v = row.get(k)
        if v is not None and v != "":
            return v
    return default


def normalize_salient_words(rows: Any, *, turn_text: str = "") -> list[dict[str, Any]]:
    """Accept token/word variants and always return DB-safe word_signals rows."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for i, raw in enumerate(as_list(rows)):
        if not isinstance(raw, dict):
            continue
        token = str(first_present(raw, "token", "word", "text", "surface", default="")).strip()
        if not token:
            continue
        try:
            pos = int(first_present(raw, "position", "index", "token_index", default=i) or i)
        except Exception:
            pos = i
        sal = clamp(first_present(raw, "salience", "score", "importance", "weight", default=0.5), default=0.5)
        role = str(first_present(raw, "role", "label", "type", default="salient") or "salient")
        why = str(first_present(raw, "why_it_matters", "reason", "why", "explanation", default="") or "")
        if not why:
            why = "mot signalé par le LLM local; contrat normalisé V15.18"
        key = (pos, token.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "position": pos,
            "token": token,
            "salience": sal,
            "role": role,
            "why_it_matters": why,
            "raw": raw,
        })
    return out


def normalize_personal_language_items(rows: Any, *, fallback_text: str = "") -> list[dict[str, Any]]:
    """Separate expression/style memory from factual/psychological conclusions."""
    out: list[dict[str, Any]] = []
    for raw in as_list(rows):
        if not isinstance(raw, dict):
            continue
        text = str(first_present(raw, "text", "expression", "phrase", default="") or "").strip()
        if not text:
            continue
        contexts = as_list(first_present(raw, "contexts", "context", "usage_contexts", default=[]))
        evidence = as_list(first_present(raw, "evidence_turn_ids", "evidence", "evidence_text", default=[]))
        if not evidence and fallback_text:
            evidence = [fallback_text]
        out.append({
            "text": text,
            "meaning": str(first_present(raw, "meaning", "meaning_for_user", "personal_meaning", default="usage personnel à confirmer") or "usage personnel à confirmer"),
            "tone": str(first_present(raw, "tone", "category", default="style") or "style"),
            "contexts": [str(x) for x in contexts if x is not None],
            "response_implication": str(first_present(raw, "response_implication", "response_rule", default="utiliser comme contexte de style seulement") or "utiliser comme contexte de style seulement"),
            "do_not_overpsychologize": bool(first_present(raw, "do_not_overpsychologize", default=True)),
            "evidence_turn_ids": [str(x) for x in evidence if x is not None],
            "confidence": clamp(first_present(raw, "confidence", "intensity", default=0.55), default=0.55),
            "raw": raw,
        })
    return out


def normalize_outcome_tracker(output: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Convert intention_outcome_links into explicit intentions + outcomes."""
    intentions: list[dict[str, Any]] = [x for x in as_list(output.get("intentions")) if isinstance(x, dict)]
    outcomes: list[dict[str, Any]] = [x for x in as_list(output.get("outcomes")) if isinstance(x, dict)]
    for link in as_list(output.get("intention_outcome_links")):
        if not isinstance(link, dict):
            continue
        intention_id = str(first_present(link, "intention_id", default="") or "")
        action_taken = str(first_present(link, "action_taken", "action", default="") or "")
        result = str(first_present(link, "result", "outcome", default="") or "")
        if intention_id or action_taken:
            intentions.append({
                "intention_id": intention_id or None,
                "intention_text": str(first_present(link, "intention_text", "intention", "goal", default=action_taken) or action_taken),
                "action_type": str(first_present(link, "action_type", default="observed_or_reported_action") or "observed_or_reported_action"),
                "target": first_present(link, "target", default=None),
                "strength": first_present(link, "strength", "confidence", default=0.55),
                "explicitness": first_present(link, "explicitness", default="llm_link"),
                "status": "linked_to_outcome" if result else "proposed",
                "evidence": as_list(link.get("evidence") or output.get("evidence")),
                "raw": link,
            })
        if action_taken or result:
            outcomes.append({
                "intention_id": intention_id or None,
                "action_taken": action_taken,
                "result": result,
                "success_level": first_present(link, "success_level", "success", default=0.0),
                "delay": first_present(link, "delay", "delay_text", default=None),
                "lesson": first_present(link, "lesson", default=None),
                "confidence": first_present(link, "confidence", default=0.55),
                "evidence": as_list(link.get("evidence") or output.get("evidence")),
                "raw": link,
            })
    return {"intentions": intentions, "outcomes": outcomes}


def normalize_similar_case_score(row: dict[str, Any]) -> float:
    return clamp(first_present(row, "overall_similarity", "final_score", "score", "similarity", default=0.0), default=0.0)


def normalize_calibration_rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in as_list(output.get("calibration")):
        if isinstance(raw, dict):
            rows.append(raw)
    if not rows and any(k in output for k in ("prediction_target", "accuracy", "expected_accuracy", "sample_size", "mean_confidence")):
        rows.append(output)
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        target = str(first_present(raw, "prediction_target", "target", "domain", default="all") or "all")
        accuracy = clamp(first_present(raw, "accuracy", "expected_accuracy", default=0.0), default=0.0)
        mean_conf = clamp(first_present(raw, "mean_confidence", "confidence", default=accuracy), default=accuracy)
        sample_size = first_present(raw, "sample_size", "n", "data_points", default=0)
        try:
            sample_size = int(sample_size or 0)
        except Exception:
            sample_size = 0
        gap = first_present(raw, "calibration_gap", default=None)
        if gap is None:
            gap = mean_conf - accuracy
        normalized.append({
            "prediction_target": target,
            "sample_size": sample_size,
            "accuracy": accuracy,
            "mean_confidence": mean_conf,
            "calibration_gap": clamp(gap, -1.0, 1.0, default=0.0),
            "notes": str(first_present(raw, "notes", "reliability_label", default="llm_calibration_contract_v15_18") or "llm_calibration_contract_v15_18"),
            "metadata": raw,
        })
    return normalized


def normalize_intervention_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal": first_present(plan, "goal", "summary", "text", "warning", default="intervention candidate"),
        "condition_text": first_present(plan, "condition_text", "condition", default=None),
        "current_trajectory": first_present(plan, "current_trajectory", "current_path", default=None),
        "desired_trajectory": first_present(plan, "desired_trajectory", "desired_path", default=None),
        "actions": as_list(first_present(plan, "actions", "action_plan", default=[])),
        "expected_effects": as_list(first_present(plan, "expected_effects", "expected_effect", default=[])),
        "risks": as_list(first_present(plan, "risks", "risk", default=[])),
        "verification_plan": as_list(first_present(plan, "verification_plan", default=[])),
        "confidence": first_present(plan, "confidence", default=0.55),
        "raw": plan,
    }


def memory_usability(*, truth_status: str | None, lifecycle_status: str | None, confidence: float | None, evidence_count: int | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Gate whether memory can influence live decisions."""
    metadata = metadata or {}
    truth = (truth_status or "").lower()
    lifecycle = (lifecycle_status or "active").lower()
    conf = clamp(confidence, default=0.0)
    ev = int(evidence_count or 0)
    if lifecycle in {"contradicted", "obsolete", "rejected", "revoked"} or truth in {"contradicted", "obsolete"}:
        return {"usable_score": 0.0, "use_policy": "forbidden", "reason": "contradicted_or_obsolete"}
    if metadata.get("do_not_overpsychologize") or metadata.get("memory_use_policy") == "style_context_only":
        return {"usable_score": min(conf, 0.65), "use_policy": "style_context_only", "reason": "expression_style_not_fact"}
    if metadata.get("confirmed_by_user") or truth == "confirmed_by_user":
        return {"usable_score": max(conf, 0.9), "use_policy": "proactive_allowed", "reason": "confirmed_by_user"}
    if truth in {"consolidated", "observed"} and (ev >= 2 or conf >= 0.7):
        return {"usable_score": max(conf, 0.7), "use_policy": "proactive_allowed", "reason": "observed_or_consolidated"}
    if truth == "inferred" and conf >= 0.75 and ev >= 3:
        return {"usable_score": conf, "use_policy": "watch_or_soft_suggestion", "reason": "repeated_inference"}
    return {"usable_score": min(conf, 0.55), "use_policy": "silent_context", "reason": "weak_or_single_model_hypothesis"}
