from __future__ import annotations

"""Life-stream memory layer for MemoryLight Omega.

This module keeps the project conversation-first, but makes each conversation able

to become a structured life trace: source item -> lifestream segment -> life event
-> canonical memory card -> facets/evidence/links/timeline.

It is still not a prediction engine. It stores the substrate a later engine needs
for decisions, errors, places, money, relations, emotions, habits and future risk.
"""

import re
from sqlite3 import Connection
from typing import Any

from .db import upsert
from .memory_foundation import (
    TRUTH_INFERRED,
    TRUTH_OBSERVED,
    add_memory_card,
    add_memory_evidence,
    add_memory_facet,
    add_memory_link,
)
from .utils import json_dumps, normalize_text, now_iso, sha256_bytes, stable_id

LIFE_MEMORY_SCHEMA_VERSION = "3.3-life-stream-foundation"

EVENT_FRAME_TYPES = {
    "choice",
    "action",
    "plan",
    "belief",
    "desire",
    "fear",
    "constraint",
    "need",
    "boundary",
    "relationship_signal",
    "identity_signal",
    "contradiction_signal",
    "question",
    "request",
    "decision",
    "commitment",
    "error",
    "expense",
    "location",
    "health",
    "work",
    "social",
}

HIGH_IMPORTANCE_FRAME_TYPES = {
    "choice",
    "action",
    "plan",
    "decision",
    "commitment",
    "fear",
    "boundary",
    "contradiction_signal",
    "relationship_signal",
    "identity_signal",
}

MONEY_RE = re.compile(r"(?P<amount>\d+(?:[,.]\d{1,2})?)\s*(?P<currency>€|eur|euros?|\$|usd|dollars?|£|gbp)", re.I)


def _clamp(value: float | int | None, default: float = 0.5) -> float:
    try:
        out = float(value if value is not None else default)
    except (TypeError, ValueError):
        out = default
    return max(0.0, min(1.0, out))


def _first_facet(facets: list[dict[str, Any]], facet_type: str) -> str | None:
    best: tuple[float, str] | None = None
    for f in facets:
        if str(f.get("facet_type")) != facet_type:
            continue
        value = str(f.get("facet_value") or "").strip()
        if not value:
            continue
        score = float(f.get("weight", 1.0) or 1.0) * float(f.get("confidence", 0.7) or 0.7)
        if best is None or score > best[0]:
            best = (score, value)
    return best[1] if best else None


