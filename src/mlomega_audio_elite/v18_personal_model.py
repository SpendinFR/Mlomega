"""V18 safe Brain2→BrainLive personal-model projection."""
from __future__ import annotations
from typing import Any

from .db import connect, write_transaction
from .governance_v18 import conversation_in_scope, projection_is_active, strict_one
from .utils import json_dumps, now_iso, stable_id

_INVALID = {"obsolete","invalidated","deleted","retracted","superseded","contradicted","rejected","quarantined","error","failed","wrong","false"}


def _row_id(row: dict[str, Any]) -> str | None:
    preferred=("hook_id","routine_id","place_model_id","action_model_id","need_model_id","expression_model_id","trajectory_model_id","contextual_model_id","affordance_pref_id","binding_id","forecast_id","warning_id","pattern_id","observed_case_id","candidate_id","live_turn_id","frame_id","observation_id","event_id","episode_id","conversation_id")
    for key in preferred:
        if row.get(key): return str(row[key])
    for key,value in row.items():
        if key.endswith("_id") and value: return str(value)
    return None


def _row_in_scope(con: Any, row: dict[str, Any], person_id: str) -> bool:
    status=str(row.get("status") or row.get("lifecycle_status") or row.get("truth_status") or "active").lower()
    if status in _INVALID: return False
    owner=row.get("person_id") or row.get("subject_person_id")
    if owner is not None:
        return str(owner)==person_id
    sid=row.get("live_session_id")
    if sid:
        sess=strict_one(con,"SELECT person_id FROM brainlive_sessions WHERE live_session_id=?",(sid,),purpose="personal model session scope")
        return bool(sess and str(sess.get("person_id"))==person_id)
    cid=row.get("conversation_id") or row.get("source_conversation_id")
    if cid:
        return conversation_in_scope(con,conversation_id=str(cid),person_id=person_id)
    return False


def _filter_tree(con: Any, value: Any, person_id: str, *, source_table: str | None = None, report: dict[str,int]) -> Any:
    if isinstance(value,list):
        out=[]
        for item in value:
            if isinstance(item,dict):
                if not _row_in_scope(con,item,person_id):
                    report["dropped_scope_or_lifecycle"] = report.get("dropped_scope_or_lifecycle",0)+1; continue
                sid=_row_id(item)
                table=source_table or str(item.get("source_table") or "")
                if sid and table and not projection_is_active(con,projection_kind="life_model" if table.startswith("brain2_") else "context",source_table=table,source_id=sid,person_id=person_id):
                    report["dropped_inactive_projection"] = report.get("dropped_inactive_projection",0)+1; continue
                out.append(_filter_tree(con,item,person_id,source_table=table or source_table,report=report))
            else:
                # Non-row scalar values can only remain inside a scoped row; a
                # standalone list at the feed boundary is not evidence.
                report["dropped_unscoped_scalar"] = report.get("dropped_unscoped_scalar",0)+1
        return out
    if isinstance(value,dict):
        return {k:_filter_tree(con,v,person_id,source_table=source_table,report=report) if isinstance(v,(list,dict)) else v for k,v in value.items()}
    return value


def install(module: Any) -> dict[str, Any]:
    old_collect=module.collect_brain2_life_feed
    old_build=module.build_brain2_live_personal_model

    def collect_brain2_life_feed(person_id: str, *, live_session_id: str | None = None, active_people: list[str] | None = None, place_hint: str | None = None, topic_hint: str | None = None, limit: int = 50) -> dict[str,Any]:
        if not person_id: raise ValueError("V18 personal model requires explicit person_id")
        raw=old_collect(person_id,live_session_id=live_session_id,active_people=active_people,place_hint=place_hint,topic_hint=topic_hint,limit=limit)
        report:dict[str,int]={}
        with connect() as con:
            # The legacy collector contains a few ownerless `IS NULL` and global
            # visual reads. A recursively filtered projection is the sole V18
            # output. Retain the raw internal feed only as a diagnostic digest.
            safe=_filter_tree(con,raw,person_id,report=report)
        if isinstance(safe,dict):
            safe["v18_scope"]={"person_id":person_id,"live_session_id":live_session_id,"filter_report":report,"owner_required":True,"projection_gate":True}
        return safe

    def build_brain2_live_personal_model(person_id: str, *, live_session_id: str | None = None, active_people: list[str] | None = None, place_hint: str | None = None, topic_hint: str | None = None, use_llm: bool = True, timeout: float = 90.0, limit: int = 50) -> dict[str,Any]:
        if not person_id: raise ValueError("V18 personal model requires explicit person_id")
        module.collect_brain2_life_feed=collect_brain2_life_feed
        raw=collect_brain2_life_feed(person_id,live_session_id=live_session_id,active_people=active_people,place_hint=place_hint,topic_hint=topic_hint,limit=limit)
        live_ready:dict[str,Any]
        error:str|None=None
        if use_llm:
            live_ready,error=module.synthesize_live_ready_model(raw,timeout=timeout)
            status="active" if not error else "quarantined_llm_error"
        else:
            live_ready={"llm_required":True,"raw_feed_available":True,"reason":"use_llm=false"}
            status="raw_only_llm_disabled"
        export_id=stable_id("blpm18",person_id,live_session_id or "global",now_iso())
        with connect() as con,write_transaction(con):
            module.upsert(con,"brainlive_personal_model_exports",{
                "export_id":export_id,"person_id":person_id,"live_session_id":live_session_id,
                "active_people_json":json_dumps(active_people or []),"place_hint":place_hint,"topic_hint":topic_hint,
                "source_counts_json":json_dumps(module._count_section(raw)),"raw_feed_json":json_dumps(raw),"live_ready_json":json_dumps(live_ready),
                "status":status,"llm_model":None,"error_text":error,"created_at":now_iso(),
            },"export_id")
        # A failed compiler output must never be selected by live retrieval.
        from .governance_v18 import set_projection_active
        set_projection_active(projection_kind="personal_model",source_table="brainlive_personal_model_exports",source_id=export_id,person_id=person_id,active=status=="active",reason=status)
        return {"version":"18.0.0-personal-model","export_id":export_id,"person_id":person_id,"status":status,"raw_feed":raw,"live_ready":live_ready,"error":error}

    return {"collect_brain2_life_feed":collect_brain2_life_feed,"build_brain2_live_personal_model":build_brain2_live_personal_model}
