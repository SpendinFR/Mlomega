from __future__ import annotations

import asyncio
import json
from typing import Any

from .config import get_settings
from .db import connect
from .sync_jobs import run_or_create_sync_job


class ExternalMemoryError(RuntimeError):
    pass


def _json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _table_exists(con, table: str) -> bool:
    try:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
    except Exception:
        return False


def _rows(con, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    try:
        return list(con.execute(sql, params))
    except Exception:
        return []


def _modern_deep_rows_for_conversation(con, conversation_id: str) -> list[dict[str, Any]]:
    """Return V13/V14/V15 post-stop rows tied to this conversation.

    External stores used to receive only legacy cards/frames/life-events.  The
    current BrainLive post-stop path writes most meaning into V13/V14/V15 tables,
    so Graphiti/Mem0 must receive those rows too.  This helper is deterministic:
    it only serializes existing rows and never interprets them.
    """
    out: list[dict[str, Any]] = []

    def add(kind: str, source_table: str, source_id: Any, text: str, row: Any) -> None:
        sid = str(source_id or "")
        body = str(text or "").strip()
        if not sid or not body:
            return
        try:
            metadata = dict(row)
        except Exception:
            metadata = {}
        out.append({"kind": kind, "source_table": source_table, "source_id": sid, "text": body[:5000], "metadata": metadata})

    if _table_exists(con, "episodes"):
        for r in _rows(con, "SELECT * FROM episodes WHERE source_conversation_id=? ORDER BY COALESCE(start_time, created_at)", (conversation_id,)):
            add("brain2_episode", "episodes", r["episode_id"], f"episode | {r['topic']} | {r['situation_summary']} | trigger={r['trigger_summary']} | action={r['speech_or_action_summary']} | outcome={r['outcome_summary']}", r)
    if _table_exists(con, "situation_episodes"):
        for r in _rows(con, "SELECT s.* FROM situation_episodes s JOIN episodes e ON e.episode_id=s.episode_id WHERE e.source_conversation_id=? ORDER BY s.created_at", (conversation_id,)):
            add("brain2_situation", "situation_episodes", r["situation_id"], f"situation | {r['situation_type']} | domain={r['life_domain']} | stakes={r['stakes']} | constraints={r['constraints_json']}", r)
    if _table_exists(con, "internal_state_snapshots"):
        for r in _rows(con, "SELECT st.* FROM internal_state_snapshots st JOIN episodes e ON e.episode_id=st.episode_id WHERE e.source_conversation_id=? ORDER BY st.created_at", (conversation_id,)):
            add("brain2_internal_state", "internal_state_snapshots", r["state_id"], f"internal_state | {r['dominant_emotion']} | {r['evidence_text']} | confidence={r['confidence']}", r)
    if _table_exists(con, "thought_hypotheses"):
        for r in _rows(con, "SELECT th.* FROM thought_hypotheses th JOIN episodes e ON e.episode_id=th.episode_id WHERE e.source_conversation_id=? ORDER BY th.created_at", (conversation_id,)):
            add("brain2_thought_hypothesis", "thought_hypotheses", r["thought_id"], f"thought_hypothesis | {r['thought_type']} | {r['content']} | evidence={r['evidence_text']}", r)
    if _table_exists(con, "action_intentions"):
        for r in _rows(con, "SELECT ai.* FROM action_intentions ai LEFT JOIN episodes e ON e.episode_id=ai.episode_id WHERE e.source_conversation_id=? ORDER BY ai.created_at", (conversation_id,)):
            add("brain2_action_intention", "action_intentions", r["intention_id"], f"action_intention | {r['action_type']} | {r['intention_text']} | status={r['status']} | evidence={r['evidence_text']}", r)
    if _table_exists(con, "action_outcomes"):
        for r in _rows(con, "SELECT ao.* FROM action_outcomes ao LEFT JOIN episodes e ON e.episode_id=ao.episode_id WHERE e.source_conversation_id=? ORDER BY ao.created_at", (conversation_id,)):
            add("brain2_action_outcome", "action_outcomes", r["outcome_id"], f"action_outcome | {r['outcome_type']} | {r['outcome_summary']} | evidence={r['evidence_text']}", r)
    if _table_exists(con, "brain2_observed_cases_v17"):
        for r in _rows(con, "SELECT * FROM brain2_observed_cases_v17 WHERE conversation_id=? AND status='active' ORDER BY COALESCE(observed_at, created_at)", (conversation_id,)):
            add("brain2_observed_life_case_v17", "brain2_observed_cases_v17", r["observed_case_id"], f"observed_case | type={r['case_type']} | title={r['title']} | context={r['context_summary']} | trigger={r['trigger_summary']} | action={r['action_summary']} | outcome={r['outcome_summary']} | people={r['people_json']} | tags={r['tags_json']}", r)
    if _table_exists(con, "prediction_cases"):
        for r in _rows(con, "SELECT pc.* FROM prediction_cases pc LEFT JOIN episodes e ON e.episode_id=pc.episode_id WHERE e.source_conversation_id=? AND COALESCE(pc.usable_for_prediction,0)=1 ORDER BY pc.created_at", (conversation_id,)):
            add("brain2_prediction_case", "prediction_cases", r["case_id"], f"prediction_case | context={r['context_summary']} | action={r['action_taken']} | outcome={r['outcome']} | quality={r['quality_score']}", r)
    if _table_exists(con, "predictions") and _table_exists(con, "prediction_cases"):
        rows = _rows(con, """
            SELECT DISTINCT p.*
            FROM predictions p
            JOIN prediction_cases pc ON p.evidence_cases_json LIKE '%' || pc.case_id || '%'
            LEFT JOIN episodes e ON e.episode_id=pc.episode_id
            WHERE e.source_conversation_id=? AND p.status IN ('open','active','watch')
            ORDER BY p.created_at
        """, (conversation_id,))
        for r in rows:
            add("brain2_prediction", "predictions", r["prediction_id"], f"prediction | target={r['prediction_target']} | context={r['current_context']} | value={r['predicted_value']} | horizon={r['horizon']} | confidence={r['confidence']}", r)
    if _table_exists(con, "v14_5_personal_open_loops") and _table_exists(con, "v14_5_open_loop_updates"):
        for r in _rows(con, """
            SELECT DISTINCT l.*
            FROM v14_5_personal_open_loops l
            JOIN v14_5_open_loop_updates u ON u.loop_id=l.loop_id
            WHERE u.conversation_id=?
            ORDER BY l.updated_at
        """, (conversation_id,)):
            add("brain2_open_loop", "v14_5_personal_open_loops", r["loop_id"], f"open_loop | {r['title']} | {r['canonical_summary']} | status={r['current_status']} | next={r['progress_definition']}", r)
    # V14.6 loop/model tables are person-level hypotheses without a reliable
    # source_conversation_id in the current schema. Keep them in SQLite/vector
    # sync via global/lifecycle surfaces; do not inject all person-level rows into
    # a conversation-specific external episode.
    if _table_exists(con, "v14_7_intervention_opportunities"):
        for r in _rows(con, "SELECT * FROM v14_7_intervention_opportunities WHERE conversation_id=? ORDER BY updated_at", (conversation_id,)):
            add("brain2_proactive_intervention_opportunity", "v14_7_intervention_opportunities", r["opportunity_id"], f"intervention_opportunity | {r['title']} | {r['intervention_message']} | why={r['why_now']} | risk={r['risk_if_ignored']}", r)
    if _table_exists(con, "v14_7_intervention_queue"):
        for r in _rows(con, "SELECT * FROM v14_7_intervention_queue WHERE conversation_id=? ORDER BY updated_at", (conversation_id,)):
            add("brain2_proactive_intervention_queue", "v14_7_intervention_queue", r["queue_id"], f"intervention_queue | {r['title']} | {r['message']} | action={r['recommended_action']} | why={r['why_now']}", r)
    if _table_exists(con, "v14_8_clarification_items"):
        for r in _rows(con, "SELECT * FROM v14_8_clarification_items WHERE conversation_id=? ORDER BY updated_at", (conversation_id,)):
            add("brain2_clarification", "v14_8_clarification_items", r["item_id"], f"clarification | {r['title']} | {r['question_text']} | why={r['why_needed']} | risk={r['risk_if_wrong']}", r)
    return out


def _modern_deep_episode_lines(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps({"kind": r["kind"], "source_table": r["source_table"], "source_id": r["source_id"], "text": r["text"]}, ensure_ascii=False) for r in rows)


def _conversation_context(con, conversation_id: str) -> tuple[Any, list[Any]]:
    conv = con.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not conv:
        raise ExternalMemoryError(f"Conversation inconnue: {conversation_id}")
    turns = list(con.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY idx", (conversation_id,)))
    return conv, turns


def _memory_cards_for_conversation(con, conversation_id: str) -> list[Any]:
    """Return canonical V3.2 cards attached to a conversation.

    Cards may originate from turns, legacy atomic memories or memory frames. This
    joins through the source tables instead of trusting free-form metadata, so the
    external sync stays stable even if card metadata changes later.
    """
    return list(con.execute(
        """
        SELECT DISTINCT c.*
        FROM memory_cards c
        LEFT JOIN turns t
          ON c.source_table='turns' AND c.source_id=t.turn_id
        LEFT JOIN atomic_memories a
          ON c.source_table='atomic_memories' AND c.source_id=a.memory_id
        LEFT JOIN memory_frames f
          ON c.source_table='memory_frames' AND c.source_id=f.frame_id
        LEFT JOIN conversation_discourse_maps d
          ON c.source_table='conversation_discourse_maps' AND c.source_id=d.discourse_id
        LEFT JOIN conversation_topic_threads th
          ON c.source_table='conversation_topic_threads' AND c.source_id=th.thread_id
        LEFT JOIN conversation_callbacks cb
          ON c.source_table='conversation_callbacks' AND c.source_id=cb.callback_id
        LEFT JOIN life_events le
          ON c.source_table='life_events' AND c.source_id=le.event_id
        WHERE t.conversation_id=?
           OR a.source_conversation_id=?
           OR f.source_conversation_id=?
           OR d.conversation_id=?
           OR th.conversation_id=?
           OR cb.conversation_id=?
           OR le.source_conversation_id=?
        ORDER BY c.time_start, c.created_at, c.card_type
        """,
        (conversation_id, conversation_id, conversation_id, conversation_id, conversation_id, conversation_id, conversation_id),
    ))


def _memory_frames_for_conversation(con, conversation_id: str) -> list[Any]:
    return list(con.execute(
        "SELECT * FROM memory_frames WHERE source_conversation_id=? ORDER BY frame_time, created_at",
        (conversation_id,),
    ))


def _life_events_for_conversation(con, conversation_id: str) -> list[Any]:
    return list(con.execute(
        "SELECT * FROM life_events WHERE source_conversation_id=? ORDER BY occurred_start, created_at",
        (conversation_id,),
    ))



def _discourse_for_conversation(con, conversation_id: str) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    maps = list(con.execute("SELECT * FROM conversation_discourse_maps WHERE conversation_id=? ORDER BY created_at", (conversation_id,)))
    threads = list(con.execute("SELECT * FROM conversation_topic_threads WHERE conversation_id=? ORDER BY importance DESC, start_turn_idx", (conversation_id,)))
    links = list(con.execute("SELECT * FROM utterance_discourse_links WHERE conversation_id=? ORDER BY turn_idx, thread_key", (conversation_id,)))
    callbacks = list(con.execute("SELECT * FROM conversation_callbacks WHERE conversation_id=? ORDER BY from_turn_idx, to_turn_idx", (conversation_id,)))
    return maps, threads, links, callbacks


def _discourse_episode_lines(maps: list[Any], threads: list[Any], links: list[Any], callbacks: list[Any]) -> str:
    lines: list[str] = []
    for m in maps:
        lines.append(json.dumps({
            "kind": "conversation_discourse_map",
            "discourse_id": m["discourse_id"],
            "conversation_id": m["conversation_id"],
            "primary_subject": m["primary_subject"],
            "subject_is_stable": bool(m["subject_is_stable"]),
            "conversation_summary": m["conversation_summary"],
            "emotional_arc": m["emotional_arc"],
            "intent_arc": m["intent_arc"],
            "unresolved_questions": _json(m["unresolved_questions_json"]),
        }, ensure_ascii=False))
    for t in threads:
        lines.append(json.dumps({
            "kind": "conversation_topic_thread",
            "thread_id": t["thread_id"],
            "thread_key": t["thread_key"],
            "label": t["label"],
            "summary": t["summary"],
            "life_domain": t["life_domain"],
            "status": t["status"],
            "importance": t["importance"],
            "turn_range": [t["start_turn_idx"], t["end_turn_idx"]],
        }, ensure_ascii=False))
    for l in links:
        lines.append(json.dumps({
            "kind": "utterance_discourse_link",
            "turn_id": l["turn_id"],
            "turn_idx": l["turn_idx"],
            "thread_key": l["thread_key"],
            "local_subject": l["local_subject"],
            "relation_to_previous": l["relation_to_previous"],
            "context_summary": l["context_summary"],
            "emotional_continuity": l["emotional_continuity"],
        }, ensure_ascii=False))
    for c in callbacks:
        lines.append(json.dumps({
            "kind": "conversation_callback",
            "callback_id": c["callback_id"],
            "thread_key": c["thread_key"],
            "relation_type": c["relation_type"],
            "from_turn_idx": c["from_turn_idx"],
            "to_turn_idx": c["to_turn_idx"],
            "summary": c["summary"],
            "evidence_text": c["evidence_text"],
            "confidence": c["confidence"],
        }, ensure_ascii=False))
    return "\n".join(lines)


def _facets_for_target(con, target_table: str, target_id: str, limit: int = 12) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(
        """
        SELECT facet_type, facet_value, confidence, weight
        FROM memory_facets
        WHERE target_table=? AND target_id=?
        ORDER BY weight DESC, confidence DESC, facet_type
        LIMIT ?
        """,
        (target_table, target_id, limit),
    )]


def _evidence_for_target(con, target_table: str, target_id: str, limit: int = 5) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(
        """
        SELECT evidence_role, evidence_text, confidence
        FROM memory_evidence
        WHERE target_table=? AND target_id=?
        ORDER BY confidence DESC, created_at
        LIMIT ?
        """,
        (target_table, target_id, limit),
    )]


