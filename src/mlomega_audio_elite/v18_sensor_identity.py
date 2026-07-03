"""V18 persistent unknown-speaker clusters with owner scope."""
from __future__ import annotations
from typing import Any

from .db import connect, write_transaction, upsert
from .utils import json_dumps, json_loads, now_iso, stable_id


def install(module:Any)->dict[str,Any]:
    old_unknown=module._unknown_voice_cluster
    def _unknown_voice_cluster(live_session_id:str,embedding:list[float]|None,*,abs_start:str|None)->dict[str,Any]:
        # No acoustic vector means no cross-session identity continuity claim.
        if not embedding:
            return old_unknown(live_session_id,embedding,abs_start=abs_start)
        with connect() as con:
            sess=con.execute("SELECT person_id FROM brainlive_sessions WHERE live_session_id=?",(live_session_id,)).fetchone()
            owner=str(sess['person_id']) if sess and sess['person_id'] else None
            rows=con.execute("SELECT * FROM voice_clusters WHERE status IN ('unknown','pending','active') ORDER BY last_seen_at DESC LIMIT 500").fetchall()
        if not owner:return old_unknown(live_session_id,embedding,abs_start=abs_start)
        threshold=module._env_float("MLOMEGA_BRAINLIVE_UNKNOWN_CLUSTER_MIN",.72,min_value=0,max_value=1)
        best=None;score=0.0
        for r in rows:
            meta=json_loads(r['metadata_json'],{}) or {}
            if str(meta.get('owner_person_id') or '')!=owner:continue
            proto=json_loads(r['centroid_embedding_json'],[]) or []
            s=module._cosine_vec(embedding,proto)
            if s>score:best=dict(r);score=s
        now=abs_start or now_iso()
        if best and score>=threshold:
            count=int(best.get('observation_count') or 0)+1
            proto=json_loads(best.get('centroid_embedding_json'),[]) or []
            n=min(len(proto),len(embedding));mean=[((float(proto[i])*(count-1))+float(embedding[i]))/count for i in range(n)] if n else embedding
            with connect() as con,write_transaction(con):
                con.execute("UPDATE voice_clusters SET observation_count=?,centroid_embedding_json=?,last_seen_at=?,confidence=? WHERE cluster_id=?",(count,json_dumps(mean),now,max(float(best.get('confidence') or 0),score),best['cluster_id']))
            return {"label":best.get('display_label') or f"other_unknown_{str(best['cluster_id'])[-6:]}","cluster_id":best['cluster_id'],"cluster_match_score":score,"cluster_observations":count,"persistent":True}
        cid=stable_id("v18_unknown_voice",owner,stable_id(embedding)[:16])
        label=f"other_unknown_{cid[-6:]}"
        with connect() as con,write_transaction(con):
            upsert(con,"voice_clusters",{"cluster_id":cid,"canonical_person_id":None,"display_label":label,"status":"unknown","first_seen_at":now,"last_seen_at":now,"observation_count":1,"total_duration_s":0.0,"often_with_user_count":0,"prompt_status":"pending","prompt_after_count":5,"prompt_after_duration_s":30.0,"centroid_embedding_json":json_dumps(embedding),"model":"speechbrain_ecapa_live","confidence":0.0,"metadata_json":json_dumps({"owner_person_id":owner,"scope":"persistent_unknown_voice_v18","not_identity":True}),"created_at":now,"updated_at":now},"cluster_id")
        return {"label":label,"cluster_id":cid,"cluster_match_score":1.0,"cluster_observations":1,"persistent":True}
    return {"_unknown_voice_cluster":_unknown_voice_cluster}
