from __future__ import annotations

"""Operational user correction/invalidation for the canonical memory layer.

The memory engine never hard-trusts an extracted item forever. User correction is a
first-class memory mutation: it writes a revision, updates the canonical target,
updates/invalidates related memory cards, and queues durable secondary sync.
"""

from typing import Any

from .db import connect
from .life_memory import record_memory_revision
from .memory_foundation import add_memory_facet
from .sync_jobs import schedule_external_sync, schedule_vector_sync
from .governance_v18 import ScopeError, conversation_in_scope, ensure_v18_schema
from .utils import json_dumps, json_loads, now_iso

INACTIVE_STATUSES = {"deleted", "invalidated", "retracted", "superseded", "obsolete"}
REVISION_TO_STATUS = {
    "correct": "active",
    "correction": "active",
    "update": "active",
    "restore": "active",
    "invalidate": "invalidated",
    "delete": "deleted",
    "soft_delete": "deleted",
    "retract": "retracted",
    "supersede": "superseded",
    "archive": "archived",
}


class MemoryCorrectionError(RuntimeError):
    pass


def _merge_json(raw: str | None, patch: dict[str, Any]) -> str:
    base = json_loads(raw, {})
    if not isinstance(base, dict):
        base = {"previous_metadata": base}
    base.update({k: v for k, v in patch.items() if v is not None})
    return json_dumps(base)


def _previous_status(row, target_table: str) -> str | None:
    if target_table == "memory_cards":
        return row["lifecycle_status"]
    if target_table == "life_events":
        return row["event_status"]
    meta = json_loads(row["metadata_json"] if "metadata_json" in row.keys() else None, {})
    if isinstance(meta, dict):
        return meta.get("lifecycle_status")
    return None


def _conversation_for_target(con, target_table: str, target_id: str) -> str | None:
    if target_table == "conversations":
        return target_id
    lookup_sql = {
        "turns": "SELECT conversation_id FROM turns WHERE turn_id=?",
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
        "conversation_turning_points": "SELECT conversation_id FROM conversation_turning_points WHERE turning_point_id=?",
        "conversation_callbacks": "SELECT conversation_id FROM conversation_callbacks WHERE callback_id=?",
    }.get(target_table)
    if lookup_sql:
        row = con.execute(lookup_sql, (target_id,)).fetchone()
        return row["conversation_id"] if row and row["conversation_id"] else None
    if target_table == "memory_cards":
        row = con.execute("SELECT source_table, source_id FROM memory_cards WHERE card_id=?", (target_id,)).fetchone()
        if row:
            return _conversation_for_target(con, row["source_table"], row["source_id"])
    return None


def _related_card_ids(con, target_table: str, target_id: str) -> list[str]:
    ids = {r["card_id"] for r in con.execute("SELECT card_id FROM memory_cards WHERE source_table=? AND source_id=?", (target_table, target_id))}
    if target_table == "memory_cards":
        ids.add(target_id)
    for r in con.execute("SELECT from_id FROM memory_links WHERE from_table='memory_cards' AND to_table=? AND to_id=?", (target_table, target_id)):
        ids.add(r["from_id"])
    return sorted(ids)


def _set_card_status(con, card_id: str, status: str, *, reason: str | None, revision_id: str | None) -> None:
    now = now_iso()
    valid_until = now if status in INACTIVE_STATUSES else None
    con.execute(
        """UPDATE memory_cards
           SET lifecycle_status=?, valid_until=COALESCE(?, valid_until), updated_at=?
           WHERE card_id=?""",
        (status, valid_until, now, card_id),
    )
    con.execute("DELETE FROM memory_facets WHERE target_table='memory_cards' AND target_id=? AND facet_type='lifecycle_status'", (card_id,))
    add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="lifecycle_status", facet_value=status, source="revision", confidence=1.0, metadata={"revision_id": revision_id, "reason": reason})
    if revision_id:
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="revision_id", facet_value=revision_id, source="revision", confidence=1.0)


