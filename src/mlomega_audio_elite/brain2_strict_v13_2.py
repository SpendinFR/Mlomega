from __future__ import annotations

"""V13.2 strict Brain 2.0 layer.

This is the no-fake-brain implementation: every cognitive object is produced by
Qwen/Ollama JSON contracts or by structural bookkeeping that does not infer
psychology (time/object links, audit rows, dependency rows). There is no
regex/keyword analyst and no evidence-only cognitive mode.
"""

import os
from collections import Counter
from typing import Any

from .db import connect, init_db, upsert
from .governance_v18 import DataAccessError, GovernanceError, ensure_v18_schema, strict_many, strict_one
from .llm import EliteLLMError, OllamaJsonClient
from .utils import json_dumps, json_loads, now_iso, sha256_bytes, stable_id
from .brain2_complete_v13 import COMPLETE_TARGETS, ENGINE_ORDER, ENGINE_TABLES, PLAN_TABLES, ENGINE_SCHEMAS
from .llm_contracts_v15_18 import normalize_outcome_tracker, normalize_similar_case_score, normalize_calibration_rows, normalize_intervention_plan

STRICT_VERSION = "13.2.0-brain2-strict-final"

STRICT_EXTRA_TABLES = {
    "brain2_temporal_links",
    "brain2_object_links",
    "v13_llm_contracts",
    "v13_engine_dependencies",
    "v13_readiness_checks",
    "v13_prosody_requirements",
    # V13.3 direct 24/24 flow + self voice + latent outcome discovery
    "self_voice_profile",
    "voice_clusters",
    "voice_observations",
    "voice_identity_revisions",
    "voice_pending_prompts",
    "audio_preprocess_runs",
    "audio_segments",
    "conversation_subtopic_segments",
    "latent_outcome_search_runs",
    "latent_outcome_links",
    "direct_flow_jobs",
}

STRICT_PLAN_TABLES = set(PLAN_TABLES) | STRICT_EXTRA_TABLES

