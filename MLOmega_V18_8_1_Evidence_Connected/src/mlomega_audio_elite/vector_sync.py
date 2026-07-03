from __future__ import annotations

import hashlib
import json
import uuid
from typing import Iterable

from .config import get_settings
from .db import connect, upsert
from .sync_jobs import run_or_create_sync_job
from .vector_memory import get_embedder, get_vector_store, VectorPoint


def _point_id(kind: str, source_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mlomega:{kind}:{source_id}"))


def _row_payload(source_type: str, source_id: str, text: str, **extra) -> dict:
    return {"source_type": source_type, "source_id": source_id, "text": text, **extra}


def _table_exists(con, table: str) -> bool:
    try:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
    except Exception:
        return False


def _resolve_source_conversation_id(con, source_table: str | None, source_id: str | None, *, _seen: set[tuple[str, str]] | None = None) -> str | None:
    """Resolve the original conversation behind a derived memory source.

    Memory cards do not own conversation_id directly: they point to their source
    through source_table/source_id.  Conversation-scoped vector sync must follow
    that pointer, otherwise cards produced by V13/V14 are skipped even when the
    user asks to sync one conversation.
    """
    if not source_table or not source_id:
        return None
    source_table = str(source_table)
    source_id = str(source_id)
    seen = _seen or set()
    key = (source_table, source_id)
    if key in seen:
        return None
    seen.add(key)
    if source_table == "conversations":
        return source_id
    lookup_sql = {
        "turns": "SELECT conversation_id FROM turns WHERE turn_id=?",
        "source_spans": "SELECT conversation_id FROM source_spans WHERE span_id=?",
        "extraction_runs": "SELECT source_conversation_id AS conversation_id FROM extraction_runs WHERE run_id=?",
        "life_events": "SELECT source_conversation_id AS conversation_id FROM life_events WHERE event_id=?",
        "atomic_memories": "SELECT source_conversation_id AS conversation_id FROM atomic_memories WHERE memory_id=?",
        "memory_frames": "SELECT source_conversation_id AS conversation_id FROM memory_frames WHERE frame_id=?",
        "source_items": "SELECT conversation_id FROM source_items WHERE source_item_id=?",
        "lifestream_segments": "SELECT conversation_id FROM lifestream_segments WHERE segment_id=?",
        "activation_signals": "SELECT conversation_id FROM activation_signals WHERE activation_id=?",
        "utterance_analyses": "SELECT conversation_id FROM utterance_analyses WHERE analysis_id=?",
        "decisions": "SELECT conversation_id FROM decisions WHERE decision_id=?",
        "commitments": "SELECT conversation_id FROM commitments WHERE commitment_id=?",
        "conversation_discourse_maps": "SELECT conversation_id FROM conversation_discourse_maps WHERE discourse_id=?",
        "conversation_topic_threads": "SELECT conversation_id FROM conversation_topic_threads WHERE thread_id=?",
        "utterance_discourse_links": "SELECT conversation_id FROM utterance_discourse_links WHERE link_id=?",
        "conversation_turning_points": "SELECT conversation_id FROM conversation_turning_points WHERE turning_point_id=?",
        "conversation_callbacks": "SELECT conversation_id FROM conversation_callbacks WHERE callback_id=?",
        "episodes": "SELECT source_conversation_id AS conversation_id FROM episodes WHERE episode_id=?",
        "situation_episodes": "SELECT e.source_conversation_id AS conversation_id FROM situation_episodes s JOIN episodes e ON e.episode_id=s.episode_id WHERE s.situation_id=?",
        "internal_state_snapshots": "SELECT e.source_conversation_id AS conversation_id FROM internal_state_snapshots s JOIN episodes e ON e.episode_id=s.episode_id WHERE s.state_id=?",
        "thought_hypotheses": "SELECT e.source_conversation_id AS conversation_id FROM thought_hypotheses t JOIN episodes e ON e.episode_id=t.episode_id WHERE t.thought_id=?",
        "speech_acts": "SELECT e.source_conversation_id AS conversation_id FROM speech_acts sa JOIN episodes e ON e.episode_id=sa.episode_id WHERE sa.speech_act_id=?",
        "action_intentions": "SELECT e.source_conversation_id AS conversation_id FROM action_intentions ai LEFT JOIN episodes e ON e.episode_id=ai.episode_id WHERE ai.intention_id=?",
        "action_outcomes": "SELECT e.source_conversation_id AS conversation_id FROM action_outcomes ao LEFT JOIN episodes e ON e.episode_id=ao.episode_id WHERE ao.outcome_id=?",
        "choice_episodes": "SELECT e.source_conversation_id AS conversation_id FROM choice_episodes c LEFT JOIN episodes e ON e.episode_id=c.episode_id WHERE c.choice_id=?",
        "v14_5_personal_open_loops": "SELECT conversation_id FROM v14_5_personal_open_loops WHERE loop_id=?",
        "v14_6_interpersonal_loop_cards": "SELECT conversation_id FROM v14_6_interpersonal_loop_cards WHERE loop_id=?",
        "v14_7_intervention_opportunities": "SELECT conversation_id FROM v14_7_intervention_opportunities WHERE opportunity_id=?",
        "v14_7_intervention_queue": "SELECT conversation_id FROM v14_7_intervention_queue WHERE queue_id=?",
        "v14_8_clarification_items": "SELECT conversation_id FROM v14_8_clarification_items WHERE item_id=?",
        "brain2_observed_cases_v17": "SELECT conversation_id FROM brain2_observed_cases_v17 WHERE observed_case_id=?",
        "brain2_global_life_patterns_v17": "SELECT NULL AS conversation_id FROM brain2_global_life_patterns_v17 WHERE pattern_id=?",
        "brain2_case_similarity_edges_v17": "SELECT oc.conversation_id FROM brain2_case_similarity_edges_v17 e JOIN brain2_observed_cases_v17 oc ON oc.observed_case_id=e.anchor_case_id WHERE e.edge_id=?",
    }.get(source_table)
    if lookup_sql:
        try:
            row = con.execute(lookup_sql, (source_id,)).fetchone()
            if row and row["conversation_id"]:
                return str(row["conversation_id"])
        except Exception:
            pass
    if source_table == "prediction_cases":
        try:
            row = con.execute("""
                SELECT e.source_conversation_id AS conversation_id
                FROM prediction_cases pc
                LEFT JOIN episodes e ON e.episode_id=pc.episode_id
                WHERE pc.case_id=?
            """, (source_id,)).fetchone()
            if row and row["conversation_id"]:
                return str(row["conversation_id"])
        except Exception:
            pass
    if source_table == "predictions":
        # Predictions can cite cases in JSON.  Resolve the first case that links
        # back to a conversation so conversation-scoped sync can include them.
        try:
            row = con.execute("SELECT evidence_cases_json FROM predictions WHERE prediction_id=?", (source_id,)).fetchone()
            ids: list[str] = []
            if row and row["evidence_cases_json"]:
                try:
                    raw = json.loads(row["evidence_cases_json"] or "[]")
                    if isinstance(raw, list):
                        for item in raw:
                            if isinstance(item, str):
                                ids.append(item)
                            elif isinstance(item, dict):
                                v = item.get("case_id") or item.get("source_id") or item.get("id")
                                if v:
                                    ids.append(str(v))
                except Exception:
                    ids = []
            for case_id in ids:
                cid = _resolve_source_conversation_id(con, "prediction_cases", case_id, _seen=seen)
                if cid:
                    return cid
        except Exception:
            pass
    if source_table == "memory_cards":
        try:
            row = con.execute("SELECT source_table, source_id, source_span_id, extraction_run_id FROM memory_cards WHERE card_id=?", (source_id,)).fetchone()
            if row:
                cid = _resolve_source_conversation_id(con, row["source_table"], row["source_id"], _seen=seen)
                if cid:
                    return cid
                cid = _resolve_source_conversation_id(con, "source_spans", row["source_span_id"], _seen=seen)
                if cid:
                    return cid
                cid = _resolve_source_conversation_id(con, "extraction_runs", row["extraction_run_id"], _seen=seen)
                if cid:
                    return cid
        except Exception:
            pass
    return None


def _iter_memory_rows(limit: int | None = None, conversation_id: str | None = None) -> Iterable[dict]:
    with connect() as con:
        sql = "SELECT * FROM retrieval_chunks"
        params: list = []
        if conversation_id:
            sql += " WHERE conversation_id=?"
            params.append(conversation_id)
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        for r in con.execute(sql, tuple(params)):
            yield _row_payload(
                r["source_type"],
                r["source_id"],
                r["text"],
                chunk_id=r["chunk_id"],
                conversation_id=r["conversation_id"],
                person_id=r["person_id"],
                topic=r["topic"],
                time_start=r["time_start"],
                time_end=r["time_end"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        if limit:
            return
        for r in con.execute("SELECT * FROM conversation_discourse_maps ORDER BY created_at DESC"):
            yield _row_payload(
                "conversation_discourse",
                r["discourse_id"],
                f"conversation_discourse | subject={r['primary_subject']} | stable={bool(r['subject_is_stable'])} | summary={r['conversation_summary']} | emotional_arc={r['emotional_arc']} | intent_arc={r['intent_arc']}",
                conversation_id=r["conversation_id"],
                topic=r["primary_subject"],
                confidence=0.9,
                metadata=json.loads(r["discourse_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM conversation_topic_threads ORDER BY created_at DESC"):
            yield _row_payload(
                "conversation_topic_thread",
                r["thread_id"],
                f"topic_thread | {r['label']} | key={r['thread_key']} | domain={r['life_domain']} | status={r['status']} | {r['summary']}",
                conversation_id=r["conversation_id"],
                topic=r["label"],
                time_start=r["start_s"],
                time_end=r["end_s"],
                confidence=r["importance"],
                thread_key=r["thread_key"],
                life_domain=r["life_domain"],
                status=r["status"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM utterance_discourse_links ORDER BY created_at DESC"):
            yield _row_payload(
                "utterance_discourse_link",
                r["link_id"],
                f"utterance_discourse | thread={r['thread_key']} | subject={r['local_subject']} | relation={r['relation_to_previous']} | context={r['context_summary']} | emotion_continuity={r['emotional_continuity']}",
                conversation_id=r["conversation_id"],
                topic=r["local_subject"],
                confidence=r["confidence"],
                turn_id=r["turn_id"],
                thread_id=r["thread_id"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM conversation_callbacks ORDER BY created_at DESC"):
            yield _row_payload(
                "conversation_callback",
                r["callback_id"],
                f"conversation_callback | {r['relation_type']} | thread={r['thread_key']} | from={r['from_turn_idx']} to={r['to_turn_idx']} | {r['summary']} | evidence={r['evidence_text']}",
                conversation_id=r["conversation_id"],
                topic=r["thread_key"],
                confidence=r["confidence"],
                from_turn_id=r["from_turn_id"],
                to_turn_id=r["to_turn_id"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM conversation_turning_points ORDER BY created_at DESC"):
            yield _row_payload(
                "conversation_turning_point",
                r["turning_point_id"],
                f"turning_point | type={r['turning_point_type']} | turn={r['turn_idx']} | {r['summary']} | before={r['before_state']} | after={r['after_state']} | evidence={r['evidence_text']}",
                conversation_id=r["conversation_id"],
                topic=r["turning_point_type"],
                confidence=r["confidence"],
                turn_id=r["turn_id"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM activation_signals ORDER BY created_at DESC"):
            yield _row_payload(
                "activation_signal",
                r["activation_id"],
                f"activation_signal | person={r['person_id']} | other={r['other_person_id']} | topic={r['topic']} | trigger={r['trigger_summary']} | emotion={r['emotion']} | reaction={r['reaction_rule']} | evidence={r['evidence_text']}",
                conversation_id=r["conversation_id"],
                person_id=r["person_id"],
                topic=r["topic"],
                confidence=r["confidence"],
                turn_id=r["turn_id"],
                other_person_id=r["other_person_id"],
                emotion=r["emotion"],
                metadata={},
            )
        for r in con.execute("SELECT * FROM person_reaction_patterns ORDER BY created_at DESC"):
            yield _row_payload(
                "person_reaction_pattern",
                r["pattern_id"],
                f"person_reaction_pattern | person={r['person_id']} | other={r['other_person_id']} | topic={r['topic']} | trigger={r['trigger_norm']} | emotion={r['emotion']} | reaction={r['typical_reaction']} | preuves={r['evidence_count']}",
                person_id=r["person_id"],
                topic=r["topic"],
                confidence=r["confidence"],
                evidence_count=r["evidence_count"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM source_items ORDER BY created_at DESC"):
            yield _row_payload(
                "source_item",
                r["source_item_id"],
                f"source_item | type={r['source_type']} | channel={r['channel']} | author={r['author_person_id']} | title={r['title']} | content={r['content_text']}",
                conversation_id=r["conversation_id"],
                person_id=r["author_person_id"],
                topic=r["title"],
                time_start=r["captured_at"],
                source_type_original=r["source_type"],
                channel=r["channel"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM lifestream_segments ORDER BY captured_start DESC, start_s DESC"):
            yield _row_payload(
                "lifestream_segment",
                r["segment_id"],
                f"lifestream_segment | kind={r['segment_kind']} | channel={r['channel']} | speaker={r['speaker_person_id']} | importance={r['importance_score']} | keep={r['keep_level']} | text={r['observed_summary']}",
                conversation_id=r["conversation_id"],
                person_id=r["speaker_person_id"],
                time_start=r["captured_start"],
                time_end=r["captured_end"],
                confidence=1.0,
                importance_score=r["importance_score"],
                keep_level=r["keep_level"],
                compression_status=r["compression_status"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM life_events ORDER BY updated_at DESC"):
            yield _row_payload(
                "life_event",
                r["event_id"],
                f"life_event | type={r['event_type']} | status={r['event_status']} | domain={r['life_domain']} | topic={r['topic']} | title={r['title']} | summary={r['summary']} | location={r['location_text']} | money={r['money_amount']} {r['money_currency']} | valence={r['emotional_valence']} | evidence={r['evidence_text']}",
                conversation_id=r["source_conversation_id"],
                person_id=r["subject_person_id"],
                topic=r["topic"],
                time_start=r["occurred_start"],
                time_end=r["occurred_end"],
                confidence=r["confidence"],
                importance_score=r["importance_score"],
                event_type=r["event_type"],
                event_status=r["event_status"],
                life_domain=r["life_domain"],
                temporal_status=r["temporal_status"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM memory_revisions ORDER BY created_at DESC"):
            yield _row_payload(
                "memory_revision",
                r["revision_id"],
                f"memory_revision | target={r['target_table']}:{r['target_id']} | type={r['revision_type']} | {r['previous_status']} -> {r['new_status']} | reason={r['reason']}",
                conversation_id=r["source_conversation_id"],
                time_start=r["valid_from"],
                time_end=r["valid_until"],
                confidence=r["confidence"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM memory_cards ORDER BY updated_at DESC"):
            facets = [dict(x) for x in con.execute("SELECT facet_type, facet_value, confidence, weight FROM memory_facets WHERE target_table='memory_cards' AND target_id=?", (r["card_id"],))]
            evidence = [dict(x) for x in con.execute("SELECT evidence_role, evidence_text, confidence FROM memory_evidence WHERE target_table='memory_cards' AND target_id=? LIMIT 5", (r["card_id"],))]
            source_conversation_id = _resolve_source_conversation_id(con, r["source_table"], r["source_id"]) or _resolve_source_conversation_id(con, "source_spans", r["source_span_id"]) or _resolve_source_conversation_id(con, "extraction_runs", r["extraction_run_id"])
            yield _row_payload(
                "memory_card",
                r["card_id"],
                f"{r['truth_status']} | {r['card_type']} | {r['title']} | {r['summary']} | person={r['person_id']} | topic={r['topic']} | preuves={r['evidence_count']}",
                conversation_id=source_conversation_id,
                person_id=r["person_id"],
                topic=r["topic"],
                time_start=r["time_start"],
                time_end=r["time_end"],
                confidence=r["confidence"],
                evidence_count=r["evidence_count"],
                importance_score=r["importance_score"],
                lifecycle_status=r["lifecycle_status"],
                recurrence_key=r["recurrence_key"],
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
                source_table=r["source_table"],
                original_source_id=r["source_id"],
                truth_status=r["truth_status"],
                card_type=r["card_type"],
                facets=facets,
                evidence=evidence,
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM memory_frames ORDER BY created_at DESC"):
            yield _row_payload(
                "memory_frame",
                r["frame_id"],
                f"{r['frame_type']} | {r['summary']} | actor={r['actor_person_id']} | target={r['target']} | status={r['temporal_status']} | evidence={r['evidence_text']}",
                conversation_id=r["source_conversation_id"],
                person_id=r["actor_person_id"],
                topic=r["topic"],
                time_start=r["frame_time"],
                confidence=r["confidence"],
                frame_type=r["frame_type"],
                temporal_status=r["temporal_status"],
                polarity=r["polarity"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM atomic_memories ORDER BY created_at DESC"):
            yield _row_payload(
                "atomic_memory",
                r["memory_id"],
                f"{r['kind']} | {r['topic']} | {r['content']} | stance={r['stance']} | evidence={r['evidence_text']}",
                conversation_id=r["source_conversation_id"],
                person_id=r["person_id"],
                topic=r["topic"],
                time_start=r["memory_time"],
                confidence=r["confidence"],
            )
        for r in con.execute("SELECT * FROM reflection_states ORDER BY created_at DESC"):
            yield _row_payload(
                "reflection_state",
                r["state_id"],
                f"{r['person_id']} | {r['topic']} | {r['stance']} | {r['summary']} | preuves={r['evidence_count']}",
                person_id=r["person_id"],
                topic=r["topic"],
                time_start=r["period_start"],
                time_end=r["period_end"],
                confidence=r["confidence"],
                evidence_count=r["evidence_count"],
            )
        for r in con.execute("SELECT * FROM patterns ORDER BY created_at DESC"):
            yield _row_payload(
                "pattern",
                r["pattern_id"],
                f"{r['title']} | {r['description']} | type={r['pattern_type']} | scope={r['scope']} | preuves={r['evidence_count']}",
                confidence=r["confidence"],
                evidence_count=r["evidence_count"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        for r in con.execute("SELECT * FROM self_model_facts ORDER BY updated_at DESC"):
            yield _row_payload(
                "self_model",
                r["fact_id"],
                f"{r['fact_type']} | {r['content']} | scope={r['scope']} | preuves={r['evidence_count']}",
                confidence=r["confidence"],
                evidence_count=r["evidence_count"],
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )


        # Modern BrainLive post-stop / V13-V15 deep outputs.  V15.15 treats
        # secondary memory as part of the usable memory surface, so SQLite-only
        # V13/V14 rows must be exposed to vector sync as first-class objects.
        def emit_deep_table(table: str, pk: str, kind: str, text_keys: list[str], *, person_key: str | None = "person_id", conv_resolve_table: str | None = None, where: str = "", order: str = "created_at DESC"):
            if not _table_exists(con, table):
                return
            try:
                limit_sql = "" if conversation_id else " LIMIT 1000"
                rows = con.execute(f"SELECT * FROM {table} {where} ORDER BY {order}{limit_sql}").fetchall()
            except Exception:
                return
            for rr in rows:
                d = dict(rr)
                sid = str(d.get(pk) or "")
                if not sid:
                    continue
                cid = _resolve_source_conversation_id(con, conv_resolve_table or table, sid)
                text_parts = [str(d.get(k) or "") for k in text_keys if d.get(k)]
                if not text_parts:
                    text_parts = [json.dumps({k: v for k, v in d.items() if k.endswith("_json") or k in text_keys}, ensure_ascii=False)[:1200]]
                yield _row_payload(
                    kind, sid,
                    f"{kind} | " + " | ".join(text_parts)[:3500],
                    conversation_id=cid,
                    person_id=d.get(person_key) if person_key else None,
                    confidence=d.get("confidence") or d.get("probability") or d.get("quality_score"),
                    status=d.get("status") or d.get("current_status") or d.get("lifecycle_status"),
                    metadata={"source_table": table, "created_at": d.get("created_at"), "updated_at": d.get("updated_at")},
                )

        for payload in emit_deep_table("turns", "turn_id", "brain2_turn", ["text"], person_key="person_id", conv_resolve_table="turns", order="idx ASC"):
            yield payload
        for payload in emit_deep_table("episodes", "episode_id", "brain2_episode", ["topic", "situation_summary", "trigger_summary", "speech_or_action_summary", "outcome_summary"], person_key="target_person_id", conv_resolve_table="episodes", order="COALESCE(start_time, created_at) DESC"):
            yield payload
        for payload in emit_deep_table("situation_episodes", "situation_id", "brain2_situation", ["situation_type", "life_domain", "stakes", "constraints_json"], person_key="main_person_id", conv_resolve_table="situation_episodes"):
            yield payload
        for payload in emit_deep_table("internal_state_snapshots", "state_id", "brain2_internal_state", ["dominant_emotion", "evidence_text", "metadata_json"], conv_resolve_table="internal_state_snapshots"):
            yield payload
        for payload in emit_deep_table("thought_hypotheses", "thought_id", "brain2_thought_hypothesis", ["thought_type", "content", "evidence_text", "related_need", "related_goal"], conv_resolve_table="thought_hypotheses"):
            yield payload
        for payload in emit_deep_table("action_intentions", "intention_id", "brain2_action_intention", ["intention_text", "action_type", "status", "evidence_text"], conv_resolve_table="action_intentions"):
            yield payload
        for payload in emit_deep_table("action_outcomes", "outcome_id", "brain2_action_outcome", ["outcome_type", "outcome_summary", "evidence_text"], conv_resolve_table="action_outcomes"):
            yield payload
        for payload in emit_deep_table("prediction_cases", "case_id", "brain2_prediction_case", ["context_summary", "action_taken", "outcome"], conv_resolve_table="prediction_cases", where="WHERE COALESCE(usable_for_prediction,1)=1"):
            yield payload
        for payload in emit_deep_table("predictions", "prediction_id", "brain2_prediction", ["prediction_target", "current_context", "predicted_value", "horizon"], conv_resolve_table="predictions", where="WHERE status IN ('open','active','watch')"):
            yield payload
        for payload in emit_deep_table("v14_5_personal_open_loops", "loop_id", "brain2_open_loop", ["loop_summary", "current_status", "next_action_hint"], conv_resolve_table="v14_5_personal_open_loops"):
            yield payload
        for payload in emit_deep_table("v14_6_interpersonal_loop_cards", "loop_id", "brain2_interpersonal_loop", ["loop_summary", "risk_pattern", "repair_pattern"], conv_resolve_table="v14_6_interpersonal_loop_cards"):
            yield payload
        for payload in emit_deep_table("v14_7_intervention_opportunities", "opportunity_id", "brain2_proactive_intervention_opportunity", ["title", "intervention_message", "recommended_action", "why_now", "risk_if_ignored"], conv_resolve_table="v14_7_intervention_opportunities"):
            yield payload
        for payload in emit_deep_table("v14_7_intervention_queue", "queue_id", "brain2_proactive_intervention_queue", ["title", "message", "recommended_action", "why_now"], conv_resolve_table="v14_7_intervention_queue"):
            yield payload
        for payload in emit_deep_table("v14_8_clarification_items", "item_id", "brain2_clarification", ["title", "question_text", "why_needed", "risk_if_wrong"], conv_resolve_table="v14_8_clarification_items"):
            yield payload
        for payload in emit_deep_table("brain2_observed_cases_v17", "observed_case_id", "brain2_observed_life_case_v17", ["title", "context_summary", "trigger_summary", "action_summary", "outcome_summary", "embedding_text"], conv_resolve_table="brain2_observed_cases_v17"):
            yield payload
        for payload in emit_deep_table("brain2_global_life_patterns_v17", "pattern_id", "brain2_global_life_pattern_v17", ["title", "description", "usual_trigger", "usual_action", "usual_outcome", "hidden_loop_hypothesis"], conv_resolve_table="brain2_global_life_patterns_v17"):
            yield payload
        for payload in emit_deep_table("brain2_life_model_strata", "stratum_id", "brain2_life_model_stratum", ["stratum", "model_json"], conv_resolve_table="brain2_life_model_strata"):
            yield payload


def ensure_vector_sync_manifest_schema() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS vector_sync_manifest(
                point_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                conversation_id TEXT,
                synced_at TEXT NOT NULL,
                backend TEXT,
                collection TEXT,
                model TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vector_manifest_source ON vector_sync_manifest(source_type, source_id);
            CREATE INDEX IF NOT EXISTS idx_vector_manifest_conv ON vector_sync_manifest(conversation_id, synced_at);
            """
        )
        con.commit()


def _text_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _payload_matches_conversation(payload: dict, conversation_id: str | None) -> bool:
    if not conversation_id:
        return True
    return payload.get("conversation_id") == conversation_id

def _sync_vectors_untracked(limit: int | None = None, conversation_id: str | None = None, incremental: bool = True) -> dict:
    """Synchronise memory layers into the vector DB.

    Default mode is incremental: unchanged source_id/text pairs are skipped using
    vector_sync_manifest. This prevents re-embedding the whole life archive after
    every new 24/24 audio ingestion.
    """
    ensure_vector_sync_manifest_schema()
    settings = get_settings()
    embedder = get_embedder()
    store = get_vector_store(vector_size=embedder.dims)
    synced = 0
    skipped = 0
    scanned = 0
    by_type: dict[str, int] = {}
    batch: list[VectorPoint] = []
    manifests: list[dict] = []

    def flush() -> None:
        nonlocal synced, batch, manifests
        if not batch:
            return
        store.upsert(batch)
        with connect() as con:
            for m in manifests:
                upsert(con, "vector_sync_manifest", m, "point_id")
            con.commit()
        synced += len(batch)
        batch = []
        manifests = []

    for payload in _iter_memory_rows(limit=limit, conversation_id=conversation_id):
        if not _payload_matches_conversation(payload, conversation_id):
            continue
        scanned += 1
        text = payload["text"]
        st = payload["source_type"]
        sid = payload["source_id"]
        pid = _point_id(st, sid)
        tsha = _text_sha(text)
        if incremental:
            with connect() as con:
                row = con.execute("SELECT text_sha256 FROM vector_sync_manifest WHERE point_id=?", (pid,)).fetchone()
            if row and row["text_sha256"] == tsha:
                skipped += 1
                continue
        vec = embedder.embed(text)
        by_type[st] = by_type.get(st, 0) + 1
        batch.append(VectorPoint(point_id=pid, vector=vec, payload=payload))
        manifests.append({
            "point_id": pid,
            "source_type": st,
            "source_id": sid,
            "text_sha256": tsha,
            "conversation_id": payload.get("conversation_id"),
            "synced_at": __import__("datetime").datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "backend": settings.vector_backend,
            "collection": settings.qdrant_collection,
            "model": embedder.model_name,
        })
        if len(batch) >= 64:
            flush()
    flush()
    return {
        "backend": settings.vector_backend,
        "collection": settings.qdrant_collection,
        "model": embedder.model_name,
        "dims": embedder.dims,
        "scanned": scanned,
        "synced": synced,
        "skipped_unchanged": skipped,
        "incremental": incremental,
        "conversation_id": conversation_id,
        "by_type": by_type,
    }

def sync_vectors(limit: int | None = None, conversation_id: str | None = None, full: bool = False) -> dict:
    """Tracked vector synchronization; incremental by default.

    Use full=True only for an intentional rebuild. Ingestion passes conversation_id
    so new conversations do not force a full archive re-embedding.
    """
    settings = get_settings()
    return run_or_create_sync_job(
        backend=f"vector:{settings.vector_backend}",
        operation="upsert_full" if full else "upsert_incremental",
        target_table="all_memory" if conversation_id is None else "conversation",
        target_id="global" if conversation_id is None else conversation_id,
        conversation_id=conversation_id,
        payload={"limit": limit, "conversation_id": conversation_id, "incremental": not full},
        work=lambda: _sync_vectors_untracked(limit=limit, conversation_id=conversation_id, incremental=not full),
    )

# V18 remediation: manifest full payload/lifecycle, owner scope and tombstones.
# Save original manifest initializer for the V18 installer.
_v17_ensure_vector_sync_manifest_schema = ensure_vector_sync_manifest_schema
from .v18_sync import install_vector as _install_v18_vector
_globals_v18_vector = _install_v18_vector(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_vector)
