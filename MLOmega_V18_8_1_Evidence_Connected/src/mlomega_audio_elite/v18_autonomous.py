"""V18 autonomous insights: scoped candidate queue, never immediate canonical mutation."""
from __future__ import annotations
from typing import Any

from .db import connect, insert_only, write_transaction
from .governance_v18 import conversation_in_scope, strict_one
from .utils import json_dumps, now_iso, stable_id

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS v18_autonomous_candidate_runs(
  run_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  trigger_type TEXT NOT NULL,
  status TEXT NOT NULL,
  output_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v18_autonomous_candidates(
  candidate_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  candidate_type TEXT NOT NULL,
  title TEXT,
  summary TEXT,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  counter_evidence_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate',
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v18_autonomous_candidate_owner ON v18_autonomous_candidates(person_id,status,created_at);
"""


def install_autonomous(module: Any) -> dict[str, Any]:
    # Preserve the legacy schema initializer.  The legacy module has no public
    # ``SCHEMA`` constant, so replaying a guessed script was a broken bridge.
    old_ensure_autonomous_schema = module.ensure_autonomous_schema

    def ensure_autonomous_schema() -> None:
        old_ensure_autonomous_schema()
        with connect() as con,write_transaction(con):
            con.executescript(SCHEMA)

    def run_autonomous_insights(conversation_id: str, *, person_id: str, trigger_type: str = "post_ingest") -> dict[str,Any]:
        if not person_id: raise ValueError("V18 autonomous insights requires explicit person_id")
        ensure_autonomous_schema()
        with connect() as con:
            if not conversation_in_scope(con,conversation_id=conversation_id,person_id=person_id):
                raise ValueError("conversation is not proven in supplied person scope")
            bundle=module._bundle_for_autonomy(con,conversation_id,person_id)
        run_id=stable_id("v18autonrun",conversation_id,person_id,trigger_type,now_iso())
        try:
            out=module._llm_json(
                "Tu es un générateur de candidats autonomes V18. JSON strict. Les sorties sont des hypothèses candidates, jamais des vérités ni des mutations automatiques.",
                {"mission":"Proposer des hypothèses/predictions/interventions candidates. Citer des preuves et contre-preuves. Aucune mise à jour de mémoire canonique.","bundle":bundle,"schema":module.INSIGHT_SCHEMA},
                module.INSIGHT_SCHEMA,
            )
            status="ok"; error=None
        except Exception as exc:
            out={"insights":[]}; status="error"; error=str(exc)[:2000]
        created=[]
        with connect() as con,write_transaction(con):
            insert_only(con,"v18_autonomous_candidate_runs",{"run_id":run_id,"conversation_id":conversation_id,"person_id":person_id,"trigger_type":trigger_type,"status":status,"output_json":json_dumps(out),"error_text":error,"created_at":now_iso()},on_conflict="ignore")
            if status=="ok":
                for index,item in enumerate(out.get("insights") or []):
                    if not isinstance(item,dict):continue
                    evidence=item.get("why") or item.get("evidence") or []
                    if isinstance(evidence,str): evidence=[evidence]
                    counter=item.get("counter_evidence") or []
                    if isinstance(counter,str): counter=[counter]
                    cid=stable_id("v18autoncandidate",run_id,index,item.get("title"),item.get("summary"))
                    insert_only(con,"v18_autonomous_candidates",{
                        "candidate_id":cid,"run_id":run_id,"conversation_id":conversation_id,"person_id":person_id,
                        "candidate_type":str(item.get("insight_type") or "hypothesis"),"title":str(item.get("title") or item.get("summary") or "Autonomous candidate")[:300],
                        "summary":str(item.get("summary") or "")[:4000],"evidence_json":json_dumps(evidence),"counter_evidence_json":json_dumps(counter),
                        "confidence":max(0.0,min(1.0,float(item.get("confidence") or 0.0))),"status":"candidate","raw_json":json_dumps(item),"created_at":now_iso(),"updated_at":now_iso(),
                    },on_conflict="ignore")
                    created.append(cid)
        return {"version":"18.0.0-autonomous-candidates","run_id":run_id,"conversation_id":conversation_id,"person_id":person_id,"status":status,"candidate_ids":created,"error":error}
    return {"ensure_autonomous_schema":ensure_autonomous_schema,"run_autonomous_insights":run_autonomous_insights}


def install_behavior(module: Any) -> dict[str,Any]:
    old_build=module.build_v13_for_conversation
    old_all=module.build_v13_all
    def build_v13_for_conversation(conversation_id: str, *, require_llm: bool|None=None, max_episodes:int|None=None, person_id:str|None=None, run_extensions:bool=True)->dict[str,Any]:
        if not person_id: raise ValueError("V18 V13 build requires explicit person_id")
        # Core strict build validates scope. Extensions are re-run explicitly,
        # avoiding old default-user autonomous writes.
        core=old_build(conversation_id,require_llm=require_llm,max_episodes=max_episodes,person_id=person_id,run_extensions=False)
        if not run_extensions:return core
        from .brain2_flow_v13_3 import build_subtopic_segments,discover_latent_outcomes_from_conversation
        from .autonomous_v13_4 import run_autonomous_insights
        return {**core,
                "v13_3_subtopics":build_subtopic_segments(conversation_id),
                "v13_3_latent_outcomes":discover_latent_outcomes_from_conversation(conversation_id,person_id=person_id),
                "v13_4_autonomous_candidates":run_autonomous_insights(conversation_id,person_id=person_id,trigger_type="post_v13_build")}
    def build_v13_all(*,require_llm:bool|None=None,max_episodes_per_conversation:int|None=None)->dict[str,Any]:
        # Original all-mode used every conversation with a hidden default owner.
        with connect() as con:
            rows=con.execute("SELECT conversation_id,person_id FROM v18_conversation_scopes WHERE active=1 ORDER BY conversation_id,person_id").fetchall()
        grouped:dict[str,set[str]]={}
        for r in rows: grouped.setdefault(str(r['conversation_id']),set()).add(str(r['person_id']))
        results=[];skipped=[]
        for cid,owners in grouped.items():
            if len(owners)!=1:skipped.append({"conversation_id":cid,"reason":"ambiguous_owner"});continue
            results.append(build_v13_for_conversation(cid,require_llm=require_llm,max_episodes=max_episodes_per_conversation,person_id=next(iter(owners))))
        return {"version":"18.0.0-v13-batch","results":results,"skipped":skipped,"conversations":len(results)}
    return {"build_v13_for_conversation":build_v13_for_conversation,"build_v13_all":build_v13_all}