def _apply_memory_card_patch(con, card_id: str, *, status: str, patch: dict[str, Any], revision_id: str, reason: str | None) -> None:
    allowed = {"title", "summary", "person_id", "topic", "time_start", "time_end", "confidence", "importance_score"}
    fields = {k: v for k, v in patch.items() if k in allowed and v is not None}
    if fields:
        assignments = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE memory_cards SET {assignments}, updated_at=? WHERE card_id=?", (*fields.values(), now_iso(), card_id))
    _set_card_status(con, card_id, status, reason=reason, revision_id=revision_id)


def _apply_target_patch(con, target_table: str, target_id: str, *, status: str, patch: dict[str, Any]) -> None:
    now = now_iso()
    if target_table == "life_events":
        allowed = {
            "title", "summary", "subject_person_id", "life_domain", "topic", "location_text",
            "money_amount", "money_currency", "emotional_valence", "temporal_status",
            "occurred_start", "occurred_end", "importance_score", "confidence", "evidence_text",
        }
        fields = {k: v for k, v in patch.items() if k in allowed and v is not None}
        fields["event_status"] = "observed_or_reported" if status == "active" else status
        fields["updated_at"] = now
        assignments = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE life_events SET {assignments} WHERE event_id=?", (*fields.values(), target_id))
    elif target_table == "atomic_memories":
        allowed = {"person_id", "topic", "content", "stance", "confidence", "memory_time", "evidence_text"}
        fields = {k: v for k, v in patch.items() if k in allowed and v is not None}
        row = con.execute("SELECT metadata_json FROM atomic_memories WHERE memory_id=?", (target_id,)).fetchone()
        if row:
            fields["metadata_json"] = _merge_json(row["metadata_json"], {"lifecycle_status": status, "corrected_at": now})
        if fields:
            assignments = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE atomic_memories SET {assignments} WHERE memory_id=?", (*fields.values(), target_id))
    elif target_table == "memory_frames":
        allowed = {"actor_person_id", "target", "topic", "summary", "polarity", "temporal_status", "frame_time", "confidence", "evidence_text"}
        fields = {k: v for k, v in patch.items() if k in allowed and v is not None}
        row = con.execute("SELECT metadata_json FROM memory_frames WHERE frame_id=?", (target_id,)).fetchone()
        if row:
            fields["metadata_json"] = _merge_json(row["metadata_json"], {"lifecycle_status": status, "corrected_at": now})
        if fields:
            assignments = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE memory_frames SET {assignments} WHERE frame_id=?", (*fields.values(), target_id))
    elif target_table in {"source_items", "lifestream_segments"}:
        pk = "source_item_id" if target_table == "source_items" else "segment_id"
        row = con.execute(f"SELECT metadata_json FROM {target_table} WHERE {pk}=?", (target_id,)).fetchone()
        if row:
            con.execute(f"UPDATE {target_table} SET metadata_json=? WHERE {pk}=?", (_merge_json(row["metadata_json"], {"lifecycle_status": status, "corrected_at": now, **patch}), target_id))


