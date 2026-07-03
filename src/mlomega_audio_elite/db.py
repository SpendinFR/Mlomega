from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Any

from .config import get_settings

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS raw_assets (
  asset_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  path TEXT,
  sha256 TEXT,
  captured_at TEXT,
  source TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  title TEXT,
  started_at TEXT,
  ended_at TEXT,
  topic TEXT,
  channel TEXT,
  participants_json TEXT DEFAULT '[]',
  speaker_map_json TEXT DEFAULT '{}',
  relationship_context_json TEXT DEFAULT '{}',
  source_asset_id TEXT,
  raw_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  speaker_label TEXT,
  person_id TEXT,
  start_s REAL,
  end_s REAL,
  text TEXT NOT NULL,
  previous_turn_id TEXT,
  metadata_json TEXT DEFAULT '{}',
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS speaker_profiles (
  person_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  is_user INTEGER DEFAULT 0,
  aliases_json TEXT DEFAULT '[]',
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_embeddings (
  embedding_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  source_path TEXT,
  embedding_json TEXT NOT NULL,
  model TEXT NOT NULL,
  confidence REAL DEFAULT 1.0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(person_id) REFERENCES speaker_profiles(person_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS speaker_matches (
  match_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  speaker_label TEXT NOT NULL,
  person_id TEXT,
  confidence REAL,
  method TEXT,
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_signals (
  word_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  token TEXT NOT NULL,
  normalized TEXT NOT NULL,
  position INTEGER NOT NULL,
  salience REAL NOT NULL,
  role TEXT,
  why_it_matters TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS expression_signals (
  expression_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  expression TEXT NOT NULL,
  normalized TEXT NOT NULL,
  category TEXT,
  personal_meaning TEXT,
  why_now TEXT,
  intensity REAL DEFAULT 0.5,
  evidence_text TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS utterance_analyses (
  analysis_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  surface_meaning TEXT,
  deep_intent TEXT,
  emotion TEXT,
  emotion_intensity REAL,
  why_now TEXT,
  trigger_summary TEXT,
  hidden_expectation TEXT,
  response_rule TEXT,
  confidence REAL DEFAULT 0.6,
  analysis_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ideas (
  idea_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  canonical_topic TEXT NOT NULL,
  idea_text TEXT NOT NULL,
  stance TEXT,
  novelty REAL DEFAULT 0.5,
  importance REAL DEFAULT 0.5,
  evidence_text TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  decision_text TEXT NOT NULL,
  rationale TEXT,
  confidence REAL DEFAULT 0.6,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
  commitment_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  promised_by TEXT,
  promised_to TEXT,
  content TEXT NOT NULL,
  status TEXT DEFAULT 'open',
  due_at TEXT,
  evidence_text TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  aliases_json TEXT DEFAULT '[]',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
  relation_id TEXT PRIMARY KEY,
  from_entity_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  to_entity_id TEXT NOT NULL,
  valid_from TEXT,
  valid_until TEXT,
  confidence REAL DEFAULT 0.7,
  evidence_type TEXT,
  evidence_id TEXT,
  context_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(from_entity_id) REFERENCES entities(entity_id),
  FOREIGN KEY(to_entity_id) REFERENCES entities(entity_id)
);

CREATE TABLE IF NOT EXISTS atomic_memories (
  memory_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  subject_entity_id TEXT,
  person_id TEXT,
  topic TEXT,
  content TEXT NOT NULL,
  stance TEXT,
  source_conversation_id TEXT,
  source_turn_id TEXT,
  evidence_text TEXT,
  confidence REAL DEFAULT 0.7,
  memory_time TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reflection_states (
  state_id TEXT PRIMARY KEY,
  subject_entity_id TEXT,
  person_id TEXT,
  topic TEXT NOT NULL,
  stance TEXT,
  summary TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  evidence_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.7,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reflection_edges (
  edge_id TEXT PRIMARY KEY,
  from_state_id TEXT,
  to_state_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  explanation TEXT,
  confidence REAL DEFAULT 0.7,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patterns (
  pattern_id TEXT PRIMARY KEY,
  pattern_type TEXT NOT NULL,
  scope TEXT,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.7,
  first_seen TEXT,
  last_seen TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS self_model_facts (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL,
  content TEXT NOT NULL,
  scope TEXT,
  evidence_count INTEGER DEFAULT 1,
  confidence REAL DEFAULT 0.7,
  valid_from TEXT,
  valid_until TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_chunks (
  chunk_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  conversation_id TEXT,
  person_id TEXT,
  topic TEXT,
  text TEXT NOT NULL,
  time_start TEXT,
  time_end TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_spans (
  span_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  person_id TEXT,
  source_asset_id TEXT,
  span_role TEXT NOT NULL,
  start_s REAL,
  end_s REAL,
  char_start INTEGER,
  char_end INTEGER,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS extraction_runs (
  run_id TEXT PRIMARY KEY,
  extractor_name TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  source_conversation_id TEXT,
  source_turn_id TEXT,
  model TEXT,
  prompt_sha256 TEXT,
  schema_version TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  metadata_json TEXT DEFAULT '{}',
  FOREIGN KEY(source_conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(source_turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_cards (
  card_id TEXT PRIMARY KEY,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  card_type TEXT NOT NULL,
  truth_status TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  person_id TEXT,
  topic TEXT,
  time_start TEXT,
  time_end TEXT,
  confidence REAL DEFAULT 0.7,
  importance_score REAL DEFAULT 0.5,
  lifecycle_status TEXT DEFAULT 'active',
  recurrence_key TEXT,
  valid_from TEXT,
  valid_until TEXT,
  evidence_count INTEGER DEFAULT 1,
  source_span_id TEXT,
  extraction_run_id TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_span_id) REFERENCES source_spans(span_id),
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS memory_evidence (
  evidence_id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  source_span_id TEXT,
  evidence_role TEXT NOT NULL,
  evidence_text TEXT,
  evidence_sha256 TEXT,
  extraction_run_id TEXT,
  confidence REAL DEFAULT 1.0,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_span_id) REFERENCES source_spans(span_id),
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS memory_facets (
  facet_id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  facet_type TEXT NOT NULL,
  facet_value TEXT NOT NULL,
  facet_value_norm TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL DEFAULT 0.7,
  weight REAL DEFAULT 1.0,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_links (
  link_id TEXT PRIMARY KEY,
  from_table TEXT NOT NULL,
  from_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  to_table TEXT NOT NULL,
  to_id TEXT NOT NULL,
  confidence REAL DEFAULT 0.7,
  evidence_text TEXT,
  extraction_run_id TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS memory_frames (
  frame_id TEXT PRIMARY KEY,
  frame_type TEXT NOT NULL,
  actor_person_id TEXT,
  target TEXT,
  topic TEXT,
  summary TEXT NOT NULL,
  polarity TEXT,
  temporal_status TEXT,
  source_conversation_id TEXT NOT NULL,
  source_turn_id TEXT NOT NULL,
  source_span_id TEXT,
  extraction_run_id TEXT,
  frame_time TEXT,
  confidence REAL DEFAULT 0.7,
  evidence_text TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(source_turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE,
  FOREIGN KEY(source_span_id) REFERENCES source_spans(span_id),
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);


CREATE TABLE IF NOT EXISTS source_items (
  source_item_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  external_id TEXT,
  conversation_id TEXT,
  turn_id TEXT,
  source_asset_id TEXT,
  author_person_id TEXT,
  channel TEXT,
  direction TEXT,
  title TEXT,
  content_text TEXT,
  content_sha256 TEXT,
  captured_at TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE,
  FOREIGN KEY(source_asset_id) REFERENCES raw_assets(asset_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lifestream_segments (
  segment_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  source_item_id TEXT,
  source_asset_id TEXT,
  segment_kind TEXT NOT NULL,
  channel TEXT,
  speaker_person_id TEXT,
  start_s REAL,
  end_s REAL,
  captured_start TEXT,
  captured_end TEXT,
  transcript_text TEXT,
  observed_summary TEXT,
  importance_score REAL DEFAULT 0.5,
  novelty_score REAL DEFAULT 0.5,
  density_score REAL DEFAULT 0.5,
  keep_level TEXT DEFAULT 'transcript',
  compression_status TEXT DEFAULT 'raw_kept',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE,
  FOREIGN KEY(source_item_id) REFERENCES source_items(source_item_id) ON DELETE SET NULL,
  FOREIGN KEY(source_asset_id) REFERENCES raw_assets(asset_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS life_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  event_status TEXT DEFAULT 'observed_or_reported',
  subject_person_id TEXT,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  life_domain TEXT,
  topic TEXT,
  location_text TEXT,
  money_amount REAL,
  money_currency TEXT,
  people_json TEXT DEFAULT '[]',
  objects_json TEXT DEFAULT '[]',
  emotional_valence TEXT,
  temporal_status TEXT,
  occurred_start TEXT,
  occurred_end TEXT,
  importance_score REAL DEFAULT 0.6,
  confidence REAL DEFAULT 0.7,
  source_conversation_id TEXT,
  source_turn_id TEXT,
  source_span_id TEXT,
  source_item_id TEXT,
  extraction_run_id TEXT,
  evidence_text TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(source_turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL,
  FOREIGN KEY(source_span_id) REFERENCES source_spans(span_id) ON DELETE SET NULL,
  FOREIGN KEY(source_item_id) REFERENCES source_items(source_item_id) ON DELETE SET NULL,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS life_event_entities (
  event_entity_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  role TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_value TEXT NOT NULL,
  entity_value_norm TEXT NOT NULL,
  confidence REAL DEFAULT 0.7,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(event_id) REFERENCES life_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_timeline_edges (
  timeline_edge_id TEXT PRIMARY KEY,
  from_event_id TEXT,
  to_event_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  relation_order INTEGER,
  confidence REAL DEFAULT 0.8,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(from_event_id) REFERENCES life_events(event_id) ON DELETE CASCADE,
  FOREIGN KEY(to_event_id) REFERENCES life_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_revisions (
  revision_id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  revision_type TEXT NOT NULL,
  previous_status TEXT,
  new_status TEXT,
  reason TEXT,
  source_conversation_id TEXT,
  source_turn_id TEXT,
  source_span_id TEXT,
  extraction_run_id TEXT,
  confidence REAL DEFAULT 0.8,
  valid_from TEXT,
  valid_until TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_span_id) REFERENCES source_spans(span_id) ON DELETE SET NULL,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id) ON DELETE SET NULL
);


CREATE TABLE IF NOT EXISTS conversation_discourse_maps (
  discourse_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  primary_subject TEXT,
  subject_is_stable INTEGER DEFAULT 0,
  conversation_summary TEXT NOT NULL,
  emotional_arc TEXT,
  intent_arc TEXT,
  unresolved_questions_json TEXT DEFAULT '[]',
  discourse_json TEXT DEFAULT '{}',
  extraction_run_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS conversation_topic_threads (
  thread_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  thread_key TEXT NOT NULL,
  label TEXT NOT NULL,
  summary TEXT NOT NULL,
  life_domain TEXT,
  status TEXT,
  importance REAL DEFAULT 0.7,
  start_turn_idx INTEGER,
  end_turn_idx INTEGER,
  start_s REAL,
  end_s REAL,
  participants_json TEXT DEFAULT '[]',
  metadata_json TEXT DEFAULT '{}',
  extraction_run_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS utterance_discourse_links (
  link_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  turn_idx INTEGER NOT NULL,
  thread_id TEXT,
  thread_key TEXT,
  local_subject TEXT,
  relation_to_previous TEXT,
  context_summary TEXT,
  emotional_continuity TEXT,
  unresolved_references_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.7,
  extraction_run_id TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE,
  FOREIGN KEY(thread_id) REFERENCES conversation_topic_threads(thread_id) ON DELETE CASCADE,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS conversation_callbacks (
  callback_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  from_turn_id TEXT,
  to_turn_id TEXT,
  from_turn_idx INTEGER,
  to_turn_idx INTEGER,
  thread_id TEXT,
  thread_key TEXT,
  relation_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_text TEXT,
  confidence REAL DEFAULT 0.7,
  extraction_run_id TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(from_turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL,
  FOREIGN KEY(to_turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL,
  FOREIGN KEY(thread_id) REFERENCES conversation_topic_threads(thread_id) ON DELETE SET NULL,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);



CREATE TABLE IF NOT EXISTS speaker_uncertainty_segments (
  uncertainty_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  turn_idx INTEGER,
  speaker_label TEXT,
  person_id TEXT,
  confidence REAL DEFAULT 0.0,
  uncertainty_reason TEXT,
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_turning_points (
  turning_point_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  turn_idx INTEGER,
  turning_point_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  before_state TEXT,
  after_state TEXT,
  evidence_text TEXT,
  confidence REAL DEFAULT 0.7,
  extraction_run_id TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS activation_signals (
  activation_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  person_id TEXT,
  other_person_id TEXT,
  topic TEXT,
  trigger_summary TEXT,
  emotion TEXT,
  emotion_intensity REAL,
  reaction_rule TEXT,
  evidence_text TEXT,
  confidence REAL DEFAULT 0.7,
  extraction_run_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE,
  FOREIGN KEY(extraction_run_id) REFERENCES extraction_runs(run_id)
);

CREATE TABLE IF NOT EXISTS person_reaction_patterns (
  pattern_id TEXT PRIMARY KEY,
  person_id TEXT,
  other_person_id TEXT,
  topic TEXT,
  trigger_norm TEXT NOT NULL,
  emotion TEXT,
  typical_reaction TEXT,
  evidence_count INTEGER DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  confidence REAL DEFAULT 0.7,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_jobs (
  job_id TEXT PRIMARY KEY,
  backend TEXT NOT NULL,
  operation TEXT NOT NULL,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  conversation_id TEXT,
  priority INTEGER DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER DEFAULT 0,
  max_attempts INTEGER DEFAULT 5,
  next_attempt_at TEXT,
  locked_at TEXT,
  lock_token TEXT,
  last_attempt_at TEXT,
  last_success_at TEXT,
  external_ref_json TEXT DEFAULT '{}',
  payload_json TEXT DEFAULT '{}',
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS consolidation_runs (
  run_id TEXT PRIMARY KEY,
  run_type TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  summary TEXT,
  metadata_json TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status, priority, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_backend_target ON sync_jobs(backend, target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_conversation ON sync_jobs(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_source_items_conversation ON source_items(conversation_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_source_items_type_time ON source_items(source_type, captured_at);
CREATE INDEX IF NOT EXISTS idx_lifestream_segments_conv_time ON lifestream_segments(conversation_id, captured_start, start_s);
CREATE INDEX IF NOT EXISTS idx_lifestream_segments_keep ON lifestream_segments(keep_level, importance_score);
CREATE INDEX IF NOT EXISTS idx_life_events_person_time ON life_events(subject_person_id, occurred_start);
CREATE INDEX IF NOT EXISTS idx_life_events_type_domain ON life_events(event_type, life_domain);
CREATE INDEX IF NOT EXISTS idx_life_events_topic_importance ON life_events(topic, importance_score);
CREATE INDEX IF NOT EXISTS idx_life_event_entities_lookup ON life_event_entities(entity_type, entity_value_norm);
CREATE INDEX IF NOT EXISTS idx_timeline_edges_to ON memory_timeline_edges(to_event_id, relation_order);
CREATE INDEX IF NOT EXISTS idx_memory_revisions_target ON memory_revisions(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_discourse_maps_conversation ON conversation_discourse_maps(conversation_id);
CREATE INDEX IF NOT EXISTS idx_topic_threads_conversation_key ON conversation_topic_threads(conversation_id, thread_key);
CREATE INDEX IF NOT EXISTS idx_topic_threads_label ON conversation_topic_threads(label);
CREATE INDEX IF NOT EXISTS idx_utterance_discourse_turn ON utterance_discourse_links(turn_id);
CREATE INDEX IF NOT EXISTS idx_utterance_discourse_thread ON utterance_discourse_links(thread_id);
CREATE INDEX IF NOT EXISTS idx_callbacks_conversation ON conversation_callbacks(conversation_id, from_turn_idx, to_turn_idx);
CREATE INDEX IF NOT EXISTS idx_callbacks_thread ON conversation_callbacks(thread_id);
CREATE INDEX IF NOT EXISTS idx_source_spans_turn ON source_spans(turn_id);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_turn ON extraction_runs(source_turn_id);
CREATE INDEX IF NOT EXISTS idx_memory_cards_source ON memory_cards(source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_memory_cards_person_topic_time ON memory_cards(person_id, topic, time_start);
CREATE INDEX IF NOT EXISTS idx_memory_cards_truth ON memory_cards(truth_status, card_type);
CREATE INDEX IF NOT EXISTS idx_memory_evidence_target ON memory_evidence(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_memory_facets_lookup ON memory_facets(facet_type, facet_value_norm);
CREATE INDEX IF NOT EXISTS idx_memory_facets_target ON memory_facets(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_memory_links_from ON memory_links(from_table, from_id);
CREATE INDEX IF NOT EXISTS idx_memory_links_to ON memory_links(to_table, to_id);
CREATE INDEX IF NOT EXISTS idx_memory_frames_actor_topic_time ON memory_frames(actor_person_id, topic, frame_time);
CREATE INDEX IF NOT EXISTS idx_memory_frames_type ON memory_frames(frame_type, temporal_status);
CREATE INDEX IF NOT EXISTS idx_speaker_uncertainty_conv ON speaker_uncertainty_segments(conversation_id, turn_idx);
CREATE INDEX IF NOT EXISTS idx_turning_points_conv ON conversation_turning_points(conversation_id, turn_idx);
CREATE INDEX IF NOT EXISTS idx_activation_person_topic ON activation_signals(person_id, topic, emotion);
CREATE INDEX IF NOT EXISTS idx_reaction_patterns_person ON person_reaction_patterns(person_id, other_person_id, topic);


-- V12 Brain 2.0 foundation: canonical analysis/prediction memory.
CREATE TABLE IF NOT EXISTS v12_schema_migrations (
  migration_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS v12_canonical_facets (
  facet_key TEXT PRIMARY KEY,
  facet_type TEXT NOT NULL,
  canonical_value TEXT NOT NULL,
  aliases_json TEXT DEFAULT '[]',
  description TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v12_quality_findings (
  finding_id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  finding_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT,
  status TEXT DEFAULT 'open',
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v12_quarantine (
  quarantine_id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'pending_review',
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY,
  episode_type TEXT NOT NULL,
  source_conversation_id TEXT,
  start_turn_id TEXT,
  end_turn_id TEXT,
  start_time TEXT,
  end_time TEXT,
  participants_json TEXT DEFAULT '[]',
  location_text TEXT,
  channel TEXT,
  topic TEXT,
  situation_summary TEXT NOT NULL,
  trigger_summary TEXT,
  user_state_before_json TEXT DEFAULT '{}',
  speech_or_action_summary TEXT,
  target_person_id TEXT,
  target_reaction_summary TEXT,
  user_state_after_json TEXT DEFAULT '{}',
  outcome_summary TEXT,
  unresolved_tension TEXT,
  truth_status TEXT DEFAULT 'observed',
  confidence REAL DEFAULT 0.65,
  importance_score REAL DEFAULT 0.5,
  lifecycle_status TEXT DEFAULT 'active',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(start_turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL,
  FOREIGN KEY(end_turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS episode_evidence (
  episode_evidence_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  source_span_id TEXT,
  turn_id TEXT,
  evidence_role TEXT NOT NULL,
  evidence_text TEXT,
  confidence REAL DEFAULT 1.0,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE,
  FOREIGN KEY(source_span_id) REFERENCES source_spans(span_id) ON DELETE SET NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS episode_links (
  episode_link_id TEXT PRIMARY KEY,
  from_episode_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  to_episode_id TEXT NOT NULL,
  confidence REAL DEFAULT 0.65,
  evidence_text TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(from_episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE,
  FOREIGN KEY(to_episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS situation_episodes (
  situation_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  situation_type TEXT NOT NULL,
  life_domain TEXT,
  participants_json TEXT DEFAULT '[]',
  main_person_id TEXT,
  secondary_people_json TEXT DEFAULT '[]',
  place_explicit TEXT,
  place_inferred TEXT,
  channel TEXT,
  social_context TEXT,
  power_balance TEXT,
  stakes TEXT,
  constraints_json TEXT DEFAULT '[]',
  trigger_event_id TEXT,
  related_project TEXT,
  related_relationship_id TEXT,
  confidence REAL DEFAULT 0.6,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS interaction_episodes (
  interaction_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  user_person_id TEXT,
  other_person_id TEXT,
  relationship_type TEXT,
  trust_level REAL,
  tension_level REAL,
  dependency_level REAL,
  message_direction TEXT,
  user_speech_act TEXT,
  other_reaction TEXT,
  user_followup TEXT,
  communication_result TEXT,
  confidence REAL DEFAULT 0.6,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS speech_acts (
  speech_act_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  episode_id TEXT,
  speaker_person_id TEXT,
  target_person_id TEXT,
  act_type TEXT NOT NULL,
  directness REAL DEFAULT 0.5,
  politeness REAL DEFAULT 0.5,
  pressure_level REAL DEFAULT 0.5,
  certainty_level REAL DEFAULT 0.5,
  emotional_charge REAL DEFAULT 0.5,
  implicit_request TEXT,
  evidence_text TEXT,
  confidence REAL DEFAULT 0.65,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS internal_state_snapshots (
  state_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  turn_id TEXT,
  time_start TEXT,
  time_end TEXT,
  energy REAL,
  stress REAL,
  motivation REAL,
  confidence_state REAL,
  clarity REAL,
  frustration REAL,
  curiosity REAL,
  urgency REAL,
  sense_of_control REAL,
  feeling_understood REAL,
  social_safety REAL,
  emotional_valence TEXT,
  dominant_emotion TEXT,
  secondary_emotions_json TEXT DEFAULT '[]',
  evidence_text TEXT,
  source_type TEXT DEFAULT 'text_context',
  truth_status TEXT DEFAULT 'inferred',
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS thought_hypotheses (
  thought_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  turn_id TEXT,
  thought_type TEXT NOT NULL,
  content TEXT NOT NULL,
  consciousness_level TEXT DEFAULT 'inferred',
  evidence_text TEXT,
  trigger_summary TEXT,
  related_need TEXT,
  related_fear TEXT,
  related_goal TEXT,
  truth_status TEXT DEFAULT 'inferred',
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS state_transitions (
  transition_id TEXT PRIMARY KEY,
  person_id TEXT,
  from_state_id TEXT,
  to_state_id TEXT NOT NULL,
  transition_type TEXT,
  change_summary TEXT,
  trigger_summary TEXT,
  confidence REAL DEFAULT 0.6,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(from_state_id) REFERENCES internal_state_snapshots(state_id) ON DELETE SET NULL,
  FOREIGN KEY(to_state_id) REFERENCES internal_state_snapshots(state_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS action_intentions (
  intention_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  turn_id TEXT,
  intention_text TEXT NOT NULL,
  action_type TEXT,
  target TEXT,
  deadline TEXT,
  strength REAL DEFAULT 0.5,
  explicitness TEXT DEFAULT 'inferred',
  obstacles_json TEXT DEFAULT '[]',
  required_conditions_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'open',
  evidence_text TEXT,
  confidence REAL DEFAULT 0.65,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS action_outcomes (
  outcome_id TEXT PRIMARY KEY,
  intention_id TEXT,
  episode_id TEXT,
  person_id TEXT,
  action_taken TEXT,
  result TEXT,
  success_level REAL,
  delay_text TEXT,
  obstacle_encountered TEXT,
  emotion_after TEXT,
  lesson TEXT,
  evidence_text TEXT,
  truth_status TEXT DEFAULT 'observed_or_inferred',
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(intention_id) REFERENCES action_intentions(intention_id) ON DELETE SET NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS choice_episodes (
  choice_id TEXT PRIMARY KEY,
  episode_id TEXT,
  person_id TEXT,
  turn_id TEXT,
  choice_context TEXT NOT NULL,
  options_json TEXT DEFAULT '[]',
  criteria_json TEXT DEFAULT '[]',
  preferred_option_before TEXT,
  chosen_option TEXT,
  rejected_options_json TEXT DEFAULT '[]',
  decision_time TEXT,
  confidence_before REAL,
  confidence_after REAL,
  reason_given TEXT,
  real_reason_hypothesis TEXT,
  outcome_id TEXT,
  satisfaction_after REAL,
  regret_after REAL,
  evidence_text TEXT,
  truth_status TEXT DEFAULT 'observed_or_inferred',
  confidence REAL DEFAULT 0.6,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL,
  FOREIGN KEY(outcome_id) REFERENCES action_outcomes(outcome_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS causal_edges (
  causal_edge_id TEXT PRIMARY KEY,
  from_table TEXT NOT NULL,
  from_id TEXT NOT NULL,
  to_table TEXT NOT NULL,
  to_id TEXT NOT NULL,
  causal_type TEXT NOT NULL,
  strength REAL DEFAULT 0.5,
  lag_time_text TEXT,
  evidence_text TEXT,
  counter_evidence_text TEXT,
  truth_status TEXT DEFAULT 'hypothesis',
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contradiction_events (
  contradiction_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  declared_table TEXT,
  declared_id TEXT,
  observed_table TEXT,
  observed_id TEXT,
  contradiction_type TEXT NOT NULL,
  severity REAL DEFAULT 0.5,
  possible_explanation TEXT,
  resolved INTEGER DEFAULT 0,
  evidence_for TEXT,
  evidence_against TEXT,
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS relationship_models (
  relationship_id TEXT PRIMARY KEY,
  person_a TEXT NOT NULL,
  person_b TEXT NOT NULL,
  relationship_type TEXT,
  trust_level REAL DEFAULT 0.5,
  tension_level REAL DEFAULT 0.5,
  attachment_level REAL DEFAULT 0.5,
  dependency_level REAL DEFAULT 0.5,
  power_balance TEXT,
  conflict_frequency REAL DEFAULT 0.0,
  repair_frequency REAL DEFAULT 0.0,
  communication_style TEXT,
  common_triggers_json TEXT DEFAULT '[]',
  common_loops_json TEXT DEFAULT '[]',
  current_status TEXT DEFAULT 'active',
  evidence_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.5,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS self_model_dimensions (
  dimension_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  dimension_key TEXT NOT NULL,
  score REAL NOT NULL,
  confidence REAL DEFAULT 0.5,
  evidence_count INTEGER DEFAULT 0,
  active_contexts_json TEXT DEFAULT '[]',
  counterexamples_json TEXT DEFAULT '[]',
  validity_status TEXT DEFAULT 'candidate',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(person_id, dimension_key)
);

CREATE TABLE IF NOT EXISTS behavior_signals (
  signal_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  turn_id TEXT,
  signal_type TEXT NOT NULL,
  signal_value TEXT NOT NULL,
  strength REAL DEFAULT 0.5,
  evidence_text TEXT,
  status TEXT DEFAULT 'isolated_signal',
  confidence REAL DEFAULT 0.6,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_patterns (
  candidate_pattern_id TEXT PRIMARY KEY,
  person_id TEXT,
  pattern_type TEXT NOT NULL,
  pattern_key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence_count INTEGER DEFAULT 1,
  first_seen TEXT,
  last_seen TEXT,
  activation_contexts_json TEXT DEFAULT '[]',
  counterexamples_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'candidate',
  confidence REAL DEFAULT 0.45,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmed_patterns (
  confirmed_pattern_id TEXT PRIMARY KEY,
  candidate_pattern_id TEXT,
  person_id TEXT,
  pattern_type TEXT NOT NULL,
  pattern_key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence_count INTEGER DEFAULT 0,
  counterexample_count INTEGER DEFAULT 0,
  activation_conditions_json TEXT DEFAULT '[]',
  escape_conditions_json TEXT DEFAULT '[]',
  usual_outcome TEXT,
  confidence REAL DEFAULT 0.7,
  validity_status TEXT DEFAULT 'active',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(candidate_pattern_id) REFERENCES candidate_patterns(candidate_pattern_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS loop_patterns (
  loop_id TEXT PRIMARY KEY,
  person_id TEXT,
  loop_type TEXT NOT NULL,
  trigger_summary TEXT,
  phase_1 TEXT,
  phase_2 TEXT,
  phase_3 TEXT,
  phase_4 TEXT,
  usual_outcome TEXT,
  escape_conditions_json TEXT DEFAULT '[]',
  evidence_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personal_language_patterns (
  language_pattern_id TEXT PRIMARY KEY,
  person_id TEXT,
  expression TEXT NOT NULL,
  normalized_expression TEXT NOT NULL,
  context_type TEXT,
  preceding_context TEXT,
  following_context TEXT,
  emotion_context TEXT,
  speech_act_context TEXT,
  frequency INTEGER DEFAULT 1,
  last_seen TEXT,
  examples_json TEXT DEFAULT '[]',
  probability_boost REAL DEFAULT 0.0,
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(person_id, normalized_expression, context_type)
);

CREATE TABLE IF NOT EXISTS phrase_templates (
  template_id TEXT PRIMARY KEY,
  person_id TEXT,
  template_text TEXT NOT NULL,
  template_type TEXT,
  context_type TEXT,
  frequency INTEGER DEFAULT 1,
  confidence REAL DEFAULT 0.5,
  examples_json TEXT DEFAULT '[]',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_cases (
  case_id TEXT PRIMARY KEY,
  case_type TEXT NOT NULL,
  episode_id TEXT,
  person_id TEXT,
  context_summary TEXT NOT NULL,
  situation_vector_json TEXT DEFAULT '{}',
  state_vector_json TEXT DEFAULT '{}',
  action_taken TEXT,
  speech_next TEXT,
  emotion_next TEXT,
  thought_next_hypothesis TEXT,
  outcome TEXT,
  usable_for_prediction INTEGER DEFAULT 1,
  quality_score REAL DEFAULT 0.6,
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS predictions (
  prediction_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  person_id TEXT,
  prediction_target TEXT NOT NULL,
  horizon TEXT NOT NULL,
  current_context TEXT NOT NULL,
  predicted_value TEXT NOT NULL,
  probability REAL DEFAULT 0.5,
  confidence REAL DEFAULT 0.5,
  alternatives_json TEXT DEFAULT '[]',
  evidence_cases_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  assumptions_json TEXT DEFAULT '[]',
  intervention_options_json TEXT DEFAULT '[]',
  verification_due_at TEXT,
  status TEXT DEFAULT 'open',
  metadata_json TEXT DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_results (
  result_id TEXT PRIMARY KEY,
  prediction_id TEXT NOT NULL,
  observed_value TEXT,
  match_score REAL,
  was_correct INTEGER,
  why_correct TEXT,
  why_wrong TEXT,
  model_update TEXT,
  verified_at TEXT NOT NULL,
  metadata_json TEXT DEFAULT '{}',
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS simulation_branches (
  branch_id TEXT PRIMARY KEY,
  prediction_id TEXT,
  branch_name TEXT NOT NULL,
  if_condition TEXT NOT NULL,
  probability REAL DEFAULT 0.5,
  expected_path TEXT NOT NULL,
  risk_level REAL DEFAULT 0.5,
  opportunity_level REAL DEFAULT 0.5,
  recommended_intervention TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS calibration_scores (
  calibration_id TEXT PRIMARY KEY,
  person_id TEXT,
  prediction_target TEXT NOT NULL,
  sample_size INTEGER DEFAULT 0,
  accuracy REAL,
  mean_confidence REAL,
  calibration_gap REAL,
  notes TEXT,
  calculated_at TEXT NOT NULL,
  metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS recommended_actions (
  recommendation_id TEXT PRIMARY KEY,
  person_id TEXT,
  prediction_id TEXT,
  episode_id TEXT,
  recommendation_type TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL,
  expected_effect TEXT,
  confidence REAL DEFAULT 0.55,
  status TEXT DEFAULT 'open',
  evidence_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);



CREATE TABLE IF NOT EXISTS emotion_evidence (
  emotion_evidence_id TEXT PRIMARY KEY,
  state_id TEXT,
  person_id TEXT,
  episode_id TEXT,
  turn_id TEXT,
  source_type TEXT NOT NULL,
  emotion_label TEXT,
  signal_text TEXT,
  signal_strength REAL DEFAULT 0.5,
  missing_evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.55,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(state_id) REFERENCES internal_state_snapshots(state_id) ON DELETE CASCADE,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS similar_case_scores (
  similar_case_id TEXT PRIMARY KEY,
  prediction_id TEXT,
  case_id TEXT NOT NULL,
  person_id TEXT,
  prediction_target TEXT NOT NULL,
  semantic_similarity REAL DEFAULT 0.0,
  situation_similarity REAL DEFAULT 0.0,
  state_similarity REAL DEFAULT 0.0,
  relationship_similarity REAL DEFAULT 0.0,
  outcome_similarity REAL DEFAULT 0.0,
  language_similarity REAL DEFAULT 0.0,
  final_score REAL DEFAULT 0.0,
  explanation TEXT,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE CASCADE,
  FOREIGN KEY(case_id) REFERENCES prediction_cases(case_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS future_scenarios (
  scenario_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  prediction_id TEXT,
  scenario_type TEXT NOT NULL,
  horizon TEXT,
  if_condition TEXT NOT NULL,
  expected_future TEXT NOT NULL,
  probability REAL DEFAULT 0.5,
  risk_level REAL DEFAULT 0.5,
  opportunity_level REAL DEFAULT 0.5,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS trajectory_warnings (
  warning_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  prediction_id TEXT,
  warning_type TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL,
  severity REAL DEFAULT 0.5,
  probability REAL DEFAULT 0.5,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS escape_conditions (
  escape_id TEXT PRIMARY KEY,
  person_id TEXT,
  loop_id TEXT,
  prediction_id TEXT,
  condition_text TEXT NOT NULL,
  expected_effect TEXT,
  confidence REAL DEFAULT 0.55,
  evidence_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(loop_id) REFERENCES loop_patterns(loop_id) ON DELETE SET NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS v12_engine_runs (
  run_id TEXT PRIMARY KEY,
  engine_name TEXT NOT NULL,
  target_id TEXT,
  target_table TEXT,
  status TEXT NOT NULL,
  counts_json TEXT DEFAULT '{}',
  warnings_json TEXT DEFAULT '[]',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  metadata_json TEXT DEFAULT '{}'
);



-- =========================
-- V13 Brain 2.0 real cognitive cycle layer
-- =========================

CREATE TABLE IF NOT EXISTS v13_cognitive_cycles (
  cycle_id TEXT PRIMARY KEY,
  cycle_type TEXT NOT NULL,
  conversation_id TEXT,
  episode_id TEXT,
  person_id TEXT,
  input_context TEXT,
  status TEXT NOT NULL,
  stage TEXT,
  require_llm INTEGER DEFAULT 1,
  llm_model TEXT,
  counts_json TEXT DEFAULT '{}',
  warnings_json TEXT DEFAULT '[]',
  missing_json TEXT DEFAULT '[]',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  metadata_json TEXT DEFAULT '{}',
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE SET NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS v13_llm_extractions (
  extraction_id TEXT PRIMARY KEY,
  cycle_id TEXT,
  role_name TEXT NOT NULL,
  target_table TEXT,
  target_id TEXT,
  schema_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  input_summary TEXT,
  output_json TEXT NOT NULL,
  validation_status TEXT NOT NULL,
  confidence REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  error_text TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(cycle_id) REFERENCES v13_cognitive_cycles(cycle_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v13_dynamic_models (
  dynamic_model_id TEXT PRIMARY KEY,
  cycle_id TEXT,
  episode_id TEXT,
  person_id TEXT,
  situation_model_json TEXT DEFAULT '{}',
  internal_state_model_json TEXT DEFAULT '{}',
  thought_model_json TEXT DEFAULT '{}',
  speech_action_model_json TEXT DEFAULT '{}',
  causal_model_json TEXT DEFAULT '{}',
  contradiction_model_json TEXT DEFAULT '{}',
  outcome_model_json TEXT DEFAULT '{}',
  prediction_readiness REAL DEFAULT 0.0,
  confidence REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  truth_status TEXT DEFAULT 'inferred',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(cycle_id) REFERENCES v13_cognitive_cycles(cycle_id) ON DELETE CASCADE,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v13_user_model_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  person_id TEXT,
  scope TEXT NOT NULL,
  source_conversation_id TEXT,
  source_episode_id TEXT,
  dimensions_json TEXT DEFAULT '{}',
  relationship_models_json TEXT DEFAULT '[]',
  loop_patterns_json TEXT DEFAULT '[]',
  language_signature_json TEXT DEFAULT '{}',
  calibration_json TEXT DEFAULT '{}',
  open_loops_json TEXT DEFAULT '[]',
  model_strength REAL DEFAULT 0.0,
  missing_data_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v13_case_clusters (
  cluster_id TEXT PRIMARY KEY,
  person_id TEXT,
  cluster_type TEXT NOT NULL,
  cluster_key TEXT NOT NULL,
  case_ids_json TEXT DEFAULT '[]',
  episode_ids_json TEXT DEFAULT '[]',
  centroid_summary TEXT,
  typical_trigger TEXT,
  typical_state TEXT,
  typical_action TEXT,
  typical_outcome TEXT,
  evidence_count INTEGER DEFAULT 0,
  counterexample_count INTEGER DEFAULT 0,
  strength REAL DEFAULT 0.0,
  confidence REAL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v13_prediction_explanations (
  explanation_id TEXT PRIMARY KEY,
  prediction_id TEXT NOT NULL,
  explanation_json TEXT NOT NULL,
  why_json TEXT DEFAULT '[]',
  similar_cases_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  assumptions_json TEXT DEFAULT '[]',
  intervention_json TEXT DEFAULT '[]',
  uncertainty_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v13_memory_contract_checks (
  check_id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  requirement_key TEXT NOT NULL,
  expected_object TEXT NOT NULL,
  actual_object TEXT,
  status TEXT NOT NULL,
  severity TEXT DEFAULT 'medium',
  detail TEXT,
  evidence_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v13_replay_events (
  replay_id TEXT PRIMARY KEY,
  person_id TEXT,
  prediction_id TEXT,
  source_case_id TEXT,
  episode_id TEXT,
  predicted_target TEXT NOT NULL,
  predicted_value TEXT,
  observed_value TEXT,
  match_score REAL,
  verdict TEXT,
  lesson_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS v13_intervention_plans (
  intervention_plan_id TEXT PRIMARY KEY,
  prediction_id TEXT,
  person_id TEXT,
  episode_id TEXT,
  goal TEXT NOT NULL,
  current_trajectory TEXT,
  desired_trajectory TEXT,
  actions_json TEXT DEFAULT '[]',
  expected_effects_json TEXT DEFAULT '[]',
  risks_json TEXT DEFAULT '[]',
  verification_plan_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.0,
  status TEXT DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS v13_plan_audit_rows (
  audit_id TEXT PRIMARY KEY,
  plan_section TEXT NOT NULL,
  plan_item TEXT NOT NULL,
  v12_status TEXT,
  v13_status TEXT NOT NULL,
  object_name TEXT,
  object_type TEXT,
  gap TEXT,
  action_taken TEXT,
  created_at TEXT NOT NULL
);


-- V13 FINAL Brain 2.0 complete plan coverage: no-magic cognitive operating layer.
-- These tables are deliberately explicit instead of burying critical objects in JSON blobs.
CREATE TABLE IF NOT EXISTS v13_plan_requirements (
  requirement_id TEXT PRIMARY KEY,
  section TEXT NOT NULL,
  item_key TEXT NOT NULL,
  item_type TEXT NOT NULL,
  required_tables_json TEXT DEFAULT '[]',
  required_engines_json TEXT DEFAULT '[]',
  rationale TEXT,
  status TEXT DEFAULT 'declared',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v13_component_coverage (
  coverage_id TEXT PRIMARY KEY,
  requirement_id TEXT,
  component_name TEXT NOT NULL,
  component_type TEXT NOT NULL,
  coverage_status TEXT NOT NULL,
  evidence_json TEXT DEFAULT '[]',
  missing_json TEXT DEFAULT '[]',
  severity TEXT DEFAULT 'info',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(requirement_id) REFERENCES v13_plan_requirements(requirement_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS v13_engine_runs (
  engine_run_id TEXT PRIMARY KEY,
  engine_name TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  cycle_id TEXT,
  conversation_id TEXT,
  episode_id TEXT,
  person_id TEXT,
  input_hash TEXT,
  require_llm INTEGER DEFAULT 1,
  llm_model TEXT,
  status TEXT NOT NULL,
  stage TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  counts_json TEXT DEFAULT '{}',
  warnings_json TEXT DEFAULT '[]',
  missing_json TEXT DEFAULT '[]',
  error_text TEXT,
  metadata_json TEXT DEFAULT '{}',
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS v13_engine_outputs (
  output_id TEXT PRIMARY KEY,
  engine_run_id TEXT NOT NULL,
  engine_name TEXT NOT NULL,
  target_table TEXT,
  target_id TEXT,
  output_type TEXT NOT NULL,
  output_json TEXT DEFAULT '{}',
  confidence REAL DEFAULT 0.0,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  validation_status TEXT DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  FOREIGN KEY(engine_run_id) REFERENCES v13_engine_runs(engine_run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audio_prosody_events (
  prosody_event_id TEXT PRIMARY KEY,
  conversation_id TEXT,
  turn_id TEXT,
  source_asset_id TEXT,
  person_id TEXT,
  start_s REAL,
  end_s REAL,
  event_type TEXT NOT NULL,
  feature_json TEXT DEFAULT '{}',
  interpretation TEXT,
  confidence REAL DEFAULT 0.0,
  source_method TEXT DEFAULT 'audio_model_required',
  evidence_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS episode_boundaries (
  boundary_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  episode_id TEXT,
  boundary_type TEXT NOT NULL,
  turn_id TEXT,
  idx INTEGER,
  reason TEXT,
  confidence REAL DEFAULT 0.5,
  evidence_text TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS choice_options (
  option_id TEXT PRIMARY KEY,
  choice_id TEXT NOT NULL,
  option_text TEXT NOT NULL,
  option_status TEXT DEFAULT 'available',
  evidence_text TEXT,
  confidence REAL DEFAULT 0.5,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(choice_id) REFERENCES choice_episodes(choice_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS choice_criteria (
  criterion_id TEXT PRIMARY KEY,
  choice_id TEXT NOT NULL,
  criterion_key TEXT NOT NULL,
  criterion_value TEXT,
  weight REAL DEFAULT 0.5,
  evidence_text TEXT,
  confidence REAL DEFAULT 0.5,
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(choice_id) REFERENCES choice_episodes(choice_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS causal_hypotheses (
  hypothesis_id TEXT PRIMARY KEY,
  episode_id TEXT,
  person_id TEXT,
  hypothesis_text TEXT NOT NULL,
  cause_table TEXT,
  cause_id TEXT,
  effect_table TEXT,
  effect_id TEXT,
  causal_type TEXT,
  strength REAL DEFAULT 0.5,
  evidence_json TEXT DEFAULT '[]',
  counter_evidence_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'candidate',
  confidence REAL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS counter_evidence_items (
  counter_evidence_id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  counter_evidence_type TEXT NOT NULL,
  counter_evidence_text TEXT,
  source_span_id TEXT,
  strength REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_span_id) REFERENCES source_spans(span_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS social_roles (
  social_role_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  role_label TEXT NOT NULL,
  role_context TEXT,
  relation_to_user TEXT,
  evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_history (
  trust_history_id TEXT PRIMARY KEY,
  relationship_id TEXT,
  person_a TEXT,
  person_b TEXT,
  episode_id TEXT,
  trust_delta REAL DEFAULT 0.0,
  tension_delta REAL DEFAULT 0.0,
  reason TEXT,
  evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  FOREIGN KEY(relationship_id) REFERENCES relationship_models(relationship_id) ON DELETE SET NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS conflict_loops (
  conflict_loop_id TEXT PRIMARY KEY,
  relationship_id TEXT,
  person_a TEXT,
  person_b TEXT,
  loop_summary TEXT NOT NULL,
  trigger_pattern TEXT,
  escalation_path TEXT,
  deescalation_path TEXT,
  evidence_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(relationship_id) REFERENCES relationship_models(relationship_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS repair_patterns (
  repair_pattern_id TEXT PRIMARY KEY,
  relationship_id TEXT,
  person_a TEXT,
  person_b TEXT,
  repair_action TEXT NOT NULL,
  works_when TEXT,
  fails_when TEXT,
  evidence_count INTEGER DEFAULT 0,
  confidence REAL DEFAULT 0.5,
  status TEXT DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(relationship_id) REFERENCES relationship_models(relationship_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pattern_contexts (
  pattern_context_id TEXT PRIMARY KEY,
  pattern_table TEXT NOT NULL,
  pattern_id TEXT NOT NULL,
  context_type TEXT NOT NULL,
  context_value TEXT NOT NULL,
  activation_strength REAL DEFAULT 0.5,
  evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pattern_counterexamples (
  counterexample_id TEXT PRIMARY KEY,
  pattern_table TEXT NOT NULL,
  pattern_id TEXT NOT NULL,
  episode_id TEXT,
  counterexample_summary TEXT NOT NULL,
  why_it_matters TEXT,
  strength REAL DEFAULT 0.5,
  evidence_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS next_phrase_cases (
  next_phrase_case_id TEXT PRIMARY KEY,
  person_id TEXT,
  episode_id TEXT,
  turn_id TEXT,
  previous_text TEXT,
  actual_next_text TEXT,
  predicted_next_text TEXT,
  speech_act_context TEXT,
  emotion_context TEXT,
  interlocutor_context TEXT,
  match_score REAL,
  usable_for_prediction INTEGER DEFAULT 1,
  created_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL,
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS style_state_snapshots (
  style_state_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  episode_id TEXT,
  context_type TEXT,
  directness REAL DEFAULT 0.5,
  detail_level REAL DEFAULT 0.5,
  correction_tendency REAL DEFAULT 0.5,
  validation_seeking REAL DEFAULT 0.5,
  emotional_charge REAL DEFAULT 0.5,
  typical_phrases_json TEXT DEFAULT '[]',
  evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS language_ngrams (
  ngram_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  n INTEGER NOT NULL,
  ngram TEXT NOT NULL,
  context_type TEXT,
  frequency INTEGER DEFAULT 1,
  examples_json TEXT DEFAULT '[]',
  probability REAL DEFAULT 0.0,
  last_seen TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS similar_case_retrieval_runs (
  retrieval_run_id TEXT PRIMARY KEY,
  prediction_id TEXT,
  person_id TEXT,
  query_context TEXT NOT NULL,
  target TEXT,
  semantic_weight REAL DEFAULT 0.2,
  situation_weight REAL DEFAULT 0.2,
  state_weight REAL DEFAULT 0.2,
  relationship_weight REAL DEFAULT 0.15,
  outcome_weight REAL DEFAULT 0.15,
  language_weight REAL DEFAULT 0.1,
  selected_cases_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS prediction_target_scores (
  score_id TEXT PRIMARY KEY,
  person_id TEXT,
  prediction_target TEXT NOT NULL,
  total_predictions INTEGER DEFAULT 0,
  verified_predictions INTEGER DEFAULT 0,
  correct_predictions INTEGER DEFAULT 0,
  mean_match_score REAL DEFAULT 0.0,
  mean_confidence REAL DEFAULT 0.0,
  calibration_gap REAL DEFAULT 0.0,
  reliability_label TEXT DEFAULT 'unproven',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_revisions (
  model_revision_id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT NOT NULL,
  revision_type TEXT NOT NULL,
  previous_json TEXT DEFAULT '{}',
  new_json TEXT DEFAULT '{}',
  reason TEXT,
  evidence_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_interventions (
  trajectory_intervention_id TEXT PRIMARY KEY,
  prediction_id TEXT,
  person_id TEXT,
  intervention_type TEXT NOT NULL,
  current_path TEXT,
  desired_path TEXT,
  action_plan_json TEXT DEFAULT '[]',
  expected_effect_json TEXT DEFAULT '{}',
  risk_json TEXT DEFAULT '{}',
  verification_plan_json TEXT DEFAULT '[]',
  status TEXT DEFAULT 'candidate',
  confidence REAL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS v13_complete_contract_checks (
  check_id TEXT PRIMARY KEY,
  check_group TEXT NOT NULL,
  check_name TEXT NOT NULL,
  required_status TEXT NOT NULL,
  actual_status TEXT NOT NULL,
  detail TEXT,
  severity TEXT DEFAULT 'info',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_v13_req_section ON v13_plan_requirements(section, item_type, status);
CREATE INDEX IF NOT EXISTS idx_v13_cov_component ON v13_component_coverage(component_name, component_type, coverage_status);
CREATE INDEX IF NOT EXISTS idx_v13_engine_runs_name ON v13_engine_runs(engine_name, status, started_at);
CREATE INDEX IF NOT EXISTS idx_v13_engine_outputs_run ON v13_engine_outputs(engine_run_id, engine_name, validation_status);
CREATE INDEX IF NOT EXISTS idx_audio_prosody_turn ON audio_prosody_events(conversation_id, turn_id, event_type);
CREATE INDEX IF NOT EXISTS idx_episode_boundaries_conv ON episode_boundaries(conversation_id, episode_id, idx);
CREATE INDEX IF NOT EXISTS idx_choice_options_choice ON choice_options(choice_id, option_status);
CREATE INDEX IF NOT EXISTS idx_choice_criteria_choice ON choice_criteria(choice_id, criterion_key);
CREATE INDEX IF NOT EXISTS idx_causal_hypotheses_episode ON causal_hypotheses(episode_id, status, causal_type);
CREATE INDEX IF NOT EXISTS idx_counter_evidence_target ON counter_evidence_items(target_table, target_id, status);
CREATE INDEX IF NOT EXISTS idx_social_roles_person ON social_roles(person_id, role_label, status);
CREATE INDEX IF NOT EXISTS idx_trust_history_people ON trust_history(person_a, person_b, created_at);
CREATE INDEX IF NOT EXISTS idx_conflict_loops_people ON conflict_loops(person_a, person_b, status);
CREATE INDEX IF NOT EXISTS idx_repair_patterns_people ON repair_patterns(person_a, person_b, status);
CREATE INDEX IF NOT EXISTS idx_pattern_contexts_pattern ON pattern_contexts(pattern_table, pattern_id, context_type);
CREATE INDEX IF NOT EXISTS idx_pattern_counterexamples_pattern ON pattern_counterexamples(pattern_table, pattern_id);
CREATE INDEX IF NOT EXISTS idx_next_phrase_person ON next_phrase_cases(person_id, usable_for_prediction, created_at);
CREATE INDEX IF NOT EXISTS idx_style_state_person ON style_state_snapshots(person_id, context_type, created_at);
CREATE INDEX IF NOT EXISTS idx_language_ngrams_person ON language_ngrams(person_id, n, ngram);
CREATE INDEX IF NOT EXISTS idx_similar_case_runs_prediction ON similar_case_retrieval_runs(prediction_id, person_id);
CREATE INDEX IF NOT EXISTS idx_prediction_target_scores ON prediction_target_scores(person_id, prediction_target);
CREATE INDEX IF NOT EXISTS idx_model_revisions_target ON model_revisions(target_table, target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_trajectory_interventions_prediction ON trajectory_interventions(prediction_id, status);
CREATE INDEX IF NOT EXISTS idx_v13_complete_contract ON v13_complete_contract_checks(check_group, actual_status, severity);

CREATE INDEX IF NOT EXISTS idx_v13_cycles_conv_episode ON v13_cognitive_cycles(conversation_id, episode_id, status);
CREATE INDEX IF NOT EXISTS idx_v13_llm_cycle_role ON v13_llm_extractions(cycle_id, role_name, validation_status);
CREATE INDEX IF NOT EXISTS idx_v13_dynamic_episode ON v13_dynamic_models(episode_id, person_id, confidence);
CREATE INDEX IF NOT EXISTS idx_v13_snapshots_person ON v13_user_model_snapshots(person_id, scope, created_at);
CREATE INDEX IF NOT EXISTS idx_v13_clusters_person_key ON v13_case_clusters(person_id, cluster_type, cluster_key);
CREATE INDEX IF NOT EXISTS idx_v13_expl_prediction ON v13_prediction_explanations(prediction_id);
CREATE INDEX IF NOT EXISTS idx_v13_contract_status ON v13_memory_contract_checks(category, status, severity);
CREATE INDEX IF NOT EXISTS idx_v13_replay_target ON v13_replay_events(person_id, predicted_target, verdict);
CREATE INDEX IF NOT EXISTS idx_v13_intervention_prediction ON v13_intervention_plans(prediction_id, status);

CREATE INDEX IF NOT EXISTS idx_episodes_conv_time ON episodes(source_conversation_id, start_time, episode_type);
CREATE INDEX IF NOT EXISTS idx_episodes_topic ON episodes(topic, episode_type, confidence);
CREATE INDEX IF NOT EXISTS idx_episode_evidence_episode ON episode_evidence(episode_id, evidence_role);
CREATE INDEX IF NOT EXISTS idx_episode_links_from ON episode_links(from_episode_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_situation_type ON situation_episodes(situation_type, life_domain, confidence);
CREATE INDEX IF NOT EXISTS idx_interactions_people ON interaction_episodes(user_person_id, other_person_id, relationship_type);
CREATE INDEX IF NOT EXISTS idx_speech_acts_turn ON speech_acts(turn_id, act_type);
CREATE INDEX IF NOT EXISTS idx_speech_acts_person_type ON speech_acts(speaker_person_id, act_type);
CREATE INDEX IF NOT EXISTS idx_internal_state_person_time ON internal_state_snapshots(person_id, time_start, dominant_emotion);
CREATE INDEX IF NOT EXISTS idx_thoughts_person_type ON thought_hypotheses(person_id, thought_type, confidence);
CREATE INDEX IF NOT EXISTS idx_intentions_person_status ON action_intentions(person_id, status, action_type);
CREATE INDEX IF NOT EXISTS idx_outcomes_intention ON action_outcomes(intention_id, result);
CREATE INDEX IF NOT EXISTS idx_choices_person_time ON choice_episodes(person_id, decision_time, choice_context);
CREATE INDEX IF NOT EXISTS idx_causal_edges_from_to ON causal_edges(from_table, from_id, to_table, to_id);
CREATE INDEX IF NOT EXISTS idx_contradictions_person_type ON contradiction_events(person_id, contradiction_type, resolved);
CREATE INDEX IF NOT EXISTS idx_relationships_people ON relationship_models(person_a, person_b);
CREATE INDEX IF NOT EXISTS idx_self_dimensions_person ON self_model_dimensions(person_id, dimension_key);
CREATE INDEX IF NOT EXISTS idx_behavior_signals_person_type ON behavior_signals(person_id, signal_type, signal_value);
CREATE INDEX IF NOT EXISTS idx_candidate_patterns_person_key ON candidate_patterns(person_id, pattern_type, pattern_key);
CREATE INDEX IF NOT EXISTS idx_confirmed_patterns_person_key ON confirmed_patterns(person_id, pattern_type, pattern_key);
CREATE INDEX IF NOT EXISTS idx_loop_patterns_person_type ON loop_patterns(person_id, loop_type);
CREATE INDEX IF NOT EXISTS idx_language_patterns_person_expr ON personal_language_patterns(person_id, normalized_expression, frequency);
CREATE INDEX IF NOT EXISTS idx_prediction_cases_type_person ON prediction_cases(case_type, person_id, quality_score);
CREATE INDEX IF NOT EXISTS idx_predictions_person_target ON predictions(person_id, prediction_target, status, created_at);
CREATE INDEX IF NOT EXISTS idx_prediction_results_prediction ON prediction_results(prediction_id, was_correct);
CREATE INDEX IF NOT EXISTS idx_simulation_prediction ON simulation_branches(prediction_id, probability);

CREATE INDEX IF NOT EXISTS idx_emotion_evidence_state ON emotion_evidence(state_id, source_type, emotion_label);
CREATE INDEX IF NOT EXISTS idx_similar_case_scores_prediction ON similar_case_scores(prediction_id, final_score);
CREATE INDEX IF NOT EXISTS idx_future_scenarios_person_type ON future_scenarios(person_id, scenario_type, status);
CREATE INDEX IF NOT EXISTS idx_trajectory_warnings_person_type ON trajectory_warnings(person_id, warning_type, status);
CREATE INDEX IF NOT EXISTS idx_escape_conditions_person_loop ON escape_conditions(person_id, loop_id, status);
CREATE INDEX IF NOT EXISTS idx_v12_engine_runs_engine ON v12_engine_runs(engine_name, status, started_at);


"""


# Journal mode is persistent per SQLite file.  A process-local cache prevents
# every short-lived service/schema connection from requesting the same exclusive
# journal-mode transition while another V18 stage is committing.
_JOURNAL_MODE_LOCK = threading.Lock()
_JOURNAL_MODE_READY: set[str] = set()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open SQLite with the durability and lock behaviour required by live writes.

    The old connection factory only enabled foreign keys.  Under service +
    post-stop + sync activity it could fail immediately with ``database is
    locked`` and offered no bounded retry window.  WAL plus a busy timeout does
    not make concurrent writes magically safe, but it makes a single writer
    contention observable and recoverable instead of random.
    """
    settings = get_settings()
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    # WAL is persistent per database.  Re-applying journal_mode=WAL on every
    # connection asks SQLite for an exclusive lock and can stall a post-stop
    # coordinator immediately after a live service iteration.  Configure once
    # per process/path; subsequent connections only use the normal busy policy.
    journal_key = str(path.resolve())
    if journal_key not in _JOURNAL_MODE_READY:
        with _JOURNAL_MODE_LOCK:
            if journal_key not in _JOURNAL_MODE_READY:
                con.execute("PRAGMA journal_mode=WAL")
                _JOURNAL_MODE_READY.add(journal_key)
    con.execute("PRAGMA synchronous=FULL")
    return con


@contextmanager
def write_transaction(con: sqlite3.Connection, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Commit all writes in a logical unit or roll them back together."""
    began = False
    try:
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            began = True
        yield con
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    else:
        if began and con.in_transaction:
            con.commit()


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


# ``init_db`` is called by many bridged engines before their own lightweight
# schema migrations.  Replaying the large base schema on every call causes
# avoidable SQLite DDL contention during live→post-stop handoff.  Cache only
# after a successful commit, scoped to the concrete database path.
_INIT_DB_LOCK = threading.Lock()
_INITIALIZED_DB_PATHS: set[str] = set()


def init_db(db_path: Path | None = None) -> Path:
    settings = get_settings()
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    path = Path(db_path or settings.db_path).expanduser().resolve()
    key = str(path)
    if key not in _INITIALIZED_DB_PATHS:
        with _INIT_DB_LOCK:
            if key not in _INITIALIZED_DB_PATHS:
                with connect(path) as con:
                    con.executescript(SCHEMA)
                    con.commit()
                _INITIALIZED_DB_PATHS.add(key)
    return path


# Tables where a collision means a historical fact was re-emitted.  These are
# append-only at V18 boundaries; a duplicate is idempotent only when callers use
# ``insert_only(..., on_conflict="ignore")`` explicitly.
IMMUTABLE_FACT_TABLES = {
    "turns", "brainlive_turn_buffer", "brainlive_sensor_events", "brainlive_audio_segments_v154",
    "vision_frames", "brainlive_raw_timeline_v1514", "brainlive_prediction_outcomes",
    "memory_evidence", "source_spans", "voice_observations", "brain2_observed_cases_v17",
}


def upsert(con: sqlite3.Connection, table: str, values: Mapping[str, Any], pk: str) -> None:
    """Update mutable projections without rewriting historical creation time.

    Legacy callers use a generic upsert for caches and projections.  V18 keeps
    that compatibility but never overwrites ``created_at`` on conflict and
    refuses accidental use for immutable fact tables.  Fact writers must call
    :func:`insert_only` or a versioned artifact writer instead.
    """
    table = _identifier(table)
    pk = _identifier(pk)
    cols = [_identifier(str(c)) for c in values.keys()]
    if not cols or pk not in cols:
        raise ValueError("upsert requires a non-empty mapping containing the primary key")
    if table in IMMUTABLE_FACT_TABLES:
        # An exact existing row is a legitimate retry; changing payload is not.
        existing = con.execute(f"SELECT * FROM {table} WHERE {pk}=?", (values[pk],)).fetchone()
        if existing:
            existing_d = dict(existing)
            changed = [c for c in cols if c not in {pk, "created_at", "updated_at", "processed_at"} and c in existing_d and existing_d[c] != values[c]]
            if changed:
                raise ValueError(f"immutable fact collision for {table}/{values[pk]} fields={changed}")
            return
        placeholders = ",".join("?" for _ in cols)
        con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", [values[c] for c in values.keys()])
        return
    placeholders = ",".join("?" for _ in cols)
    # created_at records first observation and is intentionally never changed by
    # a correction/recompute. Evidence/source fields are versioned elsewhere.
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in {pk, "created_at"})
    if updates:
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT({pk}) DO UPDATE SET {updates}"
    else:
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT({pk}) DO NOTHING"
    con.execute(sql, [values[c] for c in values.keys()])


def insert_only(con: sqlite3.Connection, table: str, values: Mapping[str, Any], *, on_conflict: str = "error") -> bool:
    """Append-only insert for immutable facts; return False for ignored duplicate."""
    table = _identifier(table)
    cols = [_identifier(str(c)) for c in values.keys()]
    if not cols:
        raise ValueError("insert_only requires values")
    if on_conflict not in {"error", "ignore"}:
        raise ValueError("on_conflict must be 'error' or 'ignore'")
    prefix = "INSERT OR IGNORE" if on_conflict == "ignore" else "INSERT"
    cur = con.execute(
        f"{prefix} INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
        tuple(values[c] for c in values.keys()),
    )
    return cur.rowcount > 0


def rows(con: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(con.execute(sql, tuple(params)))