def _card_episode_lines(con, cards: list[Any]) -> str:
    lines: list[str] = []
    for c in cards:
        facets = _facets_for_target(con, "memory_cards", c["card_id"])
        evidence = _evidence_for_target(con, "memory_cards", c["card_id"])
        lines.append(
            json.dumps({
                "kind": "memory_card",
                "card_id": c["card_id"],
                "type": c["card_type"],
                "truth_status": c["truth_status"],
                "person_id": c["person_id"],
                "topic": c["topic"],
                "time_start": c["time_start"],
                "confidence": c["confidence"],
                "title": c["title"],
                "summary": c["summary"],
                "facets": facets,
                "evidence": evidence,
                "source_table": c["source_table"],
                "source_id": c["source_id"],
            }, ensure_ascii=False)
        )
    return "\n".join(lines)


def _frame_episode_lines(frames: list[Any]) -> str:
    lines: list[str] = []
    for f in frames:
        lines.append(
            json.dumps({
                "kind": "memory_frame",
                "frame_id": f["frame_id"],
                "frame_type": f["frame_type"],
                "actor_person_id": f["actor_person_id"],
                "target": f["target"],
                "topic": f["topic"],
                "summary": f["summary"],
                "polarity": f["polarity"],
                "temporal_status": f["temporal_status"],
                "frame_time": f["frame_time"],
                "confidence": f["confidence"],
                "evidence_text": f["evidence_text"],
                "metadata": _json(f["metadata_json"]),
            }, ensure_ascii=False)
        )
    return "\n".join(lines)


