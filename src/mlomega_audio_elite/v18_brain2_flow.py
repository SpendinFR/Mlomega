"""V18 Brain2 orchestration: scoped ownership, durable stage gates, no best effort.

This module replaces the old 'collect warnings and continue' orchestration for
all paths that can feed V17, the Life Model, or live hooks.  Optional user-facing
analysis remains optional, but a failed/ambiguous core never advances to a
longitudinal or canonical model write.
"""
from __future__ import annotations
import json
from typing import Any, Callable

from .db import connect, insert_only, upsert, write_transaction
from .governance_v18 import (
    Scope,
    StageGateError,
    begin_run,
    conversation_in_scope,
    finish_stage,
    register_conversation_scope,
    start_stage,
    strict_many,
    strict_one,
    update_run,
)
from .utils import json_dumps, now_iso, stable_id

_ALLOWED_LATENT = {
    "action_intentions": {"id": "intention_id", "text": "intention_text", "status": {"done", "abandoned", "postponed", "active", "contradicted"}},
    "predictions": {"id": "prediction_id", "text": "predicted_value", "status": {"closed_confirmed", "closed_wrong", "closed_partial", "expired", "indeterminate"}},
}


def _ensure_conversation_scope(conversation_id: str, person_id: str) -> None:
    with connect() as con:
        if conversation_in_scope(con, conversation_id=conversation_id, person_id=person_id):
            return
        export = strict_one(
            con,
            "SELECT export_id,bundle_id FROM brainlive_brain2_event_exports_v1514 WHERE conversation_id=? AND person_id=? AND export_status IN ('exported','active','ok') ORDER BY updated_at DESC LIMIT 1",
            (conversation_id, person_id),
            purpose="derive conversation export owner",
        )
    if export:
        register_conversation_scope(conversation_id=conversation_id, person_id=person_id, evidence_kind="explicit_export", evidence=export)
        return
    raise ValueError("conversation is not proven in supplied person scope")


