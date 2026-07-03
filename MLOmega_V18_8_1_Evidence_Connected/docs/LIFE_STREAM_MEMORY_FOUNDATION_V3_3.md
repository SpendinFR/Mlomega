# MemoryLight Omega V3.3 — Life-Stream Memory Foundation

This layer keeps the project **conversation-first** while making the memory base ready for 24/7 life capture and future multi-source ingestion.

## Goal

A conversation is not treated as “just dialogue”. It is treated as a life stream:

```text
source item → lifestream segment → source span → typed frame → life event → memory card → facets/evidence/links/timeline
```

The analysis engine can later reason over this substrate, but V3.3 stays focused on memory: storing exact, structured, time-aware traces of what the user says, lives, decides, avoids, regrets, spends, feels, repeats and plans.

## New canonical tables

- `source_items`: universal source units for audio turns, chat messages, emails, notes or later external imports.
- `lifestream_segments`: time-bounded 24/7 stream slices with importance, novelty, density and compression status.
- `life_events`: normalized lived/reported events extracted from conversation frames.
- `life_event_entities`: people, places, money amounts, objects and other event-linked entities.
- `memory_timeline_edges`: order and temporal relation between life events.
- `memory_revisions`: first-class correction/contradiction/replacement layer for long-term memory hygiene.

## Memory cards upgraded

`memory_cards` now carry:

- `importance_score`
- `lifecycle_status`
- `recurrence_key`
- `valid_from`
- `valid_until`

This lets a future engine distinguish a current strong memory from an old, weak, contradicted or recurring one.

## Ingestion behavior

Every transcript conversation now registers:

1. a conversation-level `source_item`,
2. one turn-level `source_item` per turn,
3. one `lifestream_segment` per turn,
4. life events generated from typed `memory_frames`,
5. timeline edges between consecutive life events,
6. canonical cards and facets for those life events.

## External memory and retrieval

V3.3 exports and indexes:

- `source_item`
- `lifestream_segment`
- `life_event`
- `memory_revision`

Graphiti receives a dedicated `v3.3_life_events` episode. Mem0 receives life events as a first-class layer.

## Design boundary

This is still **not** the prediction engine. It does not claim to predict the future by itself.

It makes the future engine possible by storing a high-resolution, provenance-preserving life substrate.


## V3.3.1 — Memory reliability hardening

V3.3.1 keeps the scope on memory only and adds three production-grade guarantees:

1. **Revision/correction is first-class.** `memory_correction.revise_memory()` records a `memory_revisions` row, updates the canonical target, updates all related `memory_cards`, replaces the lifecycle facet and queues secondary sync. It supports `correct`, `invalidate`, `delete`, `retract`, `supersede`, `archive` and `restore`.
2. **External indexes are no longer silent side effects.** `sync_jobs` tracks Qdrant/LanceDB, Graphiti and Mem0 synchronization with backend, operation, target, status, attempts, last success/error and retry support. Ingestion still fails loudly if mandatory elite sync fails, but the database now retains exactly what is pending or failed.
3. **Life-stream time is absolute.** Audio offsets remain stored, but every memory-facing timestamp is anchored to `conversation.started_at + offset`: source items, lifestream segments, observed turn cards, extracted cards, frames, life events, retrieval chunks and discourse cards.

CLI additions:

```bash
mlomega-audio memory-revise memory_cards <card_id> --type invalidate --reason "faux souvenir"
mlomega-audio memory-revise life_events <event_id> --type correct --reason "montant corrigé" --patch '{"money_amount": 12.5}'
mlomega-audio sync-jobs --status failed
mlomega-audio sync-pending
```