def _life_event_episode_lines(events: list[Any]) -> str:
    lines: list[str] = []
    for e in events:
        lines.append(
            json.dumps({
                "kind": "life_event",
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "event_status": e["event_status"],
                "subject_person_id": e["subject_person_id"],
                "title": e["title"],
                "summary": e["summary"],
                "life_domain": e["life_domain"],
                "topic": e["topic"],
                "location_text": e["location_text"],
                "money_amount": e["money_amount"],
                "money_currency": e["money_currency"],
                "emotional_valence": e["emotional_valence"],
                "temporal_status": e["temporal_status"],
                "occurred_start": e["occurred_start"],
                "importance_score": e["importance_score"],
                "confidence": e["confidence"],
                "evidence_text": e["evidence_text"],
                "metadata": _json(e["metadata_json"]),
            }, ensure_ascii=False)
        )
    return "\n".join(lines)


async def _graphiti_add_episode(graphiti: Any, *, name: str, body: str, source_description: str, reference_time: str | None) -> None:
    if hasattr(graphiti, "add_episode"):
        try:
            await graphiti.add_episode(
                name=name,
                episode_body=body,
                source_description=source_description,
                reference_time=reference_time,
            )
        except TypeError:
            await graphiti.add_episode(name, body, source_description, reference_time)
    elif hasattr(graphiti, "add_episode_bulk"):
        await graphiti.add_episode_bulk([{
            "name": name,
            "episode_body": body,
            "source_description": source_description,
            "reference_time": reference_time,
        }])
    else:
        raise ExternalMemoryError("graphiti-core importé mais méthode add_episode introuvable")


