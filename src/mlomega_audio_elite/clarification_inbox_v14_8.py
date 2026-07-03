from __future__ import annotations

"""V14.8 Smart Clarification Inbox + Natural Correction Router.

This layer is deliberately *not* another notification channel. It is the trust
boundary for questions the system should ask the user only when a hypothesis is
important enough, risky enough, or blocking enough that waiting for future
conversation evidence is worse than asking.

It solves the missing product loop:
- V14.5/6/7 can create hypotheses about unknown voices, relationships,
  emotions, jokes, other-person states, interventions and self-model objects.
- Some hypotheses can be resolved automatically by future conversations.
- Some are sensitive and should never be silently confirmed.
- The user needs one central inbox and one natural-language answer command.

Principles:
- Ask rarely. Prefer watch/wait when future evidence can resolve the ambiguity.
- Never put administrative clarification questions into the proactive
  intervention inbox unless they matter for immediate action.
- Keep identity and relationship truth boundaries: hypotheses stay pending until
  explicit user confirmation.
- Use Qwen/Ollama JSON contracts for cognitive interpretation. This module does
  not use regex/keyword rules to decide identities, emotions or intentions.
"""

from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .proactive_interventions_v14_7 import ensure_v14_7_schema
from .utils import json_dumps, json_loads, now_iso, stable_id

V14_8_VERSION = "14.8.0-smart-clarification-inbox-final"

V14_8_TABLES = {
    "v14_8_clarification_runs",
    "v14_8_clarification_items",
    "v14_8_clarification_answers",
    "v14_8_clarification_policies",
    "v14_8_clarification_resolution_attempts",
    "v14_8_clarification_exports",
    "v14_8_contract_checks",
}

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "ask_rarely",
    "min_queue_confidence": 0.50,
    "min_ask_now_confidence": 0.62,
    "max_new_questions_per_run": 3,
    "max_open_questions": 12,
    "prefer_wait_when_future_evidence_likely": True,
    "never_ask_low_value_curiosity": True,
    "ask_identity_only_when_confident_or_repeated": True,
    "identity_requires_explicit_user_confirmation": True,
    "relationship_requires_explicit_user_confirmation": True,
    "sensitive_mindreading_stays_hypothesis": True,
    "default_status_for_nonurgent": "watching",
    "allowed_ask_types": [
        "voice_identity",
        "relationship_confirmation",
        "emotion_or_tone_correction",
        "joke_irony_correction",
        "self_model_correction",
        "important_open_loop_confirmation",
        "intervention_preference",
        "prediction_outcome_confirmation",
        "other_person_model_boundary",
    ],
}

CLARIFICATION_SCHEMA: dict[str, Any] = {
    "clarification_items": [
        {
            "source_table": "",
            "source_id": "",
            "conversation_id": None,
            "clarification_type": "voice_identity|relationship_confirmation|emotion_or_tone_correction|joke_irony_correction|self_model_correction|important_open_loop_confirmation|intervention_preference|prediction_outcome_confirmation|other_person_model_boundary|other",
            "title": "",
            "question_text": "",
            "why_needed": "",
            "why_can_wait": "",
            "what_the_system_should_watch_next": [],
            "possible_answers": [],
            "ask_now": False,
            "recommended_status": "watching|queued|skip|auto_resolved",
            "priority": "low|medium|high|critical",
            "risk_if_wrong": "",
            "risk_if_asked_too_early": "",
            "user_burden": "low|medium|high",
            "confidence": 0.0,
        }
    ],
    "do_not_ask": [
        {
            "source_table": "",
            "source_id": "",
            "reason": "",
            "what_to_watch_instead": [],
        }
    ],
    "summary": "",
    "confidence": 0.0,
}

