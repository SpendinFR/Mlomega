from __future__ import annotations

"""Canonical Memory Foundation helpers.

This module is deliberately not a prediction engine. It creates the memory layer
that a future pattern/prediction engine can trust and exploit:

- every extracted item can point back to a source span;
- every item gets a canonical memory card;
- every card/item can be faceted for filtered retrieval;
- every assertion is typed by truth layer: observed, inferred, consolidated;
- links between memories are explicit, not buried in prose.
"""

from sqlite3 import Connection
from typing import Any

from .db import upsert
from .utils import json_dumps, normalize_text, now_iso, sha256_bytes, stable_id

MEMORY_SCHEMA_VERSION = "3.3-life-stream-foundation"

TRUTH_OBSERVED = "observed"
TRUTH_INFERRED = "inferred"
TRUTH_CONSOLIDATED = "consolidated"
TRUTH_EXTERNAL = "external"


def record_source_span(
    con: Connection,
    *,
    conversation_id: str,
    turn_id: str | None,
    person_id: str | None,
    source_asset_id: str | None,
    text: str,
    start_s: float | None = None,
    end_s: float | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    span_role: str = "turn_text",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Register a source quote/span once and return its stable id."""
    span_id = stable_id("span", conversation_id, turn_id, person_id, start_s, end_s, char_start, char_end, text)
    created_at = now_iso()
    upsert(con, "source_spans", {
        "span_id": span_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "person_id": person_id,
        "source_asset_id": source_asset_id,
        "span_role": span_role,
        "start_s": start_s,
        "end_s": end_s,
        "char_start": char_start,
        "char_end": char_end,
        "text": text,
        "text_sha256": sha256_bytes(text.encode("utf-8")),
        "metadata_json": json_dumps(metadata or {}),
        "created_at": created_at,
    }, "span_id")
    return span_id


def record_extraction_run(
    con: Connection,
    *,
    extractor_name: str,
    source_conversation_id: str | None,
    source_turn_id: str | None,
    model: str | None,
    schema_version: str = MEMORY_SCHEMA_VERSION,
    prompt_sha256: str | None = None,
    status: str = "completed",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Record which model/schema produced a memory item."""
    run_id = stable_id("xrun", extractor_name, source_conversation_id, source_turn_id, model, schema_version)
    now = now_iso()
    upsert(con, "extraction_runs", {
        "run_id": run_id,
        "extractor_name": extractor_name,
        "extractor_version": schema_version,
        "source_conversation_id": source_conversation_id,
        "source_turn_id": source_turn_id,
        "model": model,
        "prompt_sha256": prompt_sha256,
        "schema_version": schema_version,
        "started_at": now,
        "finished_at": now,
        "status": status,
        "metadata_json": json_dumps(metadata or {}),
    }, "run_id")
    return run_id


def add_memory_evidence(
    con: Connection,
    *,
    target_table: str,
    target_id: str,
    source_span_id: str | None,
    evidence_role: str = "primary",
    evidence_text: str | None = None,
    extraction_run_id: str | None = None,
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> str:
    evidence_id = stable_id("ev", target_table, target_id, source_span_id, evidence_role, evidence_text)
    upsert(con, "memory_evidence", {
        "evidence_id": evidence_id,
        "target_table": target_table,
        "target_id": target_id,
        "source_span_id": source_span_id,
        "evidence_role": evidence_role,
        "evidence_text": evidence_text,
        "evidence_sha256": sha256_bytes((evidence_text or "").encode("utf-8")) if evidence_text else None,
        "extraction_run_id": extraction_run_id,
        "confidence": confidence,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "evidence_id")
    return evidence_id


def add_memory_facet(
    con: Connection,
    *,
    target_table: str,
    target_id: str,
    facet_type: str,
    facet_value: str,
    source: str = "llm",
    confidence: float = 0.7,
    weight: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> str:
    value_norm = normalize_text(facet_value)
    facet_id = stable_id("facet", target_table, target_id, facet_type, value_norm, source)
    upsert(con, "memory_facets", {
        "facet_id": facet_id,
        "target_table": target_table,
        "target_id": target_id,
        "facet_type": facet_type,
        "facet_value": facet_value,
        "facet_value_norm": value_norm,
        "source": source,
        "confidence": confidence,
        "weight": weight,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "facet_id")
    return facet_id


def add_memory_card(
    con: Connection,
    *,
    source_table: str,
    source_id: str,
    card_type: str,
    truth_status: str,
    title: str,
    summary: str,
    person_id: str | None = None,
    topic: str | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    confidence: float = 0.7,
    importance_score: float | None = None,
    lifecycle_status: str = "active",
    recurrence_key: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    evidence_count: int = 1,
    source_span_id: str | None = None,
    extraction_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create the canonical card a future engine should read first."""
    card_id = stable_id("card", source_table, source_id, card_type, truth_status)
    now = now_iso()
    upsert(con, "memory_cards", {
        "card_id": card_id,
        "source_table": source_table,
        "source_id": source_id,
        "card_type": card_type,
        "truth_status": truth_status,
        "title": title,
        "summary": summary,
        "person_id": person_id,
        "topic": topic,
        "time_start": time_start,
        "time_end": time_end,
        "confidence": confidence,
        "importance_score": importance_score if importance_score is not None else confidence,
        "lifecycle_status": lifecycle_status,
        "recurrence_key": recurrence_key,
        "valid_from": valid_from or time_start,
        "valid_until": valid_until,
        "evidence_count": evidence_count,
        "source_span_id": source_span_id,
        "extraction_run_id": extraction_run_id,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now,
        "updated_at": now,
    }, "card_id")
    if source_span_id or extraction_run_id:
        add_memory_evidence(
            con,
            target_table="memory_cards",
            target_id=card_id,
            source_span_id=source_span_id,
            evidence_role="card_source",
            evidence_text=summary,
            extraction_run_id=extraction_run_id,
            confidence=confidence,
        )
    # default facets: every card can be filtered by type, truth layer, person and topic
    add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="card_type", facet_value=card_type, source="system", confidence=1.0)
    add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="truth_status", facet_value=truth_status, source="system", confidence=1.0)
    add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="lifecycle_status", facet_value=lifecycle_status, source="system", confidence=1.0)
    if recurrence_key:
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="recurrence_key", facet_value=recurrence_key, source="system", confidence=1.0)
    if person_id:
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="person", facet_value=person_id, source="system", confidence=1.0)
    if topic:
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="topic", facet_value=topic, source="system", confidence=1.0)
    return card_id


def add_memory_link(
    con: Connection,
    *,
    from_table: str,
    from_id: str,
    relation_type: str,
    to_table: str,
    to_id: str,
    confidence: float = 0.7,
    evidence_text: str | None = None,
    extraction_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    link_id = stable_id("mlink", from_table, from_id, relation_type, to_table, to_id)
    upsert(con, "memory_links", {
        "link_id": link_id,
        "from_table": from_table,
        "from_id": from_id,
        "relation_type": relation_type,
        "to_table": to_table,
        "to_id": to_id,
        "confidence": confidence,
        "evidence_text": evidence_text,
        "extraction_run_id": extraction_run_id,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "link_id")
    return link_id


def add_memory_frame(
    con: Connection,
    *,
    frame_type: str,
    actor_person_id: str | None,
    topic: str | None,
    summary: str,
    source_conversation_id: str,
    source_turn_id: str,
    source_span_id: str | None,
    extraction_run_id: str | None,
    frame_time: str | None,
    confidence: float = 0.7,
    polarity: str | None = None,
    temporal_status: str | None = None,
    target: str | None = None,
    evidence_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Store an LLM-typed event/choice/action/belief/desire/constraint frame."""
    frame_id = stable_id("frame", frame_type, actor_person_id, topic, source_turn_id, summary)
    upsert(con, "memory_frames", {
        "frame_id": frame_id,
        "frame_type": frame_type,
        "actor_person_id": actor_person_id,
        "target": target,
        "topic": topic,
        "summary": summary,
        "polarity": polarity,
        "temporal_status": temporal_status,
        "source_conversation_id": source_conversation_id,
        "source_turn_id": source_turn_id,
        "source_span_id": source_span_id,
        "extraction_run_id": extraction_run_id,
        "frame_time": frame_time,
        "confidence": confidence,
        "evidence_text": evidence_text,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "frame_id")
    add_memory_evidence(
        con,
        target_table="memory_frames",
        target_id=frame_id,
        source_span_id=source_span_id,
        evidence_role="frame_source",
        evidence_text=evidence_text or summary,
        extraction_run_id=extraction_run_id,
        confidence=confidence,
    )
    add_memory_card(
        con,
        source_table="memory_frames",
        source_id=frame_id,
        card_type=f"frame:{frame_type}",
        truth_status=TRUTH_INFERRED if frame_type not in {"said", "observed_action"} else TRUTH_OBSERVED,
        title=f"{frame_type}: {summary[:90]}",
        summary=summary,
        person_id=actor_person_id,
        topic=topic,
        time_start=frame_time,
        confidence=confidence,
        source_span_id=source_span_id,
        extraction_run_id=extraction_run_id,
        metadata={"polarity": polarity, "temporal_status": temporal_status, "target": target, **(metadata or {})},
    )
    add_memory_facet(con, target_table="memory_frames", target_id=frame_id, facet_type="frame_type", facet_value=frame_type, source="llm", confidence=confidence)
    if temporal_status:
        add_memory_facet(con, target_table="memory_frames", target_id=frame_id, facet_type="temporal_status", facet_value=temporal_status, source="llm", confidence=confidence)
    if polarity:
        add_memory_facet(con, target_table="memory_frames", target_id=frame_id, facet_type="polarity", facet_value=polarity, source="llm", confidence=confidence)
    return frame_id


def add_facets_from_llm(
    con: Connection,
    *,
    target_table: str,
    target_id: str,
    facets: list[dict[str, Any]],
    extraction_run_id: str | None = None,
) -> list[str]:
    created: list[str] = []
    for f in facets:
        facet_type = str(f.get("facet_type") or f.get("type") or "").strip()
        facet_value = str(f.get("facet_value") or f.get("value") or "").strip()
        if not facet_type or not facet_value:
            continue
        created.append(add_memory_facet(
            con,
            target_table=target_table,
            target_id=target_id,
            facet_type=facet_type,
            facet_value=facet_value,
            source=str(f.get("source") or "llm"),
            confidence=float(f.get("confidence", 0.7) or 0.7),
            weight=float(f.get("weight", 1.0) or 1.0),
            metadata={"extraction_run_id": extraction_run_id, "raw": f},
        ))
    return created


def memory_overview(con: Connection) -> dict[str, Any]:
    """Small DB-native overview used by CLI/doctor."""
    def counts(sql: str) -> dict[str, int]:
        return {str(r[0]): int(r[1]) for r in con.execute(sql)}

    return {
        "cards_by_type": counts("SELECT card_type, COUNT(*) FROM memory_cards GROUP BY card_type ORDER BY COUNT(*) DESC"),
        "cards_by_truth": counts("SELECT truth_status, COUNT(*) FROM memory_cards GROUP BY truth_status ORDER BY COUNT(*) DESC"),
        "frames_by_type": counts("SELECT frame_type, COUNT(*) FROM memory_frames GROUP BY frame_type ORDER BY COUNT(*) DESC"),
        "facets_by_type": counts("SELECT facet_type, COUNT(*) FROM memory_facets GROUP BY facet_type ORDER BY COUNT(*) DESC"),
        "evidence_count": con.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0],
        "links_count": con.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0],
        "source_spans_count": con.execute("SELECT COUNT(*) FROM source_spans").fetchone()[0],
        "extraction_runs_count": con.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0],
    }
