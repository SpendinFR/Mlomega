from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .config import get_settings
from .db import connect, init_db, upsert, write_transaction
from .graph_memory import add_relation, ensure_entity
from .microscope import ConversationMicroscope
from .discourse_context import (
    DISCOURSE_SCHEMA_VERSION,
    ConversationDiscourseAnalyzer,
    discourse_prompt_hash,
    store_conversation_discourse,
)
from .segmentation import build_context_window, normalize_transcript_turns
from .memory_foundation import (
    TRUTH_CONSOLIDATED,
    TRUTH_INFERRED,
    TRUTH_OBSERVED,
    add_facets_from_llm,
    add_memory_card,
    add_memory_evidence,
    add_memory_facet,
    add_memory_frame,
    add_memory_link,
    record_extraction_run,
    record_source_span,
)
from .life_memory import (
    add_life_event_from_frame,
    add_timeline_edge,
    record_lifestream_segment,
    record_source_item,
)
from .sync_jobs import schedule_post_ingest_sync
from .governance_v18 import ScopeError, ensure_v18_schema, register_conversation_scope_in_transaction
from .utils import json_dumps, normalize_text, now_iso, sha256_bytes, sha256_file, stable_id, iso_add_seconds


def _insert_chunk_and_embedding(con, chunk_id: str, source_type: str, source_id: str, text: str, *, conversation_id=None, person_id=None, topic=None, time_start=None, time_end=None, metadata=None):
    created = now_iso()
    upsert(con, "retrieval_chunks", {
        "chunk_id": chunk_id,
        "source_type": source_type,
        "source_id": source_id,
        "conversation_id": conversation_id,
        "person_id": person_id,
        "topic": topic,
        "text": text,
        "time_start": time_start,
        "time_end": time_end,
        "metadata_json": json_dumps(metadata or {}),
        "created_at": created,
    }, "chunk_id")
    # Vectorization is performed by sync_vectors() into Qdrant/LanceDB.



def _speaker_confidence(turn: dict[str, Any], person_id: str | None, label: str | None) -> tuple[float, str]:
    """Best-effort confidence extraction for diarization/person resolution.

    WhisperX/pyannote do not always expose a single calibrated speaker confidence.
    This helper preserves any confidence found in metadata and marks unresolved
    speaker/person mapping as uncertain instead of hiding it in JSON.
    """
    meta = turn.get("metadata") if isinstance(turn.get("metadata"), dict) else {}
    candidates = [
        turn.get("speaker_confidence"),
        turn.get("person_confidence"),
        meta.get("speaker_confidence"),
        meta.get("person_confidence"),
        meta.get("confidence"),
    ]
    whisperx_seg = meta.get("whisperx_segment") if isinstance(meta.get("whisperx_segment"), dict) else {}
    candidates += [whisperx_seg.get("speaker_confidence"), whisperx_seg.get("confidence")]
    for value in candidates:
        try:
            if value is not None:
                return float(value), "reported_by_pipeline"
        except (TypeError, ValueError):
            pass
    if not person_id or str(person_id).upper().endswith("UNKNOWN") or str(label or "").upper().endswith("UNKNOWN"):
        return 0.25, "unresolved_speaker_or_person"
    if person_id == label and str(label).startswith("SPEAKER_"):
        return 0.45, "speaker_label_not_mapped_to_person"
    return 0.85, "mapped_or_provided"



def _resolve_memory_owner(metadata: dict[str, Any], participants: list[Any], speaker_map: dict[str, Any]) -> tuple[str, str]:
    """Resolve the *memory owner* once, never from the last speaker.

    V17 implicitly reused whichever ``person_id`` happened to be in scope at
    the end of ingestion. That made sync ownership depend on turn order. V18
    accepts an explicit metadata field; a narrowly-defined legacy proof is
    retained only when exactly one canonical user alias is listed.
    """
    explicit = {
        str(value).strip()
        for value in (
            metadata.get("memory_owner_id"),
            metadata.get("owner_person_id"),
            metadata.get("person_id"),
        )
        if isinstance(value, str) and value.strip()
    }
    if len(explicit) == 1:
        return next(iter(explicit)), "metadata_owner"
    if len(explicit) > 1:
        raise ScopeError(f"conflicting memory owner metadata: {sorted(explicit)!r}")

    values = [str(v).strip() for v in [*participants, *speaker_map.values()] if isinstance(v, str) and v.strip()]
    aliases = {v for v in values if v.casefold() in {"me", "moi", "user"}}
    if len(aliases) == 1:
        return next(iter(aliases)), "legacy_unique_user_alias"
    raise ScopeError(
        "ingest requires metadata.memory_owner_id (or one unambiguous canonical user alias); "
        "owner must never be inferred from turn order"
    )


def ingest_transcript_file(path: Path) -> str:
    init_db()
    data = json.loads(path.read_text(encoding="utf-8"))
    return ingest_transcript(data, source_path=path)


