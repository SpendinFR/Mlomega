from __future__ import annotations

"""V14.2 Brain 2.0 Router + Vector Fusion + Noise-Aware Selection.

This is the final safety layer above raw recall, V13 prediction, and V14 pattern
mirror. It does not replace any previous layer. It adds four things:

1. Natural route -> SQL structured candidates -> semantic vector candidates ->
   fusion/ranking -> grounded answer.
2. Noise-aware candidate scoring so high-volume 24/24 audio does not turn every
   repeated phrase into a deep pattern.
3. Explicit vector-search run records, including errors when Qdrant/LanceDB or
   embeddings are unavailable.
4. No regex routing in this layer. Natural interpretation is Qwen JSON-contract
   based; selection/ranking uses structured fields, scores, timestamps and vector
   hits.
"""

from typing import Any

from .db import connect, init_db, upsert
from .llm import OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, sha256_bytes, stable_id
from .pattern_mirror_v14 import pattern_mirror_digest
from .brain2_router_v14_1 import (
    ANSWER_SCHEMA,
    ROUTER_SCHEMA,
    route_question as route_question_v14_1,
    select_candidates as select_candidates_v14_1,
    _clamp,
    _default_user,
    _hash_payload,
    _many,
    _one_value,
)

V14_2_VERSION = "14.2.0-brain2-vector-quality-final"

V14_2_TABLES = {
    "v14_2_vector_search_runs",
    "v14_2_vector_candidates",
    "v14_2_fusion_runs",
    "v14_2_fused_candidates",
    "v14_2_noise_guardrail_reports",
    "v14_2_selection_signal_scores",
    "v14_2_answer_packets",
    "v14_2_contract_checks",
}

VECTOR_RUN_SCHEMA: dict[str, Any] = {
    "vector_needed": True,
    "semantic_query": "",
    "why_vector_needed": "",
    "risk_if_vector_missing": "low|medium|high",
}

FUSION_AUDIT_SCHEMA: dict[str, Any] = {
    "selected_items": [
        {"candidate_id": "", "reason": "", "priority": "low|medium|high|critical", "confidence": 0.0}
    ],
    "missed_risk": "low|medium|high",
    "noise_warnings": [],
    "what_should_not_be_overinterpreted": [],
}


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any], timeout: int = 360) -> dict[str, Any]:
    data = OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=timeout)
    if not isinstance(data, dict):
        raise RuntimeError("Brain2 V14.2 returned non-object JSON")
    return data


def _safe_json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    return json_loads(str(value), default if default is not None else {})


def _text_from_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ["text", "summary", "content", "title", "evidence_text", "predicted_value", "outcome_summary"]:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    for key in ["text", "summary", "content"]:
        val = metadata.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _word_count(text: str) -> int:
    # Deliberately not regex. This is only a rough length/noise feature.
    return len([p for p in text.replace("\n", " ").split(" ") if p.strip()])