async def _sync_graphiti_async_untracked(conversation_id: str) -> dict[str, Any]:
    """Push raw episode plus canonical V3.2 memory layer to Graphiti/Neo4j.

    Graphiti is mandatory in this elite build. There is no Neo4j-only projection
    substitute: if graphiti-core or the service API fails, ingestion stops.
    """
    settings = get_settings()
    with connect() as con:
        conv, turns = _conversation_context(con, conversation_id)
        cards = _memory_cards_for_conversation(con, conversation_id)
        frames = _memory_frames_for_conversation(conversation_id=conversation_id, con=con)
        life_events = _life_events_for_conversation(con, conversation_id)
        maps, threads, discourse_links, callbacks = _discourse_for_conversation(con, conversation_id)
        modern_rows = _modern_deep_rows_for_conversation(con, conversation_id)
        raw_text = "\n".join(f"{t['person_id'] or t['speaker_label']}: {t['text']}" for t in turns)
        card_text = _card_episode_lines(con, cards)
        frame_text = _frame_episode_lines(frames)
        life_event_text = _life_event_episode_lines(life_events)
        discourse_text = _discourse_episode_lines(maps, threads, discourse_links, callbacks)
        modern_text = _modern_deep_episode_lines(modern_rows)

    episode_name = conv["title"] or conversation_id
    base_description = f"MemoryLight Omega audio conversation topic={conv['topic']} participants={conv['participants_json']}"

    try:
        from graphiti_core import Graphiti
    except Exception as exc:  # pragma: no cover - requires elite deps
        raise ExternalMemoryError("graphiti-core absent. Installe requirements-rtx3070.txt") from exc

    try:
        graphiti = Graphiti(settings.graphiti_uri, settings.graphiti_user, settings.graphiti_password)
        if hasattr(graphiti, "build_indices_and_constraints"):
            await graphiti.build_indices_and_constraints()

        await _graphiti_add_episode(
            graphiti,
            name=episode_name,
            body=raw_text,
            source_description=base_description + " | raw_turns",
            reference_time=conv["started_at"],
        )
        if card_text:
            await _graphiti_add_episode(
                graphiti,
                name=f"{episode_name} | canonical memory cards",
                body=card_text,
                source_description=base_description + " | v3.2_memory_cards",
                reference_time=conv["started_at"],
            )
        if frame_text:
            await _graphiti_add_episode(
                graphiti,
                name=f"{episode_name} | typed memory frames",
                body=frame_text,
                source_description=base_description + " | v3.2_memory_frames",
                reference_time=conv["started_at"],
            )
        if life_event_text:
            await _graphiti_add_episode(
                graphiti,
                name=f"{episode_name} | life stream events",
                body=life_event_text,
                source_description=base_description + " | v3.3_life_events",
                reference_time=conv["started_at"],
            )
        if discourse_text:
            await _graphiti_add_episode(
                graphiti,
                name=f"{episode_name} | global discourse context",
                body=discourse_text,
                source_description=base_description + " | v3.2.3_global_discourse_context",
                reference_time=conv["started_at"],
            )
        if modern_text:
            await _graphiti_add_episode(
                graphiti,
                name=f"{episode_name} | Brain2 V13-V17 deep outputs",
                body=modern_text,
                source_description=base_description + " | v17_modern_brain2_deep_outputs",
                reference_time=conv["started_at"],
            )
        return {
            "backend": "graphiti",
            "conversation_id": conversation_id,
            "status": "synced",
            "turns": len(turns),
            "memory_cards": len(cards),
            "memory_frames": len(frames),
            "life_events": len(life_events),
            "episodes": 1 + int(bool(card_text)) + int(bool(frame_text)) + int(bool(life_event_text)) + int(bool(discourse_text)),
            "discourse_maps": len(maps),
            "topic_threads": len(threads),
            "discourse_links": len(discourse_links),
            "callbacks": len(callbacks),
            "modern_deep_rows": len(modern_rows),
        }
    except Exception as exc:  # pragma: no cover - service dependent
        raise ExternalMemoryError(f"Graphiti sync impossible: {exc}") from exc