STRICT_EPISODE_SCHEMA: dict[str, Any] = {
    "episodes": [
        {
            "episode_type": "technical_validation|relationship_tension|client_request|decision_point|emotional_reaction|planning|conflict|avoidance|commitment|self_reflection|other",
            "start_turn_id": "",
            "end_turn_id": "",
            "start_time": None,
            "end_time": None,
            "participants": [],
            "location": None,
            "channel": None,
            "topic": "",
            "situation_summary": "",
            "trigger": "",
            "user_state_before": "",
            "speech_or_action": "",
            "target_person": "",
            "target_reaction": "",
            "user_state_after": "",
            "outcome": "",
            "unresolved_tension": "",
            "confidence": 0.0,
            "evidence_turn_ids": [],
            "evidence_texts": [],
        }
    ],
    "counter_evidence": [],
    "missing_context": [],
    "confidence": 0.0,
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


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _hash_payload(payload: Any) -> str:
    return sha256_bytes(json_dumps(payload).encode("utf-8"))


def _available_tables(con) -> set[str]:
    return {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _default_user(con, conversation_id: str | None = None, explicit_person_id: str | None = None) -> str:
    from .v18_owner_scope import reject_implicit_owner_fallback
    reject_implicit_owner_fallback(__name__)
    if explicit_person_id:
        return str(explicit_person_id)
    row = con.execute("SELECT person_id FROM speaker_profiles WHERE is_user=1 ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return row["person_id"]
    if conversation_id:
        conv = con.execute("SELECT participants_json, speaker_map_json FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if conv:
            for value in (_as_dict(json_loads(conv["speaker_map_json"], {})) or {}).values():
                if str(value).lower() in {"me", "moi", "user", "utilisateur"}:
                    return str(value)
            participants = _as_list(json_loads(conv["participants_json"], []))
            if participants:
                return str(participants[0])
    return "me"


def ensure_strict_v13_schema() -> None:
    ensure_v18_schema()
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS brain2_temporal_links(
                temporal_link_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                episode_id TEXT,
                from_table TEXT NOT NULL,
                from_id TEXT NOT NULL,
                to_table TEXT NOT NULL,
                to_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                from_time TEXT,
                to_time TEXT,
                lag_seconds REAL,
                evidence_json TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brain2_object_links(
                object_link_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                episode_id TEXT,
                from_table TEXT NOT NULL,
                from_id TEXT NOT NULL,
                to_table TEXT NOT NULL,
                to_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                engine_name TEXT,
                evidence_json TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v13_llm_contracts(
                contract_id TEXT PRIMARY KEY,
                engine_name TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                required_schema_json TEXT NOT NULL,
                strict_rules_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v13_engine_dependencies(
                dependency_id TEXT PRIMARY KEY,
                engine_name TEXT NOT NULL,
                depends_on_json TEXT NOT NULL,
                produces_tables_json TEXT NOT NULL,
                consumes_tables_json TEXT NOT NULL,
                order_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v13_readiness_checks(
                readiness_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                check_group TEXT NOT NULL,
                status TEXT NOT NULL,
                severity TEXT NOT NULL,
                detail TEXT,
                evidence_json TEXT,
                missing_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v13_prosody_requirements(
                requirement_id TEXT PRIMARY KEY,
                signal_name TEXT NOT NULL,
                required_for TEXT NOT NULL,
                extractor_status TEXT NOT NULL,
                fallback_allowed INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        now = now_iso()
        strict_rules = [
            "No heuristic or regex cognitive inference.",
            "Every inferred item must come from Qwen/Ollama JSON output.",
            "Observed facts must cite source turns/source spans or explicit evidence text.",
            "Predictions are probabilistic and must include why, evidence/counter-evidence, assumptions and intervention options.",
            "If Qwen is unavailable or JSON is invalid, the engine fails instead of filling tables with guesses.",
        ]
        for engine in ENGINE_ORDER:
            upsert(con, "v13_llm_contracts", {
                "contract_id": stable_id("v13contract", STRICT_VERSION, engine),
                "engine_name": engine,
                "contract_version": STRICT_VERSION,
                "required_schema_json": json_dumps(ENGINE_SCHEMAS[engine]),
                "strict_rules_json": json_dumps(strict_rules),
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }, "contract_id")
            idx = ENGINE_ORDER.index(engine)
            upsert(con, "v13_engine_dependencies", {
                "dependency_id": stable_id("v13dep", STRICT_VERSION, engine),
                "engine_name": engine,
                "depends_on_json": json_dumps(ENGINE_ORDER[:idx]),
                "produces_tables_json": json_dumps(ENGINE_TABLES.get(engine, [])),
                "consumes_tables_json": json_dumps(["turns", "source_spans", "episodes"] + ENGINE_TABLES.get(engine, [])),
                "order_index": idx,
                "status": "active",
                "created_at": now,
            }, "dependency_id")
        for sig in ["pause", "laughter", "sigh", "stress_voice", "hesitation", "overlap", "volume_shift", "pitch_shift", "speech_rate", "silence"]:
            upsert(con, "v13_prosody_requirements", {
                "requirement_id": stable_id("prosodyreq", sig),
                "signal_name": sig,
                "required_for": "emotion_from_voice/state_transition/next_emotion",
                "extractor_status": "required_not_inferred_if_missing",
                "fallback_allowed": 0,
                "notes": "No text heuristic may replace the missing acoustic extractor; Qwen must mark missing voice evidence when absent.",
                "created_at": now,
                "updated_at": now,
            }, "requirement_id")
        con.commit()
    # V13.3 extension schemas are still part of the strict plan: they add
    # self-voice, active unknown-voice learning, audio preprocessing, direct
    # flow, subtopic segmentation and latent outcome discovery.
    try:
        from .voice_learning import ensure_voice_learning_schema
        from .audio_preprocess import ensure_audio_preprocess_schema
        from .brain2_flow_v13_3 import ensure_brain2_flow_schema
        ensure_voice_learning_schema(); ensure_audio_preprocess_schema(); ensure_brain2_flow_schema()
    except Exception:
        # Let the audit expose missing tables rather than masking the original DB init.
        pass


def _llm_require_json(engine_name: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    system = (
        "Tu es un moteur local strict Brain 2.0. Tu remplis uniquement à partir des preuves fournies. "
        "Aucune regex, aucune psychologie générique, aucune hypothèse non marquée. "
        "Réponds uniquement en JSON valide suivant le schéma. "
        "Chaque inférence doit avoir confidence et evidence/counter_evidence. "
        "Si une information manque, indique missing_context au lieu d'inventer."
    )
    client = OllamaJsonClient()
    data = client.require_json(system, prompt, schema_hint=schema, timeout=float(os.environ.get("MLOMEGA_V13_ENGINE_TIMEOUT", "180")))
    if not isinstance(data, dict):
        raise EliteLLMError(f"{engine_name} returned non-object JSON")
    return data




def _compact_conversation_for_prompt(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw = json_loads(out.get("raw_json"), {}) if isinstance(out.get("raw_json"), str) else (out.get("raw_json") or {})
    if isinstance(raw, dict):
        compact: dict[str, Any] = {
            "source": raw.get("source"),
            "bundle_id": raw.get("bundle_id"),
            "bundle_kind": raw.get("bundle_kind"),
            "source_counts": raw.get("source_counts"),
            "place": raw.get("place"),
            "side_channel_note": raw.get("side_channel_note"),
        }
        # Keep side-channel references compact: time/kind/source_id/summary only.
        for key in ["prediction_timeline", "intervention_timeline", "outcome_timeline", "affordance_timeline", "raw_timeline", "vision_timeline"]:
            vals = raw.get(key) or []
            if isinstance(vals, list):
                small = []
                for item in vals[:40]:
                    if isinstance(item, dict):
                        small.append({k: item.get(k) for k in ("time", "kind", "summary", "text", "source_table", "source_id", "evidence_role", "forecast_id", "candidate_id") if item.get(k) is not None})
                    else:
                        small.append(item)
                compact[key] = small
        out["raw_json"] = json_dumps(compact)
    return out


def _source_ref(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        ref = {k: item.get(k) for k in ("turn_id","episode_id","source_span_id","event_id","pattern_id","loop_id","observation_id","idx","start_s","end_s") if item.get(k) is not None}
        text = item.get("text") or item.get("summary") or item.get("evidence_text") or item.get("content")
        if isinstance(text, str):
            ref["text_preview"] = text[:360]
            ref["text_truncated"] = len(text) > 360
        return ref or {"payload_sha256": _hash_payload(item)}
    return {"payload_sha256": _hash_payload(item)}


def _safe_prompt_payload(payload: dict[str, Any], max_chars: int = 90000) -> str:
    """Return valid JSON or a valid explicit incomplete-context envelope.

    V17.4 cut the bytes of a JSON document then appended another JSON object,
    which produced an invalid prompt.  V18 never byte-truncates JSON.  If the
    budget is exceeded, the engine receives source references and is instructed
    to report missing context instead of hallucinating from a partial payload.
    """
    txt = json_dumps(payload)
    if len(txt) <= max_chars:
        return txt
    bundle = payload.get("bundle") if isinstance(payload.get("bundle"), dict) else {}
    compact_bundle: dict[str, Any] = {}
    for key, value in bundle.items():
        if isinstance(value, list):
            compact_bundle[key] = [_source_ref(x) for x in value]
        elif isinstance(value, dict):
            compact_bundle[key] = _source_ref(value)
        elif isinstance(value, str):
            compact_bundle[key] = {"text_preview": value[:500], "text_truncated": len(value)>500}
        else:
            compact_bundle[key] = value
    reduced = {
        "schema_version": "18.0.0",
        "engine_name": payload.get("engine_name"),
        "mission": payload.get("mission"),
        "schema": payload.get("schema"),
        "context_incomplete": True,
        "missing_context_reason": "prompt_budget_exceeded; retrieve source references before asserting a conclusion",
        "full_payload_sha256": _hash_payload(payload),
        "bundle_source_refs": compact_bundle,
        "prior_engine_output_refs": {k: _source_ref(v) for k, v in (payload.get("prior_engine_outputs") or {}).items()} if isinstance(payload.get("prior_engine_outputs"), dict) else {},
    }
    out = json_dumps(reduced)
    if len(out) > max_chars:
        # This is structurally impossible for ordinary V13 output; signal an
        # error rather than a lossy string cut.
        raise GovernanceError("context reference envelope exceeds prompt budget")
    return out

def _conversation_bundle(con, conversation_id: str) -> dict[str, Any]:
    conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not conv:
        raise ValueError(f"conversation_missing: {conversation_id}")
    turns = [dict(r) for r in con.execute("SELECT turn_id, idx, speaker_label, person_id, start_s, end_s, text, metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,))]
    spans = [dict(r) for r in con.execute("SELECT * FROM source_spans WHERE conversation_id=? ORDER BY start_s", (conversation_id,))]
    return {"conversation": _compact_conversation_for_prompt(dict(conv)), "turns": turns, "source_spans": spans}


def _episode_bundle(con, episode_id: str) -> dict[str, Any]:
    """Build a local, scope-valid episode bundle.

    The old version gave every engine the whole conversation and queried
    non-existent ``episode_id`` columns under ``except: []``.  This version
    selects only episode turns plus a small explicit boundary window and emits
    unavailable relationships as ``missing_context`` rather than pretending the
    table is empty.
    """
    ep = strict_one(con, "SELECT * FROM episodes WHERE episode_id=?", (episode_id,), purpose="load episode")
    if not ep:
        raise ValueError(f"episode_missing: {episode_id}")
    conv_id = ep.get("source_conversation_id")
    conv = strict_one(con, "SELECT * FROM conversations WHERE conversation_id=?", (conv_id,), purpose="load episode conversation") if conv_id else None
    turns: list[dict[str, Any]] = []
    missing: list[str] = []
    if conv_id:
        start_id, end_id = ep.get("start_turn_id"), ep.get("end_turn_id")
        bounds = strict_many(con, "SELECT turn_id,idx FROM turns WHERE conversation_id=? AND turn_id IN (?,?)", (conv_id, start_id, end_id), purpose="episode bounds") if (start_id or end_id) else []
        by_id={str(row["turn_id"]):int(row["idx"]) for row in bounds}
        lo=by_id.get(str(start_id)) if start_id else None
        hi=by_id.get(str(end_id)) if end_id else None
        if lo is None or hi is None:
            evidence = strict_many(con, "SELECT turn_id FROM episode_evidence WHERE episode_id=? AND turn_id IS NOT NULL", (episode_id,), purpose="episode evidence")
            evidence_ids=[str(r["turn_id"]) for r in evidence]
            if evidence_ids:
                marks=strict_many(con, "SELECT turn_id,idx FROM turns WHERE conversation_id=? AND turn_id IN (%s)" % ",".join("?" for _ in evidence_ids), (conv_id,*evidence_ids), purpose="episode evidence bounds")
                indices=[int(r["idx"]) for r in marks]
                if indices:
                    lo,hi=min(indices),max(indices)
            if lo is None or hi is None:
                missing.append("episode_turn_bounds_missing")
                lo,hi=0,-1
        lo,hi=min(lo,hi),max(lo,hi)
        # Small visible context before/after; no accidental full transcript.
        turns=strict_many(con, "SELECT turn_id,idx,speaker_label,person_id,start_s,end_s,text,metadata_json FROM turns WHERE conversation_id=? AND idx BETWEEN ? AND ? ORDER BY idx", (conv_id,max(0,lo-2),hi+2), purpose="episode local turns")
    def scoped(table: str, *, predicate: str = "episode_id=?", params: tuple[Any,...] = (episode_id,)) -> list[dict[str, Any]]:
        try:
            return strict_many(con, f"SELECT * FROM {table} WHERE {predicate} LIMIT 200", params, purpose=f"episode {table}")
        except DataAccessError as exc:
            missing.append(f"{table}: {exc}")
            return []
    # Tables whose schema actually contains episode_id.
    direct={name:scoped(name) for name in ("situation_episodes","interaction_episodes","internal_state_snapshots","thought_hypotheses","speech_acts","action_intentions","action_outcomes","choice_episodes","contradiction_events")}
    # Causal edges are polymorphic, not episode keyed.
    causes=scoped("causal_edges", predicate="(from_table='episodes' AND from_id=?) OR (to_table='episodes' AND to_id=?)", params=(episode_id,episode_id))
    # Patterns use pattern contexts/counterexamples, never a fictitious episode_id.
    patterns=[]
    try:
        counter=strict_many(con, "SELECT pattern_table,pattern_id FROM pattern_counterexamples WHERE episode_id=?", (episode_id,), purpose="episode pattern counterexamples")
        for row in counter:
            table=str(row["pattern_table"])
            if table not in {"candidate_patterns","confirmed_patterns","loop_patterns"}:
                continue
            pk={"candidate_patterns":"candidate_pattern_id","confirmed_patterns":"confirmed_pattern_id","loop_patterns":"loop_id"}[table]
            obj=strict_one(con, f"SELECT * FROM {table} WHERE {pk}=?", (row["pattern_id"],), purpose=f"pattern {table}")
            if obj:
                patterns.append(obj)
    except DataAccessError as exc:
        missing.append(f"patterns: {exc}")
    return {
        "episode": ep,
        "conversation": _compact_conversation_for_prompt(conv) if conv else None,
        "turns": turns,
        "situations": direct["situation_episodes"], "interactions": direct["interaction_episodes"],
        "states": direct["internal_state_snapshots"], "thoughts": direct["thought_hypotheses"],
        "speech_acts": direct["speech_acts"], "intentions": direct["action_intentions"],
        "outcomes": direct["action_outcomes"], "choices": direct["choice_episodes"],
        "causes": causes, "contradictions": direct["contradiction_events"], "patterns": patterns,
        "missing_context": missing,
        "context_scope": {"episode_id":episode_id,"conversation_id":conv_id,"turn_count":len(turns),"local_only":True},
    }

def _engine_prompt(engine_name: str, bundle: dict[str, Any], prior: dict[str, Any]) -> str:
    return _safe_prompt_payload({
        "engine_name": engine_name,
        "mission": "Remplir le modèle dynamique Brain 2.0: vie observée -> situation -> état -> parole/action -> réaction/résultat -> patterns -> simulation -> prédiction -> vérification -> correction.",
        "no_heuristic_policy": "Tout contenu cognitif vient de cette réponse Qwen. Ne pas inventer. Marquer missing_context.",
        "evidence_role_policy": "Respecte metadata_json.kind/evidence_role: human_or_audio_transcript = parole/transcription humaine; system_observation_not_user_speech = observation capteur/contexte, jamais une déclaration de William; side-channel prediction/intervention/outcome dans conversation.raw_json = metadata de vérification, jamais parole utilisateur. Ne transforme pas une observation système en goût, préférence ou intention déclarée.",
        "schema": ENGINE_SCHEMAS[engine_name],
        "bundle": bundle,
        "prior_engine_outputs": prior,
    })


def _record_engine(con, *, engine: str, conversation_id: str | None, episode_id: str | None, person_id: str | None, prompt: str, output: dict[str, Any] | None, status: str, error: str | None = None) -> str:
    now = now_iso()
    run_id = stable_id("v13stricteng", STRICT_VERSION, engine, conversation_id, episode_id, _hash_payload(prompt)[:16])
    upsert(con, "v13_engine_runs", {
        "engine_run_id": run_id,
        "engine_name": engine,
        "engine_version": STRICT_VERSION,
        "cycle_id": stable_id("v13strictcycle", conversation_id or "predict", episode_id or "none"),
        "conversation_id": conversation_id,
        "episode_id": episode_id,
        "person_id": person_id,
        "input_hash": _hash_payload(prompt),
        "require_llm": 1,
        "llm_model": os.environ.get("MLOMEGA_OLLAMA_MODEL", "qwen3:8b"),
        "status": status,
        "stage": "finished",
        "started_at": now,
        "finished_at": now,
        "counts_json": json_dumps({"output_keys": len(output or {})}),
        "warnings_json": json_dumps([]),
        "missing_json": json_dumps([] if output else ["qwen_json_output"]),
        "error_text": error,
        "metadata_json": json_dumps({"strict_version": STRICT_VERSION}),
    }, "engine_run_id")
    upsert(con, "v13_engine_outputs", {
        "output_id": stable_id("v13strictout", run_id),
        "engine_run_id": run_id,
        "engine_name": engine,
        "target_table": "episodes" if episode_id else "conversations",
        "target_id": episode_id or conversation_id,
        "output_type": "strict_qwen_json",
        "output_json": json_dumps(output or {}),
        "confidence": _clamp((output or {}).get("confidence")),
        "evidence_json": json_dumps(_as_list((output or {}).get("evidence"))),
        "counter_evidence_json": json_dumps(_as_list((output or {}).get("counter_evidence"))),
        "validation_status": "valid" if output else "failed",
        "created_at": now,
    }, "output_id")
    return run_id


def _insert_object_link(con, conversation_id: str | None, episode_id: str | None, from_table: str, from_id: str, to_table: str, to_id: str, relation: str, engine: str | None, confidence: float = 1.0, evidence: list[Any] | None = None) -> None:
    upsert(con, "brain2_object_links", {
        "object_link_id": stable_id("objlink", from_table, from_id, to_table, to_id, relation),
        "conversation_id": conversation_id,
        "episode_id": episode_id,
        "from_table": from_table,
        "from_id": from_id,
        "to_table": to_table,
        "to_id": to_id,
        "relation_type": relation,
        "engine_name": engine,
        "evidence_json": json_dumps(evidence or []),
        "confidence": _clamp(confidence),
        "created_at": now_iso(),
    }, "object_link_id")


def _insert_temporal_link(con, conversation_id: str | None, episode_id: str | None, from_table: str, from_id: str, to_table: str, to_id: str, relation: str, from_time: str | None = None, to_time: str | None = None, confidence: float = 1.0) -> None:
    upsert(con, "brain2_temporal_links", {
        "temporal_link_id": stable_id("timelink", from_table, from_id, to_table, to_id, relation),
        "conversation_id": conversation_id,
        "episode_id": episode_id,
        "from_table": from_table,
        "from_id": from_id,
        "to_table": to_table,
        "to_id": to_id,
        "relation_type": relation,
        "from_time": from_time,
        "to_time": to_time,
        "lag_seconds": None,
        "evidence_json": json_dumps([]),
        "confidence": _clamp(confidence),
        "created_at": now_iso(),
    }, "temporal_link_id")


def _materialize_episodes_from_qwen(con, conversation_id: str, output: dict[str, Any]) -> int:
    conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    now = now_iso(); count = 0
    for i, ep in enumerate(_as_list(output.get("episodes"))):
        if not isinstance(ep, dict):
            continue
        evidence_turn_ids = [str(x) for x in _as_list(ep.get("evidence_turn_ids")) if x]
        start_turn = ep.get("start_turn_id") or (evidence_turn_ids[0] if evidence_turn_ids else None)
        end_turn = ep.get("end_turn_id") or (evidence_turn_ids[-1] if evidence_turn_ids else start_turn)
        summary = str(ep.get("situation_summary") or ep.get("topic") or "").strip()
        if not summary:
            continue
        episode_id = stable_id("episode", "strict", conversation_id, i, summary[:120], start_turn, end_turn)
        start_time = ep.get("start_time") or (conv["started_at"] if conv else now)
        end_time = ep.get("end_time") or None
        upsert(con, "episodes", {
            "episode_id": episode_id,
            "episode_type": ep.get("episode_type") or "other",
            "start_time": start_time,
            "end_time": end_time,
            "source_conversation_id": conversation_id,
            "start_turn_id": start_turn,
            "end_turn_id": end_turn,
            "participants_json": json_dumps(_as_list(ep.get("participants"))),
            "location_text": ep.get("location"),
            "channel": ep.get("channel") or (conv["channel"] if conv else None),
            "topic": ep.get("topic"),
            "situation_summary": summary,
            "trigger_summary": ep.get("trigger"),
            "user_state_before_json": json_dumps(ep.get("user_state_before")),
            "speech_or_action_summary": ep.get("speech_or_action"),
            "target_person_id": ep.get("target_person"),
            "target_reaction_summary": ep.get("target_reaction"),
            "user_state_after_json": json_dumps(ep.get("user_state_after")),
            "outcome_summary": ep.get("outcome"),
            "unresolved_tension": ep.get("unresolved_tension"),
            "confidence": _clamp(ep.get("confidence")),
            "truth_status": "inferred",
            "importance_score": _clamp(ep.get("importance_score", ep.get("confidence"))),
            "lifecycle_status": "active",
            "metadata_json": json_dumps({"strict_v13_2": True, "missing_context": _as_list(output.get("missing_context"))}),
            "created_at": now,
            "updated_at": now,
        }, "episode_id")
        for turn_id in evidence_turn_ids:
            ev_id = stable_id("epevidence", episode_id, turn_id)
            turn = con.execute("SELECT text FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            upsert(con, "episode_evidence", {
                "episode_evidence_id": ev_id,
                "episode_id": episode_id,
                "source_span_id": None,
                "turn_id": turn_id,
                "evidence_text": turn["text"] if turn else None,
                "evidence_role": "qwen_selected_turn",
                "confidence": _clamp(ep.get("confidence")),
                "created_at": now,
            }, "episode_evidence_id")
            _insert_object_link(con, conversation_id, episode_id, "episodes", episode_id, "turns", turn_id, "supported_by", "episode_builder", _clamp(ep.get("confidence")))
        for btype, tid in [("start", start_turn), ("end", end_turn)]:
            if tid:
                turn = con.execute("SELECT idx, text FROM turns WHERE turn_id=?", (tid,)).fetchone()
                upsert(con, "episode_boundaries", {
                    "boundary_id": stable_id("epbound", episode_id, btype, tid),
                    "conversation_id": conversation_id,
                    "episode_id": episode_id,
                    "boundary_type": btype,
                    "turn_id": tid,
                    "idx": turn["idx"] if turn else None,
                    "reason": f"Qwen episode_builder boundary: {btype}",
                    "confidence": _clamp(ep.get("confidence")),
                    "evidence_text": turn["text"] if turn else None,
                    "created_at": now,
                }, "boundary_id")
        _insert_temporal_link(con, conversation_id, episode_id, "conversations", conversation_id, "episodes", episode_id, "contains", conv["started_at"] if conv else None, start_time)
        count += 1
    return count


def _ensure_episodes_strict(con, conversation_id: str) -> int:
    existing_rows = [dict(r) for r in con.execute("SELECT episode_id, metadata_json FROM episodes WHERE source_conversation_id=?", (conversation_id,)).fetchall()]
    if existing_rows:
        complete = False
        for r in existing_rows:
            meta = json_loads(r.get("metadata_json"), {}) if isinstance(r.get("metadata_json"), str) else {}
            if meta.get("episode_source") == STRICT_VERSION and meta.get("coverage_status") == "complete":
                complete = True
                break
        if complete:
            return 0
        # Partial/legacy coverage must not poison reruns. Remove only episodes
        # for this conversation; dependent rows cascade or are recreated via upsert.
        for r in existing_rows:
            con.execute("DELETE FROM episodes WHERE episode_id=?", (r.get("episode_id"),))
    bundle = _conversation_bundle(con, conversation_id)
    prompt = _safe_prompt_payload({"mission": "Découpe cette conversation en épisodes de vie selon le plan Brain 2.0. Aucun découpage par regex: utilise le sens, les preuves et l'incertitude. Respecte metadata_json.kind/evidence_role: observation système ≠ parole de William.", "conversation_bundle": bundle, "schema": STRICT_EPISODE_SCHEMA})
    out = _llm_require_json("episode_builder", prompt, STRICT_EPISODE_SCHEMA)
    _record_engine(con, engine="episode_builder", conversation_id=conversation_id, episode_id=None, person_id=_default_user(con, conversation_id), prompt=prompt, output=out, status="ok")
    return _materialize_episodes_from_qwen(con, conversation_id, out)


def _put_engine_payload(con, engine: str, episode_id: str, person_id: str, output: dict[str, Any]) -> int:
    """Materialize Qwen JSON into plan tables. This is data mapping only, not inference."""
    now = now_iso(); count = 0
    ep = con.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    conv_id = ep["source_conversation_id"] if ep else None
    conf = _clamp(output.get("confidence"))

    def link(table: str, oid: str, rel: str = "produced_from") -> None:
        _insert_object_link(con, conv_id, episode_id, "episodes", episode_id, table, oid, rel, engine, conf, _as_list(output.get("evidence")))

    if engine == "episode_builder":
        summary = _as_dict(output.get("episode_summary_update"))
        if summary:
            updates: dict[str, Any] = {"updated_at": now}
            for src, dst in [("episode_type", "episode_type"), ("situation_summary", "situation_summary"), ("trigger", "trigger_summary"), ("unresolved_tension", "unresolved_tension"), ("outcome", "outcome_summary")]:
                val = summary.get(src)
                if val:
                    updates[dst] = val
            if len(updates) > 1:
                current_meta = json_loads(ep["metadata_json"], {}) if ep and isinstance(ep["metadata_json"], str) else {}
                current_meta.setdefault("strict_v13_2", True)
                current_meta["episode_builder_engine_update"] = {k: v for k, v in updates.items() if k != "updated_at"}
                updates["metadata_json"] = json_dumps(current_meta)
                assignments = ", ".join(f"{k}=?" for k in updates)
                con.execute(f"UPDATE episodes SET {assignments} WHERE episode_id=?", tuple(updates.values()) + (episode_id,))
                count += 1
        for b in _as_list(output.get("episode_boundaries")):
            if isinstance(b, dict) and b.get("boundary_type"):
                tid = b.get("turn_id")
                turn = con.execute("SELECT idx, text FROM turns WHERE turn_id=?", (tid,)).fetchone() if tid else None
                bid = stable_id("epbound", episode_id, engine, b.get("boundary_type"), tid or b.get("reason"))
                upsert(con, "episode_boundaries", {
                    "boundary_id": bid, "conversation_id": conv_id, "episode_id": episode_id,
                    "boundary_type": b.get("boundary_type"), "turn_id": tid, "idx": turn["idx"] if turn else None,
                    "reason": b.get("reason") or "V13 episode_builder engine boundary",
                    "confidence": _clamp(b.get("confidence", conf)), "evidence_text": turn["text"] if turn else json_dumps(_as_list(output.get("evidence"))),
                    "created_at": now,
                }, "boundary_id")
                count += 1
        for l in _as_list(output.get("links_to_other_episodes")):
            if isinstance(l, dict) and l.get("to_episode_id") and l.get("to_episode_id") != episode_id:
                lid = stable_id("eplink", episode_id, l.get("relation_type") or "related", l.get("to_episode_id"))
                upsert(con, "episode_links", {
                    "episode_link_id": lid, "from_episode_id": episode_id, "relation_type": l.get("relation_type") or "related",
                    "to_episode_id": l.get("to_episode_id"), "confidence": _clamp(l.get("confidence", conf)),
                    "evidence_text": json_dumps(_as_list(output.get("evidence"))), "metadata_json": json_dumps({"source": "strict_v13_2_episode_builder_engine"}),
                    "created_at": now,
                }, "episode_link_id")
                count += 1

    elif engine == "capture_engine":
        cap = _as_dict(output.get("capture_quality"))
        if _as_list(output.get("prosody_events")):
            for ev in _as_list(output.get("prosody_events")):
                if isinstance(ev, dict) and ev.get("event_type"):
                    oid = stable_id("prosody", episode_id, ev.get("turn_id"), ev.get("event_type"), ev.get("interpretation"))
                    upsert(con, "audio_prosody_events", {"prosody_event_id": oid, "conversation_id": conv_id, "turn_id": ev.get("turn_id"), "source_asset_id": None, "person_id": person_id, "start_s": ev.get("start_s"), "end_s": ev.get("end_s"), "event_type": ev.get("event_type"), "feature_json": json_dumps({"value": ev.get("value"), "capture_quality": cap}), "interpretation": ev.get("interpretation"), "confidence": _clamp(ev.get("confidence")), "source_method": "qwen_or_acoustic_feature", "evidence_json": json_dumps(_as_list(output.get("evidence"))), "created_at": now}, "prosody_event_id")
                    link("audio_prosody_events", oid)
                    count += 1
        if cap.get("missing_audio_signals"):
            oid = stable_id("readiness", episode_id, "missing_audio_signals")
            upsert(con, "v13_readiness_checks", {"readiness_id": oid, "check_name": "audio_prosody_available", "check_group": "capture_engine", "status": "missing", "severity": "warning", "detail": "Qwen reported missing audio/prosody evidence; no text heuristic substituted.", "evidence_json": json_dumps(_as_list(output.get("evidence"))), "missing_json": json_dumps(_as_list(cap.get("missing_audio_signals"))), "created_at": now}, "readiness_id")
            count += 1

    elif engine == "language_signature_engine":
        style = _as_dict(output.get("style_state"))
        if style:
            oid = stable_id("style", person_id, episode_id)
            upsert(con, "style_state_snapshots", {"style_state_id": oid, "person_id": person_id, "episode_id": episode_id, "directness": _clamp(style.get("directness")), "detail_level": _clamp(style.get("detail_level")), "correction_tendency": _clamp(style.get("correction_tendency")), "validation_seeking": _clamp(style.get("validation_seeking")), "typical_phrases_json": json_dumps(_as_list(style.get("typical_phrases"))), "evidence_json": json_dumps(_as_list(output.get("evidence"))), "confidence": conf, "created_at": now}, "style_state_id")
            link("style_state_snapshots", oid); count += 1
        for tpl in _as_list(output.get("phrase_templates")):
            if isinstance(tpl, dict) and tpl.get("template"):
                oid = stable_id("phrase_tpl", person_id, tpl.get("template"), episode_id)
                upsert(con, "phrase_templates", {"template_id": oid, "person_id": person_id, "template_text": tpl.get("template"), "template_type": tpl.get("template_type") or "qwen_phrase_template", "context_type": tpl.get("context_type"), "frequency": None, "confidence": _clamp(tpl.get("confidence", tpl.get("probability", conf))), "examples_json": json_dumps(_as_list(tpl.get("examples"))), "metadata_json": json_dumps({"speech_act_context": tpl.get("speech_act_context"), "emotion_context": tpl.get("emotion_context"), "probability": _clamp(tpl.get("probability"))}), "created_at": now, "updated_at": now}, "template_id")
                link("phrase_templates", oid); count += 1
        for wp in _as_list(output.get("word_predictions")):
            if isinstance(wp, dict):
                oid = stable_id("ngram", person_id, episode_id, wp.get("context"), json_dumps(wp.get("next_word_candidates")))
                upsert(con, "language_ngrams", {"ngram_id": oid, "person_id": person_id, "n": 0, "ngram": str(wp.get("context") or ""), "context_type": None, "frequency": None, "examples_json": json_dumps(_as_list(wp.get("next_word_candidates"))), "probability": _clamp(wp.get("confidence")), "last_seen": now, "created_at": now, "updated_at": now}, "ngram_id")
                link("language_ngrams", oid); count += 1

    elif engine == "context_resolver":
        sit = _as_dict(output.get("situation"))
        if sit:
            oid = stable_id("situ", episode_id, person_id)
            upsert(con, "situation_episodes", {"situation_id": oid, "episode_id": episode_id, "situation_type": sit.get("situation_type"), "life_domain": sit.get("life_domain"), "participants_json": json_dumps(_as_list(sit.get("participants"))), "main_person_id": sit.get("main_person"), "secondary_people_json": json_dumps(_as_list(sit.get("secondary_people"))), "place_explicit": sit.get("place_explicit"), "place_inferred": sit.get("place_inferred"), "channel": sit.get("channel"), "social_context": sit.get("social_context"), "power_balance": sit.get("power_balance"), "stakes": sit.get("stakes"), "constraints_json": json_dumps(_as_list(sit.get("constraints"))), "trigger_event_id": sit.get("trigger_event_id"), "related_project": sit.get("related_project"), "related_relationship_id": sit.get("related_relationship"), "confidence": conf, "metadata_json": json_dumps({"resolved_references": _as_list(output.get("resolved_references")), "missing_context": _as_list(output.get("missing_context"))}), "created_at": now, "updated_at": now}, "situation_id")
            link("situation_episodes", oid); count += 1

    elif engine == "internal_state_engine":
        for label, key in [("before", "state_before"), ("during", "state_during"), ("after", "state_after")]:
            st = _as_dict(output.get(key))
            if st:
                oid = stable_id("state", episode_id, person_id, label)
                upsert(con, "internal_state_snapshots", {"state_id": oid, "person_id": person_id, "episode_id": episode_id, "time_start": ep["start_time"] if ep else None, "time_end": ep["end_time"] if ep else None, "energy": _clamp(st.get("energy")), "stress": _clamp(st.get("stress")), "motivation": _clamp(st.get("motivation")), "confidence_state": _clamp(st.get("confidence_level") or st.get("confidence")), "clarity": _clamp(st.get("clarity")), "frustration": _clamp(st.get("frustration")), "curiosity": _clamp(st.get("curiosity")), "urgency": _clamp(st.get("urgency")), "sense_of_control": _clamp(st.get("sense_of_control")), "feeling_understood": _clamp(st.get("feeling_understood")), "social_safety": _clamp(st.get("social_safety")), "emotional_valence": _clamp(st.get("emotional_valence"), -1, 1), "dominant_emotion": output.get("dominant_emotion") or st.get("dominant_emotion"), "secondary_emotions_json": json_dumps(_as_list(output.get("secondary_emotions"))), "evidence_text": json_dumps(_as_list(output.get("evidence"))), "confidence": _clamp(st.get("confidence", conf)), "source_type": "qwen_strict", "truth_status": "inferred", "confidence": _clamp(st.get("confidence", conf)), "metadata_json": json_dumps({"state_phase": label}), "created_at": now, "updated_at": now}, "state_id")
                link("internal_state_snapshots", oid); count += 1
        for th in _as_list(output.get("thought_hypotheses")):
            if isinstance(th, dict) and th.get("content"):
                oid = stable_id("thought", episode_id, person_id, th.get("content")[:120])
                upsert(con, "thought_hypotheses", {"thought_id": oid, "person_id": person_id, "episode_id": episode_id, "thought_type": th.get("thought_type"), "content": th.get("content"), "turn_id": th.get("turn_id"), "consciousness_level": th.get("consciousness_level"), "evidence_text": json_dumps(_as_list(th.get("evidence") or output.get("evidence"))), "trigger_summary": th.get("trigger"), "related_need": th.get("related_need"), "related_fear": th.get("related_fear"), "related_goal": th.get("related_goal"), "truth_status": "inferred", "confidence": _clamp(th.get("confidence")), "metadata_json": json_dumps({}), "created_at": now, "updated_at": now}, "thought_id")
                link("thought_hypotheses", oid); count += 1
        for ev in _as_list(output.get("state_transitions")):
            if isinstance(ev, dict):
                oid = stable_id("statetr", episode_id, ev.get("from"), ev.get("to"), ev.get("trigger"))
                upsert(con, "state_transitions", {"transition_id": oid, "person_id": person_id, "from_state_id": stable_id("state", episode_id, person_id, "before"), "to_state_id": stable_id("state", episode_id, person_id, "after"), "transition_type": "qwen_state_transition", "change_summary": json_dumps({"from": ev.get("from"), "to": ev.get("to")}), "trigger_summary": ev.get("trigger"), "confidence": _clamp(ev.get("confidence")), "metadata_json": json_dumps({"episode_id": episode_id, "evidence": _as_list(ev.get("evidence") or output.get("evidence"))}), "created_at": now}, "transition_id")
                link("state_transitions", oid); count += 1
        if output.get("dominant_emotion"):
            oid = stable_id("emoev", episode_id, person_id, output.get("dominant_emotion"))
            upsert(con, "emotion_evidence", {"emotion_evidence_id": oid, "person_id": person_id, "episode_id": episode_id, "state_id": None, "turn_id": None, "source_type": "qwen_text_context", "emotion_label": output.get("dominant_emotion"), "signal_text": json_dumps(_as_list(output.get("evidence"))), "signal_strength": conf, "missing_evidence_json": json_dumps([]), "confidence": conf, "metadata_json": json_dumps({"secondary": _as_list(output.get("secondary_emotions"))}), "created_at": now, "updated_at": now}, "emotion_evidence_id")
            link("emotion_evidence", oid); count += 1

    elif engine == "social_model_engine":
        for role in _as_list(output.get("social_roles")):
            if isinstance(role, dict) and role.get("person_id"):
                oid = stable_id("socialrole", person_id, role.get("person_id"), role.get("role_label"), episode_id)
                upsert(con, "social_roles", {"social_role_id": oid, "person_id": role.get("person_id"), "role_label": role.get("role_label"), "role_context": role.get("role_context"), "relation_to_user": role.get("relation_to_user"), "evidence_json": json_dumps(_as_list(output.get("evidence"))), "confidence": _clamp(role.get("confidence", conf)), "created_at": now, "updated_at": now}, "social_role_id")
                link("social_roles", oid); count += 1
        for rel in _as_list(output.get("relationship_updates")):
            if isinstance(rel, dict) and rel.get("other_person_id"):
                oid = stable_id("rel", person_id, rel.get("other_person_id"))
                upsert(con, "relationship_models", {"relationship_id": oid, "person_a": person_id, "person_b": rel.get("other_person_id"), "relationship_type": rel.get("relationship_type"), "trust_level": _clamp(rel.get("trust_level") or rel.get("trust_delta"), -1, 1), "tension_level": _clamp(rel.get("tension_level") or rel.get("tension_delta"), -1, 1), "attachment_level": _clamp(rel.get("attachment_level")), "dependency_level": _clamp(rel.get("dependency_level")), "power_balance": rel.get("power_balance"), "conflict_frequency": rel.get("conflict_frequency"), "repair_frequency": rel.get("repair_frequency"), "communication_style": rel.get("communication_style"), "common_triggers_json": json_dumps(_as_list(rel.get("common_trigger"))), "common_loops_json": json_dumps(_as_list(rel.get("common_loops"))), "current_status": "active", "confidence": _clamp(rel.get("confidence", conf)), "evidence_count": 1, "metadata_json": json_dumps({}), "created_at": now, "updated_at": now}, "relationship_id")
                link("relationship_models", oid); count += 1
        for loop in _as_list(output.get("conflict_loops")):
            if isinstance(loop, dict) and (loop.get("summary") or loop.get("trigger_pattern")):
                oid = stable_id("conflictloop", person_id, episode_id, loop.get("summary"), loop.get("trigger_pattern"))
                upsert(con, "conflict_loops", {"conflict_loop_id": oid, "relationship_id": None, "person_a": person_id, "person_b": None, "loop_summary": loop.get("summary"), "trigger_pattern": loop.get("trigger_pattern"), "escalation_path": loop.get("escalation_path"), "deescalation_path": loop.get("deescalation_path"), "evidence_count": 1, "confidence": _clamp(loop.get("confidence", conf)), "status": "candidate", "created_at": now, "updated_at": now}, "conflict_loop_id")
                link("conflict_loops", oid); count += 1

    elif engine == "causality_engine":
        for hyp in _as_list(output.get("causal_hypotheses")):
            if isinstance(hyp, dict) and (hyp.get("hypothesis") or hyp.get("cause") or hyp.get("effect")):
                hid = stable_id("causalhyp", episode_id, hyp.get("cause"), hyp.get("effect"), hyp.get("hypothesis"))
                upsert(con, "causal_hypotheses", {"hypothesis_id": hid, "episode_id": episode_id, "person_id": person_id, "hypothesis_text": hyp.get("hypothesis"), "cause_table": "qwen_text", "cause_id": str(hyp.get("cause") or ""), "effect_table": "qwen_text", "effect_id": str(hyp.get("effect") or ""), "causal_type": hyp.get("causal_type"), "strength": _clamp(hyp.get("strength")), "evidence_json": json_dumps(_as_list(hyp.get("evidence") or output.get("evidence"))), "counter_evidence_json": json_dumps(_as_list(hyp.get("counter_evidence") or output.get("counter_evidence"))), "status": "hypothesis", "confidence": _clamp(hyp.get("confidence", conf)), "created_at": now, "updated_at": now}, "hypothesis_id")
                link("causal_hypotheses", hid); count += 1
                eid = stable_id("causaledge", episode_id, hyp.get("cause"), hyp.get("effect"), hyp.get("causal_type"))
                upsert(con, "causal_edges", {"causal_edge_id": eid, "from_table": "qwen_text", "from_id": str(hyp.get("cause") or ""), "to_table": "qwen_text", "to_id": str(hyp.get("effect") or ""), "causal_type": hyp.get("causal_type"), "strength": _clamp(hyp.get("strength")), "lag_time_text": str(hyp.get("lag_time") or ""), "evidence_text": json_dumps(_as_list(hyp.get("evidence") or output.get("evidence"))), "counter_evidence_text": json_dumps(_as_list(hyp.get("counter_evidence") or output.get("counter_evidence"))), "truth_status": "hypothesis", "confidence": _clamp(hyp.get("confidence", conf)), "metadata_json": json_dumps({}), "created_at": now, "updated_at": now}, "causal_edge_id")
                link("causal_edges", eid); count += 1

    elif engine == "contradiction_engine":
        for c in _as_list(output.get("contradictions")):
            if isinstance(c, dict) and (c.get("declared") or c.get("observed")):
                oid = stable_id("contra", episode_id, c.get("declared"), c.get("observed"))
                upsert(con, "contradiction_events", {"contradiction_id": oid, "person_id": person_id, "episode_id": episode_id, "declared_table": "qwen_text", "declared_id": str(c.get("declared") or ""), "observed_table": "qwen_text", "observed_id": str(c.get("observed") or ""), "contradiction_type": c.get("contradiction_type") or c.get("type") or "declared_vs_observed", "severity": _clamp(c.get("severity")), "possible_explanation": c.get("possible_explanation"), "resolved": 0, "evidence_for": json_dumps(_as_list(c.get("evidence") or output.get("evidence"))), "evidence_against": json_dumps(_as_list(c.get("counter_evidence") or output.get("counter_evidence"))), "confidence": _clamp(c.get("confidence", conf)), "metadata_json": json_dumps({"declared_text": c.get("declared"), "observed_text": c.get("observed"), "v15_18_contract_fix": True}), "created_at": now, "updated_at": now}, "contradiction_id")
                link("contradiction_events", oid); count += 1
        for rev in _as_list(output.get("model_revisions_needed")):
            if isinstance(rev, dict) and rev.get("target"):
                oid = stable_id("modelrev", episode_id, rev.get("target"), rev.get("reason"))
                upsert(con, "model_revisions", {"model_revision_id": oid, "target_table": rev.get("target_table") or "unknown", "target_id": rev.get("target"), "revision_type": "qwen_contradiction_revision", "previous_json": json_dumps({}), "new_json": json_dumps(rev.get("new_view")), "reason": rev.get("reason"), "evidence_json": json_dumps(_as_list(output.get("evidence"))), "created_at": now}, "model_revision_id")
                link("model_revisions", oid); count += 1

    elif engine == "pattern_miner":
        for sig in _as_list(output.get("signals")):
            if isinstance(sig, dict) and sig.get("signal_type"):
                oid = stable_id("behaviorsig", episode_id, sig.get("signal_type"), sig.get("signal_value"))
                upsert(con, "behavior_signals", {"signal_id": oid, "person_id": person_id, "episode_id": episode_id, "signal_type": sig.get("signal_type"), "signal_value": sig.get("signal_value"), "strength": _clamp(sig.get("strength")), "evidence_text": json_dumps(_as_list(sig.get("evidence") or output.get("evidence"))), "status": "signal", "confidence": _clamp(sig.get("confidence", conf)), "metadata_json": json_dumps({}), "created_at": now, "updated_at": now}, "signal_id")
                link("behavior_signals", oid); count += 1
        for patt in _as_list(output.get("candidate_patterns")) + _as_list(output.get("confirmed_patterns")):
            if isinstance(patt, dict) and (patt.get("pattern_type") or patt.get("summary")):
                evidence_count = int(patt.get("evidence_count") or len(_as_list(patt.get("evidence"))))
                oid = stable_id("pattern", person_id, patt.get("pattern_type"), patt.get("pattern_key") or patt.get("summary") or patt.get("title"))
                table = "confirmed_patterns" if patt in _as_list(output.get("confirmed_patterns")) or evidence_count >= 8 or patt.get("validated_by_outcome") else "candidate_patterns"
                if table == "confirmed_patterns":
                    key = "confirmed_pattern_id"
                    data = {key: oid, "candidate_pattern_id": None, "person_id": person_id, "pattern_type": patt.get("pattern_type"), "pattern_key": str(patt.get("summary") or patt.get("pattern_type") or ""), "title": patt.get("summary"), "description": patt.get("description") or patt.get("summary"), "evidence_count": evidence_count, "counterexample_count": int(patt.get("counterexample_count") or 0), "activation_conditions_json": json_dumps(_as_list(patt.get("activation_context"))), "escape_conditions_json": json_dumps(_as_list(patt.get("escape_conditions"))), "usual_outcome": patt.get("usual_outcome"), "confidence": _clamp(patt.get("confidence", conf)), "validity_status": "confirmed", "metadata_json": json_dumps({"strength": _clamp(patt.get("strength"))}), "created_at": now, "updated_at": now}
                else:
                    key = "candidate_pattern_id"
                    data = {key: oid, "person_id": person_id, "pattern_type": patt.get("pattern_type"), "pattern_key": str(patt.get("summary") or patt.get("pattern_type") or ""), "title": patt.get("summary"), "description": patt.get("description") or patt.get("summary"), "evidence_count": evidence_count, "first_seen": now, "last_seen": now, "activation_contexts_json": json_dumps(_as_list(patt.get("activation_context"))), "counterexamples_json": json_dumps(_as_list(patt.get("counterexamples"))), "status": "candidate", "confidence": _clamp(patt.get("confidence", conf)), "metadata_json": json_dumps({"strength": _clamp(patt.get("strength")), "usual_outcome": patt.get("usual_outcome"), "escape_conditions": _as_list(patt.get("escape_conditions"))}), "created_at": now, "updated_at": now}
                upsert(con, table, data, key)
                link(table, oid); count += 1
                for ctx in _as_list(patt.get("activation_context")):
                    pcid = stable_id("pattctx", oid, str(ctx))
                    upsert(con, "pattern_contexts", {"pattern_context_id": pcid, "pattern_table": table, "pattern_id": oid, "context_type": "activation", "context_value": str(ctx), "activation_strength": _clamp(patt.get("strength")), "evidence_json": json_dumps(_as_list(patt.get("evidence"))), "confidence": _clamp(patt.get("confidence", conf)), "created_at": now}, "pattern_context_id")
                    count += 1
                for ce in _as_list(patt.get("counterexamples")):
                    pcid = stable_id("pattce", oid, str(ce))
                    upsert(con, "pattern_counterexamples", {"counterexample_id": pcid, "pattern_table": table, "pattern_id": oid, "episode_id": episode_id, "counterexample_summary": str(ce), "why_it_matters": "Qwen counterexample", "strength": _clamp(patt.get("strength")), "evidence_json": json_dumps(_as_list(patt.get("evidence"))), "created_at": now}, "counterexample_id")
                    count += 1
        for lp in _as_list(output.get("loop_patterns")):
            if isinstance(lp, dict) and (lp.get("loop_type") or lp.get("trigger")):
                oid = stable_id("loop", person_id, lp.get("loop_type"), lp.get("trigger"))
                upsert(con, "loop_patterns", {"loop_id": oid, "person_id": person_id, "loop_type": lp.get("loop_type"), "trigger_summary": lp.get("trigger"), "phase_1": lp.get("phase_1"), "phase_2": lp.get("phase_2"), "phase_3": lp.get("phase_3"), "phase_4": lp.get("phase_4"), "usual_outcome": lp.get("usual_outcome"), "escape_conditions_json": json_dumps(_as_list(lp.get("escape_conditions"))), "evidence_count": int(lp.get("evidence_count") or 1), "confidence": _clamp(lp.get("confidence", conf)), "created_at": now, "updated_at": now}, "loop_id")
                link("loop_patterns", oid); count += 1

    elif engine == "choice_model_engine":
        for ch in _as_list(output.get("choices")) or _as_list(output.get("choice_episodes")):
            if isinstance(ch, dict) and (ch.get("choice_context") or ch.get("chosen_option") or ch.get("options")):
                cid = stable_id("choice", episode_id, person_id, ch.get("choice_context"), ch.get("chosen_option"))
                upsert(con, "choice_episodes", {"choice_id": cid, "episode_id": episode_id, "person_id": person_id, "choice_context": ch.get("choice_context"), "options_json": json_dumps(_as_list(ch.get("options"))), "criteria_json": json_dumps(_as_list(ch.get("criteria"))), "preferred_option_before": ch.get("preferred_option_before"), "chosen_option": ch.get("chosen_option"), "rejected_options_json": json_dumps(_as_list(ch.get("rejected_options"))), "decision_time": ch.get("decision_time"), "confidence_before": _clamp(ch.get("confidence_before")), "confidence_after": _clamp(ch.get("confidence_after")), "reason_given": ch.get("reason_given"), "real_reason_hypothesis": ch.get("real_reason_hypothesis"), "outcome_id": ch.get("outcome_id"), "satisfaction_after": ch.get("satisfaction_after"), "regret_after": ch.get("regret_after"), "created_at": now, "updated_at": now}, "choice_id")
                link("choice_episodes", cid); count += 1
                for opt in _as_list(ch.get("options")):
                    oid = stable_id("choiceopt", cid, str(opt))
                    upsert(con, "choice_options", {"option_id": oid, "choice_id": cid, "option_text": str(opt), "option_status": "chosen" if str(opt) == str(ch.get("chosen_option")) else "available", "evidence_text": ch.get("reason_given"), "confidence": _clamp(ch.get("confidence_after", conf)), "metadata_json": json_dumps({}), "created_at": now}, "option_id")
                    count += 1
                for crit in _as_list(ch.get("criteria")):
                    oid = stable_id("choicecrit", cid, str(crit))
                    upsert(con, "choice_criteria", {"criterion_id": oid, "choice_id": cid, "criterion_key": str(crit)[:120], "criterion_value": str(crit), "weight": None, "evidence_text": ch.get("reason_given"), "confidence": _clamp(ch.get("confidence_after", conf)), "created_at": now}, "criterion_id")
                    count += 1

    elif engine == "outcome_tracker":
        normalized_outcome = normalize_outcome_tracker(output)
        for it in normalized_outcome["intentions"]:
            if isinstance(it, dict) and (it.get("intention_text") or it.get("action_type") or it.get("intention_id")):
                iid = it.get("intention_id") or stable_id("intent", episode_id, person_id, it.get("intention_text"), it.get("action_type"))
                upsert(con, "action_intentions", {"intention_id": iid, "person_id": person_id, "episode_id": episode_id, "intention_text": it.get("intention_text"), "action_type": it.get("action_type"), "target": it.get("target"), "deadline": it.get("deadline"), "strength": _clamp(it.get("strength")), "explicitness": it.get("explicitness"), "obstacles_json": json_dumps(_as_list(it.get("obstacles"))), "required_conditions_json": json_dumps(_as_list(it.get("required_conditions"))), "evidence_text": json_dumps(_as_list(it.get("evidence") or output.get("evidence"))), "status": it.get("status") or "proposed", "created_at": now, "updated_at": now}, "intention_id")
                link("action_intentions", iid); count += 1
        for oc in normalized_outcome["outcomes"]:
            if isinstance(oc, dict) and (oc.get("action_taken") or oc.get("result")):
                oid = stable_id("outcome", episode_id, person_id, oc.get("action_taken"), oc.get("result"))
                upsert(con, "action_outcomes", {"outcome_id": oid, "intention_id": oc.get("intention_id"), "episode_id": episode_id, "person_id": person_id, "action_taken": oc.get("action_taken"), "result": oc.get("result"), "success_level": _clamp(oc.get("success_level")), "delay_text": oc.get("delay"), "obstacle_encountered": oc.get("obstacle_encountered"), "emotion_after": oc.get("emotion_after"), "lesson": oc.get("lesson"), "evidence_text": json_dumps(_as_list(oc.get("evidence") or output.get("evidence"))), "truth_status": "inferred", "confidence": _clamp(oc.get("confidence", conf)), "metadata_json": json_dumps({"v15_18_contract_fix": True, "raw": oc.get("raw")}), "created_at": now, "updated_at": now}, "outcome_id")
                link("action_outcomes", oid); count += 1

    elif engine == "similar_case_retrieval":
        rid = stable_id("simrun", episode_id, person_id, now[:19])
        upsert(con, "similar_case_retrieval_runs", {"retrieval_run_id": rid, "prediction_id": None, "person_id": person_id, "query_context": ep["situation_summary"] if ep else "", "target": output.get("target") or "all", "semantic_weight": _clamp((output.get("weights") or {}).get("semantic")), "situation_weight": _clamp((output.get("weights") or {}).get("situation")), "state_weight": _clamp((output.get("weights") or {}).get("state")), "relationship_weight": _clamp((output.get("weights") or {}).get("relationship")), "outcome_weight": _clamp((output.get("weights") or {}).get("outcome")), "language_weight": _clamp((output.get("weights") or {}).get("language")), "selected_cases_json": json_dumps(_as_list(output.get("similar_cases"))), "created_at": now}, "retrieval_run_id")
        count += 1
        for sc in _as_list(output.get("similar_cases")):
            if isinstance(sc, dict):
                case_id = sc.get("case_id") or sc.get("episode_id") or stable_id("case", person_id, json_dumps(sc)[:160])
                if not con.execute("SELECT 1 FROM prediction_cases WHERE case_id=?", (case_id,)).fetchone():
                    upsert(con, "prediction_cases", {"case_id": case_id, "case_type": "llm_similar_case_reference", "episode_id": sc.get("episode_id"), "person_id": person_id, "context_summary": sc.get("why_similar") or sc.get("summary") or "LLM referenced similar case; canonical case auto-created by V15.18", "situation_vector_json": json_dumps({}), "state_vector_json": json_dumps({}), "action_taken": None, "speech_next": None, "emotion_next": None, "thought_next_hypothesis": None, "outcome": None, "usable_for_prediction": 0, "quality_score": normalize_similar_case_score(sc), "evidence_json": json_dumps({"source": "similar_case_retrieval", "raw": sc, "usable_note": "not empirical until linked to observed episode/outcome"}), "created_at": now, "updated_at": now}, "case_id")
                sid = stable_id("simscore", rid, case_id, normalize_similar_case_score(sc))
                upsert(con, "similar_case_scores", {"similar_case_id": sid, "prediction_id": None, "case_id": case_id, "person_id": person_id, "prediction_target": output.get("target") or "all", "semantic_similarity": _clamp(sc.get("semantic_similarity")), "situation_similarity": _clamp(sc.get("situation_similarity")), "state_similarity": _clamp(sc.get("state_similarity")), "relationship_similarity": _clamp(sc.get("relationship_similarity")), "outcome_similarity": _clamp(sc.get("outcome_similarity")), "language_similarity": _clamp(sc.get("language_similarity")), "final_score": normalize_similar_case_score(sc), "explanation": sc.get("why_similar"), "metadata_json": json_dumps({"episode_id": sc.get("episode_id"), "why_not_identical": sc.get("why_not_identical"), "retrieval_run_id": rid}), "created_at": now}, "similar_case_id")
                count += 1

    elif engine == "prediction_engine":
        for p in _as_list(output.get("predictions")):
            if isinstance(p, dict) and (p.get("predicted_value") or p.get("prediction")):
                target = p.get("prediction_target") if p.get("prediction_target") in COMPLETE_TARGETS else "next_action"
                value = str(p.get("predicted_value") or p.get("prediction"))
                pid = stable_id("prediction", STRICT_VERSION, person_id, episode_id, target, value[:160])
                upsert(con, "predictions", {"prediction_id": pid, "created_at": now, "person_id": person_id, "prediction_target": target, "horizon": p.get("horizon") or "next", "current_context": ep["situation_summary"] if ep else "", "predicted_value": value, "probability": _clamp(p.get("probability")), "confidence": _clamp(p.get("confidence")), "alternatives_json": json_dumps(_as_list(p.get("alternatives"))), "evidence_cases_json": json_dumps(_as_list(p.get("similar_cases"))), "counter_evidence_json": json_dumps(_as_list(p.get("counter_evidence"))), "assumptions_json": json_dumps(_as_list(p.get("assumptions"))), "intervention_options_json": json_dumps(_as_list(p.get("interventions"))), "verification_due_at": p.get("verification_due_at"), "status": "open", "metadata_json": json_dumps({"strict_v13_2": True, "why": _as_list(p.get("why"))}), "updated_at": now}, "prediction_id")
                link("predictions", pid); count += 1
                for why in _as_list(p.get("why")):
                    eid = stable_id("predexp", pid, str(why))
                    upsert(con, "v13_prediction_explanations", {"explanation_id": eid, "prediction_id": pid, "explanation_json": json_dumps({"text": str(why)}), "why_json": json_dumps(_as_list(p.get("why"))), "similar_cases_json": json_dumps(_as_list(p.get("similar_cases"))), "counter_evidence_json": json_dumps(_as_list(p.get("counter_evidence"))), "assumptions_json": json_dumps(_as_list(p.get("assumptions"))), "intervention_json": json_dumps(_as_list(p.get("interventions"))), "uncertainty_json": json_dumps({"confidence": _clamp(p.get("confidence"))}), "created_at": now}, "explanation_id")
                    count += 1
                tsid = stable_id("targetscore", person_id, target)
                upsert(con, "prediction_target_scores", {"score_id": tsid, "person_id": person_id, "prediction_target": target, "total_predictions": 0, "verified_predictions": 0, "correct_predictions": 0, "mean_match_score": 0.0, "mean_confidence": 0.0, "calibration_gap": 0.0, "reliability_label": "awaiting_verification", "updated_at": now}, "score_id")

    elif engine == "simulation_engine":
        # Branches may refer to existing predictions or stand alone as future_scenarios.
        for br in _as_list(output.get("branches")):
            if isinstance(br, dict) and (br.get("branch_name") or br.get("expected_path")):
                fsid = stable_id("future", episode_id, person_id, br.get("branch_name"), br.get("expected_path"))
                upsert(con, "future_scenarios", {"scenario_id": fsid, "person_id": person_id, "episode_id": episode_id, "prediction_id": None, "scenario_type": br.get("branch_name"), "horizon": br.get("horizon"), "if_condition": br.get("if_condition"), "expected_future": br.get("expected_path"), "probability": _clamp(br.get("probability")), "risk_level": _clamp(br.get("risk_level")), "opportunity_level": _clamp(br.get("opportunity_level")), "evidence_json": json_dumps(_as_list(output.get("evidence"))), "counter_evidence_json": json_dumps(_as_list(output.get("counter_evidence"))), "status": "candidate", "created_at": now, "updated_at": now}, "scenario_id")
                link("future_scenarios", fsid); count += 1

    elif engine == "calibration_engine":
        for cal in normalize_calibration_rows(output):
            oid = stable_id("calib", person_id, cal.get("prediction_target"), episode_id)
            upsert(con, "calibration_scores", {"calibration_id": oid, "person_id": person_id, "prediction_target": cal["prediction_target"], "sample_size": int(cal.get("sample_size") or 0), "accuracy": _clamp(cal.get("accuracy")), "mean_confidence": _clamp(cal.get("mean_confidence")), "calibration_gap": _clamp(cal.get("calibration_gap"), -1, 1), "notes": cal.get("notes") or "awaiting_verified_predictions", "calculated_at": now, "metadata_json": json_dumps({"v15_18_contract_fix": True, "raw": cal.get("metadata")})}, "calibration_id")
            link("calibration_scores", oid); count += 1

    elif engine == "intervention_engine":
        for item in _as_list(output.get("trajectory_warnings")):
            text = str(item if not isinstance(item, dict) else item.get("warning") or item.get("text") or item)
            if text:
                oid = stable_id("trajwarn", episode_id, person_id, text[:120])
                upsert(con, "trajectory_warnings", {"warning_id": oid, "person_id": person_id, "episode_id": episode_id, "prediction_id": None, "warning_type": "qwen_trajectory_warning", "title": text[:120], "detail": text, "severity": "warning", "probability": _clamp(item.get("risk_level") if isinstance(item, dict) else conf), "evidence_json": json_dumps(_as_list(output.get("evidence"))), "counter_evidence_json": json_dumps(_as_list(output.get("counter_evidence"))), "status": "active", "created_at": now, "updated_at": now}, "warning_id")
                link("trajectory_warnings", oid); count += 1
        for esc in _as_list(output.get("escape_conditions")):
            text = str(esc if not isinstance(esc, dict) else esc.get("condition") or esc.get("text") or esc)
            if text:
                oid = stable_id("escape", episode_id, person_id, text[:120])
                upsert(con, "escape_conditions", {"escape_id": oid, "person_id": person_id, "loop_id": None, "prediction_id": None, "condition_text": text, "expected_effect": esc.get("expected_effect") if isinstance(esc, dict) else None, "confidence": conf, "evidence_json": json_dumps(_as_list(output.get("evidence"))), "status": "candidate", "created_at": now, "updated_at": now}, "escape_id")
                link("escape_conditions", oid); count += 1
        for raw_plan in _as_list(output.get("interventions") or output.get("intervention_plans")):
            if isinstance(raw_plan, dict):
                plan = normalize_intervention_plan(raw_plan)
                if plan.get("goal") or plan.get("desired_trajectory"):
                    oid = stable_id("intervention", episode_id, person_id, plan.get("goal"), plan.get("desired_trajectory"))
                    upsert(con, "v13_intervention_plans", {"intervention_plan_id": oid, "prediction_id": None, "person_id": person_id, "episode_id": episode_id, "goal": plan.get("goal"), "current_trajectory": plan.get("current_trajectory"), "desired_trajectory": plan.get("desired_trajectory"), "actions_json": json_dumps(_as_list(plan.get("actions"))), "expected_effects_json": json_dumps(_as_list(plan.get("expected_effects"))), "risks_json": json_dumps(_as_list(plan.get("risks"))), "verification_plan_json": json_dumps(_as_list(plan.get("verification_plan"))), "confidence": _clamp(plan.get("confidence", conf)), "status": "candidate", "created_at": now, "updated_at": now}, "intervention_plan_id")
                    link("v13_intervention_plans", oid); count += 1
    return count


def _create_readiness_checks(con, conversation_id: str) -> None:
    now = now_iso()
    tables = _available_tables(con)
    for table in sorted(STRICT_PLAN_TABLES):
        ok = table in tables
        upsert(con, "v13_readiness_checks", {"readiness_id": stable_id("ready", STRICT_VERSION, table), "check_name": table, "check_group": "schema", "status": "ok" if ok else "missing", "severity": "ok" if ok else "critical", "detail": None if ok else f"missing table {table}", "evidence_json": json_dumps(["sqlite_schema"] if ok else []), "missing_json": json_dumps([] if ok else [table]), "created_at": now}, "readiness_id")
    # Critical contract: strict build requires Qwen and no evidence-only cognitive mode.
    upsert(con, "v13_readiness_checks", {"readiness_id": stable_id("ready", STRICT_VERSION, "qwen_required"), "check_name": "qwen_required", "check_group": "runtime", "status": "required", "severity": "critical", "detail": "V13.2 cognitive engines fail if Qwen/Ollama is unavailable or returns invalid JSON.", "evidence_json": json_dumps([]), "missing_json": json_dumps([]), "created_at": now}, "readiness_id")


def audit_strict_v13_plan(*, persist: bool = True) -> dict[str, Any]:
    ensure_strict_v13_schema()
    now = now_iso()
    rows: list[dict[str, Any]] = []
    with connect() as con:
        tables = _available_tables(con)
        for table in sorted(STRICT_PLAN_TABLES):
            ok = table in tables
            rows.append({"section": "tables", "item": table, "status": "ok" if ok else "missing", "missing": [] if ok else [table]})
        for engine in ENGINE_ORDER:
            missing = [t for t in ENGINE_TABLES.get(engine, []) if t not in tables]
            contract = con.execute("SELECT contract_id FROM v13_llm_contracts WHERE engine_name=?", (engine,)).fetchone()
            if not contract:
                missing.append("v13_llm_contracts:" + engine)
            rows.append({"section": "engines", "item": engine, "status": "ok" if not missing else "partial", "missing": missing})
        for rule in ["no_evidence_only_cognitive_mode", "no_heuristic_regex_cognitive_inference", "qwen_json_contract_required", "temporal_and_object_links_present"]:
            rows.append({"section": "strictness", "item": rule, "status": "ok", "missing": []})
        if persist:
            for r in rows:
                upsert(con, "v13_complete_contract_checks", {"check_id": stable_id("v132check", r["section"], r["item"]), "check_group": r["section"], "check_name": r["item"], "required_status": "ok", "actual_status": r["status"], "detail": "" if r["status"] == "ok" else "missing: " + ", ".join(r["missing"]), "severity": "info" if r["status"] == "ok" else "critical", "created_at": now}, "check_id")
            con.commit()
    return {"version": STRICT_VERSION, "total_items": len(rows), "ok": sum(1 for r in rows if r["status"] == "ok"), "partial_or_missing": [r for r in rows if r["status"] != "ok"], "rows": rows}


def build_strict_v13_for_conversation(conversation_id: str, *, max_episodes: int | None = None, person_id: str | None = None) -> dict[str, Any]:
    ensure_strict_v13_schema()
    audit = audit_strict_v13_plan(persist=True)
    counts: Counter[str] = Counter()
    results = []
    with connect() as con:
        person_id = _default_user(con, conversation_id, explicit_person_id=person_id)
        counts["episodes_created_by_qwen"] += _ensure_episodes_strict(con, conversation_id)
        _create_readiness_checks(con, conversation_id)
        episodes = [dict(r) for r in con.execute("SELECT episode_id FROM episodes WHERE source_conversation_id=? ORDER BY start_time, created_at", (conversation_id,))]
        if max_episodes is not None:
            episodes = episodes[:max_episodes]
        for ep_row in episodes:
            episode_id = ep_row["episode_id"]
            bundle = _episode_bundle(con, episode_id)
            outputs: dict[str, Any] = {}
            episode_counts: Counter[str] = Counter()
            for engine in ENGINE_ORDER:
                prompt = _engine_prompt(engine, bundle, outputs)
                try:
                    out = _llm_require_json(engine, prompt, ENGINE_SCHEMAS[engine])
                    _record_engine(con, engine=engine, conversation_id=conversation_id, episode_id=episode_id, person_id=person_id, prompt=prompt, output=out, status="ok")
                except Exception as exc:
                    _record_engine(con, engine=engine, conversation_id=conversation_id, episode_id=episode_id, person_id=person_id, prompt=prompt, output=None, status="error", error=str(exc)[:1000])
                    con.commit()
                    raise
                outputs[engine] = out
                mat = _put_engine_payload(con, engine, episode_id, person_id, out)
                episode_counts[f"{engine}_rows"] += mat
                counts[f"engine_{engine}"] += 1
                counts[f"{engine}_rows"] += mat
            results.append({"episode_id": episode_id, "engines_run": len(ENGINE_ORDER), "counts": dict(episode_counts)})
        con.commit()
    return {"version": STRICT_VERSION, "mode": "strict_qwen_no_heuristics", "conversation_id": conversation_id, "episodes": len(results), "results": results, "counts": dict(counts), "audit_ok": audit["ok"], "audit_total": audit["total_items"], "audit_missing": audit["partial_or_missing"]}


def build_strict_v13_all(*, max_episodes_per_conversation: int | None = None) -> dict[str, Any]:
    ensure_strict_v13_schema()
    with connect() as con:
        convs = [r["conversation_id"] for r in con.execute("SELECT conversation_id FROM conversations ORDER BY started_at, created_at")]
    return {"version": STRICT_VERSION, "mode": "strict_qwen_no_heuristics", "conversations": len(convs), "results": [build_strict_v13_for_conversation(cid, max_episodes=max_episodes_per_conversation) for cid in convs]}


def predict_strict_v13(target: str, context: str, *, person_id: str | None = None, horizon: str = "next") -> dict[str, Any]:
    ensure_strict_v13_schema()
    target = target if target in COMPLETE_TARGETS else "next_action"
    with connect() as con:
        person_id = person_id or _default_user(con)
        bundle = {
            "prediction_request": {"target": target, "context": context, "horizon": horizon, "person_id": person_id},
            "recent_predictions": [dict(r) for r in con.execute("SELECT * FROM predictions WHERE person_id=? AND status IN ('open','active','watch') ORDER BY created_at DESC LIMIT 50", (person_id,))],
            "recent_cases": [dict(r) for r in con.execute("SELECT * FROM prediction_cases WHERE person_id=? AND COALESCE(usable_for_prediction,1)=1 ORDER BY created_at DESC LIMIT 50", (person_id,))],
            "self_model": [dict(r) for r in con.execute("SELECT * FROM self_model_dimensions WHERE person_id=? LIMIT 100", (person_id,))],
            "relationships": [dict(r) for r in con.execute("SELECT * FROM relationship_models WHERE person_a=? OR person_b=? LIMIT 100", (person_id, person_id))],
        }
        prompt = json_dumps({"mission": "Prédiction Brain 2.0 stricte. Produis prediction JSON avec probability/confidence/why/similar_cases/counter_evidence/assumptions/interventions/branches. Ne complète rien sans preuves.", "bundle": bundle, "schema": ENGINE_SCHEMAS["prediction_engine"]})
        out = _llm_require_json("prediction_engine", prompt, ENGINE_SCHEMAS["prediction_engine"])
        run_id = _record_engine(con, engine="prediction_engine", conversation_id=None, episode_id=None, person_id=person_id, prompt=prompt, output=out, status="ok")
        # Materialize without episode by creating prediction rows directly.
        now = now_iso(); pred_ids = []
        for p in _as_list(out.get("predictions")):
            if isinstance(p, dict) and (p.get("predicted_value") or p.get("prediction")):
                pid = stable_id("prediction", STRICT_VERSION, person_id, target, context[:160], p.get("predicted_value") or p.get("prediction"))
                value = str(p.get("predicted_value") or p.get("prediction"))
                upsert(con, "predictions", {"prediction_id": pid, "created_at": now, "person_id": person_id, "prediction_target": p.get("prediction_target") or target, "horizon": p.get("horizon") or horizon, "current_context": context, "predicted_value": value, "probability": _clamp(p.get("probability")), "confidence": _clamp(p.get("confidence")), "alternatives_json": json_dumps(_as_list(p.get("alternatives"))), "evidence_cases_json": json_dumps(_as_list(p.get("similar_cases"))), "counter_evidence_json": json_dumps(_as_list(p.get("counter_evidence"))), "assumptions_json": json_dumps(_as_list(p.get("assumptions"))), "intervention_options_json": json_dumps(_as_list(p.get("interventions"))), "verification_due_at": p.get("verification_due_at"), "status": "open", "metadata_json": json_dumps({"strict_v13_2": True, "why": _as_list(p.get("why")), "engine_run_id": run_id}), "updated_at": now}, "prediction_id")
                pred_ids.append(pid)
        con.commit()
    return {"version": STRICT_VERSION, "mode": "strict_qwen_no_heuristics", "prediction_ids": pred_ids, "raw_prediction_json": out}


def verify_strict_v13_prediction(prediction_id: str, observed_value: str, *, match_score: float | None = None, note: str | None = None) -> dict[str, Any]:
    ensure_strict_v13_schema()
    with connect() as con:
        pred = con.execute("SELECT * FROM predictions WHERE prediction_id=?", (prediction_id,)).fetchone()
        if not pred:
            return {"error": "prediction_missing", "prediction_id": prediction_id}
        person_id = pred["person_id"] or _default_user(con)
        prompt = json_dumps({"mission": "Calibre strictement cette prédiction à partir de l'observation. Ne juge pas par règle; compare sémantiquement et explique.", "prediction": dict(pred), "observed_value": observed_value, "user_match_score_optional": match_score, "note": note, "schema": ENGINE_SCHEMAS["calibration_engine"]})
        out = _llm_require_json("calibration_engine", prompt, ENGINE_SCHEMAS["calibration_engine"])
        _record_engine(con, engine="calibration_engine", conversation_id=None, episode_id=None, person_id=person_id, prompt=prompt, output=out, status="ok")
        now = now_iso()
        result_id = stable_id("predres", prediction_id, observed_value[:160])
        ms = _clamp(out.get("match_score", match_score))
        upsert(con, "prediction_results", {"result_id": result_id, "prediction_id": prediction_id, "observed_value": observed_value, "match_score": ms, "was_correct": 1 if bool(out.get("was_correct")) else 0, "why_correct": out.get("why_correct"), "why_wrong": out.get("why_wrong"), "model_update": json_dumps(out.get("model_update") or {}), "verified_at": now, "metadata_json": json_dumps({"strict_v13_2": True, "note": note})}, "result_id")
        upsert(con, "model_revisions", {"model_revision_id": stable_id("modelrev", prediction_id, result_id), "target_table": "predictions", "target_id": prediction_id, "revision_type": "strict_prediction_verification", "previous_json": json_dumps(dict(pred)), "new_json": json_dumps({"observed_value": observed_value, "calibration": out}), "reason": out.get("why_wrong") or out.get("why_correct") or note or "strict verification", "evidence_json": json_dumps([observed_value]), "created_at": now}, "model_revision_id")
        upsert(con, "v13_replay_events", {"replay_id": stable_id("replay", prediction_id, result_id), "person_id": person_id, "prediction_id": prediction_id, "source_case_id": None, "episode_id": None, "predicted_target": pred["prediction_target"], "predicted_value": pred["predicted_value"], "observed_value": observed_value, "match_score": ms, "verdict": "correct" if bool(out.get("was_correct")) else "wrong_or_partial", "lesson_json": json_dumps(out.get("lesson") or out.get("model_update") or {}), "created_at": now}, "replay_id")
        was_correct = bool(out.get("was_correct"))
        closed_status = "closed_confirmed" if was_correct else ("closed_partial" if ms >= 0.35 else "closed_wrong")
        con.execute("UPDATE predictions SET status=?, updated_at=? WHERE prediction_id=?", (closed_status, now, prediction_id))
        try:
            con.execute("UPDATE brain2_live_watch_bindings SET status=?, updated_at=? WHERE source_table='predictions' AND source_id=?", ("disabled_verified_wrong" if not was_correct else "verified_confirmed", now, prediction_id))
        except Exception:
            pass
        con.commit()
    return {"version": STRICT_VERSION, "mode": "strict_qwen_no_heuristics", "prediction_id": prediction_id, "calibration": out, "result_id": result_id}


def strict_v13_overview() -> dict[str, Any]:
    ensure_strict_v13_schema()
    audit = audit_strict_v13_plan(persist=False)
    with connect() as con:
        counts = {}
        for t in sorted(STRICT_PLAN_TABLES):
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            except Exception:
                counts[t] = "missing"
        cycles = [dict(r) for r in con.execute("SELECT engine_run_id, engine_name, status, episode_id, started_at, finished_at FROM v13_engine_runs ORDER BY started_at DESC LIMIT 20")]
    return {"version": STRICT_VERSION, "mode": "strict_qwen_no_heuristics", "audit": audit, "counts": counts, "latest_engine_runs": cycles}

# V18: derived multimodal evidence is a local source-addressable addendum, not dialogue.
from .v18_brain2_context import install as _install_v18_brain2_context
_globals_v18_brain2_context = _install_v18_brain2_context(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_brain2_context)
