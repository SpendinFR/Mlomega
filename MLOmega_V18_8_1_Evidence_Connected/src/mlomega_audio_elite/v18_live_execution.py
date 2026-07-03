"""V18 one-analysis live horizon execution.

H0/H1/H2 are forecast horizons in a single validated reasoning output, not three
independent analyses of the same signal packet.
"""
from __future__ import annotations
from typing import Any
from uuid import uuid4

from .db import connect, write_transaction, upsert
from .utils import json_dumps, now_iso, stable_id


def install_realtime(module:Any)->dict[str,Any]:
    old_tick=module.live_tick
    def live_cycle_all_horizons(live_session_id:str,*,text:str|None=None,image_path:str|None=None,audio_sample_path:str|None=None,speaker_label:str|None=None,speaker_person_id:str|None=None,location_hint:str|None=None,use_vlm:bool=True,use_llm:bool=True)->dict[str,Any]:
        # One ingest/perception/context/LLM call. The LLM contract already has a
        # list of forecasts tagged H0/H1/H2; duplicating calls created three
        # potentially contradictory outputs and duplicate forecasts.
        shared=old_tick(live_session_id,horizon="H1",text=text,image_path=image_path,audio_sample_path=audio_sample_path,speaker_label=speaker_label,speaker_person_id=speaker_person_id,location_hint=location_hint,use_vlm=use_vlm,use_llm=use_llm)
        analysis=(shared.get("analysis") or {}) if isinstance(shared,dict) else {}
        output=(analysis.get("output") or {}) if isinstance(analysis,dict) else {}
        result={"analysis":analysis,"output":output,"intervention_candidates":output.get("interventions") or [],"watch_mode":output.get("watch_next") or [],"shared_analysis_run_id":analysis.get("run_id")}
        cycle={}
        for h in ("H0","H1","H2"):
            cycle[h]={**shared,"horizon":h,"result":{**result,"forecasts":[x for x in (output.get("forecasts") or []) if isinstance(x,dict) and str(x.get("horizon") or "").upper()==h]},"shared_analysis":True}
        return {"live_session_id":live_session_id,"analysis_count":1,"shared_tick_id":shared.get("tick_id"),**cycle}
    return {"live_cycle_all_horizons":live_cycle_all_horizons}


def install_sensor(module:Any,realtime_module:Any)->dict[str,Any]:
    old_decision=module._tick_decision
    def _tick_decision(tick:dict[str,Any],*,proactive_confidence_min:float,proactive_gain_min:float)->dict[str,Any]:
        # Legacy tick stored its LLM output under analysis.output, while the old
        # decision reader searched tick.result only and never delivered real work.
        result=tick.get("result") if isinstance(tick,dict) else None
        if not isinstance(result,dict):
            analysis=tick.get("analysis") if isinstance(tick,dict) else {}
            result=(analysis.get("output") if isinstance(analysis,dict) else {}) or {}
        candidates=result.get("intervention_candidates") or result.get("interventions") or []
        if not isinstance(candidates,list): candidates=[]
        best=None;score=0.0
        for c in candidates:
            if not isinstance(c,dict):continue
            conf=module._clamp(c.get("confidence"),0.0); gain=module._clamp(c.get("expected_gain"),module._clamp(c.get("urgency"),0.0)); msg=c.get("message") or c.get("intervention_message")
            value=0.55*conf+0.45*gain
            if msg and c.get("speak_now") is not False and conf>=proactive_confidence_min and gain>=proactive_gain_min and value>score:
                best=c;score=value
        return {"decision":"proactive","reason":{"best_score":score,"candidate":best}} if best else {"decision":"observe","reason":{"candidate_count":len(candidates),"thresholds":{"confidence":proactive_confidence_min,"gain":proactive_gain_min}}}
    def run_fused_horizons(live_session_id:str,*,fused_id:str|None=None,text:str|None=None,use_llm:bool=True,use_vlm:bool=True,config_id:str|None=None)->dict[str,Any]:
        module.ensure_sensor_fusion_schema()
        with connect() as con:
            sess=module._one(con,"SELECT * FROM brainlive_sessions WHERE live_session_id=?",(live_session_id,))
            if not sess:raise ValueError(f"Session BrainLive introuvable: {live_session_id}")
            cfg=module._get_config(con,person_id=sess["person_id"],config_id=config_id)
            if fused_id:
                last=module._one(con,"SELECT fused_id,speech_json FROM brainlive_fused_situations WHERE fused_id=?",(fused_id,))
            else:
                last=module._one(con,"SELECT fused_id,speech_json FROM brainlive_fused_situations WHERE live_session_id=? ORDER BY created_at DESC LIMIT 1",(live_session_id,))
        if last:
            fused_id=last.get("fused_id")
            if not text:
                speech=module.json_loads(last.get("speech_json"),[]) or []
                texts=[(x.get("asr") or {}).get("text") for x in speech if isinstance(x,dict) and isinstance(x.get("asr"),dict)]
                text=" ".join([str(x) for x in texts[-3:] if x]) or None
        if not text:text="[no_new_speech_text_available; use fused sensor context and Brain2 active context only]"
        cycle=realtime_module.live_cycle_all_horizons(live_session_id,text=text,use_vlm=False,use_llm=use_llm)
        # A single analysis permits at most one delivery. Observations are still
        # logged per horizon to keep latency/audit visibility without duplicates.
        base=cycle["H1"]; decision=_tick_decision(base,proactive_confidence_min=float(cfg.get("proactive_confidence_min") or .62),proactive_gain_min=float(cfg.get("proactive_gain_min") or .45))
        delivery_ids=[]
        if decision["decision"]=="proactive":
            delivery_ids=module.enqueue_interventions_from_tick(live_session_id,base)
        decisions={}
        with connect() as con,write_transaction(con):
            for h in ("H0","H1","H2"):
                d=decision if h=="H1" else {"decision":"observe","reason":{"shared_analysis":True,"delivery_owned_by":"H1"}}
                pid=stable_id("blpro18",live_session_id,fused_id or "none",h,cycle.get("shared_tick_id"),uuid4().hex)
                upsert(con,"brainlive_proactive_decisions",{"proactive_id":pid,"live_session_id":live_session_id,"fused_id":fused_id,"horizon":h,"decision":d["decision"],"reason_json":json_dumps(d.get("reason") or {}),"tick_json":json_dumps(cycle.get(h) or {}),"delivery_ids_json":json_dumps(delivery_ids if h=="H1" else []),"created_at":now_iso()},"proactive_id")
                decisions[h]={**d,"delivery_ids":delivery_ids if h=="H1" else []}
        return {"fused_id":fused_id,"cycle":cycle,"decisions":decisions,"delivery_ids":delivery_ids,"analysis_count":1}
    return {"_tick_decision":_tick_decision,"run_fused_horizons":run_fused_horizons}