def ingest_transcript(data: dict[str, Any], source_path: Path | None = None) -> str:
    settings = get_settings()
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    data = normalize_transcript_turns(data)
    meta = data.get("metadata", {})
    conversation_id = meta.get("conversation_id") or stable_id("conv", meta, data.get("turns", []))
    started_at = meta.get("started_at") or now_iso()
    topic = meta.get("topic") or "conversation"
    channel = meta.get("channel", "audio/transcript")
    turns = data.get("turns", [])
    # Assign stable utterance ids before global discourse mapping so distant
    # callbacks can later point to exact DB turns.
    for idx, turn in enumerate(turns):
        label = turn.get("speaker") or turn.get("speaker_label") or "UNKNOWN"
        text = str(turn.get("text", "")).strip()
        if text and not turn.get("turn_id"):
            turn["turn_id"] = stable_id("turn", conversation_id, idx, label, text)
    participant_list = list(dict.fromkeys(list(meta.get("participants", [])) + list((meta.get("speaker_map", {}) or {}).values())))
    memory_owner_id, owner_proof = _resolve_memory_owner(meta, participant_list, meta.get("speaker_map", {}) or {})
    # V18 ownership tables must exist before we enter the long ingest writer.
    # Running a migration from inside it can create a nested SQLite writer.
    ensure_v18_schema()
    discourse = ConversationDiscourseAnalyzer().analyze(
        turns=turns,
        topic=topic,
        participants=participant_list,
        relationship_context=meta.get("relationship_context", {}),
    )
    source_asset_id = None
    source_hash = None
    if source_path:
        source_path = source_path.expanduser().resolve()
        source_hash = sha256_file(source_path)
        dest = settings.raw_dir / "transcripts" / f"{conversation_id}_{source_path.name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source_path != dest:
            shutil.copy2(source_path, dest)
        source_asset_id = stable_id("asset", "transcript", source_hash)

    microscope = ConversationMicroscope()
    with connect() as con:
        if source_asset_id:
            upsert(con, "raw_assets", {
                "asset_id": source_asset_id,
                "type": "transcript",
                "path": str(dest),
                "sha256": source_hash,
                "captured_at": started_at,
                "source": meta.get("source", "manual"),
                "metadata_json": json_dumps(meta),
                "created_at": now_iso(),
            }, "asset_id")
        upsert(con, "conversations", {
            "conversation_id": conversation_id,
            "title": meta.get("title") or topic,
            "started_at": started_at,
            "ended_at": meta.get("ended_at"),
            "topic": topic,
            "channel": channel,
            "participants_json": json_dumps(meta.get("participants", [])),
            "speaker_map_json": json_dumps(meta.get("speaker_map", {})),
            "relationship_context_json": json_dumps(meta.get("relationship_context", {})),
            "source_asset_id": source_asset_id,
            "raw_json": json_dumps(data),
            "created_at": now_iso(),
        }, "conversation_id")
        conversation_source_item_id = record_source_item(
            con,
            source_type=channel or "conversation",
            external_id=meta.get("external_id") or conversation_id,
            conversation_id=conversation_id,
            source_asset_id=source_asset_id,
            channel=channel,
            direction=meta.get("direction"),
            title=meta.get("title") or topic,
            content_text="\n".join(str(t.get("text", "")).strip() for t in turns if str(t.get("text", "")).strip()),
            captured_at=started_at,
            metadata={"level": "conversation", "topic": topic, "participants": participant_list},
        )

        # entities for conversation/person/topic
        topic_eid = ensure_entity(con, "topic", topic, aliases=[normalize_text(topic)])
        conv_eid = ensure_entity(con, "conversation", conversation_id, metadata={"topic": topic})
        add_relation(con, conv_eid, "about", topic_eid, valid_from=started_at, evidence_type="conversation", evidence_id=conversation_id)
        discourse_run_id = record_extraction_run(
            con,
            extractor_name="global_conversation_cartographer",
            source_conversation_id=conversation_id,
            source_turn_id=None,
            model=settings.ollama_model,
            schema_version=DISCOURSE_SCHEMA_VERSION,
            prompt_sha256=discourse_prompt_hash(turns=turns, topic=topic, participants=participant_list, relationship_context=meta.get("relationship_context", {})),
            metadata={"raw": discourse.raw, "conversation_summary": discourse.conversation_summary},
        )

        speaker_map = meta.get("speaker_map", {})
        participants = participant_list
        for p in participants:
            if not p:
                continue
            upsert(con, "speaker_profiles", {
                "person_id": p,
                "display_name": p,
                "is_user": 1 if p in {"me", "moi", "user"} else 0,
                "aliases_json": "[]",
                "notes": None,
                "created_at": now_iso(),
            }, "person_id")
            peid = ensure_entity(con, "person", p)
            add_relation(con, peid, "participated_in", conv_eid, valid_from=started_at, evidence_type="conversation", evidence_id=conversation_id)

        prev_turn_id = None
        prev_text = None
        last_life_event_id = None
        for idx, turn in enumerate(turns):
            label = turn.get("speaker") or turn.get("speaker_label") or "UNKNOWN"
            person_id = turn.get("person_id") or speaker_map.get(label) or label
            text = turn.get("text", "").strip()
            if not text:
                continue
            turn_id = turn.get("turn_id") or stable_id("turn", conversation_id, idx, label, text)
            turn_start_s = turn.get("start")
            turn_end_s = turn.get("end")
            turn_abs_start = iso_add_seconds(started_at, turn_start_s) or started_at
            turn_abs_end = iso_add_seconds(started_at, turn_end_s) if turn_end_s is not None else None
            turn_time_metadata = {
                "conversation_started_at": started_at,
                "absolute_start": turn_abs_start,
                "absolute_end": turn_abs_end,
                "start_s": turn_start_s,
                "end_s": turn_end_s,
            }
            upsert(con, "turns", {
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "idx": idx,
                "speaker_label": label,
                "person_id": person_id,
                "start_s": turn_start_s,
                "end_s": turn_end_s,
                "text": text,
                "previous_turn_id": prev_turn_id,
                "metadata_json": json_dumps({**(turn.get("metadata", {}) if isinstance(turn.get("metadata"), dict) else {}), **turn_time_metadata}),
            }, "turn_id")
            speaker_confidence, speaker_reason = _speaker_confidence(turn, person_id, label)
            if speaker_confidence < 0.7:
                upsert(con, "speaker_uncertainty_segments", {
                    "uncertainty_id": stable_id("speaker_uncertain", conversation_id, turn_id, label, person_id),
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "turn_idx": idx,
                    "speaker_label": label,
                    "person_id": person_id,
                    "confidence": speaker_confidence,
                    "uncertainty_reason": speaker_reason,
                    "evidence_json": json_dumps({"turn_metadata": turn.get("metadata", {}), **turn_time_metadata, "text": text[:300]}),
                    "created_at": now_iso(),
                }, "uncertainty_id")
            peid = ensure_entity(con, "person", person_id)
            add_relation(con, peid, "said", conv_eid, valid_from=turn_abs_start, evidence_type="turn", evidence_id=turn_id, context={"text": text[:280]})
            span_id = record_source_span(
                con,
                conversation_id=conversation_id,
                turn_id=turn_id,
                person_id=person_id,
                source_asset_id=source_asset_id,
                text=text,
                start_s=turn_start_s,
                end_s=turn_end_s,
                span_role="turn_text",
                metadata={"speaker_label": label, "idx": idx, "channel": channel, **turn_time_metadata},
            )
            turn_source_item_id = record_source_item(
                con,
                source_type="turn",
                external_id=turn_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                source_asset_id=source_asset_id,
                author_person_id=person_id,
                channel=channel,
                direction="spoken_or_written",
                title=f"Turn {idx} by {person_id}",
                content_text=text,
                captured_at=turn_abs_start,
                metadata={"speaker_label": label, "idx": idx, "conversation_source_item_id": conversation_source_item_id, **turn_time_metadata},
            )
            lifestream_segment_id = record_lifestream_segment(
                con,
                conversation_id=conversation_id,
                turn_id=turn_id,
                source_item_id=turn_source_item_id,
                source_asset_id=source_asset_id,
                segment_kind="conversation_turn",
                channel=channel,
                speaker_person_id=person_id,
                start_s=turn_start_s,
                end_s=turn_end_s,
                captured_start=turn_abs_start,
                captured_end=turn_abs_end or meta.get("ended_at"),
                transcript_text=text,
                observed_summary=text,
                importance_score=0.5,
                novelty_score=0.5,
                density_score=0.7 if len(text) > 140 else 0.45,
                keep_level="transcript",
                compression_status="raw_kept",
                metadata={"idx": idx, "speaker_label": label, "source": "ingest_transcript", **turn_time_metadata},
            )

            turn_card_id = add_memory_card(
                con,
                source_table="turns",
                source_id=turn_id,
                card_type="observed_turn",
                truth_status=TRUTH_OBSERVED,
                title=f"Tour observé de {person_id} sur {topic}",
                summary=text,
                person_id=person_id,
                topic=topic,
                time_start=turn_abs_start,
                time_end=turn_abs_end,
                confidence=1.0,
                source_span_id=span_id,
                metadata={"speaker_label": label, "idx": idx, "conversation_id": conversation_id},
            )
            add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="captures_lifestream_segment", to_table="lifestream_segments", to_id=lifestream_segment_id, confidence=1.0)
            add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="channel", facet_value=channel, source="system", confidence=1.0)
            add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="speaker_confidence", facet_value=f"{speaker_confidence:.2f}", source="diarization", confidence=speaker_confidence)
            if speaker_confidence < 0.7:
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="speaker_uncertain", facet_value=speaker_reason, source="diarization", confidence=1.0)
            words_for_spans = turn.get("words") if isinstance(turn.get("words"), list) else []
            for word_idx, raw_word in enumerate(words_for_spans):
                token = str(raw_word.get("word") or raw_word.get("text") or raw_word.get("token") or "").strip()
                if not token:
                    continue
                record_source_span(
                    con,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    person_id=person_id,
                    source_asset_id=source_asset_id,
                    text=token,
                    start_s=raw_word.get("start"),
                    end_s=raw_word.get("end"),
                    span_role="word",
                    metadata={
                        "word_index": word_idx,
                        "raw_word": raw_word,
                        "parent_span_id": span_id,
                        "absolute_start": iso_add_seconds(started_at, raw_word.get("start")),
                        "absolute_end": iso_add_seconds(started_at, raw_word.get("end")),
                    },
                )

            context_window = build_context_window(turns, idx, before=3, after=2)
            discourse_context = discourse.context_for_turn(idx)
            # Surface global-discourse facets directly on the observed turn card so
            # later engines can filter by long-range topic/arc without rerunning LLM.
            current_discourse = discourse_context.get("current_utterance_discourse", {})
            add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="local_subject", facet_value=str(current_discourse.get("local_subject") or topic), source="global_discourse", confidence=float(current_discourse.get("confidence", 0.8) or 0.8))
            add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="relation_to_previous", facet_value=str(current_discourse.get("relation_to_previous") or "continues"), source="global_discourse", confidence=float(current_discourse.get("confidence", 0.8) or 0.8))
            for thread in discourse_context.get("active_topic_threads", []):
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="discourse_thread", facet_value=str(thread.get("thread_key") or thread.get("label")), source="global_discourse", confidence=float(thread.get("importance", 0.8) or 0.8))
            analysis = microscope.analyze_turn(text, prev_text, topic, person_id, relationship_context=meta.get("relationship_context", {}), context_window=context_window, discourse_context=discourse_context)
            extraction_run_id = record_extraction_run(
                con,
                extractor_name="conversation_microscope",
                source_conversation_id=conversation_id,
                source_turn_id=turn_id,
                model=settings.ollama_model,
                prompt_sha256=sha256_bytes(json_dumps({"topic": topic, "speaker": person_id, "text": text, "previous": prev_text, "context_window": context_window, "discourse_context": discourse_context}).encode("utf-8")),
                metadata={"raw": analysis.llm_raw, "relationship_context": meta.get("relationship_context", {}), "context_window": context_window, "discourse_context": discourse_context, "time": turn_time_metadata},
            )
            add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="analyzed_by", to_table="extraction_runs", to_id=extraction_run_id, confidence=1.0)
            add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="memory_action", facet_value=str(analysis.memory_action.get("memory_action")), source="v15_18_router", confidence=float(analysis.memory_action.get("confidence", 0.5) or 0.5), metadata=analysis.memory_action)
            add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="signal_type", facet_value=str(analysis.memory_action.get("signal_type")), source="v15_18_router", confidence=float(analysis.memory_action.get("confidence", 0.5) or 0.5), metadata=analysis.memory_action)
            add_facets_from_llm(con, target_table="memory_cards", target_id=turn_card_id, facets=analysis.memory_facets, extraction_run_id=extraction_run_id)
            for frame in analysis.memory_frames:
                frame_id = add_memory_frame(
                    con,
                    frame_type=frame["frame_type"],
                    actor_person_id=frame["actor_person_id"],
                    target=frame.get("target"),
                    topic=frame.get("topic") or topic,
                    summary=frame["summary"],
                    polarity=frame.get("polarity"),
                    temporal_status=frame.get("temporal_status"),
                    source_conversation_id=conversation_id,
                    source_turn_id=turn_id,
                    source_span_id=span_id,
                    extraction_run_id=extraction_run_id,
                    frame_time=turn_abs_start,
                    confidence=frame["confidence"],
                    evidence_text=frame["evidence_text"],
                    metadata=frame.get("raw", frame),
                )
                add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_frame", to_table="memory_frames", to_id=frame_id, confidence=frame["confidence"], extraction_run_id=extraction_run_id)
                life_event_id = add_life_event_from_frame(
                    con,
                    frame=frame,
                    frame_id=frame_id,
                    source_conversation_id=conversation_id,
                    source_turn_id=turn_id,
                    source_span_id=span_id,
                    source_item_id=turn_source_item_id,
                    extraction_run_id=extraction_run_id,
                    occurred_start=turn_abs_start,
                    occurred_end=turn_abs_end,
                    observed_text=text,
                    conversation_topic=topic,
                    memory_facets=analysis.memory_facets,
                )
                if life_event_id:
                    add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_life_event", to_table="life_events", to_id=life_event_id, confidence=frame["confidence"], extraction_run_id=extraction_run_id)
                    add_timeline_edge(con, from_event_id=last_life_event_id, to_event_id=life_event_id, relation_order=idx, metadata={"conversation_id": conversation_id, "turn_id": turn_id})
                    last_life_event_id = life_event_id
            for w in analysis.words:
                word_id = stable_id("word", turn_id, w["position"], w["token"])
                upsert(con, "word_signals", {
                    "word_id": word_id,
                    "turn_id": turn_id,
                    "token": w["token"],
                    "normalized": normalize_text(w["token"]),
                    "position": w["position"],
                    "salience": w["salience"],
                    "role": w["role"],
                    "why_it_matters": w["why_it_matters"],
                    "created_at": now_iso(),
                }, "word_id")
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="salient_word", facet_value=w["token"], source="llm", confidence=float(w.get("salience", 0.7) or 0.7), metadata={"word_id": word_id, "role": w.get("role")})
                add_memory_evidence(con, target_table="word_signals", target_id=word_id, source_span_id=span_id, evidence_role="word_context", evidence_text=text, extraction_run_id=extraction_run_id, confidence=float(w.get("salience", 0.7) or 0.7))
            for e in analysis.expressions:
                expression_id = stable_id("expr", turn_id, e["expression"], e["category"])
                upsert(con, "expression_signals", {
                    "expression_id": expression_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "expression": e["expression"],
                    "normalized": normalize_text(e["expression"]),
                    "category": e["category"],
                    "personal_meaning": e["personal_meaning"],
                    "why_now": e["why_now"],
                    "intensity": e["intensity"],
                    "evidence_text": e["evidence_text"],
                    "created_at": now_iso(),
                }, "expression_id")
                mem_id = stable_id("mem", "expression", expression_id)
                upsert(con, "atomic_memories", {
                    "memory_id": mem_id,
                    "kind": "expression",
                    "subject_entity_id": peid,
                    "person_id": person_id,
                    "topic": topic,
                    "content": f"{person_id} utilise l'expression '{e['expression']}' : {e['personal_meaning']}",
                    "stance": e["category"],
                    "source_conversation_id": conversation_id,
                    "source_turn_id": turn_id,
                    "evidence_text": text,
                    "confidence": e["intensity"],
                    "memory_time": turn_abs_start,
                    "metadata_json": json_dumps(e),
                    "created_at": now_iso(),
                }, "memory_id")
                expr_card_id = add_memory_card(
                    con,
                    source_table="atomic_memories",
                    source_id=mem_id,
                    card_type="atomic:expression",
                    truth_status=TRUTH_INFERRED,
                    title=f"Expression personnelle: {e['expression']}",
                    summary=f"{person_id} utilise '{e['expression']}' avec le sens: {e['personal_meaning']}",
                    person_id=person_id,
                    topic=topic,
                    time_start=turn_abs_start,
                    time_end=turn_abs_end,
                    confidence=e["intensity"],
                    source_span_id=span_id,
                    extraction_run_id=extraction_run_id,
                    metadata=e,
                )
                add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_extracted_memory", to_table="memory_cards", to_id=expr_card_id, confidence=e["intensity"], extraction_run_id=extraction_run_id)
                add_memory_evidence(con, target_table="atomic_memories", target_id=mem_id, source_span_id=span_id, evidence_role="expression_evidence", evidence_text=e["evidence_text"], extraction_run_id=extraction_run_id, confidence=e["intensity"])
                add_memory_facet(con, target_table="memory_cards", target_id=expr_card_id, facet_type="expression_category", facet_value=e["category"], source="llm", confidence=e["intensity"])
                add_memory_facet(con, target_table="memory_cards", target_id=expr_card_id, facet_type="memory_use_policy", facet_value="style_context_only", source="v15_18", confidence=1.0, metadata={"do_not_overpsychologize": True})
                add_memory_facet(con, target_table="memory_cards", target_id=expr_card_id, facet_type="do_not_overpsychologize", facet_value="true", source="v15_18", confidence=1.0)
                add_relation(con, peid, "uses_expression", ensure_entity(con, "expression", e["expression"]), valid_from=turn_abs_start, evidence_type="expression", evidence_id=expression_id)

            for li in analysis.personal_language_items:
                lang_id = stable_id("plang", person_id, normalize_text(li["text"]), li.get("tone"), li.get("meaning"))
                now_seen = turn_abs_start or now_iso()
                existing = con.execute("SELECT frequency, examples_json FROM personal_language_patterns WHERE language_pattern_id=?", (lang_id,)).fetchone()
                freq = int(existing["frequency"] or 0) + 1 if existing else 1
                examples = []
                if existing:
                    try:
                        examples = json.loads(existing["examples_json"] or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        examples = []
                examples.append({"turn_id": turn_id, "text": text, "meaning": li.get("meaning"), "contexts": li.get("contexts"), "evidence": li.get("evidence_turn_ids")})
                upsert(con, "personal_language_patterns", {
                    "language_pattern_id": lang_id,
                    "person_id": person_id,
                    "expression": li["text"],
                    "normalized_expression": normalize_text(li["text"]),
                    "context_type": ",".join(li.get("contexts") or [])[:200] or li.get("tone") or "personal_language",
                    "preceding_context": prev_text,
                    "following_context": None,
                    "emotion_context": li.get("tone"),
                    "speech_act_context": analysis.memory_action.get("signal_type"),
                    "frequency": freq,
                    "last_seen": now_seen,
                    "examples_json": json_dumps(examples[-20:]),
                    "probability_boost": 0.0,
                    "confidence": float(li.get("confidence", 0.55) or 0.55),
                    "metadata_json": json_dumps({**li, "memory_use_policy": "style_context_only", "do_not_overpsychologize": True}),
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                }, "language_pattern_id")
                add_memory_facet(con, target_table="memory_cards", target_id=turn_card_id, facet_type="personal_language_item", facet_value=li["text"], source="v15_18", confidence=float(li.get("confidence", 0.55) or 0.55), metadata=li)

            ua = analysis.utterance
            analysis_id = stable_id("analysis", turn_id)
            upsert(con, "utterance_analyses", {
                "analysis_id": analysis_id,
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "surface_meaning": ua["surface_meaning"],
                "deep_intent": ua["deep_intent"],
                "emotion": ua["emotion"],
                "emotion_intensity": ua["emotion_intensity"],
                "why_now": ua["why_now"],
                "trigger_summary": ua["trigger_summary"],
                "hidden_expectation": ua["hidden_expectation"],
                "response_rule": ua["response_rule"],
                "confidence": ua["confidence"],
                "analysis_json": json_dumps(ua),
                "created_at": now_iso(),
            }, "analysis_id")
            mem_id = stable_id("mem", "intent", analysis_id)
            upsert(con, "atomic_memories", {
                "memory_id": mem_id,
                "kind": "utterance_intent",
                "subject_entity_id": peid,
                "person_id": person_id,
                "topic": topic,
                "content": f"Intention probable : {ua['deep_intent']} | Attente cachée : {ua['hidden_expectation']}",
                "stance": ua["emotion"],
                "source_conversation_id": conversation_id,
                "source_turn_id": turn_id,
                "evidence_text": text,
                "confidence": ua["confidence"],
                "memory_time": turn_abs_start,
                "metadata_json": json_dumps(ua),
                "created_at": now_iso(),
            }, "memory_id")
            intent_card_id = add_memory_card(
                con,
                source_table="atomic_memories",
                source_id=mem_id,
                card_type="atomic:utterance_intent",
                truth_status=TRUTH_INFERRED,
                title=f"Intention/attente détectée chez {person_id}",
                summary=f"Intention probable: {ua['deep_intent']} | Attente cachée: {ua['hidden_expectation']} | Émotion: {ua['emotion']}",
                person_id=person_id,
                topic=topic,
                time_start=turn_abs_start,
                time_end=turn_abs_end,
                confidence=ua["confidence"],
                source_span_id=span_id,
                extraction_run_id=extraction_run_id,
                metadata=ua,
            )
            add_memory_evidence(con, target_table="utterance_analyses", target_id=analysis_id, source_span_id=span_id, evidence_role="analysis_source", evidence_text=text, extraction_run_id=extraction_run_id, confidence=ua["confidence"])
            add_memory_evidence(con, target_table="atomic_memories", target_id=mem_id, source_span_id=span_id, evidence_role="intent_evidence", evidence_text=text, extraction_run_id=extraction_run_id, confidence=ua["confidence"])
            add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_extracted_memory", to_table="memory_cards", to_id=intent_card_id, confidence=ua["confidence"], extraction_run_id=extraction_run_id)
            add_memory_facet(con, target_table="memory_cards", target_id=intent_card_id, facet_type="emotion", facet_value=ua["emotion"], source="llm", confidence=ua["confidence"])
            add_memory_facet(con, target_table="memory_cards", target_id=intent_card_id, facet_type="hidden_expectation", facet_value=ua["hidden_expectation"], source="llm", confidence=ua["confidence"])
            add_relation(con, peid, "expresses_intent", ensure_entity(con, "intent", ua["deep_intent"][:120]), valid_from=turn_abs_start, evidence_type="analysis", evidence_id=analysis_id, context={"topic": topic})
            other_person_id = None
            if prev_turn_id:
                prev_row = con.execute("SELECT person_id FROM turns WHERE turn_id=?", (prev_turn_id,)).fetchone()
                if prev_row and prev_row["person_id"] != person_id:
                    other_person_id = prev_row["person_id"]
            activation_id = stable_id("activation", turn_id, ua["trigger_summary"], ua["emotion"])
            upsert(con, "activation_signals", {
                "activation_id": activation_id,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "person_id": person_id,
                "other_person_id": other_person_id,
                "topic": topic,
                "trigger_summary": ua["trigger_summary"],
                "emotion": ua["emotion"],
                "emotion_intensity": ua["emotion_intensity"],
                "reaction_rule": ua["response_rule"],
                "evidence_text": text,
                "confidence": ua["confidence"],
                "extraction_run_id": extraction_run_id,
                "created_at": now_iso(),
            }, "activation_id")
            activation_card_id = add_memory_card(con, source_table="activation_signals", source_id=activation_id, card_type="activation_signal", truth_status=TRUTH_INFERRED, title=f"Déclencheur/réaction: {person_id}", summary=f"Déclencheur: {ua['trigger_summary']} | Émotion: {ua['emotion']} | Réaction: {ua['response_rule']}", person_id=person_id, topic=topic, time_start=turn_abs_start, time_end=turn_abs_end, confidence=ua["confidence"], source_span_id=span_id, extraction_run_id=extraction_run_id, metadata=ua)
            add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_activation_signal", to_table="memory_cards", to_id=activation_card_id, confidence=ua["confidence"], extraction_run_id=extraction_run_id)
            add_memory_facet(con, target_table="memory_cards", target_id=activation_card_id, facet_type="activation_emotion", facet_value=ua["emotion"], source="llm", confidence=ua["confidence"])
            add_memory_facet(con, target_table="memory_cards", target_id=activation_card_id, facet_type="activation_trigger", facet_value=ua["trigger_summary"], source="llm", confidence=ua["confidence"])

            for idea in analysis.ideas:
                idea_id = stable_id("idea", turn_id, idea["idea_text"])
                upsert(con, "ideas", {
                    "idea_id": idea_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "canonical_topic": idea["canonical_topic"],
                    "idea_text": idea["idea_text"],
                    "stance": idea["stance"],
                    "novelty": idea["novelty"],
                    "importance": idea["importance"],
                    "evidence_text": idea["evidence_text"],
                    "created_at": now_iso(),
                }, "idea_id")
                idea_eid = ensure_entity(con, "idea", idea["canonical_topic"], aliases=[topic])
                add_relation(con, peid, "thinks_about", idea_eid, valid_from=turn_abs_start, evidence_type="idea", evidence_id=idea_id, context={"stance": idea["stance"], "with": meta.get("participants", [])})
                mem_id = stable_id("mem", "idea", idea_id)
                upsert(con, "atomic_memories", {
                    "memory_id": mem_id,
                    "kind": "idea",
                    "subject_entity_id": idea_eid,
                    "person_id": person_id,
                    "topic": topic,
                    "content": idea["idea_text"],
                    "stance": idea["stance"],
                    "source_conversation_id": conversation_id,
                    "source_turn_id": turn_id,
                    "evidence_text": text,
                    "confidence": idea["importance"],
                    "memory_time": turn_abs_start,
                    "metadata_json": json_dumps(idea),
                    "created_at": now_iso(),
                }, "memory_id")
                idea_card_id = add_memory_card(con, source_table="atomic_memories", source_id=mem_id, card_type="atomic:idea", truth_status=TRUTH_INFERRED, title=f"Idée: {idea['canonical_topic']}", summary=idea["idea_text"], person_id=person_id, topic=idea["canonical_topic"], time_start=turn_abs_start, time_end=turn_abs_end, confidence=idea["importance"], source_span_id=span_id, extraction_run_id=extraction_run_id, metadata=idea)
                add_memory_evidence(con, target_table="atomic_memories", target_id=mem_id, source_span_id=span_id, evidence_role="idea_evidence", evidence_text=idea["evidence_text"], extraction_run_id=extraction_run_id, confidence=idea["importance"])
                add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_extracted_memory", to_table="memory_cards", to_id=idea_card_id, confidence=idea["importance"], extraction_run_id=extraction_run_id)
                add_memory_facet(con, target_table="memory_cards", target_id=idea_card_id, facet_type="stance", facet_value=idea["stance"], source="llm", confidence=idea["importance"])

            for d in analysis.decisions:
                decision_id = stable_id("decision", turn_id, d["decision_text"])
                upsert(con, "decisions", {
                    "decision_id": decision_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "decision_text": d["decision_text"],
                    "rationale": d["rationale"],
                    "confidence": d["confidence"],
                    "created_at": now_iso(),
                }, "decision_id")
                decision_card_id = add_memory_card(con, source_table="decisions", source_id=decision_id, card_type="decision", truth_status=TRUTH_INFERRED, title="Décision détectée", summary=d["decision_text"], person_id=person_id, topic=topic, time_start=turn_abs_start, time_end=turn_abs_end, confidence=d["confidence"], source_span_id=span_id, extraction_run_id=extraction_run_id, metadata=d)
                add_memory_evidence(con, target_table="decisions", target_id=decision_id, source_span_id=span_id, evidence_role="decision_evidence", evidence_text=d["decision_text"], extraction_run_id=extraction_run_id, confidence=d["confidence"])
                add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_extracted_memory", to_table="memory_cards", to_id=decision_card_id, confidence=d["confidence"], extraction_run_id=extraction_run_id)
                add_relation(con, peid, "decided_or_required", ensure_entity(con, "decision", d["decision_text"][:120]), valid_from=turn_abs_start, evidence_type="decision", evidence_id=decision_id)
            for c in analysis.commitments:
                commitment_id = stable_id("commitment", turn_id, c["content"])
                upsert(con, "commitments", {
                    "commitment_id": commitment_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "promised_by": c["promised_by"],
                    "promised_to": c["promised_to"],
                    "content": c["content"],
                    "status": c["status"],
                    "due_at": None,
                    "evidence_text": c["evidence_text"],
                    "created_at": now_iso(),
                }, "commitment_id")
                commitment_card_id = add_memory_card(con, source_table="commitments", source_id=commitment_id, card_type="commitment", truth_status=TRUTH_INFERRED, title="Engagement détecté", summary=c["content"], person_id=c["promised_by"], topic=topic, time_start=turn_abs_start, time_end=turn_abs_end, confidence=c["confidence"], source_span_id=span_id, extraction_run_id=extraction_run_id, metadata=c)
                add_memory_evidence(con, target_table="commitments", target_id=commitment_id, source_span_id=span_id, evidence_role="commitment_evidence", evidence_text=c["evidence_text"], extraction_run_id=extraction_run_id, confidence=c["confidence"])
                add_memory_link(con, from_table="memory_cards", from_id=turn_card_id, relation_type="contains_extracted_memory", to_table="memory_cards", to_id=commitment_card_id, confidence=c["confidence"], extraction_run_id=extraction_run_id)
                add_memory_facet(con, target_table="memory_cards", target_id=commitment_card_id, facet_type="commitment_status", facet_value=c["status"], source="llm", confidence=c["confidence"])

            # retrieval chunks at turn and analysis level
            _insert_chunk_and_embedding(con, stable_id("chunk", turn_id), "turn", turn_id, text, conversation_id=conversation_id, person_id=person_id, topic=topic, time_start=turn_abs_start, time_end=turn_abs_end, metadata={"speaker": person_id, **turn_time_metadata})
            _insert_chunk_and_embedding(con, stable_id("chunk", analysis_id), "analysis", analysis_id, f"{ua['deep_intent']} {ua['emotion']} {ua['why_now']} {ua['hidden_expectation']}", conversation_id=conversation_id, person_id=person_id, topic=topic, time_start=turn_abs_start, time_end=turn_abs_end)

            prev_turn_id = turn_id
            prev_text = text
        store_conversation_discourse(
            con,
            conversation_id=conversation_id,
            discourse=discourse,
            extraction_run_id=discourse_run_id,
            turn_ids_by_idx={i: t["turn_id"] for i, t in enumerate(turns) if t.get("turn_id")},
            started_at=started_at,
            turn_times_by_idx={
                i: (iso_add_seconds(started_at, t.get("start")) or started_at, iso_add_seconds(started_at, t.get("end")) if t.get("end") is not None else None)
                for i, t in enumerate(turns) if t.get("turn_id")
            },
        )
        register_conversation_scope_in_transaction(
            con,
            conversation_id=conversation_id,
            person_id=memory_owner_id,
            evidence_kind="explicit_export" if owner_proof == "metadata_owner" else "turn_owner",
            evidence={"owner_proof": owner_proof, "source": "ingest_transcript"},
        )
        con.commit()
    from .consolidation import consolidate_all
    consolidate_all()
    from .behavior_v12 import build_v12_for_conversation
    build_v12_for_conversation(conversation_id)
    # Queue projections only after all canonical/Brain2 writers above have
    # finished. A worker must never observe a committed conversation while its
    # V12/consolidation descendants are still being constructed.
    with connect() as con, write_transaction(con):
        schedule_post_ingest_sync(con, conversation_id=conversation_id, person_id=memory_owner_id)
    # V18 never turns a successful canonical SQLite ingest into a failure merely
    # because Qdrant, Graphiti, Mem0 or a model process is unavailable. The
    # owner-scoped durable jobs were queued in the same canonical transaction
    # above; workers may execute/retry them independently after this return.
    # This prevents a late projection failure from looking like a failed ingest
    # and avoids duplicated re-ingestion on retry.
    return conversation_id


def ingest_audio(audio_path: Path, language: str = "fr", speaker_map_path: Path | None = None) -> str:
    """Elite audio entrypoint: WhisperX + pyannote on GPU, normalized into transcript schema."""
    from .audio_pipeline import transcribe_with_whisperx

    speaker_map = {}
    if speaker_map_path:
        speaker_map = json.loads(Path(speaker_map_path).read_text(encoding="utf-8"))
    data = transcribe_with_whisperx(audio_path, language=language, speaker_map=speaker_map)
    return ingest_transcript(data, source_path=audio_path)
