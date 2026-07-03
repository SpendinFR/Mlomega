"""V18 longitudinal remediation.

This module replaces the unsafe V17 projections while keeping the public V17
function names.  It deliberately keeps case material separate from predictive
retrieval: retrospective analysis may inspect outcomes; predictive retrieval
may only inspect information available before the anchor case's ``observed_at``.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .db import connect, upsert, write_transaction
from .governance_v18 import (
    Scope, ScopeError, conversation_in_scope, ensure_v18_schema,
    link_artifact_in_transaction, record_artifact_version_in_transaction,
    register_conversation_scope_in_transaction, set_projection_active, set_projection_active_in_transaction,
)
from .integrity_v176 import iso_utc, parse_iso_utc
from .utils import json_dumps, now_iso, stable_id
from .v18_predictive_retrieval import (
    PredictiveRetrievalUnavailable, PredictiveValidationError,
    calibrate_predictive_similarity, ensure_predictive_schema, get_predictive_backend,
)


def install(module: Any) -> dict[str, Any]:
    """Build callable overrides using existing parsers/material collectors.

    Passing the original module avoids copy-pasting every legacy helper while
    making all public V17 calls dispatch to the safe implementation.
    """
    old_ensure = module.ensure_longitudinal_case_schema

    def ensure_longitudinal_case_schema() -> None:
        old_ensure()
        ensure_v18_schema()
        ensure_predictive_schema()
        with connect() as con, write_transaction(con):
            cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(brain2_case_similarity_edges_v17)")}
            additions = {
                "similarity_mode": "TEXT NOT NULL DEFAULT 'legacy_retrospective'",
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "source_as_of": "TEXT",
                # RC5 makes ranking/cosine/probability separate values.
                # ``final_score`` is the calibrated event probability for
                # production predictive edges, never a cross-encoder logit.
                "dense_similarity": "REAL",
                "rerank_score": "REAL",
                "calibrated_probability": "REAL",
                "calibration_id": "TEXT",
                "embedding_revision": "TEXT",
                "retrieval_backend": "TEXT",
            }
            for name, ddl in additions.items():
                if name not in cols:
                    con.execute(f"ALTER TABLE brain2_case_similarity_edges_v17 ADD COLUMN {name} {ddl}")
            cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(brain2_observed_cases_v17)")}
            if "source_version" not in cols:
                con.execute("ALTER TABLE brain2_observed_cases_v17 ADD COLUMN source_version TEXT")
            if "invalidated_at" not in cols:
                con.execute("ALTER TABLE brain2_observed_cases_v17 ADD COLUMN invalidated_at TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS idx_b2_case_sim_v18_active ON brain2_case_similarity_edges_v17(person_id,anchor_case_id,status,similarity_mode)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_b2_case_sim_v18_calibrated ON brain2_case_similarity_edges_v17(person_id,similarity_mode,status,calibration_id)")

    def _conversation_ids_for_period(con, *, person_id: str, period_start: str | None = None,
                                     period_end: str | None = None, limit: int = 1000,
                                     as_of: str | None = None) -> list[str]:
        # Explicit scope table is authoritative; a turn owner is allowed only as
        # legacy migration proof.  No default user fallback.
        where = ["COALESCE(cs.active, 0)=1"]
        params: list[Any] = [person_id]
        if period_start:
            where.append("COALESCE(c.ended_at,c.started_at,c.created_at) >= ?"); params.append(period_start)
        if period_end:
            where.append("COALESCE(c.started_at,c.created_at) < ?"); params.append(period_end)
        if as_of:
            where.append("COALESCE(c.started_at,c.created_at) <= ?"); params.append(as_of)
        # A bundle may have an immutable fast-live export plus a newer
        # WhisperX/Pyannote revision.  Only the export whose lifecycle is
        # active belongs in global case/Life-Model selection; an ordinary
        # conversation has no export row and remains eligible.
        exports_present = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brainlive_brain2_event_exports_v1514'"
        ).fetchone())
        export_filter = ""
        if exports_present:
            export_filter = """ AND (
                NOT EXISTS(SELECT 1 FROM brainlive_brain2_event_exports_v1514 ex_any
                           WHERE ex_any.conversation_id=c.conversation_id)
                OR EXISTS(SELECT 1 FROM brainlive_brain2_event_exports_v1514 ex_active
                          WHERE ex_active.conversation_id=c.conversation_id
                            AND ex_active.export_status IN ('active','ok','exported'))
            )"""
        # Fully explicit ownership first.
        sql = f"""SELECT DISTINCT c.conversation_id FROM conversations c
                 JOIN v18_conversation_scopes cs ON cs.conversation_id=c.conversation_id
                 WHERE cs.person_id=? AND {' AND '.join(where)} {export_filter}
                 ORDER BY COALESCE(c.started_at,c.created_at),c.conversation_id LIMIT ?"""
        rows = [dict(r) for r in con.execute(sql, (*params, limit)).fetchall()]
        # Migration compatibility: only conversations containing an attributed
        # user turn; never all conversations in the period.  Apply the same
        # active-export rule so an old superseded bundle cannot be reactivated
        # by the legacy fallback.
        if not rows:
            where2 = ["EXISTS(SELECT 1 FROM turns t WHERE t.conversation_id=c.conversation_id AND t.person_id=?)"]
            p2: list[Any] = [person_id]
            if period_start:
                where2.append("COALESCE(c.ended_at,c.started_at,c.created_at) >= ?"); p2.append(period_start)
            if period_end:
                where2.append("COALESCE(c.started_at,c.created_at) < ?"); p2.append(period_end)
            if as_of:
                where2.append("COALESCE(c.started_at,c.created_at) <= ?"); p2.append(as_of)
            rows = [dict(r) for r in con.execute(
                f"SELECT c.conversation_id FROM conversations c WHERE {' AND '.join(where2)} {export_filter} ORDER BY COALESCE(c.started_at,c.created_at),c.conversation_id LIMIT ?",
                (*p2, limit),
            ).fetchall()]
            for r in rows:
                register_conversation_scope_in_transaction(
                    con, conversation_id=str(r["conversation_id"]), person_id=person_id,
                    evidence_kind="turn_owner", evidence={"migrated_by": "v18_longitudinal"},
                )
        return [str(r["conversation_id"]) for r in rows]

    def _turns_for_episode(con, episode: dict[str, Any]) -> list[dict[str, Any]]:
        cid = episode.get("source_conversation_id")
        start_id, end_id = episode.get("start_turn_id"), episode.get("end_turn_id")
        if not cid or not start_id or not end_id:
            return []
        bounds = [dict(r) for r in con.execute(
            "SELECT turn_id,idx FROM turns WHERE conversation_id=? AND turn_id IN (?,?)", (cid, start_id, end_id)
        ).fetchall()]
        by = {str(r["turn_id"]): int(r["idx"]) for r in bounds}
        if str(start_id) not in by or str(end_id) not in by:
            return []
        lo, hi = sorted((by[str(start_id)], by[str(end_id)]))
        return [dict(r) for r in con.execute(
            "SELECT turn_id,idx,person_id,speaker_label,start_s,end_s,text,metadata_json FROM turns WHERE conversation_id=? AND idx BETWEEN ? AND ? ORDER BY idx",
            (cid, lo, hi),
        ).fetchall()]

    def _case_key(case_type: str, people: list[str], tags: list[str], emotion_after: str | None,
                  outcome: str | None = None, *, include_outcome: bool = False) -> str:
        # Outcome must not define a predictive bucket.  Retrospective reports
        # can opt in explicitly without contaminating the primary case key.
        parts = [case_type]
        if people:
            parts.append("people:" + "+".join(sorted(set(people))[:3]))
        useful = sorted({str(t) for t in tags if t and str(t) not in {"unknown", "other"}})[:6]
        if useful:
            parts.append("tags:" + "+".join(useful))
        if emotion_after:
            parts.append("state:" + str(emotion_after).lower()[:30])
        if include_outcome and outcome:
            parts.append("outcome:" + str(outcome).lower()[:40])
        return "|".join(parts)[:240]

    def _quality_score(material: dict[str, Any]) -> float:
        # A case is only as good as its directly attributable evidence.  LLM
        # states/thoughts do not raise the score like raw observations.
        evidence = material.get("evidence") or []
        observed_turns = sum(1 for e in evidence if e.get("source_table") == "turns" and e.get("source_id"))
        structured = sum(1 for e in evidence if e.get("source_table") in {"action_outcomes", "action_intentions", "choice_episodes"} and e.get("source_id"))
        score = 0.20 + min(0.35, 0.07 * observed_turns) + min(0.20, 0.04 * structured)
        if material.get("people"):
            score += 0.05
        if material.get("tags"):
            score += 0.05
        if material.get("turns") and material.get("outcome"):
            score += 0.08
        return max(0.05, min(0.85, score))

    def build_observed_cases_for_conversation(conversation_id: str, *, person_id: str | None = None,
                                              force: bool = False, as_of: str | None = None) -> dict[str, Any]:
        if not person_id:
            raise ScopeError("V18 observed-case construction requires explicit person_id")
        ensure_longitudinal_case_schema()
        scope = Scope(person_id=person_id, as_of=as_of, mode="maintenance")
        now = now_iso()
        built: list[str] = []
        quarantined: list[str] = []
        with connect() as con, write_transaction(con):
            if not conversation_in_scope(con, conversation_id=conversation_id, person_id=person_id):
                raise ScopeError(f"conversation {conversation_id} has no ownership proof for {person_id}")
            conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if not conv:
                return {"status": "missing_conversation", "conversation_id": conversation_id, "cases_built": 0}
            conv = dict(conv)
            episodes = [dict(r) for r in con.execute(
                """SELECT * FROM episodes WHERE source_conversation_id=?
                   AND COALESCE(lifecycle_status,'active') NOT IN ('deleted','obsolete','contradicted','invalidated')
                   ORDER BY COALESCE(start_time,created_at),episode_id""", (conversation_id,)
            ).fetchall()]
            for ep in episodes:
                eid = str(ep.get("episode_id") or "")
                if not eid:
                    continue
                observed_at = ep.get("start_time") or conv.get("started_at") or ep.get("created_at")
                try:
                    observed_at = iso_utc(parse_iso_utc(str(observed_at)))
                except Exception:
                    quarantined.append(eid); continue
                if scope.as_of and parse_iso_utc(observed_at) > parse_iso_utc(scope.as_of):
                    continue
                turns = _turns_for_episode(con, ep)
                if not turns:
                    quarantined.append(eid)
                    continue
                existing = con.execute(
                    "SELECT observed_case_id FROM brain2_observed_cases_v17 WHERE person_id=? AND episode_id=?",
                    (person_id, eid),
                ).fetchone()
                if existing and not force:
                    built.append(str(existing["observed_case_id"])); continue
                # Temporarily route legacy collector's global lookup through the
                # strict local turns by replacing its result afterwards.
                m = module._collect_case_material(con, ep, person_id)
                m["turns"] = turns
                ctype = module._case_type_from(m.get("situation"), ep, turns)
                tags = list(m.get("tags") or [])
                key = _case_key(ctype, m.get("people") or [], tags, m.get("emotion_after"), m.get("outcome"))
                title = str(ep.get("topic") or ep.get("situation_summary") or f"Observed {ctype}")[:240]
                context = module._txt(ep.get("situation_summary"), (m.get("situation") or {}).get("social_context"), (m.get("situation") or {}).get("stakes")) or title
                vector = {
                    "case_type": ctype, "tags": tags, "people": m.get("people") or [],
                    "emotion_before": m.get("emotion_before"), "emotion_after": m.get("emotion_after"),
                    "activity_type": ctype,
                    "place": ep.get("location_text") or (m.get("situation") or {}).get("place_explicit") or (m.get("situation") or {}).get("place_inferred"),
                    "trigger_tokens": sorted(module._norm_tokenize(str(ep.get("trigger_summary") or "")))[:20],
                    "context_tokens": sorted(module._norm_tokenize(context))[:40],
                    "action_tokens": sorted(module._norm_tokenize(str(m.get("action") or m.get("choice") or "")))[:40],
                    # Stored for retrospective explanation only.  Predictive
                    # retrieval explicitly excludes it.
                    "outcome_tokens": sorted(module._norm_tokenize(str(m.get("outcome") or "")))[:40],
                }
                embedding_text = module._txt(title, context, ep.get("trigger_summary"), m.get("action"), m.get("choice"), " ".join(tags), " ".join(m.get("people") or []))
                ocid = stable_id("observedcase17", person_id, eid)
                source_payload = {"episode_id": eid, "turn_ids": [t.get("turn_id") for t in turns], "evidence": m.get("evidence") or [], "observed_at": observed_at}
                version = record_artifact_version_in_transaction(
                    con, artifact_table="brain2_observed_cases_v17", artifact_id=ocid, identity_key=f"case:{person_id}:{eid}",
                    scope=scope, source_payload=source_payload, metadata={"case_type": ctype},
                )
                row = {
                    "observed_case_id": ocid, "person_id": person_id, "conversation_id": conversation_id, "episode_id": eid,
                    "case_type": ctype, "case_key": key, "title": title, "context_summary": context[:2000],
                    "trigger_summary": ep.get("trigger_summary"), "activity_type": ctype, "place_text": vector["place"],
                    "people_json": json_dumps(m.get("people") or []),
                    "relation_context_json": json_dumps({"interaction":m.get("interaction") or {},"relationship_id":(m.get("situation") or {}).get("related_relationship_id")}),
                    "state_before_json": json_dumps(m.get("state_before") or {}), "state_after_json": json_dumps(m.get("state_after") or {}),
                    "emotion_before": m.get("emotion_before"), "emotion_after":m.get("emotion_after"),
                    "action_summary":m.get("action"), "choice_summary":m.get("choice"), "outcome_summary":m.get("outcome"),
                    "duration_s":module._episode_duration_s(ep, turns), "evidence_json":json_dumps(m.get("evidence") or []),
                    "counter_evidence_json":json_dumps([]), "tags_json":json_dumps(tags), "comparable_vector_json":json_dumps(vector),
                    "embedding_text":embedding_text[:4000], "quality_score":_quality_score(m),
                    "confidence":min(0.90,max(module._safe_float(ep.get("confidence"),0.45),_quality_score(m)*0.85)),
                    "observed_at":observed_at, "status":"active", "created_at":now, "updated_at":now,
                    "source_version":version["artifact_version_id"], "invalidated_at":None,
                }
                upsert(con,"brain2_observed_cases_v17",row,"observed_case_id")
                link_artifact_in_transaction(con, child_table="brain2_observed_cases_v17",child_id=ocid,parent_table="episodes",parent_id=eid,scope=scope,relation_type="derived_from")
                pcid = stable_id("predcase_observed17", person_id, eid)
                upsert(con,"prediction_cases",{
                    "case_id":pcid,"case_type":"observed_life_case_v17","episode_id":eid,"person_id":person_id,
                    "context_summary":context[:2000],
                    "situation_vector_json":json_dumps({"observed_case_id":ocid,"case_type":ctype,"tags":tags,"people":m.get("people") or [],"place":row["place_text"]}),
                    # Do not include after/outcome in a case marked usable for
                    # predictive retrieval. It is preserved in observed case.
                    "state_vector_json":json_dumps({"before":m.get("state_before") or {},"emotion_before":m.get("emotion_before")}),
                    "action_taken":m.get("action") or m.get("choice"),"speech_next":None,"emotion_next":None,"thought_next_hypothesis":None,
                    "outcome":m.get("outcome"),"usable_for_prediction":1 if row["quality_score"]>=0.45 else 0,
                    "quality_score":row["quality_score"],"evidence_json":json_dumps({"observed_case_id":ocid,"source_version":version["artifact_version_id"],"evidence":m.get("evidence") or []}),
                    "created_at":now,"updated_at":now,
                },"case_id")
                built.append(ocid)
        for eid in quarantined:
            set_projection_active(projection_kind="observed_case", source_table="episodes", source_id=eid, person_id=person_id, active=False, reason="episode bounds/time unavailable")
        return {"status":"ok" if not quarantined else "partial","conversation_id":conversation_id,"cases_built":len(set(built)),"observed_case_ids":sorted(set(built)),"quarantined_episode_ids":quarantined}

    def build_observed_cases_for_period(*, person_id: str | None = None, period_start: str|None=None, period_end: str|None=None,
                                        conversation_ids: list[str]|None=None, force: bool=False, as_of: str|None=None) -> dict[str, Any]:
        if not person_id:
            raise ScopeError("V18 observed-case period construction requires explicit person_id")
        ensure_longitudinal_case_schema()
        with connect() as con:
            cids = conversation_ids or _conversation_ids_for_period(con, person_id=person_id, period_start=period_start, period_end=period_end, as_of=as_of)
        results=[]; total=0
        for cid in cids:
            try:
                r=build_observed_cases_for_conversation(cid,person_id=person_id,force=force,as_of=as_of)
            except ScopeError as exc:
                r={"status":"quarantined","conversation_id":cid,"error":str(exc),"cases_built":0}
            results.append(r); total+=int(r.get("cases_built") or 0)
        return {"status":"ok" if all(r.get("status") in {"ok","partial"} for r in results) else "partial","person_id":person_id,"conversation_ids":cids,"conversations_processed":len(cids),"cases_built":total,"results":results}

    def _load_case(row: dict[str, Any]) -> dict[str, Any]:
        r=dict(row); r["tags"]=[str(x) for x in module._as_list(r.get("tags_json"))]; r["people"]=[str(x) for x in module._as_list(r.get("people_json"))]; r["vector"]=module._as_dict(r.get("comparable_vector_json")); return r

    def _case_similarity(a: dict[str, Any], b: dict[str, Any], *, mode: str="predictive") -> tuple[float,dict[str,float],dict[str,Any],dict[str,Any]]:
        if mode not in {"predictive","retrospective"}:
            raise ValueError("mode must be predictive or retrospective")
        av,bv=a.get("vector") or {},b.get("vector") or {}
        # V18 truthfully labels this lexical fallback. A real dense embedding
        # provider can later replace the dimension without reintroducing outcome leakage.
        lexical=module._jaccard(module._norm_tokenize(module._txt(a.get("embedding_text"),a.get("context_summary"))), module._norm_tokenize(module._txt(b.get("embedding_text"),b.get("context_summary"))))
        situation=0.35*(a.get("case_type")==b.get("case_type"))+0.35*module._jaccard(a.get("tags") or [],b.get("tags") or [])
        situation+=0.15 if a.get("place_text") and a.get("place_text")==b.get("place_text") else 0
        situation+=0.15 if a.get("activity_type") and a.get("activity_type")==b.get("activity_type") else 0
        state=0.25*(bool(a.get("emotion_before")) and a.get("emotion_before")==b.get("emotion_before"))
        # after state/outcome are unavailable at predictive retrieval time.
        if mode=="retrospective":
            state+=0.25*(bool(a.get("emotion_after")) and a.get("emotion_after")==b.get("emotion_after"))
        relationship=module._jaccard(a.get("people") or [],b.get("people") or [])
        language=module._jaccard(module._as_list(av.get("action_tokens") or []),module._as_list(bv.get("action_tokens") or []))
        outcome=module._jaccard(module._as_list(av.get("outcome_tokens") or []),module._as_list(bv.get("outcome_tokens") or [])) if mode=="retrospective" else 0.0
        dims={"semantic":min(1.0,lexical),"situation":min(1.0,situation),"state":min(1.0,state),"relationship":min(1.0,relationship),"outcome":min(1.0,outcome),"language":min(1.0,language)}
        weights={"semantic":.30,"situation":.32,"state":.18,"relationship":.12,"language":.08} if mode=="predictive" else {"semantic":.22,"situation":.24,"state":.16,"relationship":.14,"outcome":.14,"language":.10}
        final=sum(weights[k]*dims[k] for k in weights)
        shared={"tags":sorted(set(a.get("tags") or []) & set(b.get("tags") or []))[:20],"people":sorted(set(a.get("people") or []) & set(b.get("people") or []))[:10],"case_type_match":a.get("case_type")==b.get("case_type"),"same_place":bool(a.get("place_text") and a.get("place_text")==b.get("place_text")),"similarity_mode":mode,"semantic_method":"lexical_fallback"}
        diff={"case_a":a.get("observed_case_id"),"case_b":b.get("observed_case_id"),"outcome_compared":mode=="retrospective"}
        return min(1.0,final),dims,shared,diff

    def _case_time(case: dict[str,Any]) -> datetime|None:
        for f in ("observed_at","created_at"):
            try:
                return parse_iso_utc(str(case.get(f)))
            except Exception:
                pass
        return None

    def compute_global_case_similarities(*, person_id: str | None = None, anchor_case_ids: list[str]|None=None,
                                         period_start: str|None=None, period_end: str|None=None, top_k:int=12,
                                         min_score:float=.34,max_history:int=5000, mode:str="predictive",as_of:str|None=None) -> dict[str,Any]:
        """Materialize only calibrated dense predictive edges.

        The legacy ``min_score`` argument is retained for call compatibility but
        intentionally ignored in predictive mode: a raw lexical/cosine threshold
        is not a calibrated probability.  The accepted chronological calibration
        provides the sole decision threshold.
        """
        if not person_id:
            raise ScopeError("V18 predictive similarity requires explicit person_id")
        ensure_longitudinal_case_schema()
        if mode != "predictive":
            # RC5 does not keep a second lexical fallback route for historical
            # reporting. Retrospective analysis may inspect existing evidence,
            # but it cannot materialize V17 similarity edges without the same
            # dense/cross-encoder/calibration contract.
            return {
                "status": "abstained", "reason": "only dense calibrated predictive mode is enabled in RC5",
                "person_id": person_id, "edges_upserted": 0, "similarity_mode": mode, "as_of": as_of,
            }
        scope=Scope(person_id=person_id,as_of=as_of,mode="maintenance")
        now=now_iso()
        with connect() as con:
            base=["person_id=?","status='active'","COALESCE(invalidated_at,'')='' "];params=[person_id]
            if as_of:
                base.append("COALESCE(observed_at,created_at)<=?");params.append(scope.as_of_utc)
            if anchor_case_ids:
                q=",".join("?" for _ in anchor_case_ids); base.append(f"observed_case_id IN ({q})");params.extend(anchor_case_ids)
            else:
                if period_start: base.append("COALESCE(observed_at,created_at)>=?");params.append(period_start)
                if period_end: base.append("COALESCE(observed_at,created_at)<?");params.append(period_end)
            anchors=[_load_case(dict(r)) for r in con.execute(f"SELECT * FROM brain2_observed_cases_v17 WHERE {' AND '.join(base)} ORDER BY COALESCE(observed_at,created_at)",params).fetchall()]
            hist=[_load_case(dict(r)) for r in con.execute("SELECT * FROM brain2_observed_cases_v17 WHERE person_id=? AND status='active' AND COALESCE(invalidated_at,'')='' ORDER BY COALESCE(observed_at,created_at) DESC LIMIT ?",(person_id,max_history)).fetchall()]

        # Cases that do not have source revision/time are legacy/partial facts.
        # They are not eligible for a production predictive vector; do not guess.
        valid_history=[]; quarantined=[]
        for case in hist:
            try:
                if not str(case.get("source_version") or "").strip():
                    raise PredictiveValidationError("source_version missing")
                parse_iso_utc(str(case.get("observed_at") or case.get("created_at") or ""))
                if not str(case.get("embedding_text") or "").strip():
                    raise PredictiveValidationError("embedding_text missing")
            except Exception as exc:
                quarantined.append({"observed_case_id": case.get("observed_case_id"), "reason": str(exc)[:240]})
                continue
            valid_history.append(case)
        valid_ids={str(c["observed_case_id"]) for c in valid_history}
        valid_anchors=[a for a in anchors if str(a.get("observed_case_id")) in valid_ids]

        def invalidate_previous(reason: str) -> None:
            if not valid_anchors and not anchors:
                return
            ids=[str(a.get("observed_case_id")) for a in anchors if a.get("observed_case_id")]
            if not ids:
                return
            with connect() as con,write_transaction(con):
                placeholders=",".join("?" for _ in ids)
                # Every pre-RC5 or previous RC5 active predictive edge is a
                # projection. It must not survive an abstention/rebuild.
                con.execute(
                    f"UPDATE brain2_case_similarity_edges_v17 SET status='invalidated',updated_at=? WHERE person_id=? AND similarity_mode='predictive' AND status='active' AND anchor_case_id IN ({placeholders})",
                    (now,person_id,*ids),
                )

        if not valid_anchors or not valid_history:
            invalidate_previous("no eligible canonical cases")
            return {
                "status":"abstained","reason":"no eligible versioned observed cases",
                "person_id":person_id,"anchors":len(anchors),"history":len(hist),"eligible_history":len(valid_history),
                "edges_upserted":0,"similarity_mode":mode,"as_of":scope.as_of_utc,"quarantined_cases":quarantined,
            }
        try:
            backend=get_predictive_backend()
            index_result=backend.sync_cases(valid_history, person_id=person_id)
            calibration=calibrate_predictive_similarity(person_id=person_id,backend=backend)
        except PredictiveRetrievalUnavailable as exc:
            invalidate_previous("dense backend unavailable")
            return {
                "status":"abstained","reason":str(exc),"person_id":person_id,"anchors":len(valid_anchors),
                "history":len(valid_history),"edges_upserted":0,"similarity_mode":mode,"as_of":scope.as_of_utc,
                "quarantined_cases":quarantined,
            }
        except Exception as exc:
            invalidate_previous("predictive backend fault")
            return {
                "status":"abstained","reason":f"predictive backend fault: {exc}","person_id":person_id,
                "anchors":len(valid_anchors),"history":len(valid_history),"edges_upserted":0,"similarity_mode":mode,
                "as_of":scope.as_of_utc,"quarantined_cases":quarantined,
            }
        if not calibration.accepted:
            invalidate_previous("calibration unavailable")
            return {
                "status":"abstained","reason":calibration.reason or calibration.status,
                "person_id":person_id,"anchors":len(valid_anchors),"history":len(valid_history),
                "edges_upserted":0,"similarity_mode":mode,"as_of":scope.as_of_utc,
                "calibration":{
                    "id":calibration.calibration_id,"status":calibration.status,
                    "validation_precision":calibration.validation_precision,"validation_brier":calibration.validation_brier,
                },"index":index_result,"quarantined_cases":quarantined,
            }

        candidates={str(c["observed_case_id"]):c for c in valid_history}
        created=0; rejected_by_calibration=0; retrieval_failures=[]
        with connect() as con,write_transaction(con):
            for a in valid_anchors:
                aid=str(a["observed_case_id"])
                con.execute(
                    "UPDATE brain2_case_similarity_edges_v17 SET status='invalidated',updated_at=? WHERE person_id=? AND anchor_case_id=? AND similarity_mode='predictive' AND status='active'",
                    (now,person_id,aid),
                )
                try:
                    matches=backend.retrieve(a,canonical_candidates=candidates,limit=max(top_k, backend.rerank_candidate_limit))
                except PredictiveRetrievalUnavailable as exc:
                    retrieval_failures.append({"anchor_case_id":aid,"error":str(exc)[:500]})
                    continue
                accepted=[]
                for match in matches:
                    if match.rerank_score < float(calibration.threshold):
                        rejected_by_calibration += 1
                        continue
                    probability=calibration.probability(match.rerank_score)
                    if probability is None:
                        rejected_by_calibration += 1
                        continue
                    b=candidates.get(match.observed_case_id)
                    if not b:
                        continue
                    # Defense in depth: no future, owner-mismatched, or outcome
                    # similarity can enter a predictive materialization.
                    at=_case_time(a); bt=_case_time(b)
                    if str(b.get("person_id")) != person_id or bt >= at:
                        continue
                    accepted.append((float(probability),match,b))
                accepted.sort(key=lambda x:(-x[0],-x[1].rerank_score,-x[1].dense_similarity,x[1].observed_case_id))
                for probability,match,b in accepted[:top_k]:
                    eid=stable_id("caseedge18dense",person_id,backend.embedding_revision,aid,match.observed_case_id)
                    shared={
                        "semantic_method":"qdrant_dense_cross_encoder_calibrated",
                        "entity_kind":"v17_predictive_observed_case",
                        "qdrant_collection":backend.collection,
                        "embedding_revision":backend.embedding_revision,
                        "calibration_id":calibration.calibration_id,
                        "dense_similarity":match.dense_similarity,
                        "rerank_score":match.rerank_score,
                        "tags":sorted(set(a.get("tags") or []) & set(b.get("tags") or []))[:20],
                        "people":sorted(set(a.get("people") or []) & set(b.get("people") or []))[:10],
                    }
                    diff={
                        "case_a":aid,"case_b":match.observed_case_id,"outcome_compared":False,
                        "source_versions":[a.get("source_version"),b.get("source_version")],
                    }
                    upsert(con,"brain2_case_similarity_edges_v17",{
                        "edge_id":eid,"person_id":person_id,"anchor_case_id":aid,"similar_case_id":match.observed_case_id,
                        # This field now means calibrated probability; raw score
                        # is preserved separately and never copied into confidence.
                        "final_score":probability,"semantic_similarity":match.dense_similarity,
                        "situation_similarity":0.0,"state_similarity":0.0,"relationship_similarity":0.0,
                        "outcome_similarity":0.0,"language_similarity":0.0,
                        "shared_features_json":json_dumps(shared),"differences_json":json_dumps(diff),
                        "created_at":now,"updated_at":now,"similarity_mode":"predictive","status":"active",
                        "source_as_of":scope.as_of_utc,"dense_similarity":match.dense_similarity,
                        "rerank_score":match.rerank_score,"calibrated_probability":probability,
                        "calibration_id":calibration.calibration_id,"embedding_revision":backend.embedding_revision,
                        "retrieval_backend":"qdrant+dense+cross_encoder",
                    },"edge_id")
                    record_artifact_version_in_transaction(con, artifact_table="brain2_case_similarity_edges_v17",artifact_id=eid,identity_key=f"edge:{person_id}:predictive:{backend.embedding_revision}:{aid}:{match.observed_case_id}",scope=scope,source_payload={"anchor":aid,"similar":match.observed_case_id,"mode":"predictive","as_of":scope.as_of_utc,"calibrated_probability":probability,"rerank_score":match.rerank_score,"calibration":calibration.calibration_id})
                    link_artifact_in_transaction(con, child_table="brain2_case_similarity_edges_v17",child_id=eid,parent_table="brain2_observed_cases_v17",parent_id=aid,scope=scope,relation_type="derived_from")
                    link_artifact_in_transaction(con, child_table="brain2_case_similarity_edges_v17",child_id=eid,parent_table="brain2_observed_cases_v17",parent_id=match.observed_case_id,scope=scope,relation_type="derived_from")
                    created+=1
        status="partial" if retrieval_failures else "ok"
        return {
            "status":status,"person_id":person_id,"anchors":len(valid_anchors),"history":len(valid_history),
            "edges_upserted":created,"similarity_mode":"predictive","as_of":scope.as_of_utc,
            "index":index_result,"calibration":{
                "id":calibration.calibration_id,"status":calibration.status,"threshold":calibration.threshold,
                "validation_precision":calibration.validation_precision,"validation_recall":calibration.validation_recall,
                "validation_brier":calibration.validation_brier,"embedding_revision":backend.embedding_revision,
            },"rejected_by_calibration":rejected_by_calibration,"retrieval_failures":retrieval_failures,
            "quarantined_cases":quarantined,
        }

    def mine_global_life_patterns(*, person_id: str | None = None, period_start: str|None=None, period_end: str|None=None,
                                  min_recurrence:int=3,max_cases:int=5000,as_of:str|None=None) -> dict[str,Any]:
        if not person_id:
            raise ScopeError("V18 pattern mining requires explicit person_id")
        ensure_longitudinal_case_schema(); scope=Scope(person_id=person_id,as_of=as_of,mode="maintenance"); now=now_iso()
        with connect() as con,write_transaction(con):
            clauses=["person_id=?","status='active'","COALESCE(invalidated_at,'')='' "];params=[person_id]
            if period_start: clauses.append("COALESCE(observed_at,created_at)>=?");params.append(period_start)
            if period_end: clauses.append("COALESCE(observed_at,created_at)<?");params.append(period_end)
            if as_of: clauses.append("COALESCE(observed_at,created_at)<=?");params.append(scope.as_of_utc)
            cases=[_load_case(dict(r)) for r in con.execute(f"SELECT * FROM brain2_observed_cases_v17 WHERE {' AND '.join(clauses)} ORDER BY COALESCE(observed_at,created_at) LIMIT ?",(*params,max_cases)).fetchall()]
            groups=defaultdict(list)
            for c in cases:
                key=_case_key(str(c.get("case_type") or "life_event"),c.get("people") or [],c.get("tags") or [],c.get("emotion_after"),None)
                groups[key].append(c)
            pids=[]; active_keys=set()
            for key,group in groups.items():
                distinct_eps={c.get("episode_id") for c in group if c.get("episode_id")}; distinct_days={(str(c.get("observed_at") or "")[:10]) for c in group}
                if len(group)<min_recurrence or len(distinct_eps)<min_recurrence or len(distinct_days)<min(2,min_recurrence):
                    continue
                active_keys.add(key)
                ctype=str(group[0].get("case_type") or "life_event"); people=sorted({p for c in group for p in c.get("people") or []})
                contexts=sorted({str(c.get("context_summary") or "")[:250] for c in group if c.get("context_summary")})[:10]
                # Outcomes are described, not used to form the group.
                outcomes=[str(c.get("outcome_summary") or "") for c in group if c.get("outcome_summary")]
                actions=[str(c.get("action_summary") or c.get("choice_summary") or "") for c in group if c.get("action_summary") or c.get("choice_summary")]
                triggers=[str(c.get("trigger_summary") or "") for c in group if c.get("trigger_summary")]
                cex=module._counterexamples_for_group(group,cases)
                confidence=min(.90,.25+.06*len(group)+.035*len(distinct_days)-.03*len(cex))
                status="confirmed" if len(group)>=5 and len(distinct_days)>=4 and confidence>=.65 else "candidate"
                stratum="general" if len(group)>=8 and len(distinct_days)>=7 else "recent"
                pid=stable_id("globalpattern18",person_id,key)
                payload={"case_ids":[c["observed_case_id"] for c in group],"key":key,"period":[period_start,period_end],"as_of":scope.as_of_utc}
                record_artifact_version_in_transaction(con, artifact_table="brain2_global_life_patterns_v17",artifact_id=pid,identity_key=f"pattern:{person_id}:{key}",scope=scope,source_payload=payload)
                row={"pattern_id":pid,"person_id":person_id,"pattern_type":ctype,"pattern_key":key,"title":f"{ctype.replace('_',' ')} pattern across {len(group)} independent episodes"[:240],"description":f"Pattern supported by {len(group)} episodes over {len(distinct_days)} days; outcome was not used to form the group.","recurrence_count":len(group),"context_count":len(contexts),"people_count":len(people),"counterexample_count":len(cex),"first_seen":group[0].get("observed_at"),"last_seen":group[-1].get("observed_at"),"evidence_case_ids_json":json_dumps([c["observed_case_id"] for c in group]),"counterexample_case_ids_json":json_dumps(cex),"contexts_json":json_dumps(contexts),"people_json":json_dumps(people),"usual_trigger":Counter(triggers).most_common(1)[0][0] if triggers else None,"usual_state_before":None,"usual_action":Counter(actions).most_common(1)[0][0] if actions else None,"usual_outcome":Counter(outcomes).most_common(1)[0][0] if outcomes else None,"hidden_loop_hypothesis":None,"confidence":confidence,"status":status,"stratum":stratum,"metadata_json":json_dumps({"version":"18.0.0","independent_episode_count":len(distinct_eps),"distinct_day_count":len(distinct_days),"as_of":scope.as_of_utc,"outcome_not_in_key":True}),"created_at":now,"updated_at":now}
                upsert(con,"brain2_global_life_patterns_v17",row,"pattern_id"); pids.append(pid)
                for c in group: link_artifact_in_transaction(con, child_table="brain2_global_life_patterns_v17",child_id=pid,parent_table="brain2_observed_cases_v17",parent_id=c["observed_case_id"],scope=scope,relation_type="derived_from")
            # Patterns whose supporting set disappeared must stop feeding live.
            existing=[dict(r) for r in con.execute("SELECT pattern_id,pattern_key FROM brain2_global_life_patterns_v17 WHERE person_id=? AND status IN ('candidate','confirmed')",(person_id,)).fetchall()]
            stale=[r for r in existing if r["pattern_key"] not in active_keys]
            for r in stale:
                con.execute("UPDATE brain2_global_life_patterns_v17 SET status='deprecated',updated_at=? WHERE pattern_id=?",(now,r["pattern_id"]))
                set_projection_active_in_transaction(con, projection_kind="global_pattern",source_table="brain2_global_life_patterns_v17",source_id=str(r["pattern_id"]),person_id=person_id,active=False,reason="support set no longer meets V18 criteria")
        return {"status":"ok","person_id":person_id,"cases_considered":len(cases),"patterns_upserted":len(pids),"pattern_ids":pids,"as_of":scope.as_of_utc}

    def run_longitudinal_consolidation(*, person_id: str | None = None, period: str="day", run_date: str|None=None,
                                      period_start: str|None=None, period_end: str|None=None,use_llm:bool=True,
                                      run_periodic_mirror_layer:bool=True,force_cases:bool=False,as_of:str|None=None) -> dict[str,Any]:
        if not person_id:
            raise ScopeError("V18 longitudinal consolidation requires explicit person_id")
        ensure_longitudinal_case_schema(); scope=Scope(person_id=person_id,as_of=as_of,mode="maintenance")
        start,end,label=module.period_bounds(period,run_date=run_date,period_start=period_start,period_end=period_end)
        rid=stable_id("longrun18",person_id,period,start or "",end or "",scope.as_of_utc or "live")
        now=now_iso(); result={"period_label":label,"period_start":start,"period_end":end,"as_of":scope.as_of_utc}; status="completed"; error=None
        try:
            cases=build_observed_cases_for_period(person_id=person_id,period_start=start,period_end=end,force=force_cases,as_of=scope.as_of_utc);result["observed_cases"]=cases
            anchors=[]
            with connect() as con:
                q="SELECT observed_case_id FROM brain2_observed_cases_v17 WHERE person_id=? AND status='active' AND COALESCE(invalidated_at,'')=''";p=[person_id]
                if start: q+=" AND COALESCE(observed_at,created_at)>=?";p.append(start)
                if end: q+=" AND COALESCE(observed_at,created_at)<?";p.append(end)
                if scope.as_of_utc: q+=" AND COALESCE(observed_at,created_at)<=?";p.append(scope.as_of_utc)
                anchors=[str(r[0]) for r in con.execute(q,p).fetchall()]
            result["similarity"]=compute_global_case_similarities(person_id=person_id,anchor_case_ids=anchors,period_start=start,period_end=end,mode="predictive",as_of=scope.as_of_utc)
            result["global_patterns"]=mine_global_life_patterns(person_id=person_id,period_start=start,period_end=end,min_recurrence=2 if period in {"day","hour"} else 3,as_of=scope.as_of_utc)
            # Periodic mirror is optional and cannot make the data run look complete.
            if run_periodic_mirror_layer and use_llm:
                try:
                    from .pattern_mirror_v14 import run_periodic_mirror
                    result["periodic_mirror_v14"]=run_periodic_mirror(person_id=person_id,period=period,period_start=start,period_end=end)
                except Exception as exc:
                    result["periodic_mirror_v14"]={"status":"error","error":str(exc)[:1000]};status="partial"
        except Exception as exc:
            status="failed";error=str(exc)[:2000];result["error"]=error
        with connect() as con,write_transaction(con):
            upsert(con,"brain2_longitudinal_runs_v17",{"run_id":rid,"person_id":person_id,"period":period,"period_start":start,"period_end":end,"status":status,"conversations_processed":int((result.get("observed_cases") or {}).get("conversations_processed") or 0),"cases_built":int((result.get("observed_cases") or {}).get("cases_built") or 0),"similarity_edges":int((result.get("similarity") or {}).get("edges_upserted") or 0),"patterns_upserted":int((result.get("global_patterns") or {}).get("patterns_upserted") or 0),"results_json":json_dumps(result),"error_text":error,"created_at":now,"updated_at":now_iso()},"run_id")
        return {"version":"18.0.0","run_id":rid,"person_id":person_id,"period":period,"status":status,**result}

    def longitudinal_memory_digest(person_id: str | None = None, *,limit:int=30) -> dict[str,Any]:
        if not person_id:
            raise ScopeError("V18 longitudinal digest requires explicit person_id")
        ensure_longitudinal_case_schema()
        with connect() as con:
            pats=[dict(r) for r in con.execute("SELECT * FROM brain2_global_life_patterns_v17 WHERE person_id=? AND status IN ('candidate','confirmed') ORDER BY confidence DESC,recurrence_count DESC LIMIT ?",(person_id,limit)).fetchall()]
            cases=[dict(r) for r in con.execute("SELECT * FROM brain2_observed_cases_v17 WHERE person_id=? AND status='active' AND COALESCE(invalidated_at,'')='' ORDER BY COALESCE(observed_at,created_at) DESC LIMIT ?",(person_id,limit)).fetchall()]
            runs=[dict(r) for r in con.execute("SELECT * FROM brain2_longitudinal_runs_v17 WHERE person_id=? ORDER BY created_at DESC LIMIT 10",(person_id,)).fetchall()]
        return {"person_id":person_id,"patterns":pats,"recent_cases":cases,"runs":runs}

    return {
        "ensure_longitudinal_case_schema":ensure_longitudinal_case_schema,
        "_conversation_ids_for_period":_conversation_ids_for_period,
        "_turns_for_episode":_turns_for_episode,
        "_case_key":_case_key,
        "_quality_score":_quality_score,
        "build_observed_cases_for_conversation":build_observed_cases_for_conversation,
        "build_observed_cases_for_period":build_observed_cases_for_period,
        "_load_case":_load_case,
        "_case_similarity":_case_similarity,
        "compute_global_case_similarities":compute_global_case_similarities,
        "mine_global_life_patterns":mine_global_life_patterns,
        "run_longitudinal_consolidation":run_longitudinal_consolidation,
        "longitudinal_memory_digest":longitudinal_memory_digest,
    }