def _score_signals(candidate: dict[str, Any]) -> dict[str, Any]:
    """Compute structured, non-psychological ranking signals.

    This does not decide meaning. It protects against noise by downranking items
    that are short, low confidence, unsupported, or too repetitive without
    evidence/outcome/counterexample signals.
    """
    payload = _safe_json(candidate.get("payload_json"), {}) or {}
    if not payload and isinstance(candidate.get("payload"), dict):
        payload = candidate.get("payload") or {}
    text = _text_from_payload(payload) or str(candidate.get("text") or "")
    wc = _word_count(text)
    source_kind = str(candidate.get("source_kind") or candidate.get("source_type") or "unknown")
    base = _clamp(candidate.get("score") or 0.0)
    confidence = _clamp(payload.get("confidence") or payload.get("probability") or payload.get("quality_score") or 0.5)
    evidence_count = 0
    for key in ["evidence_count", "support_count", "case_count", "occurrence_count", "frequency"]:
        try:
            evidence_count = max(evidence_count, int(payload.get(key) or 0))
        except Exception:
            pass
    has_outcome = bool(payload.get("outcome") or payload.get("outcome_id") or payload.get("result") or source_kind in {"outcome", "prediction_result"})
    has_counter = bool(payload.get("counter_evidence") or payload.get("counterexamples") or payload.get("counter_evidence_json"))
    status_text = str(payload.get("status") or payload.get("event_status") or payload.get("lifecycle_status") or "")
    is_open = status_text in {"open", "pending", "proposed", "weak_intention", "strong_intention", "committed"}
    is_old_proof = source_kind in {"raw_turn", "source_span", "conversation", "episode", "memory_card", "lifestream_segment", "life_event"}
    length_signal = 0.10 if wc >= 18 else 0.04 if wc >= 8 else -0.08
    evidence_signal = min(0.18, evidence_count * 0.025)
    outcome_signal = 0.10 if has_outcome else 0.0
    counter_signal = 0.08 if has_counter else 0.0
    open_loop_signal = 0.11 if is_open else 0.0
    proof_signal = 0.06 if is_old_proof else 0.0
    repeated_noise_penalty = 0.0
    if source_kind in {"language", "pattern", "v14_chain"} and evidence_count < 4 and not has_outcome:
        repeated_noise_penalty = 0.12
    short_noise_penalty = 0.10 if wc <= 4 and source_kind not in {"prediction_result", "outcome"} else 0.0
    final = _clamp(base + 0.10 * confidence + length_signal + evidence_signal + outcome_signal + counter_signal + open_loop_signal + proof_signal - repeated_noise_penalty - short_noise_penalty)
    return {
        "base_score": base,
        "confidence_signal": confidence,
        "word_count": wc,
        "evidence_count": evidence_count,
        "has_outcome": has_outcome,
        "has_counter_evidence": has_counter,
        "is_open_loop": is_open,
        "is_old_proof": is_old_proof,
        "repeated_noise_penalty": repeated_noise_penalty,
        "short_noise_penalty": short_noise_penalty,
        "final_score": final,
    }