def revise_memory(
    *,
    target_table: str,
    target_id: str,
    revision_type: str,
    reason: str,
    patch: dict[str, Any] | None = None,
    source_conversation_id: str | None = None,
    source_turn_id: str | None = None,
    source_span_id: str | None = None,
    confidence: float = 1.0,
    person_id: str | None = None,
) -> dict[str, Any]:
    """Apply a user/system revision and queue all required secondary sync jobs."""
    patch = patch or {}
    if not isinstance(person_id, str) or not person_id.strip():
        raise ScopeError("memory revision requires explicit person_id; owner fallback is forbidden")
    person_id = person_id.strip()
    ensure_v18_schema()
    normalized_type = revision_type.strip().lower()
    status = REVISION_TO_STATUS.get(normalized_type)
    if not status:
        raise MemoryCorrectionError(f"revision_type non supporté: {revision_type}")
    with connect() as con:
        row = con.execute(f"SELECT * FROM {target_table} WHERE rowid IN (SELECT rowid FROM {target_table} WHERE { _pk_for_table(target_table) }=? LIMIT 1)", (target_id,)).fetchone()
        if not row:
            raise MemoryCorrectionError(f"Cible introuvable: {target_table}:{target_id}")
        previous = _previous_status(row, target_table)
        conversation_id = source_conversation_id or _conversation_for_target(con, target_table, target_id)
        if conversation_id and not conversation_in_scope(
            con, conversation_id=conversation_id, person_id=person_id, allow_legacy_turn_proof=False
        ):
            raise ScopeError("memory revision denied: target conversation is not explicitly owned by person_id")
        revision_id = record_memory_revision(
            con,
            target_table=target_table,
            target_id=target_id,
            revision_type=normalized_type,
            previous_status=previous,
            new_status=status,
            reason=reason,
            source_conversation_id=conversation_id,
            source_turn_id=source_turn_id,
            source_span_id=source_span_id,
            confidence=confidence,
            valid_from=now_iso(),
            valid_until=now_iso() if status in INACTIVE_STATUSES else None,
            metadata={"patch": patch, "source": "memory_correction"},
        )
        if target_table == "memory_cards":
            _apply_memory_card_patch(con, target_id, status=status, patch=patch, revision_id=revision_id, reason=reason)
        else:
            _apply_target_patch(con, target_table, target_id, status=status, patch=patch)
            for card_id in _related_card_ids(con, target_table, target_id):
                card_patch: dict[str, Any] = {}
                if target_table == "life_events":
                    card_patch = {"title": patch.get("title"), "summary": patch.get("summary"), "topic": patch.get("topic"), "time_start": patch.get("occurred_start"), "time_end": patch.get("occurred_end"), "confidence": patch.get("confidence"), "importance_score": patch.get("importance_score")}
                elif target_table == "atomic_memories":
                    card_patch = {"summary": patch.get("content"), "topic": patch.get("topic"), "person_id": patch.get("person_id"), "time_start": patch.get("memory_time"), "confidence": patch.get("confidence")}
                elif target_table == "memory_frames":
                    card_patch = {"summary": patch.get("summary"), "topic": patch.get("topic"), "person_id": patch.get("actor_person_id"), "time_start": patch.get("frame_time"), "confidence": patch.get("confidence")}
                _apply_memory_card_patch(con, card_id, status=status, patch=card_patch, revision_id=revision_id, reason=reason)
        affected_cards = _related_card_ids(con, target_table, target_id)
        schedule_vector_sync(
            con, reason="memory_revision", person_id=person_id, conversation_id=conversation_id,
            payload={"revision_id": revision_id, "target_table": target_table, "target_id": target_id},
        )
        external_jobs: list[str] = []
        if conversation_id:
            external_jobs.append(schedule_external_sync(
                con, conversation_id=conversation_id, backend="graphiti", reason="memory_revision",
                person_id=person_id, payload={"revision_id": revision_id},
            ))
            external_jobs.append(schedule_external_sync(
                con, conversation_id=conversation_id, backend="mem0", reason="memory_revision",
                person_id=person_id, payload={"revision_id": revision_id},
            ))
        con.commit()
    return {
        "revision_id": revision_id,
        "target_table": target_table,
        "target_id": target_id,
        "revision_type": normalized_type,
        "previous_status": previous,
        "new_status": status,
        "conversation_id": conversation_id,
        "affected_cards": affected_cards,
        "external_sync_jobs": external_jobs,
    }


def _pk_for_table(table: str) -> str:
    pks = {
        "memory_cards": "card_id",
        "life_events": "event_id",
        "atomic_memories": "memory_id",
        "memory_frames": "frame_id",
        "source_items": "source_item_id",
        "lifestream_segments": "segment_id",
        "turns": "turn_id",
        "conversations": "conversation_id",
        "activation_signals": "activation_id",
        "utterance_analyses": "analysis_id",
        "decisions": "decision_id",
        "commitments": "commitment_id",
        "conversation_discourse_maps": "discourse_id",
        "conversation_topic_threads": "thread_id",
        "conversation_turning_points": "turning_point_id",
        "conversation_callbacks": "callback_id",
    }
    if table not in pks:
        raise MemoryCorrectionError(f"Table non révisable: {table}")
    return pks[table]
