"""V18 canonical Life Model and patch governance.

The V15 tables remain the materialized read model.  V18 makes their writes
versioned, owner-scoped, evidence-addressable and retractable.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from .db import connect, write_transaction
from .governance_v18 import (
    Scope, DataAccessError, ScopeError, canonical_time, conversation_in_scope, ensure_v18_schema,
    link_artifact, record_artifact_version, set_projection_active, strict_many,
    strict_one, verify_same_owner,
)
from .integrity_v176 import parse_iso_utc, iso_utc, quarantine_in_transaction
from .utils import json_dumps, json_loads, now_iso, stable_id


def _safe_ref_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json_loads(value, [])
        except Exception:
            return []
    return [item for item in (value or []) if isinstance(item, dict)]


# LLM-provided ``source_table`` is untrusted.  Each supported evidence source
# has a fixed primary key and a verifiable ownership route; unknown polymorphic
# references are rejected instead of being kept as opaque JSON.
_DIRECT_EVIDENCE_SOURCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "life_events": ("event_id", "subject_person_id", ("occurred_start", "occurred_end", "created_at")),
    "memory_cards": ("card_id", "person_id", ("time_start", "time_end", "created_at")),
    "brain2_observed_cases_v17": ("observed_case_id", "person_id", ("observed_at", "created_at")),
    "brain2_global_life_patterns_v17": ("pattern_id", "person_id", ("last_seen", "updated_at", "created_at")),
    "brainlive_fused_situations": ("fused_id", "person_id", ("window_start", "created_at")),
    "brainlive_world_states": ("world_state_id", "person_id", ("state_time", "created_at")),
    "brainlive_short_horizon_forecasts": ("forecast_id", "person_id", ("created_at",)),
    "brainlive_life_hypotheses": ("hypothesis_id", "person_id", ("updated_at", "created_at")),
    "brainlive_sensor_events": ("event_id", "person_id", ("event_time", "created_at")),
    "brainlive_event_bundles_v1514": ("bundle_id", "person_id", ("start_time", "created_at")),
    "brainlive_deep_vision_observations_v161": ("deep_observation_id", "person_id", ("frame_time", "created_at")),
    "brainlive_silent_event_candidates_v160": ("candidate_id", "person_id", ("start_time", "created_at")),
    "brain2_personal_routine_models": ("routine_id", "person_id", ("updated_at", "created_at")),
    "brain2_place_preference_models": ("place_model_id", "person_id", ("updated_at", "created_at")),
    "brain2_action_preference_models": ("action_model_id", "person_id", ("updated_at", "created_at")),
    "brain2_need_expectation_models": ("need_model_id", "person_id", ("updated_at", "created_at")),
    "brain2_expression_state_models": ("expression_model_id", "person_id", ("updated_at", "created_at")),
    "brain2_emotional_trajectory_models": ("trajectory_model_id", "person_id", ("updated_at", "created_at")),
    "brain2_contextual_self_models": ("contextual_model_id", "person_id", ("updated_at", "created_at")),
    "brain2_live_prediction_hooks": ("hook_id", "person_id", ("updated_at", "created_at")),
    "brain2_live_affordance_preferences": ("affordance_pref_id", "person_id", ("updated_at", "created_at")),
    "self_model_dimensions": ("dimension_id", "person_id", ("updated_at", "created_at")),
    "self_model_facts": ("fact_id", "person_id", ("updated_at", "created_at")),
    "behavior_signals": ("signal_id", "person_id", ("updated_at", "created_at")),
    "action_intentions": ("intention_id", "person_id", ("updated_at", "created_at")),
    "action_outcomes": ("outcome_id", "person_id", ("updated_at", "created_at")),
    "choice_episodes": ("choice_id", "person_id", ("updated_at", "created_at")),
    "internal_state_snapshots": ("state_id", "person_id", ("time_start", "created_at")),
    "emotion_evidence": ("emotion_evidence_id", "person_id", ("updated_at", "created_at")),
    "thought_hypotheses": ("thought_id", "person_id", ("updated_at", "created_at")),
    "candidate_patterns": ("candidate_pattern_id", "person_id", ("updated_at", "created_at")),
    "confirmed_patterns": ("confirmed_pattern_id", "person_id", ("updated_at", "created_at")),
    "loop_patterns": ("loop_id", "person_id", ("updated_at", "created_at")),
    "personal_language_patterns": ("language_pattern_id", "person_id", ("updated_at", "created_at")),
    "phrase_templates": ("template_id", "person_id", ("updated_at", "created_at")),
    "visual_evidence_assets_v19": ("visual_asset_id", "person_id", ("captured_at", "created_at")),
    "visual_events_v19": ("visual_event_id", "person_id", ("occurred_at", "created_at")),
    "world_entity_links_v19": ("world_entity_link_id", "person_id", ("observed_at", "created_at")),
    "scene_session_summaries_v19": ("scene_summary_id", "person_id", ("summary_start", "summary_end", "created_at")),
    "ui_interaction_outcomes_v19": ("ui_outcome_id", "person_id", ("observed_at", "created_at")),
    "brain2_spatial_routine_models": ("routine_id", "person_id", ("last_observed", "updated_at", "created_at")),
    "brain2_visual_task_models": ("task_model_id", "person_id", ("updated_at", "created_at")),
    "brain2_ui_preference_models": ("ui_pref_id", "person_id", ("updated_at", "created_at")),
}
_CONVERSATION_EVIDENCE_SOURCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "conversations": ("conversation_id", "conversation_id", ("started_at", "created_at")),
    "turns": ("turn_id", "conversation_id", ("absolute_start", "created_at")),
    "episodes": ("episode_id", "source_conversation_id", ("start_time", "created_at")),
    "situation_episodes": ("situation_id", "episode_id", ("created_at",)),
    "interaction_episodes": ("interaction_id", "episode_id", ("created_at",)),
    "speech_acts": ("speech_act_id", "episode_id", ("created_at",)),
    "utterance_analyses": ("analysis_id", "conversation_id", ("created_at",)),
    "vision_frames": ("frame_id", "conversation_id", ("captured_at", "created_at")),
    "vision_scene_observations": ("observation_id", "conversation_id", ("created_at",)),
}
_SESSION_EVIDENCE_SOURCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "brainlive_turn_buffer": ("live_turn_id", "live_session_id", ("timestamp_start", "created_at")),
    "brainlive_audio_segments_v154": ("segment_id", "live_session_id", ("created_at",)),
    "brainlive_vlm_observations_v154": ("observation_id", "live_session_id", ("created_at",)),
    "brainlive_active_contexts": ("active_context_id", "live_session_id", ("created_at",)),
}


def _source_time_from_row(row: Mapping[str, Any], fields: tuple[str, ...]) -> str | None:
    # ``absolute_start`` is carried by some normalized turn metadata but not as
    # a table column. Prefer concrete stored event times and return no time when
    # none exists; general-stratum promotion then fails safely.
    return canonical_time(row, *fields)


def _owned_evidence_ref(con, *, person_id: str, table: str, source_id: str) -> dict[str, Any]:
    """Resolve one evidence reference through a fixed owner chain."""
    if table in _DIRECT_EVIDENCE_SOURCES:
        pk, owner_col, times = _DIRECT_EVIDENCE_SOURCES[table]
        row = con.execute(f"SELECT * FROM {table} WHERE {pk}=?", (source_id,)).fetchone()
        if not row:
            raise ScopeError(f"evidence source missing: {table}/{source_id}")
        raw = dict(row)
        if str(raw.get(owner_col) or "") != person_id:
            raise ScopeError(f"evidence source is cross-owner: {table}/{source_id}")
        occurred = _source_time_from_row(raw, times)
        return {"source_table": table, "source_id": source_id, "occurred_at": occurred}

    if table in _CONVERSATION_EVIDENCE_SOURCES:
        pk, conv_col, times = _CONVERSATION_EVIDENCE_SOURCES[table]
        row = con.execute(f"SELECT * FROM {table} WHERE {pk}=?", (source_id,)).fetchone()
        if not row:
            raise ScopeError(f"evidence source missing: {table}/{source_id}")
        raw = dict(row)
        conversation_id = str(raw.get(conv_col) or "")
        # Episode-derived rows carry episode_id rather than conversation_id.
        if table in {"situation_episodes", "interaction_episodes", "speech_acts"}:
            episode = con.execute(
                "SELECT source_conversation_id,start_time,created_at FROM episodes WHERE episode_id=?",
                (conversation_id,),
            ).fetchone()
            if not episode:
                raise ScopeError(f"evidence episode has no conversation: {table}/{source_id}")
            conversation_id = str(episode["source_conversation_id"] or "")
            raw = {**raw, "episode_start_time": episode["start_time"], "episode_created_at": episode["created_at"]}
        if not conversation_id or not conversation_in_scope(
            con,
            conversation_id=conversation_id,
            person_id=person_id,
            allow_legacy_turn_proof=False,
        ):
            raise ScopeError(f"evidence conversation is out of scope: {table}/{source_id}")
        occurred = _source_time_from_row(raw, times + ("episode_start_time", "episode_created_at"))
        return {
            "source_table": table,
            "source_id": source_id,
            "occurred_at": occurred,
            "conversation_id": conversation_id,
        }

    if table in _SESSION_EVIDENCE_SOURCES:
        pk, session_col, times = _SESSION_EVIDENCE_SOURCES[table]
        row = con.execute(f"SELECT * FROM {table} WHERE {pk}=?", (source_id,)).fetchone()
        if not row:
            raise ScopeError(f"evidence source missing: {table}/{source_id}")
        raw = dict(row)
        session_id = str(raw.get(session_col) or "")
        session = con.execute(
            "SELECT person_id FROM brainlive_sessions WHERE live_session_id=?", (session_id,)
        ).fetchone()
        if not session or str(session["person_id"] or "") != person_id:
            raise ScopeError(f"evidence session is out of scope: {table}/{source_id}")
        return {
            "source_table": table,
            "source_id": source_id,
            "occurred_at": _source_time_from_row(raw, times),
            "live_session_id": session_id,
        }

    raise ScopeError(f"evidence table is not approved: {table!r}")


def _validate_evidence_refs(
    con, *, person_id: str, refs: Any, required: bool = True
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    invalid: list[str] = []
    for raw in _safe_ref_list(refs):
        table = str(raw.get("source_table") or raw.get("table") or "").strip()
        source_id = str(raw.get("source_id") or raw.get("id") or "").strip()
        if not table or not source_id:
            invalid.append("missing source_table/source_id")
            continue
        try:
            canonical = _owned_evidence_ref(
                con, person_id=person_id, table=table, source_id=source_id
            )
        except Exception as exc:
            invalid.append(str(exc))
            continue
        key = (canonical["source_table"], canonical["source_id"])
        if key in seen:
            continue
        seen.add(key)
        # Preserve only non-authoritative descriptive fields supplied by the
        # caller. Owner/time/table/id always come from the canonical source.
        normalized.append(
            {
                **{k: v for k, v in raw.items() if k not in {"source_table", "table", "source_id", "id", "occurred_at", "person_id"}},
                **canonical,
            }
        )
    if invalid:
        raise ScopeError("invalid Life Model evidence reference(s): " + "; ".join(invalid[:3]))
    if required and not normalized:
        raise ScopeError("Life Model item requires at least one structured, owner-scoped source reference")
    return normalized

def validate_stratum_evidence(*, refs: Any, stratum: str) -> None:
    """Enforce independent evidence before generalizing a Life Model claim.

    ``general`` is not merely a confidence label: it is a cross-time claim.
    Three records from the same afternoon are therefore still recent evidence,
    not a general personal tendency.
    """
    normalized = _safe_ref_list(refs)
    day_keys = {
        str(ref.get("occurred_at") or ref.get("created_at"))[:10]
        for ref in normalized
        if ref.get("occurred_at") or ref.get("created_at")
    }
    if str(stratum).lower() == "general" and (len(normalized) < 3 or len(day_keys) < 3):
        raise ScopeError("general stratum requires >=3 evidence references across >=3 distinct days")


def _in_window(value: Any, start: str|None, end: str|None, as_of: str|None) -> bool:
    if value is None: return False
    try: dt=parse_iso_utc(str(value))
    except Exception: return False
    if start and dt < parse_iso_utc(start): return False
    if end and dt >= parse_iso_utc(end): return False
    if as_of and dt > parse_iso_utc(as_of): return False
    return True


def install_canonical(module: Any) -> dict[str, Any]:
    old_ensure=module.ensure_life_model_schema
    old_store=module.store_canonical_life_model
    old_build=module.build_brain2_canonical_life_model

    def ensure_life_model_schema() -> None:
        old_ensure(); ensure_v18_schema()

    def _query(con, sql: str, params: tuple[Any,...]=()) -> list[dict[str,Any]]:
        return strict_many(con,sql,params,purpose="life model query")

    def _compact(rows: list[dict[str,Any]], limit:int=80,max_str:int=1200)->list[dict[str,Any]]:
        # No silent claim of completeness: each cut is marked alongside stable
        # id fields so downstream Context Gateway can retrieve it exactly.
        out=[]
        for row in rows[:limit]:
            r=dict(row)
            for k,v in list(r.items()):
                if isinstance(v,str) and len(v)>max_str:
                    r[k]=v[:max_str]; r[f"{k}__truncated"]=True
            out.append(r)
        return out

    def _owner_conversation_rows(con, table: str, conv_col: str, person_id: str, *, start:str|None,end:str|None,as_of:str|None,limit:int)->list[dict[str,Any]]:
        rows=strict_many(con,f"""SELECT x.* FROM {table} x
            JOIN v18_conversation_scopes cs ON cs.conversation_id=x.{conv_col}
            WHERE cs.person_id=? AND cs.active=1 ORDER BY COALESCE(x.created_at,'') DESC LIMIT ?""",(person_id,limit*4),purpose=f"{table} owner scoped")
        filtered=[]
        for r in rows:
            t=canonical_time(r,"occurred_at","start_time","created_at","updated_at")
            if t and _in_window(t,start,end,as_of): filtered.append(r)
            elif not start and not end and not as_of: filtered.append(r)
        return filtered[:limit]

    def collect_canonical_evidence(person_id: str, *, period_start: str|None=None, period_end: str|None=None,
                                   limit:int=120, as_of:str|None=None)->dict[str,Any]:
        ensure_life_model_schema(); scope=Scope(person_id=person_id,as_of=as_of,mode="maintenance")
        with connect() as con:
            def direct(table:str, *, time_cols:tuple[str,...]=( "created_at",), where:str="person_id=?", params:tuple[Any,...]=()) -> list[dict[str,Any]]:
                cols={str(r["name"]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
                if not cols: return []
                if "person_id" not in cols and where=="person_id=?": return []
                rows=strict_many(con,f"SELECT * FROM {table} WHERE {where} ORDER BY COALESCE(updated_at,created_at) DESC LIMIT ?",((*params,limit*4) if params else (person_id,limit*4)),purpose=f"evidence:{table}")
                out=[]
                for r in rows:
                    t=canonical_time(r,*time_cols)
                    if t is None:
                        if period_start or period_end or scope.as_of_utc: continue
                    elif not _in_window(t,period_start,period_end,scope.as_of_utc): continue
                    # lifecycle suppression always wins.
                    if str(r.get("status") or r.get("lifecycle_status") or "active").lower() in {"obsolete","invalidated","deleted","contradicted","retracted"}: continue
                    out.append(r)
                return _compact(out,limit)
            episodes=_owner_conversation_rows(con,"episodes","source_conversation_id",person_id,start=period_start,end=period_end,as_of=scope.as_of_utc,limit=limit)
            ep_ids=[str(e["episode_id"]) for e in episodes if e.get("episode_id")]
            def episode_table(table:str)->list[dict[str,Any]]:
                if not ep_ids: return []
                q=','.join('?' for _ in ep_ids)
                rows=strict_many(con,f"SELECT * FROM {table} WHERE episode_id IN ({q}) ORDER BY created_at DESC LIMIT ?",(*ep_ids,limit*4),purpose=f"evidence episode {table}")
                return _compact(rows,limit)
            life_events=direct("life_events",time_cols=("occurred_start","created_at"),where="subject_person_id=?",params=(person_id,))
            observed={"episodes":_compact(episodes,limit),"life_events":life_events,"situation_episodes":episode_table("situation_episodes"),"interaction_episodes":episode_table("interaction_episodes"),"choice_episodes":episode_table("choice_episodes"),"action_intentions":episode_table("action_intentions"),"action_outcomes":episode_table("action_outcomes")}
            feed={"schema_version":"18.0.0","person_id":person_id,"period_start":period_start,"period_end":period_end,"as_of":scope.as_of_utc,"observed_life":observed,
                  "self_and_internal":{"self_model_dimensions":direct("self_model_dimensions",time_cols=("updated_at","created_at")),"self_model_facts":direct("self_model_facts",time_cols=("updated_at","created_at")),"internal_state_snapshots":episode_table("internal_state_snapshots"),"emotion_evidence":episode_table("emotion_evidence"),"thought_hypotheses":episode_table("thought_hypotheses"),"behavior_signals":direct("behavior_signals",time_cols=("updated_at","created_at"))},
                  "language":{"turns_recent":_compact(_owner_conversation_rows(con,"turns","conversation_id",person_id,start=period_start,end=period_end,as_of=scope.as_of_utc,limit=limit),limit),"personal_language_patterns":direct("personal_language_patterns",time_cols=("updated_at","created_at")),"phrase_templates":direct("phrase_templates",time_cols=("updated_at","created_at"))},
                  "memory_and_patterns":{"memory_cards":direct("memory_cards",time_cols=("updated_at","created_at")),"candidate_patterns":direct("candidate_patterns",time_cols=("updated_at","created_at")),"confirmed_patterns":direct("confirmed_patterns",time_cols=("updated_at","created_at")),"global_patterns_v17":direct("brain2_global_life_patterns_v17",time_cols=("updated_at","created_at"))},
                  "forecasts_future":{"brainlive_short_horizon_forecasts":direct("brainlive_short_horizon_forecasts",time_cols=("occurred_at","created_at"))},
                  "brain2_canonical_life_model":{}}
            # Read only canonical objects whose own status and lifecycle are active.
            for layer,(table,id_col,_name) in getattr(module,"CANONICAL_TABLES",{}).items():
                rows=direct(table,time_cols=("updated_at","created_at"))
                feed["brain2_canonical_life_model"][layer]=rows
            feed["completeness"]={"truncated":False,"source_counts":module._count(feed),"missing_owner_proof":not bool(episodes)}
            return feed

    def _item_identity(layer:str,item:dict[str,Any])->str:
        keys={"personal_routine_models":"routine_name","place_preference_models":"place_key","action_preference_models":"action_or_choice","need_expectation_models":"need_or_expectation","expression_state_models":"expression_or_style","emotional_trajectory_models":"trajectory_name","contextual_self_models":"context_key","live_prediction_hooks":"hook_name","live_affordance_preferences":"affordance_type"}
        return str(item.get(keys.get(layer,"name")) or item.get("name") or "")

    def store_canonical_life_model(person_id:str,export_id:str,model:dict[str,Any])->None:
        ensure_life_model_schema(); scope=Scope(person_id=person_id,mode="maintenance")
        # Validate every proposed item before the legacy materialized writer can
        # turn vague LLM text into an active canonical row.
        allowed:dict[str,Any]={}; quarantined=[]
        with connect() as con,write_transaction(con):
            for layer,items in (model or {}).items():
                if not isinstance(items,list):
                    continue
                keep=[]
                for item in items:
                    if not isinstance(item,dict): continue
                    try:
                        refs=_validate_evidence_refs(con,person_id=person_id,refs=item.get("evidence"),required=True)
                        ident=_item_identity(layer,item)
                        if not ident: raise ScopeError("canonical item identity missing")
                        item=dict(item); item["evidence"]=refs; keep.append(item)
                    except Exception as exc:
                        quarantined.append({"layer":layer,"item":item,"error":str(exc)})
                        quarantine_in_transaction(con,category="invalid_life_model_item",reason=str(exc),source_table="brain2_life_model_exports",source_id=export_id,person_id=person_id,raw_payload={"layer":layer,"item":item})
                if keep: allowed[layer]=keep
        if allowed:
            old_store(person_id,export_id,allowed)
            # Version all materialized items after the writer has established ids.
            tablemap={"personal_routine_models":("brain2_personal_routine_models","routine_id","routine_name"),"place_preference_models":("brain2_place_preference_models","place_model_id","place_key"),"action_preference_models":("brain2_action_preference_models","action_model_id","action_or_choice"),"need_expectation_models":("brain2_need_expectation_models","need_model_id","need_or_expectation"),"expression_state_models":("brain2_expression_state_models","expression_model_id","expression_or_style"),"emotional_trajectory_models":("brain2_emotional_trajectory_models","trajectory_model_id","trajectory_name"),"contextual_self_models":("brain2_contextual_self_models","contextual_model_id","context_key"),"live_prediction_hooks":("brain2_live_prediction_hooks","hook_id","hook_name"),"live_affordance_preferences":("brain2_live_affordance_preferences","affordance_pref_id","affordance_type")}
            # Resolve materialized ids under a read connection, then close it
            # before opening provenance writers.  Calling a writer from inside
            # an active reader is a real SQLite lock hazard under post-stop load.
            materialized: list[tuple[str, str, str, dict[str, Any]]] = []
            with connect() as con:
                for layer,items in allowed.items():
                    if layer not in tablemap: continue
                    table,pk,name=tablemap[layer]
                    for item in items:
                        ident=str(item.get(name) or item.get("name"))
                        row=con.execute(f"SELECT {pk} FROM {table} WHERE person_id=? AND {name}=?",(person_id,ident)).fetchone()
                        if row:
                            materialized.append((table, str(row[pk]), ident, item))
            for table, artifact_id, ident, item in materialized:
                record_artifact_version(
                    artifact_table=table, artifact_id=artifact_id,
                    identity_key=f"{table}:{person_id}:{ident}", scope=scope,
                    source_payload={"export_id":export_id,"item":item},
                )
        if quarantined:
            # Caller receives a failure signal through export audit instead of a
            # silent partial model.
            raise ScopeError(f"{len(quarantined)} canonical Life Model items quarantined")

    def latest_canonical_life_model(person_id:str)->dict[str,Any]|None:
        ensure_life_model_schema()
        with connect() as con:
            row=strict_one(con,"SELECT * FROM brain2_life_model_exports WHERE person_id=? AND status IN ('ok','completed','active') ORDER BY created_at DESC LIMIT 1",(person_id,),purpose="latest canonical export")
            if not row: return None
            return row

    return {"ensure_life_model_schema":ensure_life_model_schema,"_query":_query,"_compact":_compact,"collect_canonical_evidence":collect_canonical_evidence,"store_canonical_life_model":store_canonical_life_model,"latest_canonical_life_model":latest_canonical_life_model}


def install_updater(module: Any, canonical_module: Any) -> dict[str,Any]:
    old_ensure=module.ensure_life_model_updater_schema
    old_update_strata=module.update_life_model_strata
    old_run=module.run_brain2_life_model_update

    def ensure_life_model_updater_schema()->None:
        old_ensure(); ensure_v18_schema()

    def _query(con,sql:str,params:tuple[Any,...]=())->list[dict[str,Any]]:
        return strict_many(con,sql,params,purpose="life model updater query")

    def collect_life_model_delta(person_id:str,*,period_start:str|None=None,period_end:str|None=None,limit:int=120,as_of:str|None=None)->dict[str,Any]:
        ensure_life_model_updater_schema()
        # No implicit now snapshot can include the future relative to a backfill.
        if period_end is None:
            period_end=as_of or now_iso()
        if as_of and parse_iso_utc(period_end)>parse_iso_utc(as_of):
            raise ScopeError("Life Model delta period_end exceeds as_of")
        if period_start is None:
            period_start=iso_utc(parse_iso_utc(period_end)-timedelta(days=1))
        delta=canonical_module.collect_canonical_evidence(person_id,period_start=period_start,period_end=period_end,limit=limit,as_of=as_of)
        with connect() as con,write_transaction(con):
            live={}
            tables=[("brainlive_day_packages","COALESCE(period_end,updated_at,created_at)"),("brainlive_brain2_reconciliations","updated_at"),("brainlive_context_snapshots_v1512","created_at"),("brainlive_event_bundles_v1514","COALESCE(end_time,start_time,updated_at,created_at)"),("brainlive_silent_event_candidates_v160","COALESCE(end_time,start_time,updated_at,created_at)")]
            for table,timeexpr in tables:
                exists=strict_one(con,"SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name=?",(table,),purpose="delta table")
                if not exists: continue
                rows=strict_many(con,f"SELECT * FROM {table} WHERE person_id=? AND {timeexpr}>=? AND {timeexpr}<? ORDER BY {timeexpr} DESC LIMIT ?",(person_id,period_start,period_end,limit),purpose=f"delta {table}")
                # Block superseded/error sources at the boundary.
                live[table]=[r for r in rows if str(r.get("status") or "active").lower() not in {"superseded","invalidated","error","failed","quarantined"}]
            delta["brainlive_bridge_delta"]=live
            did=stable_id("b2delta18",person_id,period_start,period_end,as_of or "live")
            module.upsert(con,"brain2_life_model_delta_evidence",{"delta_id":did,"person_id":person_id,"period_start":period_start,"period_end":period_end,"status":"ready","source_counts_json":json_dumps(module._count(delta)),"raw_evidence_json":json_dumps(delta),"created_at":now_iso()},"delta_id")
        return delta

    def _validate_patch_operation(con,*,person_id:str,op:dict[str,Any],idx:int)->dict[str,Any]:
        allowed_ops={"create","confirm","update","weaken","contradict","obsolete","keep"}
        operation=str(op.get("op") or "keep").lower(); layer=str(op.get("target_layer") or "").lower(); stratum=str(op.get("stratum") or "recent").lower()
        if operation not in allowed_ops: raise ScopeError(f"unsupported patch op {operation}")
        if layer not in module.CANONICAL_TABLES: raise ScopeError(f"unknown canonical target layer {layer}")
        if stratum not in {"general","recent","very_recent"}: raise ScopeError(f"invalid stratum {stratum}")
        refs=_validate_evidence_refs(con,person_id=person_id,refs=op.get("evidence"),required=operation not in {"keep"})
        if operation in {"contradict","obsolete","weaken"} and not _safe_ref_list(op.get("counter_evidence")):
            raise ScopeError(f"{operation} requires counter_evidence")
        identity=str(op.get("identity_key") or op.get("target_id") or "").strip()
        if not identity: raise ScopeError("patch identity_key/target_id required")
        # General needs independently recurring evidence; an LLM cannot promote
        # one recent observation by selecting a string field.
        validate_stratum_evidence(refs=refs, stratum=stratum)
        op=dict(op);op["op"]=operation;op["target_layer"]=layer;op["stratum"]=stratum;op["evidence"]=refs;op["identity_key"]=identity
        return op

    def apply_life_model_patch(person_id:str,patch_run_id:str,patch:dict[str,Any])->dict[str,Any]:
        ensure_life_model_updater_schema(); scope=Scope(person_id=person_id,mode="maintenance"); now=now_iso(); ops=module._list((patch or {}).get("operations")); valid=[]; quarantined=[]
        with connect() as con,write_transaction(con):
            for i,raw in enumerate(ops):
                if not isinstance(raw,dict): continue
                try: valid.append(_validate_patch_operation(con,person_id=person_id,op=raw,idx=i))
                except Exception as exc:
                    quarantined.append({"index":i,"error":str(exc)})
                    quarantine_in_transaction(con,category="invalid_life_model_patch",reason=str(exc),source_table="brain2_life_model_patch_runs",source_id=patch_run_id,person_id=person_id,raw_payload=raw)
        if quarantined:
            # Do not apply a partial LLM patch whose skipped rows can make the
            # declared patch semantics untrue.
            raise ScopeError(f"patch quarantined: {len(quarantined)} invalid operations")
        # Delegate canonical row materialisation only after validation.
        result=module._v17_apply_life_model_patch(person_id,patch_run_id,{**patch,"operations":valid}) if hasattr(module,"_v17_apply_life_model_patch") else None
        # Legacy operation applies lifecycle but did not retire canonical rows.
        # First commit canonical-row mutations; projection/version records use
        # their own durable writer afterwards, preventing nested SQLite writers.
        projection_actions: list[tuple[str, str, bool, str, dict[str, Any]]] = []
        with connect() as con,write_transaction(con):
            for op in valid:
                table,pk,_=module.CANONICAL_TABLES[op["target_layer"]]
                tid=op.get("target_id") or stable_id({"routine":"b2routine","place":"b2place"}.get(op["target_layer"],"b2life"),person_id,op["identity_key"])
                active = True
                reason = "validated_patch"
                if op["op"] in {"contradict","obsolete"}:
                    con.execute(f"UPDATE {table} SET status='obsolete',updated_at=? WHERE {pk}=? AND person_id=?",(now,tid,person_id))
                    active = False
                    reason = op["op"]
                projection_actions.append((table, str(tid), active, reason, op))
        for table, tid, active, reason, op in projection_actions:
            set_projection_active(projection_kind="life_model",source_table=table,source_id=tid,person_id=person_id,active=active,reason=reason)
            record_artifact_version(artifact_table=table,artifact_id=tid,identity_key=f"{table}:{person_id}:{op['identity_key']}",scope=scope,source_payload=op,metadata={"patch_run_id":patch_run_id,"stratum":op["stratum"]})
        return result or {"applied_operations":len(valid),"canonical_update_layers":sorted({op["target_layer"] for op in valid})}

    def update_life_model_strata(person_id:str,*,patch_run_id:str|None=None)->dict[str,Any]:
        ensure_life_model_updater_schema(); now=now_iso(); strata={"general":{},"recent":{},"very_recent":{}}
        with connect() as con,write_transaction(con):
            for layer,(table,pk,_name) in module.CANONICAL_TABLES.items():
                rows=strict_many(con,f"SELECT * FROM {table} WHERE person_id=? AND status NOT IN ('obsolete','contradicted','invalidated')",(person_id,),purpose="strata canonical")
                lcs=strict_many(con,"SELECT * FROM brain2_life_model_item_lifecycle WHERE person_id=? AND source_table=?",(person_id,table),purpose="strata lifecycle")
                by={(str(x.get("source_id")),str(x.get("stratum"))):x for x in lcs}
                for s in strata:
                    items=[]
                    for r in rows:
                        lc=by.get((str(r.get(pk)),s))
                        if not lc: continue
                        if str(lc.get("truth_status") or "").lower() in {"obsolete","contradicted","rejected","false"}: continue
                        if str(lc.get("use_policy") or "").lower() in {"do_not_use","forbidden","never_use"}: continue
                        # Stratum is exact, never inferred from a row in another tier.
                        item=dict(r);item["lifecycle"]=lc;items.append(item)
                    strata[s][layer]=module._compact_rows(items,80)
            for s,model in strata.items():
                module.upsert(con,"brain2_life_model_strata",{"stratum_id":stable_id("b2stratum18",person_id,s),"person_id":person_id,"stratum":s,"status":"active","model_json":json_dumps(model),"evidence_window_start":None,"evidence_window_end":now,"patch_run_id":patch_run_id,"source_counts_json":json_dumps(module._count(model)),"created_at":now,"updated_at":now},"stratum_id")
        return {"person_id":person_id,"strata":{s:module._count(m) for s,m in strata.items()}}

    def run_brain2_life_model_update(person_id:str,*,period_start:str|None=None,period_end:str|None=None,use_llm:bool=True,timeout:float=180.0,limit:int=120,bootstrap_if_empty:bool=True,as_of:str|None=None)->dict[str,Any]:
        """Run a Life Model update against one immutable historical cutoff.

        The legacy runner rebuilds its delta internally. Computing a V18 delta
        here and then calling that runner without forwarding ``as_of`` used to
        look protected while allowing the second read to include future rows.
        The legacy API has no ``as_of`` argument, therefore V18 translates the
        cutoff into the authoritative exclusive ``period_end`` passed through
        every downstream read and bootstrap path.
        """
        ensure_life_model_updater_schema()
        scope=Scope(person_id=person_id,as_of=as_of,mode="maintenance")
        effective_end=period_end
        if scope.as_of_utc:
            if effective_end and parse_iso_utc(effective_end) > parse_iso_utc(scope.as_of_utc):
                raise ScopeError("Life Model period_end exceeds as_of")
            effective_end=effective_end or scope.as_of_utc
        if hasattr(module,"_v17_run_brain2_life_model_update"):
            result=module._v17_run_brain2_life_model_update(
                person_id,period_start=period_start,period_end=effective_end,
                use_llm=use_llm,timeout=timeout,limit=limit,
                bootstrap_if_empty=bootstrap_if_empty,
            )
            if isinstance(result,dict):
                result.setdefault("as_of",scope.as_of_utc)
                result.setdefault("effective_period_end",effective_end)
            return result
        delta=collect_life_model_delta(person_id,period_start=period_start,period_end=effective_end,limit=limit,as_of=scope.as_of_utc)
        return {"status":"delta_ready","delta":delta,"as_of":scope.as_of_utc,"effective_period_end":effective_end}

    return {"ensure_life_model_updater_schema":ensure_life_model_updater_schema,"_query":_query,"collect_life_model_delta":collect_life_model_delta,"apply_life_model_patch":apply_life_model_patch,"update_life_model_strata":update_life_model_strata,"run_brain2_life_model_update":run_brain2_life_model_update}