def _money_from_text(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    m = MONEY_RE.search(text)
    if not m:
        return None, None
    amount = float(m.group("amount").replace(",", "."))
    raw_currency = m.group("currency").lower()
    if raw_currency in {"€", "eur", "euro", "euros"}:
        return amount, "EUR"
    if raw_currency in {"$", "usd", "dollar", "dollars"}:
        return amount, "USD"
    if raw_currency in {"£", "gbp"}:
        return amount, "GBP"
    return amount, raw_currency.upper()


def record_source_item(
    con: Connection,
    *,
    source_type: str,
    external_id: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    source_asset_id: str | None = None,
    author_person_id: str | None = None,
    channel: str | None = None,
    direction: str | None = None,
    title: str | None = None,
    content_text: str | None = None,
    captured_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Register any future source unit: audio turn, chat message, email, note, etc."""
    source_item_id = stable_id("source_item", source_type, external_id, conversation_id, turn_id, content_text)
    upsert(con, "source_items", {
        "source_item_id": source_item_id,
        "source_type": source_type,
        "external_id": external_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "source_asset_id": source_asset_id,
        "author_person_id": author_person_id,
        "channel": channel,
        "direction": direction,
        "title": title,
        "content_text": content_text,
        "content_sha256": sha256_bytes((content_text or "").encode("utf-8")) if content_text else None,
        "captured_at": captured_at,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "source_item_id")
    return source_item_id


def record_lifestream_segment(
    con: Connection,
    *,
    conversation_id: str,
    turn_id: str | None,
    source_item_id: str | None,
    source_asset_id: str | None,
    segment_kind: str,
    channel: str | None,
    speaker_person_id: str | None,
    start_s: float | None = None,
    end_s: float | None = None,
    captured_start: str | None = None,
    captured_end: str | None = None,
    transcript_text: str | None = None,
    observed_summary: str | None = None,
    importance_score: float = 0.5,
    novelty_score: float = 0.5,
    density_score: float = 0.5,
    keep_level: str = "transcript",
    compression_status: str = "raw_kept",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Store a time-bounded life stream slice, ready for 24/7 compression."""
    segment_id = stable_id("lseg", conversation_id, turn_id, segment_kind, start_s, end_s, transcript_text)
    upsert(con, "lifestream_segments", {
        "segment_id": segment_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "source_item_id": source_item_id,
        "source_asset_id": source_asset_id,
        "segment_kind": segment_kind,
        "channel": channel,
        "speaker_person_id": speaker_person_id,
        "start_s": start_s,
        "end_s": end_s,
        "captured_start": captured_start,
        "captured_end": captured_end,
        "transcript_text": transcript_text,
        "observed_summary": observed_summary or transcript_text,
        "importance_score": _clamp(importance_score),
        "novelty_score": _clamp(novelty_score),
        "density_score": _clamp(density_score),
        "keep_level": keep_level,
        "compression_status": compression_status,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "segment_id")
    return segment_id


def _add_life_event_entity(
    con: Connection,
    *,
    event_id: str,
    role: str,
    entity_type: str,
    entity_value: str,
    confidence: float = 0.7,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    value = str(entity_value or "").strip()
    if not value:
        return None
    event_entity_id = stable_id("life_entity", event_id, role, entity_type, normalize_text(value))
    upsert(con, "life_event_entities", {
        "event_entity_id": event_entity_id,
        "event_id": event_id,
        "role": role,
        "entity_type": entity_type,
        "entity_value": value,
        "entity_value_norm": normalize_text(value),
        "confidence": _clamp(confidence, 0.7),
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "event_entity_id")
    return event_entity_id


def add_life_event(
    con: Connection,
    *,
    event_type: str,
    title: str,
    summary: str,
    subject_person_id: str | None,
    event_status: str = "observed_or_reported",
    truth_status: str = TRUTH_INFERRED,
    life_domain: str | None = None,
    topic: str | None = None,
    location_text: str | None = None,
    money_amount: float | None = None,
    money_currency: str | None = None,
    people: list[str] | None = None,
    objects: list[str] | None = None,
    emotional_valence: str | None = None,
    temporal_status: str | None = None,
    occurred_start: str | None = None,
    occurred_end: str | None = None,
    importance_score: float = 0.6,
    confidence: float = 0.7,
    source_conversation_id: str | None = None,
    source_turn_id: str | None = None,
    source_span_id: str | None = None,
    source_item_id: str | None = None,
    extraction_run_id: str | None = None,
    evidence_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create a normalized life event plus a canonical card and query facets."""
    now = now_iso()
    event_id = stable_id("life_event", event_type, subject_person_id, topic, occurred_start, source_turn_id, summary)
    people = list(dict.fromkeys([p for p in (people or []) if p]))
    objects = list(dict.fromkeys([o for o in (objects or []) if o]))
    upsert(con, "life_events", {
        "event_id": event_id,
        "event_type": event_type,
        "event_status": event_status,
        "subject_person_id": subject_person_id,
        "title": title,
        "summary": summary,
        "life_domain": life_domain,
        "topic": topic,
        "location_text": location_text,
        "money_amount": money_amount,
        "money_currency": money_currency,
        "people_json": json_dumps(people),
        "objects_json": json_dumps(objects),
        "emotional_valence": emotional_valence,
        "temporal_status": temporal_status,
        "occurred_start": occurred_start,
        "occurred_end": occurred_end,
        "importance_score": _clamp(importance_score, 0.6),
        "confidence": _clamp(confidence, 0.7),
        "source_conversation_id": source_conversation_id,
        "source_turn_id": source_turn_id,
        "source_span_id": source_span_id,
        "source_item_id": source_item_id,
        "extraction_run_id": extraction_run_id,
        "evidence_text": evidence_text,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now,
        "updated_at": now,
    }, "event_id")

    card_id = add_memory_card(
        con,
        source_table="life_events",
        source_id=event_id,
        card_type=f"life_event:{event_type}",
        truth_status=truth_status,
        title=title,
        summary=summary,
        person_id=subject_person_id,
        topic=topic or life_domain or event_type,
        time_start=occurred_start,
        time_end=occurred_end,
        confidence=_clamp(confidence, 0.7),
        importance_score=_clamp(importance_score, 0.6),
        lifecycle_status="active",
        recurrence_key=normalize_text(f"{subject_person_id or 'unknown'}:{event_type}:{topic or life_domain or ''}"),
        source_span_id=source_span_id,
        extraction_run_id=extraction_run_id,
        metadata={"life_event_id": event_id, **(metadata or {})},
    )
    add_memory_evidence(con, target_table="life_events", target_id=event_id, source_span_id=source_span_id, evidence_role="life_event_source", evidence_text=evidence_text or summary, extraction_run_id=extraction_run_id, confidence=_clamp(confidence, 0.7))
    add_memory_link(con, from_table="memory_cards", from_id=card_id, relation_type="represents_life_event", to_table="life_events", to_id=event_id, confidence=_clamp(confidence, 0.7), extraction_run_id=extraction_run_id)

    for facet_type, facet_value in {
        "life_event_type": event_type,
        "event_status": event_status,
        "life_domain": life_domain,
        "temporal_status": temporal_status,
        "emotional_valence": emotional_valence,
        "location": location_text,
        "currency": money_currency,
    }.items():
        if facet_value:
            add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type=facet_type, facet_value=str(facet_value), source="life_memory", confidence=_clamp(confidence, 0.7))

    if money_amount is not None:
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="money_amount", facet_value=str(money_amount), source="life_memory", confidence=_clamp(confidence, 0.7))
    for person in people:
        add_memory_facet(con, target_table="memory_cards", target_id=card_id, facet_type="involved_person", facet_value=person, source="life_memory", confidence=_clamp(confidence, 0.7))
        _add_life_event_entity(con, event_id=event_id, role="involved", entity_type="person", entity_value=person, confidence=_clamp(confidence, 0.7))
    for obj in objects:
        _add_life_event_entity(con, event_id=event_id, role="mentioned", entity_type="object", entity_value=obj, confidence=_clamp(confidence, 0.7))
    if location_text:
        _add_life_event_entity(con, event_id=event_id, role="where", entity_type="location", entity_value=location_text, confidence=_clamp(confidence, 0.7))
    if money_amount is not None:
        _add_life_event_entity(con, event_id=event_id, role="amount", entity_type="money", entity_value=f"{money_amount} {money_currency or ''}".strip(), confidence=_clamp(confidence, 0.7))
    return event_id


def add_life_event_from_frame(
    con: Connection,
    *,
    frame: dict[str, Any],
    frame_id: str,
    source_conversation_id: str,
    source_turn_id: str,
    source_span_id: str | None,
    source_item_id: str | None,
    extraction_run_id: str | None,
    occurred_start: str | None,
    occurred_end: str | None = None,
    observed_text: str = "",
    conversation_topic: str | None = None,
    memory_facets: list[dict[str, Any]] | None = None,
) -> str | None:
    """Convert a typed frame into a durable life event when it describes lived reality."""
    frame_type = str(frame.get("frame_type") or "").strip() or "life_trace"
    if frame_type not in EVENT_FRAME_TYPES:
        frame_type = "life_trace"
    summary = str(frame.get("summary") or "").strip()
    if not summary:
        return None
    facets = memory_facets or []
    confidence = _clamp(frame.get("confidence"), 0.7)
    importance = max(confidence, 0.85 if frame_type in HIGH_IMPORTANCE_FRAME_TYPES else 0.6)
    life_domain = _first_facet(facets, "life_domain") or _first_facet(facets, "decision_area") or conversation_topic
    location = _first_facet(facets, "location") or _first_facet(facets, "place")
    money_amount, money_currency = _money_from_text(" ".join([observed_text or "", summary]))
    people = []
    actor = frame.get("actor_person_id")
    target = frame.get("target")
    if actor:
        people.append(str(actor))
    if target and str(target).lower() not in {"none", "null", "unknown", "inconnu"}:
        people.append(str(target))
    event_id = add_life_event(
        con,
        event_type=frame_type,
        title=f"Life event: {frame_type}",
        summary=summary,
        subject_person_id=str(actor) if actor else None,
        event_status="reported_or_inferred_from_conversation",
        truth_status=TRUTH_INFERRED,
        life_domain=life_domain,
        topic=str(frame.get("topic") or conversation_topic or life_domain or frame_type),
        location_text=location,
        money_amount=money_amount,
        money_currency=money_currency,
        people=people,
        objects=[],
        emotional_valence=frame.get("polarity"),
        temporal_status=frame.get("temporal_status"),
        occurred_start=occurred_start,
        occurred_end=occurred_end,
        importance_score=importance,
        confidence=confidence,
        source_conversation_id=source_conversation_id,
        source_turn_id=source_turn_id,
        source_span_id=source_span_id,
        source_item_id=source_item_id,
        extraction_run_id=extraction_run_id,
        evidence_text=str(frame.get("evidence_text") or observed_text),
        metadata={"source_frame_id": frame_id, "frame": frame, "schema_version": LIFE_MEMORY_SCHEMA_VERSION},
    )
    add_memory_link(con, from_table="memory_frames", from_id=frame_id, relation_type="materializes_as_life_event", to_table="life_events", to_id=event_id, confidence=confidence, extraction_run_id=extraction_run_id)
    return event_id


def add_timeline_edge(
    con: Connection,
    *,
    from_event_id: str | None,
    to_event_id: str,
    relation_type: str = "next_observed",
    relation_order: int | None = None,
    confidence: float = 0.8,
    metadata: dict[str, Any] | None = None,
) -> str:
    edge_id = stable_id("timeline_edge", from_event_id, to_event_id, relation_type, relation_order)
    upsert(con, "memory_timeline_edges", {
        "timeline_edge_id": edge_id,
        "from_event_id": from_event_id,
        "to_event_id": to_event_id,
        "relation_type": relation_type,
        "relation_order": relation_order,
        "confidence": _clamp(confidence, 0.8),
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "timeline_edge_id")
    return edge_id


def record_memory_revision(
    con: Connection,
    *,
    target_table: str,
    target_id: str,
    revision_type: str,
    previous_status: str | None = None,
    new_status: str | None = None,
    reason: str | None = None,
    source_conversation_id: str | None = None,
    source_turn_id: str | None = None,
    source_span_id: str | None = None,
    extraction_run_id: str | None = None,
    confidence: float = 0.8,
    valid_from: str | None = None,
    valid_until: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    revision_id = stable_id("revision", target_table, target_id, revision_type, source_span_id, reason)
    upsert(con, "memory_revisions", {
        "revision_id": revision_id,
        "target_table": target_table,
        "target_id": target_id,
        "revision_type": revision_type,
        "previous_status": previous_status,
        "new_status": new_status,
        "reason": reason,
        "source_conversation_id": source_conversation_id,
        "source_turn_id": source_turn_id,
        "source_span_id": source_span_id,
        "extraction_run_id": extraction_run_id,
        "confidence": _clamp(confidence, 0.8),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": now_iso(),
    }, "revision_id")
    return revision_id