def install(module: Any) -> dict[str, Any]:
    old_ensure = module.ensure_brain2_flow_schema

    def ensure_brain2_flow_schema() -> None:
        old_ensure()

    def discover_latent_outcomes_from_conversation(conversation_id: str, *, person_id: str, limit_pending: int = 80) -> dict[str, Any]:
        """Resolve only scoped objects with an in-conversation evidence turn.

        LLM-supplied ids are treated as untrusted references. Unknown tables,
        source ids, cross-owner rows, or evidence outside the new conversation
        are rejected and recorded as rejected references rather than converted
        to a silent no-op or a cross-person mutation.
        """
        if not person_id:
            raise ValueError("V18 latent outcomes requires explicit person_id")
        ensure_brain2_flow_schema()
        _ensure_conversation_scope(conversation_id, person_id)
        schema = {
            "resolved_items": [{
                "source_table": "action_intentions|predictions", "source_id": "", "evidence_turn_id": "",
                "outcome_type": "done|abandoned|postponed|changed|contradicted|validated|falsified|partial|unknown",
                "outcome_summary": "", "status_update": "", "confidence": 0.0,
            }],
            "counter_evidence": [], "missing_context": [], "confidence": 0.0,
        }
        with connect() as con:
            conv = strict_one(con, "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,), purpose="latent outcome conversation")
            if not conv:
                raise ValueError("conversation_missing")
            turns = strict_many(con, "SELECT turn_id,idx,person_id,speaker_label,start_s,end_s,text,metadata_json FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,), purpose="latent outcome turns")
            turn_map = {str(row["turn_id"]): row for row in turns}
            pending: list[dict[str, Any]] = []
            for table, spec in _ALLOWED_LATENT.items():
                id_col = str(spec["id"]); text_col = str(spec["text"])
                if table == "predictions":
                    where = "person_id=? AND status IN ('open','active','watch','due')"
                else:
                    where = "person_id=? AND status NOT IN ('done','abandoned','contradicted','closed')"
                rows = strict_many(con, f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC LIMIT ?", (person_id, int(limit_pending)), purpose=f"latent scoped {table}")
                for row in rows:
                    pending.append({"source_table": table, "source_id": row[id_col], "text": row.get(text_col), "status": row.get("status")})
            payload = {
                "mission": "Relier seulement une preuve textuelle exacte de cette conversation à un objet en attente explicitement fourni. Une référence hors liste est invalide. Observation système n'est pas parole utilisateur.",
                "conversation": dict(conv), "turns": turns, "pending_items": pending,
                "schema": schema,
            }
            out = module._llm_json("Tu es le resolveur V18 d'outcomes, JSON strict.", payload, schema)
            if not isinstance(out, dict):
                raise ValueError("latent outcome LLM did not return object")
            run_id = stable_id("v18latent", conversation_id, person_id, now_iso())
            insert_only(con, "latent_outcome_search_runs", {
                "run_id": run_id, "conversation_id": conversation_id,
                "searched_pending_items_json": json_dumps(pending), "qwen_output_json": json_dumps(out), "created_at": now_iso(),
            }, on_conflict="ignore")
            allowed = {(str(item["source_table"]), str(item["source_id"])) for item in pending}
            links: list[str] = []; rejected: list[dict[str, Any]] = []
            for item in out.get("resolved_items") or []:
                if not isinstance(item, dict):
                    rejected.append({"reason":"not_object"}); continue
                table = str(item.get("source_table") or "")
                sid = str(item.get("source_id") or "")
                tid = str(item.get("evidence_turn_id") or "")
                if (table, sid) not in allowed or table not in _ALLOWED_LATENT:
                    rejected.append({"source_table":table,"source_id":sid,"reason":"untrusted_or_cross_owner_reference"}); continue
                if tid not in turn_map:
                    rejected.append({"source_table":table,"source_id":sid,"reason":"evidence_turn_not_in_current_conversation"}); continue
                status_update = str(item.get("status_update") or "")
                if status_update and status_update not in _ALLOWED_LATENT[table]["status"]:
                    rejected.append({"source_table":table,"source_id":sid,"reason":"invalid_status_transition","status":status_update}); continue
                # Re-query under owner scope immediately before mutation.
                pk = str(_ALLOWED_LATENT[table]["id"])
                source = strict_one(con, f"SELECT * FROM {table} WHERE {pk}=? AND person_id=?", (sid, person_id), purpose="latent source revalidation")
                if not source:
                    rejected.append({"source_table":table,"source_id":sid,"reason":"source_not_owned_at_commit"}); continue
                ev = turn_map[tid]
                link_id = stable_id("v18latentlink", conversation_id, table, sid, tid, str(item.get("outcome_summary") or ""))
                insert_only(con, "latent_outcome_links", {
                    "link_id": link_id, "new_conversation_id": conversation_id, "source_table": table, "source_id": sid,
                    "outcome_id": None, "evidence_turn_id": tid, "evidence_text": ev.get("text"),
                    "outcome_type": item.get("outcome_type"), "outcome_summary": item.get("outcome_summary"),
                    "status_update": status_update or None, "confidence": min(1.0, max(0.0, float(item.get("confidence") or 0.0))), "created_at": now_iso(),
                }, on_conflict="ignore")
                if status_update:
                    con.execute(f"UPDATE {table} SET status=?, updated_at=? WHERE {pk}=? AND person_id=?", (status_update, now_iso(), sid, person_id))
                links.append(link_id)
            # A durable rejection record avoids the historic silent no-op.
            if rejected:
                con.execute("CREATE TABLE IF NOT EXISTS v18_rejected_llm_references(rejection_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,person_id TEXT NOT NULL,stage TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)")
                insert_only(con, "v18_rejected_llm_references", {"rejection_id":stable_id("v18reject",run_id),"run_id":run_id,"person_id":person_id,"stage":"latent_outcomes","payload_json":json_dumps(rejected),"created_at":now_iso()}, on_conflict="ignore")
            con.commit()
        return {"conversation_id":conversation_id,"person_id":person_id,"run_id":run_id,"links_created":len(links),"link_ids":links,"rejected_references":rejected,"raw":out,"status":"ok"}

    def _run_stage(run_id: str, name: str, fn: Callable[[], Any], *, required: bool = True) -> Any:
        with connect() as con, write_transaction(con):
            start_stage(con, run_id=run_id, stage_name=name, required=required)
        try:
            result = fn()
            if isinstance(result, dict) and str(result.get("status", "ok")) not in {"ok", "completed", "skipped_llm_disabled"}:
                raise StageGateError(f"{name} status={result.get('status')}")
        except Exception as exc:
            with connect() as con, write_transaction(con):
                finish_stage(con, run_id=run_id, stage_name=name, result={"status":"error"}, status="failed", error_text=str(exc))
            raise
        with connect() as con, write_transaction(con):
            finish_stage(con, run_id=run_id, stage_name=name, result=result if isinstance(result, dict) else {"result":str(result)}, status="completed")
        return result

    def _checkpoint_engine(
        *,
        pipeline_run_id: str,
        conversation_id: str,
        step_name: str,
        fn: Callable[[], Any],
        result: dict[str, Any],
    ) -> Any:
        """Persist one completed cognitive engine before the next begins.

        ``module._record_brain2_step`` writes the durable completion marker only
        after ``fn`` has returned.  Therefore a power loss in an engine repeats
        at most that engine, never any earlier completed engine for the same
        conversation and post-stop run.
        """
        outcome = module._record_brain2_step(
            pipeline_run_id=pipeline_run_id,
            conversation_id=conversation_id,
            step_name=step_name,
            fn=fn,
        )
        if str(outcome.get("status")) == "skipped_checkpoint":
            result["resumed_steps"].append(step_name)
        else:
            result["steps"].append(step_name)
        value = outcome.get("result", {})
        result["step_results"][step_name] = value
        return value

    def run_brain2_deep_stack_for_conversation(
        conversation_id: str,
        *,
        person_id: str,
        trigger_type: str = "direct_flow",
        run_v13: bool = True,
        run_v15_after: bool = True,
        run_periodic_export: bool = True,
        use_llm: bool = True,
        checkpoint_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one Brain2 conversation with engine-level durable checkpoints.

        When called by V18.7 post-stop, ``checkpoint_run_id`` is the canonical
        day run.  A resume then reuses every V13/V14/V17 engine completion in
        that same run.  Direct/manual use retains its own durable run ID.
        """
        if not person_id:
            raise ValueError("V18 Brain2 stack requires explicit person_id")
        ensure_brain2_flow_schema()
        _ensure_conversation_scope(conversation_id, person_id)
        owns_run = not bool(checkpoint_run_id)
        if owns_run:
            scope = Scope(person_id=person_id, mode="post_stop")
            run_id = begin_run(
                pipeline_name="brain2_deep_stack",
                scope=scope,
                input_manifest={"conversation_id": conversation_id, "trigger": trigger_type},
            )
        else:
            run_id = str(checkpoint_run_id)

        result: dict[str, Any] = {
            "version": "18.7.1-brain2-flow",
            "run_id": run_id,
            "conversation_id": conversation_id,
            "person_id": person_id,
            "steps": [],
            "resumed_steps": [],
            "step_results": {},
        }
        try:
            if not run_v13:
                if owns_run:
                    update_run(run_id, status="completed")
                return {**result, "status": "skipped_v13"}
            if not use_llm:
                if owns_run:
                    update_run(run_id, status="completed")
                return {**result, "status": "skipped_llm_disabled", "llm_required": True}

            from .behavior_v13 import build_v13_for_conversation
            from .auto_verification_v14_4 import auto_verify_latent_outcome_predictions
            from .autonomous_v13_4 import run_autonomous_insights
            from .pattern_mirror_v14 import run_pattern_mirror
            from .people_openloops_v14_5 import run_v14_5_post_conversation
            from .interpersonal_state_v14_6 import run_v14_6_post_conversation
            from .proactive_interventions_v14_7 import run_proactive_interventions
            from .clarification_inbox_v14_8 import run_clarification_inbox, export_clarification_inbox
            from .brain2_longitudinal_cases_v17 import (
                build_observed_cases_for_conversation,
                compute_global_case_similarities,
            )

            def _clarifications() -> dict[str, Any]:
                return {
                    "inbox": run_clarification_inbox(
                        conversation_id, person_id=person_id, trigger_type=trigger_type
                    ),
                    "export": export_clarification_inbox(),
                }

            cases_cache: dict[str, Any] = {}

            def _v17_cases() -> dict[str, Any]:
                value = build_observed_cases_for_conversation(
                    conversation_id, person_id=person_id
                )
                cases_cache["value"] = value
                return value

            def _v17_similarity() -> dict[str, Any]:
                value = cases_cache.get("value")
                if not isinstance(value, dict):
                    # On resume V17 cases may have been checkpointed. Retrieve
                    # its persisted result rather than recomputing the engine.
                    with connect() as con:
                        row = con.execute(
                            """SELECT result_json FROM brain2_conversation_step_runs_v187
                               WHERE pipeline_run_id=? AND conversation_id=? AND step_name='v17_cases'""",
                            (run_id, conversation_id),
                        ).fetchone()
                    try:
                        value = json.loads(row["result_json"] or "{}") if row else {}
                    except Exception:
                        value = {}
                ids = list(value.get("observed_case_ids") or []) if isinstance(value, dict) else []
                if not ids:
                    return {"status": "ok", "similarity_edges": 0}
                return compute_global_case_similarities(
                    person_id=person_id, anchor_case_ids=ids, as_of=None
                )

            engines: list[tuple[str, Callable[[], Any]]] = [
                ("v13_core", lambda: build_v13_for_conversation(
                    conversation_id, person_id=person_id, run_extensions=False
                )),
                ("v13_subtopics", lambda: module.build_subtopic_segments(conversation_id)),
                ("latent_outcomes", lambda: discover_latent_outcomes_from_conversation(
                    conversation_id, person_id=person_id
                )),
                ("v14_auto_verify", lambda: auto_verify_latent_outcome_predictions(
                    conversation_id=conversation_id, person_id=person_id
                )),
                ("v13_4_autonomous", lambda: run_autonomous_insights(
                    conversation_id, person_id=person_id, trigger_type=trigger_type
                )),
                ("v14_mirror", lambda: run_pattern_mirror(
                    conversation_id, person_id=person_id, trigger_type=trigger_type,
                    scope="post_conversation_long_horizon"
                )),
                ("v14_people", lambda: run_v14_5_post_conversation(
                    conversation_id, person_id=person_id
                )),
                ("v14_interpersonal", lambda: run_v14_6_post_conversation(
                    conversation_id, person_id=person_id
                )),
                ("v14_interventions", lambda: run_proactive_interventions(
                    conversation_id, person_id=person_id, trigger_type=trigger_type
                )),
                ("v14_clarifications", _clarifications),
                ("v17_cases", _v17_cases),
                ("v17_similarity", _v17_similarity),
            ]
            # Mark the whole daily cognitive pass as post-stop so every
            # OllamaJsonClient call receives the production timeout floor and
            # the common retry/keep-alive policy. The previous code only marked
            # deep-audio/VLM phases, leaving V13--V17 on old 60--240 s limits.
            from .runtime_v18_7 import phase
            phase_name = "post_stop_brain2" if checkpoint_run_id else "brain2_direct"
            with phase(phase_name):
                for name, fn in engines:
                    _checkpoint_engine(
                        pipeline_run_id=run_id,
                        conversation_id=conversation_id,
                        step_name=name,
                        fn=fn,
                        result=result,
                    )

                if run_v15_after:
                    result["v15"] = run_v15_post_brain2_consolidation(
                        person_id=person_id,
                        run_periodic_export=run_periodic_export,
                        use_llm=use_llm,
                        checkpoint_run_id=run_id,
                    )
                    result["steps"].append("v15_consolidation")
            if owns_run:
                update_run(run_id, status="completed")
            result["status"] = "ok"
            return result
        except Exception as exc:
            if owns_run:
                update_run(run_id, status="failed", error_code="brain2_stage_gate", error_text=str(exc))
                return {**result, "status": "failed", "error": str(exc)[:2000]}
            # The caller owns the canonical post-stop run and must classify the
            # original exception (timeout / GPU OOM / network) for retry policy.
            raise

    def run_v15_post_brain2_consolidation(
        *,
        person_id: str,
        run_periodic_export: bool = True,
        use_llm: bool = True,
        checkpoint_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Run V15 day engines with the same durable per-engine semantics."""
        if not person_id:
            raise ValueError("V18 life consolidation requires explicit person_id")
        owns_run = not bool(checkpoint_run_id)
        if owns_run:
            scope = Scope(person_id=person_id, mode="maintenance")
            run_id = begin_run(
                pipeline_name="brain2_life_consolidation",
                scope=scope,
                input_manifest={"use_llm": use_llm},
            )
        else:
            run_id = str(checkpoint_run_id)
        # A day-level synthetic conversation key avoids colliding with actual
        # conversations while keeping all V15 engine checkpoints in one run.
        checkpoint_conversation_id = "__day_v15__"
        out: dict[str, Any] = {
            "version": "18.7.1-life-flow",
            "run_id": run_id,
            "person_id": person_id,
            "steps": [],
            "resumed_steps": [],
            "step_results": {},
        }
        try:
            if not use_llm:
                if owns_run:
                    update_run(run_id, status="completed")
                return {**out, "status": "skipped_llm_disabled", "llm_required": True}
            from .brain2_longitudinal_cases_v17 import run_longitudinal_consolidation
            from .brainlive_brain2_coordination_v15_12 import run_brainlive_brain2_coordination
            from .brain2_life_model_updater_v15_13 import run_brain2_life_model_update
            from .brainlive_personal_model_v15_9 import build_brain2_live_personal_model

            engines: list[tuple[str, Callable[[], Any]]] = [
                ("v17_longitudinal", lambda: run_longitudinal_consolidation(
                    person_id=person_id, period="day", use_llm=True,
                    run_periodic_mirror_layer=False, as_of=None
                )),
                ("v15_12_coordination", lambda: run_brainlive_brain2_coordination(
                    person_id=person_id, use_llm=True, timeout=180.0
                )),
                ("v15_13_life_model", lambda: run_brain2_life_model_update(
                    person_id=person_id, use_llm=True, timeout=180.0, limit=120
                )),
                ("v15_9_live_model", lambda: build_brain2_live_personal_model(
                    person_id=person_id, use_llm=True, timeout=180.0, limit=80
                )),
            ]
            if run_periodic_export:
                def _periodic() -> Any:
                    from .self_model_export_v14_3 import run_due_periodic_consolidations
                    return run_due_periodic_consolidations(
                        periods=["hour", "day", "week", "month"], force=False,
                        export_after=True
                    )
                engines.append(("v14_3_periodic_export", _periodic))
            from .runtime_v18_7 import phase
            phase_name = "post_stop_brain2" if checkpoint_run_id else "brain2_maintenance"
            with phase(phase_name):
                for name, fn in engines:
                    _checkpoint_engine(
                        pipeline_run_id=run_id,
                        conversation_id=checkpoint_conversation_id,
                        step_name=name,
                        fn=fn,
                        result=out,
                    )
            if owns_run:
                update_run(run_id, status="completed")
            out["status"] = "ok"
            return out
        except Exception as exc:
            if owns_run:
                update_run(run_id, status="failed", error_code="life_stage_gate", error_text=str(exc))
                return {**out, "status": "failed", "error": str(exc)[:2000]}
            raise

    return {
        "ensure_brain2_flow_schema": ensure_brain2_flow_schema,
        "discover_latent_outcomes_from_conversation": discover_latent_outcomes_from_conversation,
        "run_brain2_deep_stack_for_conversation": run_brain2_deep_stack_for_conversation,
        "run_v15_post_brain2_consolidation": run_v15_post_brain2_consolidation,
    }
