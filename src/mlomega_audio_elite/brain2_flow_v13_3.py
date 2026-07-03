from __future__ import annotations

"""V14 orchestration: subtopics, latent outcomes, direct flow, Pattern Mirror.

All cognitive content here is Qwen/Ollama JSON-contract based. The module does
not infer outcomes or subtopics with keyword rules; when Qwen is unavailable it
fails cleanly.
"""

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, init_db, upsert
from .ingest import ingest_audio, ingest_transcript_file
from .llm import OllamaJsonClient
from .utils import json_dumps, now_iso, stable_id, sha256_file

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
TRANSCRIPT_EXTS = {".json"}


def ensure_brain2_flow_schema() -> None:
    init_db()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation_subtopic_segments(
                subtopic_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                episode_id TEXT,
                start_turn_id TEXT,
                end_turn_id TEXT,
                start_time TEXT,
                end_time TEXT,
                subtopic_title TEXT NOT NULL,
                situation_type TEXT,
                summary TEXT,
                independent_analysis_needed INTEGER NOT NULL DEFAULT 1,
                evidence_turn_ids_json TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS latent_outcome_search_runs(
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                searched_pending_items_json TEXT NOT NULL,
                qwen_output_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS latent_outcome_links(
                link_id TEXT PRIMARY KEY,
                new_conversation_id TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_id TEXT NOT NULL,
                outcome_id TEXT,
                evidence_turn_id TEXT,
                evidence_text TEXT,
                outcome_type TEXT,
                outcome_summary TEXT,
                status_update TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS direct_flow_jobs(
                job_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                path_sha256 TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                conversation_ids_json TEXT DEFAULT '[]',
                run_v13 INTEGER NOT NULL DEFAULT 1,
                error_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- V18.7.1: engine-level durable checkpoints inside one Brain2
            -- conversation.  A shutdown during V14.7, for example, resumes at
            -- V14.7 instead of replaying V13 through V14.6.
            CREATE TABLE IF NOT EXISTS brain2_conversation_step_runs_v187(
                step_run_id TEXT PRIMARY KEY,
                pipeline_run_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                step_name TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT DEFAULT '{}',
                error_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(pipeline_run_id, conversation_id, step_name)
            );
            CREATE INDEX IF NOT EXISTS idx_brain2_step_v187_lookup
              ON brain2_conversation_step_runs_v187(pipeline_run_id, conversation_id, status);
            """
        )
        con.commit()


def _llm_json(system: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    return OllamaJsonClient().require_json(system, json_dumps(payload), schema_hint=schema, timeout=240)


def build_subtopic_segments(conversation_id: str) -> dict[str, Any]:
    """Use Qwen to split a long conversation into independent situation/subtopic segments."""
    ensure_brain2_flow_schema()
    schema = {
        "subtopics": [{
            "title": "",
            "situation_type": "technical_validation|relationship_tension|client_request|decision_point|emotional_reaction|planning|conflict|avoidance|commitment|self_reflection|other",
            "summary": "",
            "start_turn_id": "",
            "end_turn_id": "",
            "evidence_turn_ids": [],
            "episode_id": None,
            "confidence": 0.0,
        }],
        "missing_context": [],
        "confidence": 0.0,
    }
    with connect() as con:
        conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if not conv:
            raise ValueError(f"conversation_missing: {conversation_id}")
        turns = [dict(r) for r in con.execute("SELECT turn_id, idx, person_id, speaker_label, start_s, end_s, text, metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,))]
        episodes = [dict(r) for r in con.execute("SELECT episode_id, start_turn_id, end_turn_id, topic, situation_summary FROM episodes WHERE source_conversation_id=?", (conversation_id,))]
        from .v18_brain2_context import conversation_context_addenda
        context_addenda = conversation_context_addenda(con, conversation_id=conversation_id)
        payload = {
            "mission": "Découpe cette conversation longue en sous-sujets/situations indépendants. Pas de règles génériques: utilise uniquement les turns et preuves. Respecte metadata_json.kind/evidence_role: une observation système n’est jamais une parole ou préférence de William. Chaque sous-sujet doit être analysable séparément par Brain 2.0.",
            "conversation": dict(conv),
            "turns": turns,
            "known_episodes": episodes,
            "context_addenda": context_addenda,
            "schema": schema,
        }
        out = _llm_json("Tu es le Conversation Subtopic Segmenter strict. Réponds en JSON valide uniquement.", payload, schema)
        now = now_iso(); ids = []
        for i, st in enumerate(out.get("subtopics") or []):
            if not isinstance(st, dict) or not st.get("title"):
                continue
            sid = stable_id("subtopic", conversation_id, i, st.get("title"), st.get("start_turn_id"), st.get("end_turn_id"))
            upsert(con, "conversation_subtopic_segments", {
                "subtopic_id": sid,
                "conversation_id": conversation_id,
                "episode_id": st.get("episode_id"),
                "start_turn_id": st.get("start_turn_id"),
                "end_turn_id": st.get("end_turn_id"),
                "start_time": None,
                "end_time": None,
                "subtopic_title": st.get("title"),
                "situation_type": st.get("situation_type"),
                "summary": st.get("summary"),
                "independent_analysis_needed": 1,
                "evidence_turn_ids_json": json_dumps(st.get("evidence_turn_ids") or []),
                "confidence": float(st.get("confidence") or 0.0),
                "created_at": now,
                "updated_at": now,
            }, "subtopic_id")
            ids.append(sid)
        con.commit()
    return {"conversation_id": conversation_id, "subtopic_count": len(ids), "subtopic_ids": ids, "raw": out}


def discover_latent_outcomes_from_conversation(conversation_id: str, *, limit_pending: int = 80) -> dict[str, Any]:
    """Search a new conversation for dispersed evidence that resolves old intentions/predictions."""
    ensure_brain2_flow_schema()
    schema = {
        "resolved_items": [{
            "source_table": "action_intentions|predictions|choice_episodes|commitments",
            "source_id": "",
            "evidence_turn_id": "",
            "evidence_text": "",
            "outcome_type": "done|abandoned|postponed|changed|contradicted|validated|falsified|partial|unknown",
            "outcome_summary": "",
            "status_update": "",
            "confidence": 0.0,
        }],
        "counter_evidence": [],
        "missing_context": [],
        "confidence": 0.0,
    }
    with connect() as con:
        conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if not conv:
            raise ValueError(f"conversation_missing: {conversation_id}")
        turns = [dict(r) for r in con.execute("SELECT turn_id, idx, person_id, speaker_label, start_s, end_s, text, metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,))]
        from .v18_brain2_context import conversation_context_addenda
        context_addenda = conversation_context_addenda(con, conversation_id=conversation_id)
        pending: list[dict[str, Any]] = []
        for table, id_col, text_col, where in [
            ("action_intentions", "intention_id", "intention_text", "status NOT IN ('done','abandoned','contradicted')"),
            ("predictions", "prediction_id", "predicted_value", "status='open'"),
            ("commitments", "commitment_id", "content", "status NOT IN ('done','abandoned','closed')"),
            ("choice_episodes", "choice_id", "choice_context", "outcome_id IS NULL"),
        ]:
            try:
                for r in con.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC LIMIT ?", (limit_pending,)):
                    d = dict(r); d["_source_table"] = table; d["_source_id"] = d.get(id_col); d["_text"] = d.get(text_col); pending.append(d)
            except Exception:
                continue
        payload = {
            "mission": "Dans cette nouvelle conversation, cherche uniquement les indices dispersés qui résolvent, contredisent ou nuancent d'anciennes intentions/prédictions/choix/engagements. Respecte metadata_json.kind/evidence_role: observation système ≠ parole de William. Ne crée rien sans preuve textuelle exacte.",
            "new_conversation": dict(conv),
            "new_turns": turns,
            "context_addenda": context_addenda,
            "pending_items": pending[:limit_pending],
            "schema": schema,
        }
        out = _llm_json("Tu es le Latent Outcome Resolver strict. Réponds en JSON valide uniquement.", payload, schema)
        now = now_iso(); run_id = stable_id("latentout", conversation_id, now)
        upsert(con, "latent_outcome_search_runs", {"run_id": run_id, "conversation_id": conversation_id, "searched_pending_items_json": json_dumps(pending[:limit_pending]), "qwen_output_json": json_dumps(out), "created_at": now}, "run_id")
        links = []
        for item in out.get("resolved_items") or []:
            if not isinstance(item, dict) or not item.get("source_table") or not item.get("source_id"):
                continue
            link_id = stable_id("latentlink", conversation_id, item.get("source_table"), item.get("source_id"), item.get("evidence_turn_id"), item.get("outcome_summary"))
            upsert(con, "latent_outcome_links", {
                "link_id": link_id,
                "new_conversation_id": conversation_id,
                "source_table": item.get("source_table"),
                "source_id": item.get("source_id"),
                "outcome_id": None,
                "evidence_turn_id": item.get("evidence_turn_id"),
                "evidence_text": item.get("evidence_text"),
                "outcome_type": item.get("outcome_type"),
                "outcome_summary": item.get("outcome_summary"),
                "status_update": item.get("status_update"),
                "confidence": float(item.get("confidence") or 0.0),
                "created_at": now,
            }, "link_id")
            # Update status only when Qwen explicitly gives a status_update.
            st = item.get("status_update")
            if st and item.get("source_table") == "action_intentions":
                con.execute("UPDATE action_intentions SET status=?, updated_at=? WHERE intention_id=?", (st, now, item.get("source_id")))
            if st and item.get("source_table") == "predictions":
                con.execute("UPDATE predictions SET status=?, updated_at=? WHERE prediction_id=?", (st, now, item.get("source_id")))
            links.append(link_id)
        con.commit()
    return {"conversation_id": conversation_id, "run_id": run_id, "links_created": len(links), "link_ids": links, "raw": out}



def _record_brain2_step(
    *,
    pipeline_run_id: str,
    conversation_id: str,
    step_name: str,
    fn,
) -> dict[str, Any]:
    """Run one idempotent Brain2 engine with a durable completion record.

    The record is intentionally written *after* the engine returns. A power cut
    during an engine can therefore replay that one engine, but never a previous
    completed engine for the same conversation/run.
    """
    ensure_brain2_flow_schema()
    with connect() as con:
        row = con.execute(
            """SELECT status,result_json FROM brain2_conversation_step_runs_v187
               WHERE pipeline_run_id=? AND conversation_id=? AND step_name=?""",
            (pipeline_run_id, conversation_id, step_name),
        ).fetchone()
    if row and str(row["status"]) == "completed":
        try:
            cached = json.loads(row["result_json"] or "{}")
        except Exception:
            cached = {}
        return {"status": "skipped_checkpoint", "result": cached}

    now = now_iso()
    step_run_id = stable_id("brain2_step_v187", pipeline_run_id, conversation_id, step_name)
    try:
        value = fn()
    except Exception as exc:
        from .runtime_v18_7 import classify_failure
        failure = classify_failure(exc)
        with connect() as con:
            con.execute(
                """INSERT INTO brain2_conversation_step_runs_v187(
                     step_run_id,pipeline_run_id,conversation_id,step_name,status,result_json,error_text,created_at,updated_at,completed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(pipeline_run_id,conversation_id,step_name) DO UPDATE SET
                     status=excluded.status,result_json=excluded.result_json,error_text=excluded.error_text,updated_at=excluded.updated_at,completed_at=NULL""",
                (
                    step_run_id, pipeline_run_id, conversation_id, step_name,
                    "retryable_error" if failure.retryable else "blocked", "{}",
                    str(exc)[:2000], now, now,
                ),
            )
            con.commit()
        raise

    try:
        encoded = json_dumps(value if value is not None else {})
    except Exception:
        # Checkpoint metadata must never make a successful cognitive engine
        # look failed only because a non-JSON diagnostic object was returned.
        encoded = json_dumps({"result_repr": repr(value)[:2000]})
    with connect() as con:
        con.execute(
            """INSERT INTO brain2_conversation_step_runs_v187(
                 step_run_id,pipeline_run_id,conversation_id,step_name,status,result_json,error_text,created_at,updated_at,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pipeline_run_id,conversation_id,step_name) DO UPDATE SET
                 status='completed',result_json=excluded.result_json,error_text=NULL,updated_at=excluded.updated_at,completed_at=excluded.completed_at""",
            (step_run_id, pipeline_run_id, conversation_id, step_name, "completed", encoded, None, now, now, now),
        )
        con.commit()
    return {"status": "completed", "result": value}


def _run_brain2_stack_checkpointed(
    conversation_id: str,
    *,
    pipeline_run_id: str,
    person_id: str,
    trigger_type: str,
    run_v15_after: bool,
    run_periodic_export: bool,
) -> dict[str, Any]:
    """V18.7.1 resumable per-engine Brain2 stack.

    Unlike the legacy best-effort path, an optional V14/V17 engine is not
    silently converted into a terminal ``partial`` result. It is persisted as
    retryable/blocked and RESUME re-enters exactly that engine.
    """
    results: dict[str, Any] = {
        "conversation_id": conversation_id,
        "trigger_type": trigger_type,
        "checkpoint_run_id": pipeline_run_id,
        "steps": [],
        "resumed_steps": [],
        "step_results": {},
    }

    from .behavior_v13 import build_v13_for_conversation
    from .autonomous_v13_4 import run_autonomous_insights
    from .pattern_mirror_v14 import run_pattern_mirror
    from .auto_verification_v14_4 import auto_verify_latent_outcome_predictions
    from .people_openloops_v14_5 import run_v14_5_post_conversation
    from .interpersonal_state_v14_6 import run_v14_6_post_conversation
    from .proactive_interventions_v14_7 import run_proactive_interventions
    from .clarification_inbox_v14_8 import run_clarification_inbox, export_clarification_inbox
    from .brain2_longitudinal_cases_v17 import build_observed_cases_for_conversation, compute_global_case_similarities

    def _v17_cases() -> dict[str, Any]:
        cases = build_observed_cases_for_conversation(conversation_id, person_id=person_id)
        ids = list(cases.get("observed_case_ids") or []) if isinstance(cases, dict) else []
        return {
            "cases": cases,
            "similarities": compute_global_case_similarities(person_id=person_id, anchor_case_ids=ids) if ids else {},
        }

    def _clarifications() -> dict[str, Any]:
        return {
            "inbox": run_clarification_inbox(conversation_id, trigger_type=trigger_type),
            "export": export_clarification_inbox(),
        }

    steps: list[tuple[str, Any]] = [
        ("v13", lambda: build_v13_for_conversation(conversation_id, person_id=person_id, run_extensions=False)),
        ("v13_subtopics", lambda: build_subtopic_segments(conversation_id)),
        ("latent_outcomes", lambda: discover_latent_outcomes_from_conversation(conversation_id)),
        ("v14_4_auto_verification", lambda: auto_verify_latent_outcome_predictions(conversation_id=conversation_id)),
        ("v13_4_autonomous", lambda: run_autonomous_insights(conversation_id, trigger_type=trigger_type)),
        ("v14_pattern_mirror", lambda: run_pattern_mirror(conversation_id, trigger_type=trigger_type, scope="post_conversation_long_horizon")),
        ("v14_5_people_openloops", lambda: run_v14_5_post_conversation(conversation_id)),
        ("v14_6_interpersonal", lambda: run_v14_6_post_conversation(conversation_id)),
        ("v14_7_proactive_interventions", lambda: run_proactive_interventions(conversation_id, trigger_type=trigger_type)),
        ("v14_8_clarifications", _clarifications),
        ("v17_observed_cases", _v17_cases),
    ]
    if run_v15_after:
        steps.append(("v15_post_consolidation", lambda: run_v15_post_brain2_consolidation(person_id=person_id, run_periodic_export=run_periodic_export)))
    elif run_periodic_export:
        def _periodic() -> dict[str, Any]:
            from .self_model_export_v14_3 import run_due_periodic_consolidations
            return run_due_periodic_consolidations(periods=["hour", "day", "week", "month"], force=False, export_after=True)
        steps.append(("v14_3_periodic_export", _periodic))

    for step_name, fn in steps:
        out = _record_brain2_step(
            pipeline_run_id=pipeline_run_id,
            conversation_id=conversation_id,
            step_name=step_name,
            fn=fn,
        )
        if out["status"] == "skipped_checkpoint":
            results["resumed_steps"].append(step_name)
        else:
            results["steps"].append(step_name)
        results["step_results"][step_name] = out.get("result", {})
    results["status"] = "ok"
    return results


def run_brain2_deep_stack_for_conversation(
    conversation_id: str,
    *,
    person_id: str = "me",
    trigger_type: str = "direct_flow",
    run_v13: bool = True,
    run_v15_after: bool = True,
    run_periodic_export: bool = True,
    use_llm: bool = True,
    checkpoint_run_id: str | None = None,
) -> dict[str, Any]:
    """Run the calibrated Brain2 V13/V14 stack on an existing conversation.

    This is the important V15.15 refactor: Brain2 no longer needs to ingest audio
    itself in the BrainLive path.  V15.14 can materialize a complete multimodal
    event bundle as a normal conversation/turn timeline, then this function runs
    the same old Brain2 analysis stack on that conversation.

    It deliberately does not run Whisper, diarization or image analysis.  It only
    consumes already-created turns: transcript plus raw context observations
    such as [CONTEXT_VISION_RAW], [CONTEXT_WORLD_RAW] and
    [CONTEXT_AUDIO_RAW]. BrainLive predictions/interventions/outcomes stay in
    conversation raw_json side-channel metadata, not pseudo-dialogue.
    """
    ensure_brain2_flow_schema()
    results: dict[str, Any] = {"conversation_id": conversation_id, "trigger_type": trigger_type, "steps": []}
    if not run_v13:
        return {**results, "status": "skipped_v13"}
    if not use_llm:
        return {**results, "status": "skipped_llm_disabled", "llm_required": True}
    if checkpoint_run_id:
        return _run_brain2_stack_checkpointed(
            conversation_id,
            pipeline_run_id=str(checkpoint_run_id),
            person_id=person_id,
            trigger_type=trigger_type,
            run_v15_after=run_v15_after,
            run_periodic_export=run_periodic_export,
        )

    from .behavior_v13 import build_v13_for_conversation
    build_v13_for_conversation(conversation_id, person_id=person_id, run_extensions=False)
    results["steps"].append("v13")

    # build_v13_for_conversation already runs some of these in newer builds, but
    # the calls are idempotent and keep older databases/direct invocations safe.
    build_subtopic_segments(conversation_id)
    results["steps"].append("v13_subtopics")
    discover_latent_outcomes_from_conversation(conversation_id)
    results["steps"].append("latent_outcomes")

    try:
        from .auto_verification_v14_4 import auto_verify_latent_outcome_predictions
        auto_verify_latent_outcome_predictions(conversation_id=conversation_id)
        results["steps"].append("v14_4_auto_verification")
    except Exception as exc:
        results.setdefault("warnings", []).append({"step": "v14_4_auto_verification", "error": str(exc)[:500]})

    from .autonomous_v13_4 import run_autonomous_insights
    run_autonomous_insights(conversation_id, trigger_type=trigger_type)
    results["steps"].append("v13_4_autonomous")

    from .pattern_mirror_v14 import run_pattern_mirror
    run_pattern_mirror(conversation_id, trigger_type=trigger_type, scope="post_conversation_long_horizon")
    results["steps"].append("v14_pattern_mirror")

    try:
        from .people_openloops_v14_5 import run_v14_5_post_conversation
        run_v14_5_post_conversation(conversation_id)
        results["steps"].append("v14_5_people_openloops")
    except Exception as exc:
        results.setdefault("warnings", []).append({"step": "v14_5_people_openloops", "error": str(exc)[:500]})

    try:
        from .interpersonal_state_v14_6 import run_v14_6_post_conversation
        run_v14_6_post_conversation(conversation_id)
        results["steps"].append("v14_6_interpersonal")
    except Exception as exc:
        results.setdefault("warnings", []).append({"step": "v14_6_interpersonal", "error": str(exc)[:500]})

    try:
        from .proactive_interventions_v14_7 import run_proactive_interventions
        run_proactive_interventions(conversation_id, trigger_type=trigger_type)
        results["steps"].append("v14_7_proactive_interventions")
    except Exception as exc:
        results.setdefault("warnings", []).append({"step": "v14_7_proactive_interventions", "error": str(exc)[:500]})

    try:
        from .clarification_inbox_v14_8 import run_clarification_inbox, export_clarification_inbox
        run_clarification_inbox(conversation_id, trigger_type=trigger_type)
        export_clarification_inbox()
        results["steps"].append("v14_8_clarifications")
    except Exception as exc:
        results.setdefault("warnings", []).append({"step": "v14_8_clarifications", "error": str(exc)[:500]})

    # V17: after V13/V14 have extracted situations/states/choices/outcomes,
    # materialize the episode as an empirical observed case.  This is the bridge
    # that lets future runs compare today with the whole longitudinal history.
    try:
        from .brain2_longitudinal_cases_v17 import build_observed_cases_for_conversation, compute_global_case_similarities
        cases = build_observed_cases_for_conversation(conversation_id, person_id=person_id)
        results["observed_cases_v17"] = cases
        ids = list(cases.get("observed_case_ids") or [])
        if ids:
            results["similarities_v17"] = compute_global_case_similarities(person_id=person_id, anchor_case_ids=ids)
        results["steps"].append("v17_observed_cases")
    except Exception as exc:
        results.setdefault("warnings", []).append({"step": "v17_observed_cases", "error": str(exc)[:500]})

    if run_v15_after:
        results["v15"] = run_v15_post_brain2_consolidation(person_id=person_id, run_periodic_export=run_periodic_export)
    elif run_periodic_export:
        try:
            from .self_model_export_v14_3 import run_due_periodic_consolidations
            run_due_periodic_consolidations(periods=["hour", "day", "week", "month"], force=False, export_after=True)
            results["steps"].append("v14_3_periodic_export")
        except Exception as exc:
            results.setdefault("warnings", []).append({"step": "v14_3_periodic_export", "error": str(exc)[:500]})
    results["status"] = "ok" if not results.get("warnings") else "partial"
    return results


def run_v15_post_brain2_consolidation(
    *,
    person_id: str = "me",
    run_periodic_export: bool = True,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Run only the V15 layers after Brain2 has digested conversations.

    Order matters:
    1. V15.12 coordinates BrainLive evidence and Brain2 predictions.
    2. V15.13 updates the stratified Life Model by patch.
    3. V15.9 builds the live-ready feed for tomorrow's BrainLive loop.
    4. V14.3 exports readable self-model snapshots if due.
    """
    out: dict[str, Any] = {"person_id": person_id, "steps": [], "layer_status": {}}
    try:
        from .brain2_longitudinal_cases_v17 import run_longitudinal_consolidation
        out["longitudinal_v17"] = run_longitudinal_consolidation(person_id=person_id, period="day", use_llm=use_llm, run_periodic_mirror_layer=False)
        out["steps"].append("v17_longitudinal_consolidation")
        out["layer_status"]["v17_longitudinal"] = "ok"
    except Exception as exc:
        out["layer_status"]["v17_longitudinal"] = "failed"
        out.setdefault("warnings", []).append({"step": "v17_longitudinal_consolidation", "error": str(exc)[:500]})

    try:
        from .brainlive_brain2_coordination_v15_12 import run_brainlive_brain2_coordination
        out["coordination_v15_12"] = run_brainlive_brain2_coordination(person_id=person_id, use_llm=use_llm, timeout=180.0)
        out["steps"].append("v15_12_coordination")
        out["layer_status"]["v15_12"] = "ok"
    except Exception as exc:
        out["layer_status"]["v15_12"] = "failed"
        out.setdefault("warnings", []).append({"step": "v15_12_coordination", "error": str(exc)[:500]})

    try:
        from .brain2_life_model_updater_v15_13 import run_brain2_life_model_update
        out["life_model_v15_13"] = run_brain2_life_model_update(person_id=person_id, use_llm=use_llm, timeout=180.0, limit=120)
        out["steps"].append("v15_13_life_model_update")
        out["layer_status"]["v15_13"] = "ok"
    except Exception as exc:
        out["layer_status"]["v15_13"] = "failed"
        out.setdefault("warnings", []).append({"step": "v15_13_life_model_update", "error": str(exc)[:500]})

    if out["layer_status"].get("v15_13") == "failed":
        out["layer_status"]["v15_9"] = "skipped_due_to_v15_13_failure"
        out.setdefault("warnings", []).append({"step": "v15_9_live_ready_model", "error": "skipped because V15.13 Life Model update failed"})
    else:
        try:
            from .brainlive_personal_model_v15_9 import build_brain2_live_personal_model
            out["live_ready_v15_9"] = build_brain2_live_personal_model(person_id=person_id, use_llm=use_llm, timeout=180.0, limit=80)
            out["steps"].append("v15_9_live_ready_model")
            out["layer_status"]["v15_9"] = "ok"
        except Exception as exc:
            out["layer_status"]["v15_9"] = "failed"
            out.setdefault("warnings", []).append({"step": "v15_9_live_ready_model", "error": str(exc)[:500]})

    if run_periodic_export:
        try:
            from .self_model_export_v14_3 import run_due_periodic_consolidations
            out["periodic_exports"] = run_due_periodic_consolidations(periods=["hour", "day", "week", "month"], force=False, export_after=True)
            out["steps"].append("v14_3_periodic_export")
            out["layer_status"]["v14_3"] = "ok"
        except Exception as exc:
            out["layer_status"]["v14_3"] = "failed"
            out.setdefault("warnings", []).append({"step": "v14_3_periodic_export", "error": str(exc)[:500]})
    out["status"] = "ok" if not out.get("warnings") else "partial"
    return out


def run_deep_stack_for_existing_conversations(
    conversation_ids: list[str],
    *,
    person_id: str = "me",
    trigger_type: str = "existing_conversation",
    run_v13: bool = True,
    run_v15_after_all: bool = True,
) -> dict[str, Any]:
    """Run V13/V14 on already-materialized conversations, then V15 once.

    Used by V15.15 after V15.14 has exported BrainLive event bundles to Brain2
    conversations.  This is the no-duplicate path: capture once in BrainLive,
    assemble once, then analyze as the classic Brain2 pipeline.
    """
    processed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for cid in conversation_ids:
        try:
            processed.append(run_brain2_deep_stack_for_conversation(
                cid,
                person_id=person_id,
                trigger_type=trigger_type,
                run_v13=run_v13,
                run_v15_after=False,
                run_periodic_export=False,
            ))
        except Exception as exc:
            errors.append({"conversation_id": cid, "error": str(exc)[:1000]})
    v15 = run_v15_post_brain2_consolidation(person_id=person_id) if run_v15_after_all else None
    return {"status": "ok" if not errors else "partial", "person_id": person_id, "conversation_ids": conversation_ids, "processed": len(processed), "errors": errors, "v15": v15}


def process_incoming_path(path: Path, *, run_v13: bool = True, preprocess_long_audio: bool = True, max_chunk_seconds: int = 900) -> dict[str, Any]:
    """One-shot classic flow: file arrives -> ingest -> V13/V14 -> V15.

    This remains for old/imported files.  In the BrainLive daily path, V15.15
    bypasses audio ingestion and calls run_deep_stack_for_existing_conversations
    on event-bundle conversations created by V15.14.
    """
    ensure_brain2_flow_schema()
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    kind = "audio" if path.suffix.lower() in AUDIO_EXTS else "transcript" if path.suffix.lower() in TRANSCRIPT_EXTS else "unsupported"
    now = now_iso(); job_id = stable_id("flowjob", str(path), sha256_file(path) if path.is_file() else now)
    conv_ids: list[str] = []
    error = None
    try:
        if kind == "audio":
            audio_paths = [path]
            if preprocess_long_audio:
                from .audio_preprocess import preprocess_audio
                prep = preprocess_audio(path, remove_silence=False, max_chunk_seconds=max_chunk_seconds)
                audio_paths = [Path(p) for p in prep.get("chunks", [])] or [path]
            for ap in audio_paths:
                cid = ingest_audio(ap)
                try:
                    from .audio_preprocess import apply_audio_segment_mapping
                    apply_audio_segment_mapping(cid, ap)
                except Exception:
                    pass
                conv_ids.append(cid)
        elif kind == "transcript":
            conv_ids.append(ingest_transcript_file(path))
        else:
            raise ValueError(f"extension non supportée: {path.suffix}")
        if run_v13:
            run_deep_stack_for_existing_conversations(conv_ids, person_id="me", trigger_type="direct_flow", run_v13=True, run_v15_after_all=True)
        status = "ok"
    except Exception as exc:
        status = "error"; error = str(exc)[:2000]
        raise
    finally:
        with connect() as con:
            upsert(con, "direct_flow_jobs", {
                "job_id": job_id,
                "path": str(path),
                "path_sha256": sha256_file(path) if path.exists() and path.is_file() else None,
                "kind": kind,
                "status": status if 'status' in locals() else "error",
                "conversation_ids_json": json_dumps(conv_ids),
                "run_v13": 1 if run_v13 else 0,
                "error_text": error,
                "created_at": now,
                "updated_at": now_iso(),
            }, "job_id")
            con.commit()
    return {"job_id": job_id, "status": status, "kind": kind, "conversation_ids": conv_ids}

def watch_inbox(*, audio_dir: Path | None = None, transcript_dir: Path | None = None, poll_seconds: float = 30.0, once: bool = False, run_v13: bool = True) -> dict[str, Any]:
    settings = get_settings()
    audio_dir = Path(audio_dir or (settings.root_dir / "inbox" / "audio")).expanduser().resolve()
    transcript_dir = Path(transcript_dir or (settings.root_dir / "inbox" / "transcripts")).expanduser().resolve()
    processed_dir = settings.root_dir / "inbox" / "processed"
    failed_dir = settings.root_dir / "inbox" / "failed"
    for d in [audio_dir, transcript_dir, processed_dir, failed_dir]:
        d.mkdir(parents=True, exist_ok=True)
    processed = []
    while True:
        candidates = []
        candidates += [p for p in sorted(audio_dir.iterdir()) if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
        candidates += [p for p in sorted(transcript_dir.iterdir()) if p.is_file() and p.suffix.lower() in TRANSCRIPT_EXTS]
        for p in candidates:
            try:
                result = process_incoming_path(p, run_v13=run_v13)
                dest = processed_dir / p.name
                shutil.move(str(p), str(dest))
                result["moved_to"] = str(dest)
                processed.append(result)
            except Exception as exc:
                dest = failed_dir / p.name
                try:
                    shutil.move(str(p), str(dest))
                except Exception:
                    pass
                processed.append({"path": str(p), "status": "error", "error": str(exc)[:500], "moved_to": str(dest)})
        if once:
            return {"processed": processed, "audio_dir": str(audio_dir), "transcript_dir": str(transcript_dir)}
        time.sleep(poll_seconds)

# V18: Brain2 stage gates, explicit owner scope, and reference-safe outcome linking.
from .v18_brain2_flow import install as _install_v18_brain2_flow
_globals_v18_brain2_flow = _install_v18_brain2_flow(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_brain2_flow)
