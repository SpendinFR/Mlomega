"""V18 owner-safe human feedback and terminal intervention semantics."""
from __future__ import annotations
from typing import Any

from .db import connect, write_transaction
from .governance_v18 import ScopeError, ensure_v18_schema, set_projection_active
from .utils import now_iso


# Only these tables can be changed by a clarification action.  An action is
# LLM-derived input and must never be allowed to choose arbitrary SQL table or
# primary-key identifiers.  The legacy implementation interpolated the table
# name in PRAGMA/SELECT and treated an unknown table as "no owner", which could
# bypass the owner guard.
_ACTION_TARGETS: dict[str, tuple[str, str]] = {
    "v14_5_people_identity_hypotheses": ("hypothesis_id", "person_id"),
    "v14_5_relationship_inference_cards": ("card_id", "person_id"),
    "v14_8_clarification_items": ("item_id", "person_id"),
    "memory_cards": ("card_id", "person_id"),
    "life_events": ("event_id", "subject_person_id"),
    "brain2_live_prediction_hooks": ("hook_id", "person_id"),
    "brainlive_life_hypotheses": ("hypothesis_id", "person_id"),
    "brain2_global_life_patterns_v17": ("pattern_id", "person_id"),
    "brain2_observed_cases_v17": ("observed_case_id", "person_id"),
    "v14_7_intervention_queue": ("queue_id", "person_id"),
}


def _row_owner(con, table: str, key: str) -> str | None:
    spec = _ACTION_TARGETS.get(table)
    if spec is None:
        raise ScopeError(f"clarification target table is not approved: {table!r}")
    key_col, owner_col = spec
    row = con.execute(
        f"SELECT {owner_col} AS owner FROM {table} WHERE {key_col}=?", (key,)
    ).fetchone()
    return str(row["owner"]) if row and row["owner"] else None


def install_clarifications(module: Any) -> dict[str,Any]:
    old_answer=module.answer_clarification
    old_action=module._apply_specific_action
    def _apply_specific_action(
        action: dict[str,Any], *, item:dict[str,Any], answer_text:str, person_id: str | None = None
    )->dict[str,Any]:
        # Legacy ``answer_clarification`` calls this callback without an owner;
        # derive only from the already owner-checked clarification item, never
        # from an ambient default user.
        requested=str(person_id or item.get("person_id") or "")
        target_table=str(action.get("target_table") or item.get("source_table") or "v14_8_clarification_items")
        target_id=str(action.get("target_id") or item.get("source_id") or item.get("item_id") or "")
        if not requested: raise ScopeError("clarification has no owner")
        # An action may only change a row owned by the clarification owner.
        with connect() as con:
            owner = _row_owner(con, target_table, target_id)
        # Missing target rows are not harmless: an action cannot be considered
        # successful when its reference disappeared or was invented by the LLM.
        if owner is None:
            raise ScopeError("clarification action target is missing or has no owner")
        if owner != requested:
            raise ScopeError("clarification action cross-owner refused")
        return old_action(action,item=item,answer_text=answer_text,person_id=requested)
    def answer_clarification(item_id:str,answer_text:str,*,person_id:str|None=None)->dict[str,Any]:
        if not person_id: raise ScopeError("V18 clarification requires explicit person_id")
        ensure_v18_schema()
        with connect() as con:
            row=con.execute("SELECT person_id,status FROM v14_8_clarification_items WHERE item_id=?",(item_id,)).fetchone()
            if not row: raise ValueError(f"clarification introuvable: {item_id}")
            if str(row['person_id'])!=person_id: raise ScopeError("clarification cross-owner refused")
            if str(row['status'] or '').lower() in {"answered","closed","dismissed","cancelled"}: raise ScopeError("clarification already terminal")
        # Ensure legacy resolver calls V18 action guard at runtime.
        module._apply_specific_action=_apply_specific_action
        return old_answer(item_id,answer_text,person_id=person_id)
    return {"_apply_specific_action":_apply_specific_action,"answer_clarification":answer_clarification}