def ensure_v14_2_schema() -> None:
    init_db()
    # Also ensure V14.1 tables exist because V14.2 fuses its candidates.
    from .brain2_router_v14_1 import ensure_v14_1_schema
    ensure_v14_1_schema()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS v14_2_vector_search_runs(
                vector_run_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                semantic_query TEXT NOT NULL,
                backend TEXT,
                status TEXT NOT NULL,
                error_text TEXT,
                hit_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_2_vector_candidates(
                vector_candidate_id TEXT PRIMARY KEY,
                vector_run_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                source_type TEXT,
                source_id TEXT,
                vector_score REAL DEFAULT 0,
                text TEXT,
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_2_fusion_runs(
                fusion_run_id TEXT PRIMARY KEY,
                selection_run_id TEXT,
                vector_run_id TEXT,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                sql_candidate_count INTEGER DEFAULT 0,
                vector_candidate_count INTEGER DEFAULT 0,
                fused_candidate_count INTEGER DEFAULT 0,
                qwen_fusion_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_2_fused_candidates(
                fused_candidate_id TEXT PRIMARY KEY,
                fusion_run_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_table TEXT,
                source_id TEXT,
                came_from_sql INTEGER DEFAULT 0,
                came_from_vector INTEGER DEFAULT 0,
                sql_score REAL DEFAULT 0,
                vector_score REAL DEFAULT 0,
                fused_score REAL DEFAULT 0,
                ranking_signals_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_2_noise_guardrail_reports(
                report_id TEXT PRIMARY KEY,
                fusion_run_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                short_candidate_count INTEGER DEFAULT 0,
                low_evidence_pattern_count INTEGER DEFAULT 0,
                open_loop_count INTEGER DEFAULT 0,
                warning_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_2_selection_signal_scores(
                signal_id TEXT PRIMARY KEY,
                fused_candidate_id TEXT NOT NULL,
                signal_name TEXT NOT NULL,
                signal_value TEXT,
                weight REAL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_2_answer_packets(
                answer_id TEXT PRIMARY KEY,
                fusion_run_id TEXT,
                person_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v14_2_contract_checks(
                check_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v142_vector_person ON v14_2_vector_search_runs(person_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_v142_fused_score ON v14_2_fused_candidates(person_id, fused_score, created_at);
            CREATE INDEX IF NOT EXISTS idx_v142_fused_source ON v14_2_fused_candidates(source_table, source_id);
            """
        )
        now = now_iso()
        for name in sorted(V14_2_TABLES):
            upsert(con, "v14_2_contract_checks", {
                "check_id": stable_id("v142check", name),
                "check_name": f"table:{name}",
                "status": "declared",
                "detail": "V14.2 vector fusion + noise-aware selection table required.",
                "created_at": now,
            }, "check_id")
        con.commit()


def route_question(question: str, *, person_id: str | None = None) -> dict[str, Any]:
    ensure_v14_2_schema()
    return route_question_v14_1(question, person_id=person_id)


def _decide_vector_need(question: str, route: dict[str, Any]) -> dict[str, Any]:
    try:
        return _llm_json(
            "Tu es le planificateur vectoriel Brain2 V14.2. Réponds uniquement en JSON.",
            {
                "mission": "Décide si une recherche sémantique vectorielle doit compléter la sélection SQL. Réponds oui presque toujours si la question peut viser une ancienne conversation pertinente sans date exacte, une peur, une attirance, un thème, une phrase ou un souvenir sémantique.",
                "question": question,
                "route": route,
                "schema": VECTOR_RUN_SCHEMA,
            },
            VECTOR_RUN_SCHEMA,
            timeout=120,
        )
    except Exception as exc:
        # Fail safe: use vector search by default unless only a very precise temporal window was parsed.
        has_time = bool(route.get("time_filters"))
        return {
            "vector_needed": not has_time,
            "semantic_query": question,
            "why_vector_needed": f"fallback_after_vector_planner_error:{exc}",
            "risk_if_vector_missing": "medium",
        }


def _run_vector_search(question: str, *, person_id: str, route: dict[str, Any], limit: int) -> dict[str, Any]:
    ensure_v14_2_schema()
    now = now_iso()
    plan = _decide_vector_need(question, route)
    semantic_query = str(plan.get("semantic_query") or question)
    run_id = stable_id("v142vector", person_id, question, semantic_query, now)
    hits: list[dict[str, Any]] = []
    status = "skipped"
    error_text = None
    backend = None
    if bool(plan.get("vector_needed")):
        try:
            from .config import get_settings
            from .retrieval import search as vector_search
            settings = get_settings()
            backend = settings.vector_backend
            raw_hits = vector_search(semantic_query, limit=max(8, limit // 2), person_id=person_id)
            for hit in raw_hits:
                hits.append({
                    "source_type": hit.source_type,
                    "source_id": hit.source_id,
                    "score": float(hit.score),
                    "text": hit.text,
                    "metadata": hit.metadata,
                    "reason": hit.reason,
                })
            status = "ok"
        except Exception as exc:
            status = "error"
            error_text = str(exc)[:2000]
    with connect() as con:
        upsert(con, "v14_2_vector_search_runs", {
            "vector_run_id": run_id,
            "person_id": person_id,
            "question": question,
            "semantic_query": semantic_query,
            "backend": backend,
            "status": status,
            "error_text": error_text,
            "hit_count": len(hits),
            "created_at": now,
        }, "vector_run_id")
        for i, h in enumerate(hits):
            vcid = stable_id("v142vhit", run_id, i, h.get("source_type"), h.get("source_id"), h.get("score"))
            upsert(con, "v14_2_vector_candidates", {
                "vector_candidate_id": vcid,
                "vector_run_id": run_id,
                "person_id": person_id,
                "source_type": h.get("source_type"),
                "source_id": h.get("source_id"),
                "vector_score": float(h.get("score") or 0.0),
                "text": h.get("text"),
                "metadata_json": json_dumps({"metadata": h.get("metadata") or {}, "reason": h.get("reason"), "plan": plan}),
                "created_at": now,
            }, "vector_candidate_id")
        con.commit()
    return {"vector_run_id": run_id, "status": status, "error_text": error_text, "plan": plan, "hits": hits}


def _source_key(kind: str, table: str | None, source_id: str | None) -> str:
    # Origin (SQL/vector) is not part of an evidence identity.  The previous
    # key guaranteed that the exact same source became two prompt candidates.
    return f"{table or ''}|{source_id or ''}"


def _table_for_vector_source(source_type: str) -> str:
    mapping = {
        "turn": "turns",
        "analysis": "utterance_analyses",
        "memory_card": "memory_cards",
        "memory_frame": "memory_frames",
        "life_event": "life_events",
        "lifestream_segment": "lifestream_segments",
        "source_item": "source_items",
        "pattern": "patterns",
        "self_model": "self_model_facts",
    }
    return mapping.get(source_type, source_type or "vector")


def _fusion_rows(sql_candidates: list[dict[str, Any]], vector_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for c in sql_candidates:
        payload = _safe_json(c.get("payload_json"), {}) or {}
        kind = str(c.get("source_kind") or c.get("source_type") or "sql")
        table = str(c.get("source_table") or "")
        sid = str(c.get("source_id") or "")
        key = _source_key(kind, table, sid)
        item = fused.setdefault(key, {
            "source_kind": kind,
            "source_table": table,
            "source_id": sid,
            "came_from_sql": 0,
            "came_from_vector": 0,
            "sql_score": 0.0,
            "vector_score": 0.0,
            "payload": payload,
        })
        item["came_from_sql"] = 1
        item["sql_score"] = max(float(item.get("sql_score") or 0.0), float(c.get("score") or 0.0))
    for h in vector_hits:
        source_type = str(h.get("source_type") or "vector")
        table = _table_for_vector_source(source_type)
        sid = str(h.get("source_id") or h.get("id") or stable_id("v142vectorunknown", h.get("text") or ""))
        kind = source_type
        key = _source_key(kind, table, sid)
        payload = {"text": h.get("text"), "metadata": h.get("metadata") or {}, "source_type": source_type, "source_id": sid, "confidence": h.get("score")}
        item = fused.setdefault(key, {
            "source_kind": kind,
            "source_table": table,
            "source_id": sid,
            "came_from_sql": 0,
            "came_from_vector": 0,
            "sql_score": 0.0,
            "vector_score": 0.0,
            "payload": payload,
        })
        item["came_from_vector"] = 1
        item["vector_score"] = max(float(item.get("vector_score") or 0.0), float(h.get("score") or 0.0))
        if not _text_from_payload(item.get("payload") or {}):
            item["payload"] = payload
    return list(fused.values())


def select_candidates(question: str, *, person_id: str | None = None, route_payload: dict[str, Any] | None = None, limit: int = 80) -> dict[str, Any]:
    ensure_v14_2_schema()
    route_info = route_payload or route_question(question, person_id=person_id)
    route = route_info.get("route") or route_info
    person_id = str(person_id or route_info.get("person_id") or "me")
    base = select_candidates_v14_1(question, person_id=person_id, route_payload=route_info, limit=limit)
    vector = _run_vector_search(question, person_id=person_id, route=route, limit=limit)
    now = now_iso()
    fusion_run_id = stable_id("v142fusion", person_id, question, base.get("selection_run_id"), vector.get("vector_run_id"), now)
    fused_items = _fusion_rows(base.get("candidates") or [], vector.get("hits") or [])
    stored: list[dict[str, Any]] = []
    short_count = 0
    low_evidence_pattern_count = 0
    open_loop_count = 0
    with connect() as con:
        for item in fused_items:
            temp_candidate = {
                "score": max(float(item.get("sql_score") or 0.0), float(item.get("vector_score") or 0.0)),
                "source_kind": item.get("source_kind"),
                "payload_json": json_dumps(item.get("payload") or {}),
            }
            signals = _score_signals(temp_candidate)
            if signals["word_count"] <= 4:
                short_count += 1
            if signals["repeated_noise_penalty"] > 0:
                low_evidence_pattern_count += 1
            if signals["is_open_loop"]:
                open_loop_count += 1
            sql_score = float(item.get("sql_score") or 0.0)
            vector_score = float(item.get("vector_score") or 0.0)
            fused_score = _clamp(max(sql_score, vector_score) + 0.20 * signals["final_score"] + (0.08 if item.get("came_from_sql") and item.get("came_from_vector") else 0.0))
            fid = stable_id("v142fused", fusion_run_id, item.get("source_kind"), item.get("source_table"), item.get("source_id"))
            row = {
                "fused_candidate_id": fid,
                "fusion_run_id": fusion_run_id,
                "person_id": person_id,
                "source_kind": str(item.get("source_kind") or "unknown"),
                "source_table": item.get("source_table"),
                "source_id": item.get("source_id"),
                "came_from_sql": int(bool(item.get("came_from_sql"))),
                "came_from_vector": int(bool(item.get("came_from_vector"))),
                "sql_score": sql_score,
                "vector_score": vector_score,
                "fused_score": fused_score,
                "ranking_signals_json": json_dumps(signals),
                "payload_json": json_dumps(item.get("payload") or {}),
                "created_at": now,
            }
            upsert(con, "v14_2_fused_candidates", row, "fused_candidate_id")
            for name, value in signals.items():
                upsert(con, "v14_2_selection_signal_scores", {
                    "signal_id": stable_id("v142signal", fid, name),
                    "fused_candidate_id": fid,
                    "signal_name": str(name),
                    "signal_value": json_dumps(value),
                    "weight": float(signals.get("final_score") or 0.0) if name == "final_score" else 0.0,
                    "created_at": now,
                }, "signal_id")
            stored.append(row)
        stored = sorted(stored, key=lambda x: float(x.get("fused_score") or 0.0), reverse=True)[: limit * 3]
        warning = {
            "principle": "High-volume audio is not automatically high-truth. Repetition can be vocabulary, context, mood or noise; deep patterns need evidence/outcomes/counterexamples.",
            "short_candidate_count": short_count,
            "low_evidence_pattern_count": low_evidence_pattern_count,
            "open_loop_count": open_loop_count,
            "vector_status": vector.get("status"),
            "vector_error": vector.get("error_text"),
        }
        report_id = stable_id("v142noise", fusion_run_id)
        upsert(con, "v14_2_noise_guardrail_reports", {
            "report_id": report_id,
            "fusion_run_id": fusion_run_id,
            "person_id": person_id,
            "question": question,
            "short_candidate_count": short_count,
            "low_evidence_pattern_count": low_evidence_pattern_count,
            "open_loop_count": open_loop_count,
            "warning_json": json_dumps(warning),
            "created_at": now,
        }, "report_id")
        try:
            audit = _llm_json(
                "Tu es le fusionneur Brain2 V14.2. Choisis seulement parmi les candidats fournis. Réponds en JSON.",
                {
                    "question": question,
                    "route": route,
                    "sql_selection": {"selection_run_id": base.get("selection_run_id"), "candidate_count": base.get("candidate_count")},
                    "vector_search": {"vector_run_id": vector.get("vector_run_id"), "status": vector.get("status"), "error": vector.get("error_text"), "hit_count": len(vector.get("hits") or [])},
                    "fused_candidates": stored[:limit],
                    "noise_guardrail": warning,
                    "instruction": "Sélectionne ce qui doit vraiment remonter. Ne transforme pas une simple répétition en pattern profond sans preuves/outcomes/counter-exemples. Signale le risque si vector search manque.",
                    "schema": FUSION_AUDIT_SCHEMA,
                },
                FUSION_AUDIT_SCHEMA,
                timeout=240,
            )
        except Exception as exc:
            audit = {"selected_items": [], "missed_risk": "medium", "noise_warnings": [str(exc)], "what_should_not_be_overinterpreted": []}
        upsert(con, "v14_2_fusion_runs", {
            "fusion_run_id": fusion_run_id,
            "selection_run_id": base.get("selection_run_id"),
            "vector_run_id": vector.get("vector_run_id"),
            "person_id": person_id,
            "question": question,
            "sql_candidate_count": int(base.get("candidate_count") or 0),
            "vector_candidate_count": len(vector.get("hits") or []),
            "fused_candidate_count": len(stored),
            "qwen_fusion_json": json_dumps(audit),
            "created_at": now,
        }, "fusion_run_id")
        con.commit()
    return {
        "version": V14_2_VERSION,
        "selection_run_id": base.get("selection_run_id"),
        "fusion_run_id": fusion_run_id,
        "vector_run_id": vector.get("vector_run_id"),
        "person_id": person_id,
        "question": question,
        "route": route,
        "sql_candidate_count": base.get("candidate_count"),
        "vector_status": vector.get("status"),
        "vector_error": vector.get("error_text"),
        "vector_candidate_count": len(vector.get("hits") or []),
        "fused_candidate_count": len(stored),
        "candidates": stored[:limit],
        "qwen_fusion": audit,
        "noise_guardrail": warning,
    }


def ask_brain2(question: str, *, person_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    ensure_v14_2_schema()
    route_info = route_question(question, person_id=person_id)
    if route_info.get("status") != "ok":
        return {"version": V14_2_VERSION, "status": "route_failed", **route_info}
    selected = select_candidates(question, person_id=route_info.get("person_id"), route_payload=route_info, limit=limit)
    now = now_iso()
    with connect() as con:
        person_id = str(route_info.get("person_id") or _default_user(con))
        payload = {
            "mission": "Réponds au nom du cerveau 2.0 complet. Utilise la bonne couche: brut pour faits, V13 pour prédictions/simulations, V14 pour boucles longues, V14.2 vector fusion pour ne pas rater les vieux souvenirs sémantiques, V14.7 pour les interventions proactives timing-aware. Sépare fait, inférence, prédiction, manque de contexte. Ne vends jamais une simple répétition comme pattern profond sans preuves/outcome/counter-exemples. Si une intervention V14.7 existe, explique pourquoi maintenant et ce qui reste hypothétique.",
            "question": question,
            "route": selected.get("route"),
            "fused_candidates": selected.get("candidates", [])[:limit],
            "qwen_fusion": selected.get("qwen_fusion"),
            "noise_guardrail": selected.get("noise_guardrail"),
            "v14_digest": pattern_mirror_digest(person_id=person_id, limit=30),
            "answer_schema": ANSWER_SCHEMA,
        }
        out = _llm_json("Tu es l'interface Brain2 V14.2. Réponds uniquement en JSON valide.", payload, ANSWER_SCHEMA, timeout=360)
        answer_id = stable_id("v142answer", person_id, question, selected.get("fusion_run_id"), _hash_payload(out), now)
        upsert(con, "v14_2_answer_packets", {
            "answer_id": answer_id,
            "fusion_run_id": selected.get("fusion_run_id"),
            "person_id": person_id,
            "question": question,
            "answer_json": json_dumps(out),
            "created_at": now,
        }, "answer_id")
        con.commit()
    return {"version": V14_2_VERSION, "answer_id": answer_id, "person_id": person_id, "question": question, "route": selected.get("route"), "selection": {"fused_candidate_count": selected.get("fused_candidate_count"), "fusion_run_id": selected.get("fusion_run_id"), "vector_status": selected.get("vector_status")}, **out}


def audit_v14_2(*, persist: bool = True) -> dict[str, Any]:
    ensure_v14_2_schema()
    with connect() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(V14_2_TABLES - tables)
        counts: dict[str, Any] = {}
        for t in sorted(V14_2_TABLES):
            counts[t] = _one_value(con, f"SELECT COUNT(*) AS c FROM {t}", default="missing")
        if persist:
            now = now_iso()
            for t in sorted(V14_2_TABLES):
                upsert(con, "v14_2_contract_checks", {
                    "check_id": stable_id("v142audit", t),
                    "check_name": f"exists:{t}",
                    "status": "ok" if t in tables else "missing",
                    "detail": "V14.2 vector fusion/noise/ranking/raw+V13+V14 route check.",
                    "created_at": now,
                }, "check_id")
            con.commit()
    return {
        "version": V14_2_VERSION,
        "ok": not missing,
        "missing_tables": missing,
        "required_tables": sorted(V14_2_TABLES),
        "counts": counts,
        "selection_flow": "natural route -> SQL structured candidates -> semantic vector search -> fusion/ranking -> Qwen answer",
        "noise_guardrail": "noise-aware guardrail: repetition is not automatically truth; low-evidence patterns are penalized before Qwen sees them",
        "no_regex_layer": "This V14.2 module does not import or use regex. Qwen routes; scoring uses structured fields and vector hits.",
    }

# --- V18 scope/fusion hardening -------------------------------------------
from .governance_v18 import ScopeError as _V18ScopeError
_v17_select_candidates_v142 = select_candidates
_v17_ask_brain2_v142 = ask_brain2
_v17_route_question_v142 = route_question
_v17_fusion_rows_v142 = _fusion_rows

def route_question(question: str, *, person_id: str | None = None) -> dict[str, Any]:
    if not person_id:
        raise _V18ScopeError("V18 vector routing requires an explicit person_id")
    return _v17_route_question_v142(question, person_id=person_id)


def _fusion_rows(sql_candidates: list[dict[str, Any]], vector_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge by canonical (table,id), never by retrieval origin."""
    fused: dict[str, dict[str, Any]] = {}
    for c in sql_candidates:
        payload = _safe_json(c.get("payload_json"), {}) or {}
        table = str(c.get("source_table") or "")
        sid = str(c.get("source_id") or "")
        if not table or not sid:
            continue
        key = f"{table}|{sid}"
        item = fused.setdefault(key, {"source_kind":str(c.get("source_kind") or "sql"),"source_table":table,"source_id":sid,"came_from_sql":0,"came_from_vector":0,"sql_score":0.0,"vector_score":0.0,"payload":payload})
        item["came_from_sql"]=1; item["sql_score"]=max(float(item["sql_score"]),float(c.get("score") or 0.0))
    for h in vector_hits:
        source_type=str(h.get("source_type") or "vector"); table=_table_for_vector_source(source_type); sid=str(h.get("source_id") or h.get("id") or "")
        if not sid:
            continue
        key=f"{table}|{sid}"; payload={"text":h.get("text"),"metadata":h.get("metadata") or {},"source_type":source_type,"source_id":sid,"confidence":h.get("score")}
        item=fused.setdefault(key,{"source_kind":source_type,"source_table":table,"source_id":sid,"came_from_sql":0,"came_from_vector":0,"sql_score":0.0,"vector_score":0.0,"payload":payload})
        item["came_from_vector"]=1; item["vector_score"]=max(float(item["vector_score"]),float(h.get("score") or 0.0))
        if not item.get("payload") or not _text_from_payload(item.get("payload") or {}): item["payload"]=payload
    return list(fused.values())


def select_candidates(question: str, *, person_id: str | None = None, route_payload: dict[str, Any] | None = None, limit: int = 80) -> dict[str, Any]:
    explicit = person_id or (route_payload or {}).get("person_id")
    if not explicit:
        raise _V18ScopeError("V18 vector fusion requires an explicit person_id")
    out = _v17_select_candidates_v142(question, person_id=str(explicit), route_payload=route_payload, limit=limit)
    # A vector hit from a legacy index without person_id has already been
    # filtered by retrieval.search; make this visible to downstream clients.
    out.setdefault("scope", {"person_id":str(explicit),"vector_owner_filter":True,"canonical_source_merge":True})
    return out


def ask_brain2(question: str, *, person_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    if not person_id:
        raise _V18ScopeError("V18 vector answers require an explicit person_id")
    return _v17_ask_brain2_v142(question, person_id=person_id, limit=limit)