def _sync_graphiti_untracked(conversation_id: str) -> dict[str, Any]:
    return asyncio.run(_sync_graphiti_async_untracked(conversation_id))


def sync_graphiti(conversation_id: str) -> dict[str, Any]:
    return run_or_create_sync_job(
        backend="graphiti",
        operation="upsert_conversation",
        target_table="conversations",
        target_id=conversation_id,
        conversation_id=conversation_id,
        payload={"conversation_id": conversation_id},
        work=lambda: _sync_graphiti_untracked(conversation_id),
    )


def _mem0_add(memory: Any, text: str, *, user_id: str, metadata: dict[str, Any]) -> None:
    # The payload is already a canonical MemoryLight object. Prefer infer=False
    # when available so Mem0 stores it as-is instead of re-interpreting it.
    try:
        memory.add(text, user_id=user_id, metadata=metadata, infer=False)
    except TypeError:
        try:
            memory.add(text, user_id=user_id, metadata=metadata)
        except TypeError:
            memory.add(text, user_id=user_id)


def _sync_mem0_untracked(conversation_id: str) -> dict[str, Any]:
    """Push canonical V3.2 memory cards and typed frames into Mem0.

    Legacy atomic memories are still exported for backwards compatibility, but
    the canonical payload is now `memory_card`/`memory_frame` so later engines can
    consume the same classification layer everywhere.
    """
    try:
        from .local_mem0 import create_mem0_memory

        memory = create_mem0_memory()
    except Exception as exc:  # pragma: no cover - requires elite deps/service
        raise ExternalMemoryError(f"Mem0 local/Ollama sync impossible: {exc}") from exc
    with connect() as con:
        atomics = list(con.execute("SELECT * FROM atomic_memories WHERE source_conversation_id=?", (conversation_id,)))
        cards = _memory_cards_for_conversation(con, conversation_id)
        frames = _memory_frames_for_conversation(con, conversation_id)
        life_events = _life_events_for_conversation(con, conversation_id)
        maps, threads, discourse_links, callbacks = _discourse_for_conversation(con, conversation_id)
        modern_rows = _modern_deep_rows_for_conversation(con, conversation_id)
        card_facets = {c["card_id"]: _facets_for_target(con, "memory_cards", c["card_id"]) for c in cards}

    added_atomic = 0
    for r in atomics:
        user_id = r["person_id"] or "unknown"
        text = f"atomic_memory | {r['kind']} | {r['topic']} | {r['content']} | evidence={r['evidence_text']}"
        _mem0_add(memory, text, user_id=user_id, metadata={
            "layer": "legacy_atomic_memory",
            "conversation_id": conversation_id,
            "memory_id": r["memory_id"],
            "time": r["memory_time"],
            "confidence": r["confidence"],
        })
        added_atomic += 1

    added_cards = 0
    for c in cards:
        user_id = c["person_id"] or "system"
        text = f"memory_card | {c['truth_status']} | {c['card_type']} | {c['title']} | {c['summary']} | topic={c['topic']}"
        _mem0_add(memory, text, user_id=user_id, metadata={
            "layer": "memory_card",
            "conversation_id": conversation_id,
            "card_id": c["card_id"],
            "card_type": c["card_type"],
            "truth_status": c["truth_status"],
            "source_table": c["source_table"],
            "source_id": c["source_id"],
            "time_start": c["time_start"],
            "confidence": c["confidence"],
            "facets": card_facets.get(c["card_id"], []),
        })
        added_cards += 1

    added_frames = 0
    for f in frames:
        user_id = f["actor_person_id"] or "system"
        text = f"memory_frame | {f['frame_type']} | {f['summary']} | topic={f['topic']} | status={f['temporal_status']} | evidence={f['evidence_text']}"
        _mem0_add(memory, text, user_id=user_id, metadata={
            "layer": "memory_frame",
            "conversation_id": conversation_id,
            "frame_id": f["frame_id"],
            "frame_type": f["frame_type"],
            "temporal_status": f["temporal_status"],
            "polarity": f["polarity"],
            "frame_time": f["frame_time"],
            "confidence": f["confidence"],
        })
        added_frames += 1

    added_life_events = 0
    for e in life_events:
        user_id = e["subject_person_id"] or "system"
        text = f"life_event | {e['event_type']} | {e['title']} | {e['summary']} | domain={e['life_domain']} | topic={e['topic']} | evidence={e['evidence_text']}"
        _mem0_add(memory, text, user_id=user_id, metadata={
            "layer": "life_event",
            "conversation_id": conversation_id,
            "event_id": e["event_id"],
            "event_type": e["event_type"],
            "life_domain": e["life_domain"],
            "occurred_start": e["occurred_start"],
            "importance_score": e["importance_score"],
            "confidence": e["confidence"],
        })
        added_life_events += 1

    added_discourse = 0
    for m in maps:
        _mem0_add(memory, f"conversation_discourse | subject={m['primary_subject']} | summary={m['conversation_summary']} | emotional_arc={m['emotional_arc']} | intent_arc={m['intent_arc']}", user_id="system", metadata={
            "layer": "conversation_discourse",
            "conversation_id": conversation_id,
            "discourse_id": m["discourse_id"],
            "subject_is_stable": bool(m["subject_is_stable"]),
        })
        added_discourse += 1
    for t in threads:
        _mem0_add(memory, f"conversation_topic_thread | {t['label']} | {t['summary']} | domain={t['life_domain']} | status={t['status']}", user_id="system", metadata={
            "layer": "conversation_topic_thread",
            "conversation_id": conversation_id,
            "thread_id": t["thread_id"],
            "thread_key": t["thread_key"],
            "importance": t["importance"],
        })
        added_discourse += 1
    for c in callbacks:
        _mem0_add(memory, f"conversation_callback | {c['relation_type']} | {c['summary']} | from={c['from_turn_idx']} to={c['to_turn_idx']}", user_id="system", metadata={
            "layer": "conversation_callback",
            "conversation_id": conversation_id,
            "callback_id": c["callback_id"],
            "thread_key": c["thread_key"],
            "confidence": c["confidence"],
        })
        added_discourse += 1

    added_modern = 0
    for r in modern_rows:
        _mem0_add(memory, f"{r['kind']} | {r['text']}", user_id="me", metadata={
            "layer": r["kind"],
            "conversation_id": conversation_id,
            "source_table": r["source_table"],
            "source_id": r["source_id"],
            "metadata": r.get("metadata") or {},
        })
        added_modern += 1

    return {
        "backend": "mem0",
        "conversation_id": conversation_id,
        "status": "synced",
        "added_atomic": added_atomic,
        "added_memory_cards": added_cards,
        "added_memory_frames": added_frames,
        "added_life_events": added_life_events,
        "added_discourse": added_discourse,
        "added_modern": added_modern,
        "added": added_atomic + added_cards + added_frames + added_life_events + added_discourse + added_modern,
    }


def sync_mem0(conversation_id: str) -> dict[str, Any]:
    return run_or_create_sync_job(
        backend="mem0",
        operation="upsert_conversation",
        target_table="conversations",
        target_id=conversation_id,
        conversation_id=conversation_id,
        payload={"conversation_id": conversation_id},
        work=lambda: _sync_mem0_untracked(conversation_id),
    )


def sync_external_all(conversation_id: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "graphiti": sync_graphiti(conversation_id),
        "mem0": sync_mem0(conversation_id),
    }

# V18 remediation: external stores remain disabled unless their adapter has an
# explicit stable update/delete contract; no silent append-only contamination.
from .v18_external import install as _install_v18_external
_globals_v18_external = _install_v18_external(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_external)