def install_interventions(module: Any)->dict[str,Any]:
    old_feedback=module.record_intervention_feedback
    old_run=module.run_proactive_interventions
    terminal={"dismissed","acted","closed","expired","cancelled","suppressed"}
    def _suppressed_for_cooldown(con,person_id:str,cooldown:str|None)->bool:
        if not cooldown:return False
        row=con.execute("""SELECT 1 FROM v14_7_intervention_queue WHERE person_id=? AND cooldown_key=? AND status IN ('dismissed','acted','closed','cancelled','suppressed') LIMIT 1""",(person_id,cooldown)).fetchone()
        return bool(row)
    def record_intervention_feedback(queue_id:str,*,person_id:str|None=None,feedback_type:str="dismissed",note:str|None=None,helpfulness:float|None=None,action_taken:str|None=None)->dict[str,Any]:
        if not person_id: raise ScopeError("V18 intervention feedback requires explicit person_id")
        ensure_v18_schema()
        with connect() as con:
            row=con.execute("SELECT * FROM v14_7_intervention_queue WHERE queue_id=?",(queue_id,)).fetchone()
            if not row: return {"status":"not_found","queue_id":queue_id}
            if str(row['person_id'])!=person_id: raise ScopeError("intervention feedback cross-owner refused")
            if str(row['status'] or '').lower() in terminal: raise ScopeError("intervention already terminal")
            opportunity=str(row['opportunity_id'] or '')
        result=old_feedback(queue_id,person_id=person_id,feedback_type=feedback_type,note=note,helpfulness=helpfulness,action_taken=action_taken)
        if str(result.get('new_status') or '').lower() in terminal:
            set_projection_active(projection_kind="intervention",source_table="v14_7_intervention_queue",source_id=queue_id,person_id=person_id,active=False,reason=str(result.get('new_status')))
            if opportunity:
                set_projection_active(projection_kind="intervention",source_table="v14_7_intervention_opportunities",source_id=opportunity,person_id=person_id,active=False,reason=str(result.get('new_status')))
        return result
    def run_proactive_interventions(conversation_id:str,*,person_id:str|None=None,trigger_type:str="post_conversation",limit:int=12)->dict[str,Any]:
        if not person_id: raise ScopeError("V18 interventions require explicit person_id")
        result=old_run(conversation_id,person_id=person_id,trigger_type=trigger_type,limit=limit)
        run_id=result.get('run_id') if isinstance(result,dict) else None
        if not run_id:return result
        # Feedback is a terminal policy fact: new opportunities with the same
        # cooldown are suppressed rather than immediately resurrected.
        suppressed: list[str] = []
        with connect() as con,write_transaction(con):
            opps=con.execute("SELECT opportunity_id,cooldown_key FROM v14_7_intervention_opportunities WHERE run_id=? AND person_id=?",(run_id,person_id)).fetchall()
            for o in opps:
                if _suppressed_for_cooldown(con,person_id,o['cooldown_key']):
                    con.execute("UPDATE v14_7_intervention_opportunities SET status='suppressed',updated_at=? WHERE opportunity_id=?",(now_iso(),o['opportunity_id']))
                    con.execute("UPDATE v14_7_intervention_queue SET status='suppressed',updated_at=? WHERE opportunity_id=? AND person_id=? AND status IN ('pending','ready','queued')",(now_iso(),o['opportunity_id'],person_id))
                    suppressed.append(str(o['opportunity_id']))
        for opportunity_id in suppressed:
            set_projection_active(projection_kind="intervention",source_table="v14_7_intervention_opportunities",source_id=opportunity_id,person_id=person_id,active=False,reason="terminal_feedback_cooldown")
        return result
    return {"record_intervention_feedback":record_intervention_feedback,"run_proactive_interventions":run_proactive_interventions}