ANSWER_SCHEMA: dict[str, Any] = {
    "understood": False,
    "answer_summary": "",
    "answer_type": "confirm|reject|correct|clarify|dismiss|needs_more_context|other",
    "target_actions": [
        {
            "action": "confirm_voice_identity|reject_voice_identity|confirm_relationship|reject_relationship|correct_emotion_or_tone|mark_joke_or_irony|correct_self_model|dismiss_clarification|add_user_note|other",
            "target_table": "",
            "target_id": "",
            "cluster_id": None,
            "person_id": None,
            "display_name": None,
            "relation_to_user": None,
            "is_user": False,
            "patch": {},
            "confidence": 0.0,
        }
    ],
    "followup_needed": False,
    "followup_question": "",
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


def _compact(rows: list[dict[str, Any]], limit: int = 80) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        d: dict[str, Any] = {}
        for k, v in row.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > 1600:
                d[k] = v[:1600] + "…"
            else:
                d[k] = v
        out.append(d)
    return out


def _default_user(con) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["person_id"] if row else "me"


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any], timeout: int = 420) -> dict[str, Any]:
    data = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=timeout)
    if not isinstance(data, dict):
        raise RuntimeError("Brain2 V14.8 returned non-object JSON")
    return data


def ensure_v14_8_schema() -> None:
    ensure_v14_7_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_8_clarification_runs(
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                person_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                status TEXT NOT NULL,
                candidate_count INTEGER DEFAULT 0,
                queued_count INTEGER DEFAULT 0,
                watching_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                error_text TEXT,
                qwen_output_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_8_clarification_items(
                item_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                conversation_id TEXT,
                source_table TEXT,
                source_id TEXT,
                clarification_type TEXT NOT NULL,
                title TEXT,
                question_text TEXT NOT NULL,
                why_needed TEXT,
                why_can_wait TEXT,
                watch_next_json TEXT DEFAULT '[]',
                possible_answers_json TEXT DEFAULT '[]',
                ask_now INTEGER DEFAULT 0,
                priority TEXT DEFAULT 'low',
                risk_if_wrong TEXT,
                risk_if_asked_too_early TEXT,
                user_burden TEXT DEFAULT 'medium',
                confidence REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'watching',
                asked_at TEXT,
                answered_at TEXT,
                answer_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_8_clarification_answers(
                answer_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                answer_type TEXT,
                understood INTEGER DEFAULT 0,
                interpretation_json TEXT DEFAULT '{}',
                actions_json TEXT DEFAULT '[]',
                status TEXT NOT NULL,
                error_text TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_8_clarification_policies(
                person_id TEXT PRIMARY KEY,
                policy_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_8_clarification_resolution_attempts(
                attempt_id TEXT PRIMARY KEY,
                item_id TEXT,
                person_id TEXT NOT NULL,
                attempt_type TEXT NOT NULL,
                source_conversation_id TEXT,
                result_status TEXT NOT NULL,
                result_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_8_clarification_exports(
                export_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                output_path TEXT NOT NULL,
                item_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_8_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v148_items_person_status ON v14_8_clarification_items(person_id, status, priority, updated_at);
            CREATE INDEX IF NOT EXISTS idx_v148_items_source ON v14_8_clarification_items(source_table, source_id, clarification_type);
            CREATE INDEX IF NOT EXISTS idx_v148_answers_item ON v14_8_clarification_answers(item_id, created_at);
            """
        )
        now = now_iso()
        for name in [
            "ask_rarely_policy",
            "natural_language_correction_router",
            "identity_and_relationship_trust_boundary",
            "watch_before_asking_when_future_evidence_likely",
        ]:
            upsert(con, "v14_8_contract_checks", {
                "check_id": stable_id("v148check", name),
                "check_name": name,
                "status": "ok",
                "detail": "V14.8 clarification inbox installed.",
                "created_at": now,
            }, "check_id")
        con.commit()


def get_clarification_policy(person_id: str = "me") -> dict[str, Any]:
    ensure_v14_8_schema()
    now = now_iso()
    with connect() as con:
        row = con.execute("SELECT policy_json FROM v14_8_clarification_policies WHERE person_id=?", (person_id,)).fetchone()
        if row:
            policy = _safe_json(row["policy_json"], DEFAULT_POLICY.copy())
        else:
            policy = DEFAULT_POLICY.copy()
            upsert(con, "v14_8_clarification_policies", {
                "person_id": person_id,
                "policy_json": json_dumps(policy),
                "created_at": now,
                "updated_at": now,
            }, "person_id")
            con.commit()
    return {"version": V14_8_VERSION, "person_id": person_id, "policy": policy}


def update_clarification_policy(person_id: str = "me", *, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    current = get_clarification_policy(person_id)["policy"]
    if patch:
        current.update(patch)
    now = now_iso()
    with connect() as con:
        upsert(con, "v14_8_clarification_policies", {
            "person_id": person_id,
            "policy_json": json_dumps(current),
            "created_at": now,
            "updated_at": now,
        }, "person_id")
        con.commit()
    return {"version": V14_8_VERSION, "person_id": person_id, "policy": current}


def _collect_candidates(con, *, person_id: str, conversation_id: str | None, limit: int) -> dict[str, Any]:
    conv_clause = " AND conversation_id=?" if conversation_id else ""
    conv_params: tuple[Any, ...] = (conversation_id,) if conversation_id else ()
    return {
        "pending_voice_identity_hypotheses": _compact(_many(con, "SELECT * FROM v14_5_people_identity_hypotheses WHERE status IN ('pending_confirmation','hypothesis')" + conv_clause + " ORDER BY confidence DESC, updated_at DESC LIMIT ?", (*conv_params, limit)), limit),
        "relationship_hypotheses": _compact(_many(con, "SELECT * FROM v14_5_relationship_inference_cards WHERE status IN ('hypothesis','pending_confirmation') ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,)), limit),
        "people_context_profiles": _compact(_many(con, "SELECT * FROM v14_5_people_context_profiles ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,)), limit),
        "active_questions": _compact(_many(con, "SELECT * FROM v14_5_active_questions WHERE person_id=? AND status IN ('open','watching','active') ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "personal_open_loops": _compact(_many(con, "SELECT * FROM v14_5_personal_open_loops WHERE person_id=? AND current_status IN ('active','stalled','unclear') ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "interpersonal_hypotheses": _compact(_many(con, "SELECT * FROM v14_6_person_model_summaries WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact(_many(con, "SELECT * FROM v14_6_other_person_state_snapshots WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "interventions_needing_feedback": _compact(_many(con, "SELECT * FROM v14_7_intervention_queue WHERE person_id=? AND status IN ('ready','pending','snoozed') ORDER BY created_at DESC LIMIT ?", (person_id, min(limit, 20))), min(limit, 20)),
        "blindspots_and_open_questions": _compact(_many(con, "SELECT * FROM v14_open_questions WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit) + _compact(_many(con, "SELECT * FROM v14_blindspot_hypotheses WHERE person_id=? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (person_id, limit)), limit),
        "existing_clarifications": _compact(_many(con, "SELECT * FROM v14_8_clarification_items WHERE person_id=? AND status IN ('queued','watching','asked') ORDER BY updated_at DESC LIMIT ?", (person_id, 80)), 80),
        "recent_answers": _compact(_many(con, "SELECT * FROM v14_8_clarification_answers WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, 30)), 30),
    }


def _status_from_policy(item: dict[str, Any], policy: dict[str, Any], queued_so_far: int) -> str:
    recommended = str(item.get("recommended_status") or "watching")
    if recommended in {"skip", "auto_resolved"}:
        return recommended
    confidence = _clamp(item.get("confidence"))
    ask_now = bool(item.get("ask_now"))
    priority = str(item.get("priority") or "low")
    burden = str(item.get("user_burden") or "medium")
    max_new = int(policy.get("max_new_questions_per_run", 3) or 3)
    if ask_now and confidence >= float(policy.get("min_ask_now_confidence", 0.62)) and queued_so_far < max_new and burden != "high":
        return "queued"
    if priority in {"high", "critical"} and confidence >= float(policy.get("min_queue_confidence", 0.5)) and queued_so_far < max_new and burden != "high":
        return "queued"
    return str(policy.get("default_status_for_nonurgent", "watching"))


def run_clarification_inbox(conversation_id: str | None = None, *, person_id: str | None = None, trigger_type: str = "direct_flow", limit: int = 80) -> dict[str, Any]:
    ensure_v14_8_schema()
    run_id = stable_id("v148run", conversation_id or "global", person_id or "auto", trigger_type, now_iso())
    now = now_iso()
    with connect() as con:
        person_id = person_id or _default_user(con)
        policy = get_clarification_policy(person_id)["policy"]
        candidates = _collect_candidates(con, person_id=person_id, conversation_id=conversation_id, limit=limit)
    try:
        payload = {
            "version": V14_8_VERSION,
            "mission": "Créer une inbox de clarification intelligente. Ne pose pas de questions par curiosité. Demande seulement si la réponse est nécessaire pour éviter une erreur importante, confirmer une identité/relation sensible, corriger le self-model, ou débloquer une intervention/prédiction. Si une future conversation peut probablement résoudre l'ambiguïté, mets l'item en watching et explique quoi surveiller.",
            "person_id": person_id,
            "conversation_id": conversation_id,
            "trigger_type": trigger_type,
            "policy": policy,
            "candidates": candidates,
        }
        data = _llm_json(
            "Tu es le moteur V14.8 Clarification Inbox. Tu poses peu de questions. Tu préfères attendre les preuves futures quand c'est raisonnable. Tu ne confirmes jamais une identité ou une relation sensible sans réponse explicite de l'utilisateur. Réponds uniquement en JSON conforme au schéma.",
            payload,
            CLARIFICATION_SCHEMA,
            timeout=420,
        )
        items = data.get("clarification_items") if isinstance(data.get("clarification_items"), list) else []
        queued = watching = skipped = 0
        with connect() as con:
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                source_table = str(raw.get("source_table") or "unknown")
                source_id = str(raw.get("source_id") or stable_id("v148source", raw.get("title") or raw.get("question_text") or now))
                ctype = str(raw.get("clarification_type") or "other")
                status = _status_from_policy(raw, policy, queued)
                if status == "skip":
                    skipped += 1
                    continue
                if status == "queued":
                    queued += 1
                    asked_at = now
                else:
                    watching += 1
                    asked_at = None
                item_id = stable_id("v148item", person_id, source_table, source_id, ctype)
                upsert(con, "v14_8_clarification_items", {
                    "item_id": item_id,
                    "person_id": person_id,
                    "conversation_id": raw.get("conversation_id") or conversation_id,
                    "source_table": source_table,
                    "source_id": source_id,
                    "clarification_type": ctype,
                    "title": raw.get("title") or ctype,
                    "question_text": raw.get("question_text") or "Clarification nécessaire.",
                    "why_needed": raw.get("why_needed"),
                    "why_can_wait": raw.get("why_can_wait"),
                    "watch_next_json": json_dumps(raw.get("what_the_system_should_watch_next") or []),
                    "possible_answers_json": json_dumps(raw.get("possible_answers") or []),
                    "ask_now": 1 if bool(raw.get("ask_now")) else 0,
                    "priority": raw.get("priority") or "low",
                    "risk_if_wrong": raw.get("risk_if_wrong"),
                    "risk_if_asked_too_early": raw.get("risk_if_asked_too_early"),
                    "user_burden": raw.get("user_burden") or "medium",
                    "confidence": _clamp(raw.get("confidence")),
                    "status": status,
                    "asked_at": asked_at,
                    "answered_at": None,
                    "answer_summary": None,
                    "created_at": now,
                    "updated_at": now,
                }, "item_id")
            upsert(con, "v14_8_clarification_runs", {
                "run_id": run_id,
                "conversation_id": conversation_id,
                "person_id": person_id,
                "trigger_type": trigger_type,
                "status": "ok",
                "candidate_count": sum(len(v) for v in candidates.values() if isinstance(v, list)),
                "queued_count": queued,
                "watching_count": watching,
                "skipped_count": skipped,
                "error_text": None,
                "qwen_output_json": json_dumps(data),
                "created_at": now,
            }, "run_id")
            con.commit()
        return {"version": V14_8_VERSION, "run_id": run_id, "person_id": person_id, "queued": queued, "watching": watching, "skipped": skipped}
    except Exception as exc:
        with connect() as con:
            upsert(con, "v14_8_clarification_runs", {
                "run_id": run_id,
                "conversation_id": conversation_id,
                "person_id": person_id or "me",
                "trigger_type": trigger_type,
                "status": "error",
                "candidate_count": 0,
                "queued_count": 0,
                "watching_count": 0,
                "skipped_count": 0,
                "error_text": str(exc)[:2000],
                "qwen_output_json": "{}",
                "created_at": now,
            }, "run_id")
            con.commit()
        return {"version": V14_8_VERSION, "run_id": run_id, "status": "error", "error": str(exc)[:500]}


def list_clarifications(*, person_id: str | None = None, status: str | None = "queued", limit: int = 50) -> dict[str, Any]:
    ensure_v14_8_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
        clauses = ["person_id=?"]
        params: list[Any] = [person_id]
        if status and status != "all":
            clauses.append("status=?")
            params.append(status)
        params.append(limit)
        rows = _many(con, "SELECT * FROM v14_8_clarification_items WHERE " + " AND ".join(clauses) + " ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, updated_at DESC LIMIT ?", tuple(params))
    return {"version": V14_8_VERSION, "person_id": person_id, "status": status, "items": rows}


def _apply_specific_action(action: dict[str, Any], *, item: dict[str, Any], answer_text: str, person_id: str) -> dict[str, Any]:
    now = now_iso()
    act = str(action.get("action") or "")
    result: dict[str, Any] = {"action": act, "status": "ignored"}
    if act == "confirm_voice_identity":
        cluster_id = action.get("cluster_id") or item.get("source_id")
        person_id = action.get("person_id")
        display_name = action.get("display_name") or person_id
        if cluster_id and person_id:
            from .voice_learning import name_unknown_voice
            named = name_unknown_voice(str(cluster_id), str(person_id), display_name=str(display_name), is_user=bool(action.get("is_user", False)), reason="v14_8_natural_clarification_answer")
            result = {"action": act, "status": "applied", "result": named}
    elif act in {"reject_voice_identity", "confirm_relationship", "reject_relationship", "correct_emotion_or_tone", "mark_joke_or_irony", "correct_self_model", "add_user_note", "dismiss_clarification"}:
        target_table = str(action.get("target_table") or item.get("source_table") or "v14_8_clarification_items")
        target_id = str(action.get("target_id") or item.get("source_id") or item.get("item_id"))
        patch = action.get("patch") if isinstance(action.get("patch"), dict) else {}
        with connect() as con:
            previous = {}
            try:
                row = con.execute(f"SELECT * FROM {target_table} WHERE {target_table[:-1] if target_table.endswith('s') else 'id'}=?", (target_id,)).fetchone()
                previous = dict(row) if row else {}
            except Exception:
                previous = {}
            if target_table == "v14_5_people_identity_hypotheses" and act == "reject_voice_identity":
                try:
                    con.execute("UPDATE v14_5_people_identity_hypotheses SET status='rejected', updated_at=? WHERE hypothesis_id=?", (now, target_id))
                except Exception:
                    pass
            if target_table == "v14_5_relationship_inference_cards" and act in {"confirm_relationship", "reject_relationship"}:
                try:
                    con.execute("UPDATE v14_5_relationship_inference_cards SET status=?, updated_at=? WHERE card_id=?", ("confirmed" if act == "confirm_relationship" else "rejected", now, target_id))
                except Exception:
                    pass
            upsert(con, "model_revisions", {
                "model_revision_id": stable_id("modelrev", "v148", item.get("item_id"), act, now),
                "target_table": target_table,
                "target_id": target_id,
                "revision_type": act,
                "previous_json": json_dumps(previous),
                "new_json": json_dumps({"patch": patch, "answer_text": answer_text, "relation_to_user": action.get("relation_to_user")}),
                "reason": "v14_8_natural_clarification_answer",
                "evidence_json": json_dumps([{"clarification_item_id": item.get("item_id"), "answer": answer_text}]),
                "created_at": now,
            }, "model_revision_id")
            con.commit()
        memory_revision_result = None
        memory_revision_error = None
        if act in {"reject_voice_identity", "reject_relationship", "correct_emotion_or_tone", "mark_joke_or_irony", "correct_self_model", "add_user_note"}:
            try:
                from importlib import import_module
                _memory_correction = import_module("." + "memory_correction", package=__package__)
                revision_type = "invalidate" if act in {"reject_voice_identity", "reject_relationship"} else "correction"
                memory_revision_result = _memory_correction.revise_memory(
                    target_table=target_table,
                    target_id=target_id,
                    revision_type=revision_type,
                    reason=f"v14_8_user_answer_priority:{act}",
                    patch={**patch, "user_answer": answer_text, "confirmed_by_user": True, "clarification_action": act},
                    confidence=float(action.get("confidence") or 1.0),
                    person_id=person_id,
                )
            except Exception as exc:
                memory_revision_error = str(exc)[:500]
        result = {"action": act, "status": "recorded_revision", "target_table": target_table, "target_id": target_id, "memory_revision": memory_revision_result, "memory_revision_error": memory_revision_error}
    return result


def answer_clarification(item_id: str, answer_text: str, *, person_id: str | None = None) -> dict[str, Any]:
    ensure_v14_8_schema()
    now = now_iso()
    with connect() as con:
        row = con.execute("SELECT * FROM v14_8_clarification_items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError(f"clarification introuvable: {item_id}")
        item = dict(row)
        person_id = person_id or item.get("person_id") or _default_user(con)
    payload = {
        "version": V14_8_VERSION,
        "mission": "Interprète la réponse naturelle de l'utilisateur à une question de clarification. Applique uniquement les actions clairement confirmées. Si l'utilisateur est ambigu, demande une suite au lieu de modifier une identité/relation sensible.",
        "clarification_item": item,
        "answer_text": answer_text,
        "rules": {
            "identity_requires_explicit_confirmation": True,
            "do_not_invent_person_id": True,
            "if_answer_is_unclear_return_needs_more_context": True,
        },
    }
    try:
        data = _llm_json(
            "Tu es le routeur de correction naturelle V14.8. Réponds uniquement en JSON. Ne force jamais une identité ou relation si la réponse utilisateur n'est pas explicite.",
            payload,
            ANSWER_SCHEMA,
            timeout=300,
        )
        actions = data.get("target_actions") if isinstance(data.get("target_actions"), list) else []
        applied: list[dict[str, Any]] = []
        for action in actions:
            if isinstance(action, dict):
                applied.append(_apply_specific_action(action, item=item, answer_text=answer_text, person_id=person_id))
        status = "answered" if not data.get("followup_needed") else "needs_followup"
        answer_id = stable_id("v148answer", item_id, answer_text, now)
        with connect() as con:
            upsert(con, "v14_8_clarification_answers", {
                "answer_id": answer_id,
                "item_id": item_id,
                "person_id": person_id,
                "answer_text": answer_text,
                "answer_type": data.get("answer_type"),
                "understood": 1 if bool(data.get("understood")) else 0,
                "interpretation_json": json_dumps(data),
                "actions_json": json_dumps(applied),
                "status": status,
                "error_text": None,
                "created_at": now,
            }, "answer_id")
            con.execute("UPDATE v14_8_clarification_items SET status=?, answered_at=?, answer_summary=?, updated_at=? WHERE item_id=?", (status, now, data.get("answer_summary") or answer_text[:500], now, item_id))
            con.commit()
        return {"version": V14_8_VERSION, "item_id": item_id, "answer_id": answer_id, "status": status, "interpretation": data, "applied_actions": applied}
    except Exception as exc:
        answer_id = stable_id("v148answer", item_id, "error", now)
        with connect() as con:
            upsert(con, "v14_8_clarification_answers", {
                "answer_id": answer_id,
                "item_id": item_id,
                "person_id": person_id or "me",
                "answer_text": answer_text,
                "answer_type": "error",
                "understood": 0,
                "interpretation_json": "{}",
                "actions_json": "[]",
                "status": "error",
                "error_text": str(exc)[:2000],
                "created_at": now,
            }, "answer_id")
            con.commit()
        return {"version": V14_8_VERSION, "item_id": item_id, "status": "error", "error": str(exc)[:500]}


def export_clarification_inbox(*, person_id: str | None = None, output_dir: str | Path | None = None, limit: int = 50) -> dict[str, Any]:
    ensure_v14_8_schema()
    with connect() as con:
        person_id = person_id or _default_user(con)
    rows = list_clarifications(person_id=person_id, status="queued", limit=limit)["items"]
    watching = list_clarifications(person_id=person_id, status="watching", limit=limit)["items"]
    out_dir = Path(output_dir) if output_dir else get_settings().data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"clarification_inbox_{person_id}.md"
    lines: list[str] = []
    lines.append(f"# Clarification Inbox — {person_id}")
    lines.append("")
    lines.append("Ces questions ne sont pas des interventions. Elles servent à corriger/valider le modèle quand le système ne doit pas décider seul.")
    lines.append("")
    lines.append("## À répondre")
    if not rows:
        lines.append("Aucune question urgente.")
    for r in rows:
        lines.append("")
        lines.append(f"### {r.get('item_id')} — {r.get('title') or r.get('clarification_type')}")
        lines.append(f"Question: {r.get('question_text')}")
        lines.append(f"Pourquoi: {r.get('why_needed') or ''}")
        lines.append(f"Priorité: {r.get('priority')} — confiance: {r.get('confidence')}")
        lines.append(f"Répondre: mlomega-audio v14-answer {r.get('item_id')} \"votre réponse\"")
    lines.append("")
    lines.append("## En surveillance, pas encore demandé")
    if not watching:
        lines.append("Aucune ambiguïté en surveillance.")
    for r in watching[:20]:
        lines.append(f"- {r.get('title') or r.get('clarification_type')}: {r.get('question_text')} — attend: {r.get('why_can_wait') or 'preuves futures'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    export_id = stable_id("v148export", person_id, str(path), now_iso())
    with connect() as con:
        upsert(con, "v14_8_clarification_exports", {
            "export_id": export_id,
            "person_id": person_id,
            "output_path": str(path),
            "item_count": len(rows),
            "created_at": now_iso(),
        }, "export_id")
        con.commit()
    return {"version": V14_8_VERSION, "person_id": person_id, "path": str(path), "queued_count": len(rows), "watching_count": len(watching)}


def audit_v14_8(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_8_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_8_TABLES - tables)
        counts: dict[str, int | None] = {}
        for table in sorted(V14_8_TABLES):
            try:
                counts[table] = int(con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            except Exception:
                counts[table] = None
    return {
        "version": V14_8_VERSION,
        "ok": not missing,
        "missing_tables": missing,
        "required_tables": sorted(V14_8_TABLES),
        "counts": counts,
        "capabilities": [
            "smart_clarification_inbox",
            "ask_rarely_policy",
            "watch_before_asking",
            "natural_language_answers",
            "identity_relation_confirmation_boundary",
            "self_model_correction_router",
        ],
        "commands": [
            "mlomega-audio v14-8-audit",
            "mlomega-audio v14-clarifications --person-id me",
            "mlomega-audio v14-clarification-export --person-id me",
            "mlomega-audio v14-answer <item_id> \"Oui ...\"",
            "mlomega-audio v14-clarification-policy --person-id me --patch '{...}'",
        ],
    }

# V18 remediation: all user corrections are explicit-owner and cannot mutate a
# target from another owner scope.
from .v18_interactions import install_clarifications as _install_v18_clarifications
_globals_v18_clarifications = _install_v18_clarifications(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_clarifications)
