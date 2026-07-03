"""V18 Brain2↔BrainLive coordination: owner scope and revocation propagation."""
from __future__ import annotations
from typing import Any

from .db import connect, write_transaction, upsert
from .governance_v18 import Scope, invalidate_descendants, projection_is_active, set_projection_active, strict_one
from .utils import json_dumps, now_iso, stable_id


def install(module:Any)->dict[str,Any]:
    old_collect=module.collect_brain2_forecast_evidence
    old_valid=module._valid_source_ref
    old_compile=module.compile_brain2_forecasts_to_live_bindings
    old_reconcile=module.reconcile_brainlive_with_brain2
    old_lifecycle=module.update_life_model_lifecycle

    def collect_brain2_forecast_evidence(person_id:str,*,limit:int=120)->dict[str,Any]:
        if not person_id: raise ValueError("V18 coordination requires explicit person_id")
        raw=old_collect(person_id,limit=limit)
        # Legacy queries allowed person_id IS NULL.  V18 denies ownerless objects
        # to live prediction; manual migration must assign scope first.
        safe:dict[str,Any]={}
        with connect() as con:
            for section,rows in raw.items():
                source_table=module.SOURCE_SECTION_TABLE_MAP.get(section,section)
                pk=module.SOURCE_PK_MAP.get(source_table)
                keep=[]
                for r in rows if isinstance(rows,list) else []:
                    if not isinstance(r,dict):continue
                    owner=r.get("person_id")
                    if str(owner or "")!=person_id:continue
                    sid=str(r.get(pk) or "") if pk else ""
                    if not sid:continue
                    if not projection_is_active(con,projection_kind="life_model" if source_table.startswith("brain2_") else "watch",source_table=source_table,source_id=sid,person_id=person_id):continue
                    keep.append(r)
                safe[section]=keep[:limit]
        return safe

    def _valid_source_ref(con,person_id:str,source_table:str,source_id:str)->tuple[bool,str|None]:
        ok,reason=old_valid(con,person_id,source_table,source_id)
        if not ok:return ok,reason
        row=module._one(con,f"SELECT * FROM {source_table} WHERE {module.SOURCE_PK_MAP[source_table]}=?",(source_id,))
        if not row or str(row.get("person_id") or "")!=person_id:return False,"owner_required"
        if not projection_is_active(con,projection_kind="life_model" if source_table.startswith("brain2_") else "watch",source_table=source_table,source_id=source_id,person_id=person_id):
            return False,"projection_inactive"
        return True,None

    def compile_brain2_forecasts_to_live_bindings(person_id:str="me",*,use_llm:bool=True,timeout:float=120.0,limit:int=120)->dict[str,Any]:
        if not person_id:raise ValueError("explicit person_id required")
        # Ensure legacy function resolves references with the strengthened rule.
        module.collect_brain2_forecast_evidence=collect_brain2_forecast_evidence
        module._valid_source_ref=_valid_source_ref
        result=old_compile(person_id,use_llm=use_llm,timeout=timeout,limit=limit)
        # Retire every active binding whose source no longer passes the same gate.
        retired=[]
        with connect() as con,write_transaction(con):
            rows=con.execute("SELECT * FROM brain2_live_watch_bindings WHERE person_id=? AND status='active'",(person_id,)).fetchall()
            for r in rows:
                ok,reason=_valid_source_ref(con,person_id,str(r['source_table']),str(r['source_id']))
                if not ok:
                    con.execute("UPDATE brain2_live_watch_bindings SET status='revoked',updated_at=? WHERE binding_id=?",(now_iso(),r['binding_id']))
                    retired.append((str(r['binding_id']),str(r['source_table']),str(r['source_id']),reason))
        for binding_id,table,sid,reason in retired:
            set_projection_active(projection_kind="binding",source_table="brain2_live_watch_bindings",source_id=binding_id,person_id=person_id,active=False,reason=reason)
        result["bindings_retired"]=len(retired)
        return result

    def reconcile_brainlive_with_brain2(person_id:str="me",*,package_id:str|None=None,use_llm:bool=True,timeout:float=180.0,limit:int=100)->dict[str,Any]:
        if not person_id: raise ValueError("explicit person_id required")
        result=old_reconcile(person_id,package_id=package_id,use_llm=use_llm,timeout=timeout,limit=limit)
        revoke=[]
        with connect() as con,write_transaction(con):
            rec_ids=result.get("reconciliation_ids") or []
            for rec_id in rec_ids:
                r=con.execute("SELECT * FROM brainlive_brain2_reconciliations WHERE reconciliation_id=? AND person_id=?",(rec_id,person_id)).fetchone()
                if not r:continue
                verdict=str(r['verdict'] or '').lower()
                table=str(r['brain2_source_table'] or '');sid=str(r['brain2_source_id'] or '')
                if table and sid and (verdict.startswith('contradict') or verdict in {'wrong_context','rejected','invalid'}):
                    con.execute("UPDATE brainlive_brain2_reconciliations SET status='resolved_revoked',updated_at=? WHERE reconciliation_id=?",(now_iso(),rec_id))
                    con.execute("UPDATE brain2_live_watch_bindings SET status='revoked',updated_at=? WHERE person_id=? AND source_table=? AND source_id=? AND status='active'",(now_iso(),person_id,table,sid))
                    # Direct canonical hook revocation; only life-model table
                    # needs its status changed, generic sources remain immutable.
                    if table=='brain2_live_prediction_hooks':
                        con.execute("UPDATE brain2_live_prediction_hooks SET status='obsolete',updated_at=? WHERE person_id=? AND hook_id=?",(now_iso(),person_id,sid))
                    revoke.append((table,sid,rec_id))
        for table,sid,rec_id in revoke:
            set_projection_active(projection_kind="life_model" if table.startswith('brain2_') else "watch",source_table=table,source_id=sid,person_id=person_id,active=False,reason="brainlive_brain2_contradiction")
            try:
                invalidate_descendants(root_table=table,root_id=sid,scope=Scope(person_id=person_id,mode="maintenance"),reason="coordination_contradiction",run_id=rec_id)
            except Exception:
                # The source revocation above is already durable. Invalidation can
                # be retried by maintenance and must not undo it.
                pass
        result["revocations"]=len(revoke)
        return result

    def update_life_model_lifecycle(person_id:str="me")->dict[str,Any]:
        result=old_lifecycle(person_id)
        # Lifecycle contradiction/staleness drives the same projection gate live
        # readers use; it no longer remains an audit-only side table.
        changes=[]
        with connect() as con:
            rows=con.execute("SELECT source_table,source_id,validity_status FROM brain2_life_model_lifecycle WHERE person_id=?",(person_id,)).fetchall()
        for r in rows:
            active=str(r['validity_status'] or '').startswith('active')
            if not active:
                set_projection_active(projection_kind="life_model",source_table=str(r['source_table']),source_id=str(r['source_id']),person_id=person_id,active=False,reason=str(r['validity_status']))
                changes.append(str(r['source_id']))
        result['live_projection_revoked']=len(changes)
        return result
    return {"collect_brain2_forecast_evidence":collect_brain2_forecast_evidence,"_valid_source_ref":_valid_source_ref,"compile_brain2_forecasts_to_live_bindings":compile_brain2_forecasts_to_live_bindings,"reconcile_brainlive_with_brain2":reconcile_brainlive_with_brain2,"update_life_model_lifecycle":update_life_model_lifecycle}
