"""V18 governance kernel.

This module centralises the invariants that were previously reimplemented (and
frequently weakened) in every BrainLive/Brain2 module:

* a mandatory owner + session + knowledge-time scope;
* durable pipeline runs and stage gates;
* immutable source/event identities with exact time semantics;
* explicit incomplete/quarantine results instead of ``[]`` on data failures;
* versioned artifacts, lineage, invalidation and retrievable context manifests;
* atomic work leases for Inbox, post-stop and external synchronisation.

It is intentionally dependency-light.  The rest of the codebase may use it
without importing heavy ASR/VLM/LLM packages.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from uuid import uuid4

from .db import connect, init_db, write_transaction
from .integrity_v176 import (
    IntegrityError,
    TimestampError,
    iso_utc,
    new_id,
    parse_iso_utc,
    quarantine,
    quarantine_in_transaction,
)
from .utils import json_dumps, json_loads, now_iso

V18_SCHEMA_VERSION = "18.7.1"


class GovernanceError(IntegrityError):
    """A V18 invariant failed and must never be represented as absent data."""


class ScopeError(GovernanceError):
    """A read/write crossed person, session, mode or ``as_of`` scope."""


class DataAccessError(GovernanceError):
    """A query/schema/lock issue must not silently turn into an empty result."""


class StageGateError(GovernanceError):
    """A pipeline stage attempted to advance with unmet prerequisites."""


class LeaseError(GovernanceError):
    """A worker attempted work without owning the durable lease."""


class ContextBudgetError(GovernanceError):
    """A context cannot be safely reduced under the requested contract."""


@dataclass(frozen=True)
class Scope:
    """The complete visibility boundary of a computation.

    ``as_of`` is the latest knowledge time allowed for a prediction or replay.
    It is deliberately mandatory for replay/predictive paths and optional only
    for a real-time observational live write.
    """

    person_id: str
    live_session_id: str | None = None
    as_of: str | None = None
    mode: str = "live"  # live | post_stop | replay | maintenance
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.person_id, str) or not self.person_id.strip():
            raise ScopeError("person_id is required; implicit owner fallback is forbidden")
        if self.mode not in {"live", "post_stop", "replay", "maintenance", "migration"}:
            raise ScopeError(f"unsupported scope mode: {self.mode!r}")
        if self.as_of is not None:
            parse_iso_utc(self.as_of)
        if self.mode == "replay" and self.as_of is None:
            raise ScopeError("replay requires an explicit as_of knowledge boundary")

    @property
    def as_of_utc(self) -> str | None:
        return iso_utc(parse_iso_utc(self.as_of)) if self.as_of else None


@dataclass(frozen=True)
class EventTime:
    """The only accepted time representation for source events."""

    occurred_at: str
    captured_at: str | None = None
    received_at: str | None = None
    processed_at: str | None = None

    def __post_init__(self) -> None:
        occurred = parse_iso_utc(self.occurred_at)
        for name, value in (("captured_at", self.captured_at), ("received_at", self.received_at), ("processed_at", self.processed_at)):
            if value is not None:
                candidate = parse_iso_utc(value)
                # Receipt/processing can be later, but never before occurrence
                # for the same physical event.  A device clock can be wrong: in
                # that case retain raw value in quarantine rather than inventing
                # a chronology.
                if name in {"received_at", "processed_at"} and candidate < occurred:
                    raise TimestampError(f"{name} cannot precede occurred_at")

    def normalized(self) -> dict[str, str | None]:
        return {
            "occurred_at": iso_utc(parse_iso_utc(self.occurred_at)),
            "captured_at": iso_utc(parse_iso_utc(self.captured_at)) if self.captured_at else None,
            "received_at": iso_utc(parse_iso_utc(self.received_at)) if self.received_at else None,
            "processed_at": iso_utc(parse_iso_utc(self.processed_at)) if self.processed_at else None,
        }


@dataclass(frozen=True)
class ContextItem:
    source_table: str
    source_id: str
    person_id: str
    occurred_at: str
    text: str
    evidence_kind: str = "observation"  # observation | hypothesis | decision | summary
    confidence: float | None = None
    version: str | None = None
    # V18 context callers must preserve the reason an item was included.  These
    # fields used to be passed by the gateway but were absent from this value
    # object, causing a runtime TypeError precisely when a real live/replay
    # context was built.  They are deliberately explicit rather than being
    # smuggled into an untyped dict.
    importance: float = 0.5
    metadata: Mapping[str, Any] | None = None
    retrievable: bool = True

    def __post_init__(self) -> None:
        if not self.source_table or not self.source_id:
            raise GovernanceError("context items require table + stable source id")
        parse_iso_utc(self.occurred_at)
        if self.evidence_kind not in {"observation", "hypothesis", "decision", "summary", "system"}:
            raise GovernanceError(f"unsupported evidence kind: {self.evidence_kind}")
        if self.confidence is not None:
            value = float(self.confidence)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise GovernanceError("context confidence must be finite within [0,1]")
        importance = float(self.importance)
        if not math.isfinite(importance) or not 0 <= importance <= 1:
            raise GovernanceError("context importance must be finite within [0,1]")
        if self.metadata is not None and not isinstance(self.metadata, Mapping):
            raise GovernanceError("context metadata must be a mapping")


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS governance_schema_migrations_v18(
  migration_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v18_pipeline_runs(
  run_id TEXT PRIMARY KEY,
  pipeline_name TEXT NOT NULL,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  mode TEXT NOT NULL CHECK(mode IN ('live','post_stop','replay','maintenance','migration')),
  as_of TEXT,
  input_manifest_sha256 TEXT,
  status TEXT NOT NULL CHECK(status IN ('started','running','completed','failed','partial','quarantined','cancelled')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error_code TEXT,
  error_text TEXT,
  idempotency_key TEXT,
  resume_count INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_v18_runs_scope ON v18_pipeline_runs(person_id, live_session_id, mode, started_at);

CREATE TABLE IF NOT EXISTS v18_pipeline_stages(
  stage_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  required INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('pending','running','retryable_error','blocked','completed','failed','skipped','quarantined','invalidated')),
  input_digest TEXT,
  output_digest TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(run_id, stage_name),
  FOREIGN KEY(run_id) REFERENCES v18_pipeline_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_v18_stages_run_status ON v18_pipeline_stages(run_id, status);

-- A completion manifest is the durable cleanup gate.  It separates a stage
-- having returned from the stronger statement that every expected exported
-- object has a verified retained result.
CREATE TABLE IF NOT EXISTS v18_pipeline_output_manifests(
  manifest_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  person_id TEXT NOT NULL,
  expected_json TEXT NOT NULL DEFAULT '[]',
  observed_json TEXT NOT NULL DEFAULT '[]',
  complete INTEGER NOT NULL CHECK(complete IN (0,1)),
  reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES v18_pipeline_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_v18_output_manifest_complete
  ON v18_pipeline_output_manifests(person_id, complete, updated_at);

CREATE TABLE IF NOT EXISTS v18_pipeline_stage_attempts(
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','retryable_error','blocked','completed','failed','skipped','quarantined','invalidated','abandoned')),
  input_digest TEXT,
  output_digest TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(run_id, stage_name, attempt_no),
  FOREIGN KEY(run_id) REFERENCES v18_pipeline_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_v18_stage_attempts_run ON v18_pipeline_stage_attempts(run_id, stage_name, attempt_no DESC);

CREATE TABLE IF NOT EXISTS v18_work_leases(
  work_key TEXT PRIMARY KEY,
  work_type TEXT NOT NULL,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  source_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','leased','retryable_error','completed','quarantined','cancelled')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  retry_after TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(work_type, source_key)
);
CREATE INDEX IF NOT EXISTS idx_v18_work_claim ON v18_work_leases(work_type, state, retry_after, lease_expires_at);


-- V18.7 recovery state is additive so older SQLite CHECK constraints stay compatible.
-- Retryable failures keep the canonical run active; this table records why it is safe
-- to resume it and prevents cleanup until every pending stage is cleared.
CREATE TABLE IF NOT EXISTS v18_pipeline_recovery_v186(
  run_id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK(state IN ('running','retryable_error','blocked','completed','cancelled')),
  stage_name TEXT,
  error_code TEXT,
  error_text TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES v18_pipeline_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_v18_recovery_state_v186 ON v18_pipeline_recovery_v186(state, updated_at);


-- Individual retry events are observability only; pipeline state remains the
-- authority for resumption.  Keeping retries append-only makes timeouts and
-- crash recovery explainable without making a failed attempt look completed.
CREATE TABLE IF NOT EXISTS v18_pipeline_retry_events_v186(
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  stage_name TEXT,
  component TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  error_code TEXT NOT NULL,
  error_text TEXT,
  retry_after_seconds INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES v18_pipeline_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_v18_retry_events_run_v186
  ON v18_pipeline_retry_events_v186(run_id, stage_name, created_at);

CREATE TABLE IF NOT EXISTS v18_artifact_versions(
  artifact_version_id TEXT PRIMARY KEY,
  artifact_table TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  identity_key TEXT NOT NULL,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  source_digest TEXT NOT NULL,
  version INTEGER NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  status TEXT NOT NULL CHECK(status IN ('active','superseded','invalidated','quarantined','deleted')),
  created_at TEXT NOT NULL,
  superseded_at TEXT,
  invalidated_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(artifact_table, artifact_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_v18_artifact_one_active
  ON v18_artifact_versions(artifact_table, identity_key, person_id) WHERE active=1;
CREATE INDEX IF NOT EXISTS idx_v18_artifact_scope ON v18_artifact_versions(person_id, live_session_id, artifact_table, active);

CREATE TABLE IF NOT EXISTS v18_invalidations(
  invalidation_id TEXT PRIMARY KEY,
  root_table TEXT NOT NULL,
  root_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  run_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed')),
  affected_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  completed_at TEXT,
  error_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_v18_invalidations_root ON v18_invalidations(root_table, root_id, status);

CREATE TABLE IF NOT EXISTS v18_source_tombstones(
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  invalidation_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  invalidated_at TEXT NOT NULL,
  PRIMARY KEY(source_table, source_id, person_id),
  FOREIGN KEY(invalidation_id) REFERENCES v18_invalidations(invalidation_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_v18_tombstones_owner ON v18_source_tombstones(person_id, invalidated_at);

CREATE TABLE IF NOT EXISTS v18_turn_source_map(
  source_identity TEXT PRIMARY KEY,
  live_session_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  live_turn_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('partial','final','superseded','quarantined')),
  source_event_id TEXT,
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v18_turn_source_session ON v18_turn_source_map(live_session_id, occurred_at);

CREATE TABLE IF NOT EXISTS v18_context_manifests(
  context_id TEXT PRIMARY KEY,
  run_id TEXT,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  as_of TEXT,
  purpose TEXT NOT NULL,
  requested_budget_chars INTEGER NOT NULL,
  rendered_chars INTEGER NOT NULL,
  incomplete INTEGER NOT NULL CHECK(incomplete IN (0,1)),
  manifest_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v18_context_scope ON v18_context_manifests(person_id, live_session_id, created_at);

CREATE TABLE IF NOT EXISTS v18_sync_manifest(
  sync_id TEXT PRIMARY KEY,
  backend TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  source_version TEXT,
  payload_sha256 TEXT NOT NULL,
  truth_status TEXT NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  external_ref_json TEXT NOT NULL DEFAULT '{}',
  synced_at TEXT NOT NULL,
  retracted_at TEXT,
  UNIQUE(backend, source_table, source_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_sync_retract ON v18_sync_manifest(backend, active, person_id);

-- External adapters have a stricter identity than the legacy generic sync
-- manifest: the same conversation/source identifier is allowed in distinct
-- owner scopes, but never overwrites another owner's external projection.
CREATE TABLE IF NOT EXISTS v18_external_sync_manifest(
  external_sync_id TEXT PRIMARY KEY,
  backend TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  source_version TEXT,
  payload_sha256 TEXT NOT NULL,
  truth_status TEXT NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  external_ref_json TEXT NOT NULL DEFAULT '{}',
  synced_at TEXT NOT NULL,
  retracted_at TEXT,
  UNIQUE(backend, source_table, source_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_external_sync_owner ON v18_external_sync_manifest(backend, person_id, active);

CREATE TABLE IF NOT EXISTS v18_replay_runs(
  replay_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  conversation_id TEXT,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  as_of TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('started','completed','failed','quarantined')),
  isolated_namespace TEXT NOT NULL UNIQUE,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error_text TEXT
);

CREATE TABLE IF NOT EXISTS v18_conversation_scopes(
  conversation_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('explicit_export','turn_owner','manual','migration')),
  evidence_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(conversation_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_conversation_scope_person ON v18_conversation_scopes(person_id, active, conversation_id);

CREATE TABLE IF NOT EXISTS v18_source_projection_state(
  projection_id TEXT PRIMARY KEY,
  projection_kind TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(projection_kind, source_table, source_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_projection_active ON v18_source_projection_state(projection_kind, person_id, active);
"""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def strict_many(con: sqlite3.Connection, sql: str, params: Iterable[Any] = (), *, purpose: str = "query") -> list[dict[str, Any]]:
    """Query without degrading a DB/schema exception into an empty collection."""
    try:
        return [dict(r) for r in con.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error as exc:
        raise DataAccessError(f"{purpose} failed: {exc}") from exc


def strict_one(con: sqlite3.Connection, sql: str, params: Iterable[Any] = (), *, purpose: str = "query") -> dict[str, Any] | None:
    try:
        row = con.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        raise DataAccessError(f"{purpose} failed: {exc}") from exc


def _add_column_if_missing(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_pipeline_stage_statuses_v187(con: sqlite3.Connection) -> None:
    """Expand stage status constraints without discarding durable history.

    SQLite cannot alter CHECK constraints in place.  V18.7 rebuilds only the
    two stage-ledger tables, copies every row byte-for-byte, then recreates the
    lookup index.  The migration is idempotent and runs inside the existing
    governance transaction.
    """
    stage_sql_row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='v18_pipeline_stages'").fetchone()
    stage_sql = str(stage_sql_row[0] or "") if stage_sql_row else ""
    if "retryable_error" not in stage_sql or "blocked" not in stage_sql:
        con.executescript("""
            ALTER TABLE v18_pipeline_stages RENAME TO v18_pipeline_stages_pre_v187;
            CREATE TABLE v18_pipeline_stages(
              stage_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              stage_name TEXT NOT NULL,
              required INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL CHECK(status IN ('pending','running','retryable_error','blocked','completed','failed','skipped','quarantined','invalidated')),
              input_digest TEXT,
              output_digest TEXT,
              result_json TEXT NOT NULL DEFAULT '{}',
              error_text TEXT,
              started_at TEXT,
              finished_at TEXT,
              UNIQUE(run_id, stage_name),
              FOREIGN KEY(run_id) REFERENCES v18_pipeline_runs(run_id) ON DELETE CASCADE
            );
            INSERT INTO v18_pipeline_stages(stage_id,run_id,stage_name,required,status,input_digest,output_digest,result_json,error_text,started_at,finished_at)
              SELECT stage_id,run_id,stage_name,required,status,input_digest,output_digest,result_json,error_text,started_at,finished_at
              FROM v18_pipeline_stages_pre_v187;
            DROP TABLE v18_pipeline_stages_pre_v187;
            CREATE INDEX IF NOT EXISTS idx_v18_stages_run_status ON v18_pipeline_stages(run_id, status);
        """)
    attempt_sql_row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='v18_pipeline_stage_attempts'").fetchone()
    attempt_sql = str(attempt_sql_row[0] or "") if attempt_sql_row else ""
    if "retryable_error" not in attempt_sql or "blocked" not in attempt_sql:
        con.executescript("""
            ALTER TABLE v18_pipeline_stage_attempts RENAME TO v18_pipeline_stage_attempts_pre_v187;
            CREATE TABLE v18_pipeline_stage_attempts(
              attempt_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              stage_name TEXT NOT NULL,
              attempt_no INTEGER NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('running','retryable_error','blocked','completed','failed','skipped','quarantined','invalidated','abandoned')),
              input_digest TEXT,
              output_digest TEXT,
              result_json TEXT NOT NULL DEFAULT '{}',
              error_text TEXT,
              started_at TEXT,
              finished_at TEXT,
              UNIQUE(run_id, stage_name, attempt_no),
              FOREIGN KEY(run_id) REFERENCES v18_pipeline_runs(run_id) ON DELETE CASCADE
            );
            INSERT INTO v18_pipeline_stage_attempts(attempt_id,run_id,stage_name,attempt_no,status,input_digest,output_digest,result_json,error_text,started_at,finished_at)
              SELECT attempt_id,run_id,stage_name,attempt_no,status,input_digest,output_digest,result_json,error_text,started_at,finished_at
              FROM v18_pipeline_stage_attempts_pre_v187;
            DROP TABLE v18_pipeline_stage_attempts_pre_v187;
            CREATE INDEX IF NOT EXISTS idx_v18_stage_attempts_run_stage ON v18_pipeline_stage_attempts(run_id, stage_name, attempt_no);
        """)


def ensure_v18_schema() -> None:
    """Install V18 governance tables and additive compatibility migrations.

    Existing V17 rows remain historical material.  A migration may create an
    explicit proof, quarantine a contradiction, or add an invariant; it never
    silently turns ambiguous legacy material into verified V18 evidence.
    """
    init_db()
    from .integrity_v176 import ensure_integrity_schema
    ensure_integrity_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)
        _migrate_pipeline_stage_statuses_v187(con)
        # RC4 adds resumable/idempotent pipeline identity without rebuilding
        # existing databases.
        _add_column_if_missing(con, "v18_pipeline_runs", "idempotency_key", "TEXT")
        _add_column_if_missing(con, "v18_pipeline_runs", "resume_count", "INTEGER NOT NULL DEFAULT 0")
        # RC2 migration: active artifact identities are owner-scoped.  The
        # RC1 unique index silently let one user's material supersede another
        # user's same logical identity.
        con.execute("DROP INDEX IF EXISTS idx_v18_artifact_one_active")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_v18_artifact_one_active ON v18_artifact_versions(artifact_table, identity_key, person_id) WHERE active=1")
        # Only one active execution may own one logical run key.  Terminal runs
        # remain historical and do not prevent a deliberately new execution.
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_v18_runs_active_idempotency
                     ON v18_pipeline_runs(
                       pipeline_name, person_id, COALESCE(live_session_id,''), mode, idempotency_key
                     )
                     WHERE idempotency_key IS NOT NULL AND status IN ('started','running')""")
        con.execute(
            """INSERT INTO governance_schema_migrations_v18(migration_id, applied_at, details_json)
               VALUES(?,?,?)
               ON CONFLICT(migration_id) DO UPDATE SET applied_at=excluded.applied_at, details_json=excluded.details_json""",
            ("v18_governance_kernel", now_iso(), json_dumps({"version": V18_SCHEMA_VERSION, "release": "v18_7_retryable_resume_and_cleanup_guard"})),
        )



def recovery_state(*, run_id: str, con: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Return the V18.7 resumability state for one canonical run.

    ``con`` is accepted so callers already inside a write transaction do not
    open a second SQLite connection while holding the writer lock.
    """
    if con is not None:
        return strict_one(
            con,
            "SELECT run_id,state,stage_name,error_code,error_text,attempt_count,next_retry_at,updated_at "
            "FROM v18_pipeline_recovery_v186 WHERE run_id=?",
            (run_id,),
            purpose="pipeline recovery lookup",
        )
    ensure_v18_schema()
    with connect() as own:
        return recovery_state(run_id=run_id, con=own)


def record_recovery_state(
    *,
    run_id: str,
    state: str,
    stage_name: str | None = None,
    error_code: str | None = None,
    error_text: str | None = None,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    """Persist one explicit resume boundary without terminalising the run.

    Retryable failure deliberately leaves ``v18_pipeline_runs.status`` as
    ``running``.  The old schema has a terminal ``failed`` state, while the
    logical close-day must remain claimable by the next RESUME invocation.
    """
    if state not in {"running", "retryable_error", "blocked", "completed", "cancelled"}:
        raise GovernanceError(f"invalid recovery state {state!r}")
    ensure_v18_schema()
    next_retry_at: str | None = None
    if retry_after_seconds is not None:
        if int(retry_after_seconds) < 0:
            raise GovernanceError("retry_after_seconds must be non-negative")
        next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=int(retry_after_seconds))).isoformat().replace("+00:00", "Z")
    with connect() as con, write_transaction(con):
        run = strict_one(con, "SELECT run_id FROM v18_pipeline_runs WHERE run_id=?", (run_id,), purpose="recovery run lookup")
        if not run:
            raise StageGateError(f"unknown pipeline run: {run_id}")
        prior = recovery_state(run_id=run_id, con=con)
        attempts = int((prior or {}).get("attempt_count") or 0) + (1 if state == "retryable_error" else 0)
        con.execute(
            """INSERT INTO v18_pipeline_recovery_v186(
                 run_id,state,stage_name,error_code,error_text,attempt_count,next_retry_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,stage_name=excluded.stage_name,
                 error_code=excluded.error_code,error_text=excluded.error_text,attempt_count=excluded.attempt_count,
                 next_retry_at=excluded.next_retry_at,updated_at=excluded.updated_at""",
            (run_id, state, stage_name, error_code, (error_text or "")[:4000] or None, attempts, next_retry_at, now_iso()),
        )
        return recovery_state(run_id=run_id, con=con) or {"run_id": run_id, "state": state}


def record_retry_event(
    *,
    run_id: str,
    component: str,
    attempt_no: int,
    error_code: str,
    error_text: str,
    stage_name: str | None = None,
    retry_after_seconds: int | None = None,
) -> None:
    """Append a durable retry history entry; never changes completion state."""
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO v18_pipeline_retry_events_v186(
                event_id,run_id,stage_name,component,attempt_no,error_code,error_text,retry_after_seconds,created_at
              ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (new_id("retry_v186"), run_id, stage_name, component, max(1, int(attempt_no)), error_code,
             (error_text or "")[:4000], retry_after_seconds, now_iso()),
        )


def clear_recovery_state(*, run_id: str) -> None:
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.execute("DELETE FROM v18_pipeline_recovery_v186 WHERE run_id=?", (run_id,))


def assert_run_resumable(*, run_id: str, force: bool = False, con: sqlite3.Connection | None = None) -> None:
    state = recovery_state(run_id=run_id, con=con)
    if not state:
        return
    current = str(state.get("state") or "running")
    if current == "blocked":
        # A blocked state means that evidence, configuration or a local
        # dependency needs attention; it must *not* destroy completed durable
        # checkpoints.  A normal invocation remains conservative.  An explicit
        # operator-approved ``--force`` after fixing the cause reuses the same
        # logical run and retries only its unresolved stage.
        if not force:
            raise StageGateError(
                f"run {run_id} is blocked at {state.get('stage_name') or 'unknown stage'}: "
                f"{state.get('error_code') or 'manual intervention required'}; "
                "repair the cause then use explicit RESUME --force"
            )
        return
    if current in {"completed", "cancelled"}:
        raise StageGateError(f"run {run_id} is not resumable: recovery state is {current}")
    retry_at = state.get("next_retry_at")
    if current == "retryable_error" and retry_at and not force:
        try:
            if parse_iso_utc(str(retry_at)) > datetime.now(timezone.utc):
                raise StageGateError(f"run {run_id} is retryable but backoff remains until {retry_at}; use explicit resume --force to override")
        except TimestampError:
            pass


def mark_run_retryable(
    *, run_id: str, stage_name: str, error_code: str, error_text: str, retry_after_seconds: int = 0
) -> dict[str, Any]:
    """Make a failed stage resumable while retaining the canonical run id."""
    return record_recovery_state(
        run_id=run_id,
        state="retryable_error",
        stage_name=stage_name,
        error_code=error_code,
        error_text=error_text,
        retry_after_seconds=retry_after_seconds,
    )


def begin_run(*, pipeline_name: str, scope: Scope, input_manifest: Any | None = None) -> str:
    ensure_v18_schema()
    run_id = scope.run_id or new_id("run_v18")
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO v18_pipeline_runs(
                 run_id,pipeline_name,person_id,live_session_id,mode,as_of,input_manifest_sha256,status,started_at,metadata_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, pipeline_name, scope.person_id, scope.live_session_id, scope.mode, scope.as_of_utc,
                _digest(input_manifest) if input_manifest is not None else None,
                "running", now_iso(), json_dumps({"schema_version": V18_SCHEMA_VERSION}),
            ),
        )
    return run_id


def begin_or_resume_run(
    *,
    pipeline_name: str,
    scope: Scope,
    input_manifest: Any | None = None,
    idempotency_key: str,
    force_resume: bool = False,
    profile_invalidation_map: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, bool]:
    """Create one active run or return the exact active run already owning it.

    The old orchestration created a new post-stop run on every retry.  A crash
    or double stop notification could therefore duplicate assembly and
    downstream writes.  This primitive coalesces active runs.  When an operator
    explicitly uses ``force_resume`` with a changed declared processing profile,
    only the configured affected stages are invalidated; completed independent
    checkpoints remain reusable.  A terminal run can be rerun only through an
    explicit new invocation and leaves its history intact.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise GovernanceError("idempotency_key is required for resumable runs")
    ensure_v18_schema()
    key = idempotency_key.strip()
    manifest_digest = _digest(input_manifest) if input_manifest is not None else None
    with connect() as con, write_transaction(con):
        existing = strict_one(
            con,
            """SELECT run_id,input_manifest_sha256,metadata_json FROM v18_pipeline_runs
               WHERE pipeline_name=? AND person_id=?
                 AND COALESCE(live_session_id,'')=COALESCE(?, '')
                 AND mode=? AND idempotency_key=?
                 AND status IN ('started','running')
               ORDER BY started_at DESC LIMIT 1""",
            (pipeline_name, scope.person_id, scope.live_session_id, scope.mode, key),
            purpose="idempotent pipeline run lookup",
        )
        if existing:
            run_id_existing = str(existing["run_id"])
            assert_run_resumable(run_id=run_id_existing, force=force_resume, con=con)
            if (existing.get("input_manifest_sha256") or None) != manifest_digest:
                if not force_resume:
                    raise StageGateError(
                        "active idempotency key was requested with a different input/configuration manifest; "
                        "repair the cause then use explicit RESUME --force to invalidate only affected stages"
                    )
                metadata = json_loads(existing.get("metadata_json"), {}) or {}
                old_manifest = metadata.get("input_manifest") if isinstance(metadata, dict) else None
                changed_keys = set()
                if isinstance(old_manifest, Mapping) and isinstance(input_manifest, Mapping):
                    changed_keys = {
                        str(name) for name in set(old_manifest) | set(input_manifest)
                        if old_manifest.get(name) != input_manifest.get(name)
                    }
                else:
                    changed_keys = {"*"}
                mapping = profile_invalidation_map or {"*": ("*")}
                affected: set[str] = set()
                for changed_key in changed_keys or {"*"}:
                    configured = mapping.get(changed_key) or mapping.get("*") or ("*",)
                    affected.update(str(stage) for stage in configured)
                running = strict_many(
                    con,
                    "SELECT stage_name FROM v18_pipeline_stages WHERE run_id=? AND status='running'",
                    (run_id_existing,),
                    purpose="profile invalidation running stage lookup",
                )
                if running:
                    raise StageGateError(
                        "cannot change processing profile while a stage is still running: "
                        + ", ".join(str(row["stage_name"]) for row in running)
                    )
                if "*" in affected:
                    con.execute(
                        """UPDATE v18_pipeline_stages
                           SET status='invalidated', error_text=?, finished_at=?
                           WHERE run_id=? AND status IN ('completed','failed','skipped','invalidated','quarantined')""",
                        ("explicit profile change during forced resume", now_iso(), run_id_existing),
                    )
                elif affected:
                    placeholders = ",".join("?" for _ in affected)
                    con.execute(
                        f"""UPDATE v18_pipeline_stages
                              SET status='invalidated', error_text=?, finished_at=?
                            WHERE run_id=? AND stage_name IN ({placeholders})
                              AND status IN ('completed','failed','skipped','invalidated','quarantined')""",
                        ("explicit profile change during forced resume", now_iso(), run_id_existing, *sorted(affected)),
                    )
                con.execute("DELETE FROM v18_pipeline_output_manifests WHERE run_id=?", (run_id_existing,))
                metadata = dict(metadata) if isinstance(metadata, dict) else {}
                metadata["schema_version"] = V18_SCHEMA_VERSION
                metadata["input_manifest"] = input_manifest
                metadata["profile_changed_keys"] = sorted(changed_keys)
                con.execute(
                    "UPDATE v18_pipeline_runs SET input_manifest_sha256=?, metadata_json=? WHERE run_id=?",
                    (manifest_digest, json_dumps(metadata), run_id_existing),
                )
            con.execute(
                "UPDATE v18_pipeline_runs SET resume_count=COALESCE(resume_count,0)+1 WHERE run_id=?",
                (run_id_existing,),
            )
            return run_id_existing, True
        run_id = scope.run_id or new_id("run_v18")
        # ``INSERT OR IGNORE`` is intentional here.  Catching an IntegrityError
        # inside an active SQLite transaction is unsafe on several drivers: the
        # transaction can be left in an error state before the race lookup is
        # attempted.  A conflict is expected idempotency control-flow, not an
        # exceptional partially failed write.
        created = con.execute(
            """INSERT OR IGNORE INTO v18_pipeline_runs(
                 run_id,pipeline_name,person_id,live_session_id,mode,as_of,input_manifest_sha256,
                 status,started_at,idempotency_key,resume_count,metadata_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, pipeline_name, scope.person_id, scope.live_session_id, scope.mode, scope.as_of_utc,
                manifest_digest,
                "running", now_iso(), key, 0, json_dumps({"schema_version": V18_SCHEMA_VERSION, "input_manifest": input_manifest}),
            ),
        )
        if created.rowcount == 1:
            return run_id, False
        # A competing writer inserted the active key after our lookup.  It is
        # also possible (though extremely unlikely) that the generated run_id
        # collided.  Only a matching active logical run is resumable; anything
        # else is a visible invariant failure, never a silent retry.
        existing = strict_one(
            con,
            """SELECT run_id,input_manifest_sha256 FROM v18_pipeline_runs
               WHERE pipeline_name=? AND person_id=?
                 AND COALESCE(live_session_id,'')=COALESCE(?, '')
                 AND mode=? AND idempotency_key=?
                 AND status IN ('started','running')
               ORDER BY started_at DESC LIMIT 1""",
            (pipeline_name, scope.person_id, scope.live_session_id, scope.mode, key),
            purpose="idempotent pipeline run race lookup",
        )
        if not existing:
            raise GovernanceError("pipeline run insert was ignored without a matching active idempotency owner")
        if (existing.get("input_manifest_sha256") or None) != manifest_digest:
            raise StageGateError(
                "idempotency race resolved to an active run with a different input/configuration manifest"
            )
        assert_run_resumable(run_id=str(existing["run_id"]), force=force_resume, con=con)
        con.execute(
            "UPDATE v18_pipeline_runs SET resume_count=COALESCE(resume_count,0)+1 WHERE run_id=?",
            (existing["run_id"],),
        )
        return str(existing["run_id"]), True


def recover_stale_stages(
    *, run_id: str, stale_after_seconds: int = 600, reason: str = "worker_lease_expired"
) -> dict[str, Any]:
    """Mark only genuinely stale running stage attempts abandoned.

    This is the explicit crash-recovery boundary.  It never steals a fresh
    running stage, and it preserves the abandoned attempt for audit before a
    later caller starts the next attempt.
    """
    if stale_after_seconds < 0:
        raise GovernanceError("stale_after_seconds must be non-negative")
    ensure_v18_schema()
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(seconds=int(stale_after_seconds))
    recovered: list[str] = []
    with connect() as con, write_transaction(con):
        run = strict_one(con, "SELECT status FROM v18_pipeline_runs WHERE run_id=?", (run_id,), purpose="recovery run lookup")
        if not run:
            raise StageGateError(f"unknown pipeline run: {run_id}")
        if str(run["status"]) in {"completed", "failed", "partial", "quarantined", "cancelled"}:
            return {"run_id": run_id, "recovered": recovered, "terminal": True}
        rows = strict_many(
            con,
            "SELECT stage_name,started_at FROM v18_pipeline_stages WHERE run_id=? AND status='running'",
            (run_id,),
            purpose="stale stage lookup",
        )
        for row in rows:
            started_raw = str(row.get("started_at") or "")
            try:
                started = parse_iso_utc(started_raw)
            except Exception as exc:
                # A malformed running timestamp is never safe to leave live.
                started = datetime.min.replace(tzinfo=timezone.utc)
                stage_reason = f"{reason}: invalid started_at ({exc})"
            else:
                if started > cutoff:
                    continue
                stage_reason = reason
            name = str(row["stage_name"])
            stamp = now_iso()
            con.execute(
                """UPDATE v18_pipeline_stage_attempts
                   SET status='abandoned', error_text=?, finished_at=?
                   WHERE run_id=? AND stage_name=? AND status='running'""",
                (stage_reason[:4000], stamp, run_id, name),
            )
            cur = con.execute(
                """UPDATE v18_pipeline_stages
                   SET status='retryable_error', error_text=?, finished_at=?
                   WHERE run_id=? AND stage_name=? AND status='running'""",
                (stage_reason[:4000], stamp, run_id, name),
            )
            if cur.rowcount == 1:
                recovered.append(name)
    if recovered:
        record_recovery_state(
            run_id=run_id,
            state="retryable_error",
            stage_name=",".join(recovered),
            error_code="stale_stage_recovered",
            error_text=reason,
            retry_after_seconds=0,
        )
    return {"run_id": run_id, "recovered": recovered, "terminal": False}


def record_output_manifest(
    *, run_id: str, person_id: str, expected: Sequence[str], observed: Sequence[str], reason: str | None = None
) -> dict[str, Any]:
    """Persist a retained-output manifest used by cleanup gates.

    The comparison is intentionally set-based after rejecting duplicate expected
    identifiers.  A duplicate export is not a second success.
    """
    ensure_v18_schema()
    expected_list = [str(x) for x in expected if str(x)]
    observed_list = [str(x) for x in observed if str(x)]
    if len(expected_list) != len(set(expected_list)):
        raise GovernanceError("expected output manifest contains duplicate identifiers")
    complete = set(expected_list) == set(observed_list) and len(observed_list) == len(set(observed_list))
    with connect() as con, write_transaction(con):
        verify = strict_one(con, "SELECT person_id FROM v18_pipeline_runs WHERE run_id=?", (run_id,), purpose="output manifest run owner")
        if not verify:
            raise StageGateError(f"unknown pipeline run: {run_id}")
        if str(verify["person_id"]) != person_id:
            raise ScopeError("output manifest owner does not match run owner")
        con.execute(
            """INSERT INTO v18_pipeline_output_manifests(
                 manifest_id,run_id,person_id,expected_json,observed_json,complete,reason,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET expected_json=excluded.expected_json,
                 observed_json=excluded.observed_json,complete=excluded.complete,reason=excluded.reason,updated_at=excluded.updated_at""",
            (new_id("output_manifest"), run_id, person_id, json_dumps(expected_list), json_dumps(observed_list), 1 if complete else 0, reason, now_iso(), now_iso()),
        )
    return {"run_id": run_id, "complete": complete, "expected": expected_list, "observed": observed_list}


def assert_cleanup_eligible(*, run_id: str, person_id: str, required_stages: Sequence[str]) -> dict[str, Any]:
    """Require a completed run, complete required stages, and retained outputs.

    A phone/raw-media cleanup caller must use this rather than treating one
    post-stop function return value as proof that downstream evidence survived.
    """
    ensure_v18_schema()
    with connect() as con:
        run = strict_one(con, "SELECT status,person_id FROM v18_pipeline_runs WHERE run_id=?", (run_id,), purpose="cleanup run lookup")
        if not run:
            raise StageGateError(f"unknown pipeline run: {run_id}")
        if str(run["person_id"]) != person_id:
            raise ScopeError("cleanup owner does not match run owner")
        if str(run["status"]) != "completed":
            raise StageGateError(f"cleanup blocked: run status is {run['status']}")
        pending = strict_one(con, "SELECT state,stage_name,error_code FROM v18_pipeline_recovery_v186 WHERE run_id=?", (run_id,), purpose="cleanup recovery lookup")
        if pending and str(pending.get("state")) not in {"completed"}:
            raise StageGateError(
                f"cleanup blocked: recovery state {pending.get('state')} at {pending.get('stage_name') or 'unknown stage'} "
                f"({pending.get('error_code') or 'pending work'})"
            )
        assert_stages_complete(con, run_id=run_id, stage_names=required_stages)
        manifest = strict_one(con, "SELECT complete,expected_json,observed_json,reason FROM v18_pipeline_output_manifests WHERE run_id=?", (run_id,), purpose="cleanup manifest lookup")
        if not manifest or not bool(int(manifest["complete"])):
            raise StageGateError("cleanup blocked: retained output manifest is absent or incomplete")
        return {"run_id": run_id, "eligible": True, "expected": json_loads(manifest["expected_json"], []), "observed": json_loads(manifest["observed_json"], [])}


def update_run(run_id: str, *, status: str, error_code: str | None = None, error_text: str | None = None) -> None:
    if status not in {"started", "running", "completed", "failed", "partial", "quarantined", "cancelled"}:
        raise GovernanceError(f"invalid run status {status!r}")
    terminal = {"completed", "failed", "partial", "quarantined", "cancelled"}
    with connect() as con, write_transaction(con):
        row = strict_one(con, "SELECT status FROM v18_pipeline_runs WHERE run_id=?", (run_id,), purpose="run lifecycle lookup")
        if not row:
            raise StageGateError(f"unknown pipeline run: {run_id}")
        previous = str(row["status"])
        if previous in terminal and status != previous:
            raise StageGateError(f"terminal pipeline run cannot transition {previous} -> {status}")
        cur = con.execute(
            """UPDATE v18_pipeline_runs SET status=?, error_code=?, error_text=?,
               finished_at=CASE WHEN ? IN ('completed','failed','partial','quarantined','cancelled') THEN ? ELSE finished_at END
               WHERE run_id=?""",
            (status, error_code, error_text[:4000] if error_text else None, status, now_iso(), run_id),
        )
        if cur.rowcount != 1:
            raise StageGateError(f"run update lost: {run_id}")
    if status == "completed":
        record_recovery_state(run_id=run_id, state="completed")
    elif status in {"failed", "partial", "quarantined", "cancelled"}:
        record_recovery_state(run_id=run_id, state="blocked" if status != "cancelled" else "cancelled", error_code=error_code, error_text=error_text)


def start_stage(con: sqlite3.Connection, *, run_id: str, stage_name: str, required: bool = True, input_payload: Any | None = None) -> str:
    """Start one durable stage attempt without erasing a failed history."""
    run = strict_one(con, "SELECT status FROM v18_pipeline_runs WHERE run_id=?", (run_id,), purpose="stage run lookup")
    if not run:
        raise StageGateError(f"unknown pipeline run: {run_id}")
    if str(run["status"]) in {"completed", "failed", "partial", "quarantined", "cancelled"}:
        raise StageGateError(f"cannot start stage on terminal run: {run_id}")
    row = strict_one(con, "SELECT status FROM v18_pipeline_stages WHERE run_id=? AND stage_name=?", (run_id, stage_name), purpose="stage lookup")
    if row and row["status"] == "completed":
        return str(stage_name)
    if row and row["status"] == "running":
        # A caller must resolve or explicitly invalidate a running stage rather
        # than silently taking over its work after a crash.
        raise StageGateError(f"stage already running: {stage_name}")
    previous_attempt = strict_one(con, "SELECT MAX(attempt_no) AS n FROM v18_pipeline_stage_attempts WHERE run_id=? AND stage_name=?", (run_id, stage_name), purpose="stage attempt lookup")
    attempt_no = int((previous_attempt or {}).get("n") or 0) + 1
    digest = _digest(input_payload) if input_payload is not None else None
    started = now_iso()
    con.execute(
        """INSERT INTO v18_pipeline_stages(stage_id,run_id,stage_name,required,status,input_digest,result_json,started_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id,stage_name) DO UPDATE SET status='running', required=excluded.required,
              input_digest=excluded.input_digest, started_at=excluded.started_at, finished_at=NULL,
              output_digest=NULL, result_json='{}', error_text=NULL""",
        (new_id("stage"), run_id, stage_name, 1 if required else 0, "running", digest, "{}", started),
    )
    con.execute(
        """INSERT INTO v18_pipeline_stage_attempts(attempt_id,run_id,stage_name,attempt_no,status,input_digest,result_json,started_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (new_id("stage_attempt"), run_id, stage_name, attempt_no, "running", digest, "{}", started),
    )
    return str(stage_name)


def finish_stage(con: sqlite3.Connection, *, run_id: str, stage_name: str, result: Any, status: str = "completed", error_text: str | None = None) -> None:
    if status not in {"completed", "retryable_error", "blocked", "failed", "skipped", "quarantined", "invalidated"}:
        raise GovernanceError(f"invalid stage status {status!r}")
    finished = now_iso()
    cur = con.execute(
        """UPDATE v18_pipeline_stages SET status=?, result_json=?, output_digest=?, error_text=?, finished_at=?
           WHERE run_id=? AND stage_name=? AND status='running'""",
        (status, json_dumps(result), _digest(result), error_text[:4000] if error_text else None, finished, run_id, stage_name),
    )
    if cur.rowcount != 1:
        raise StageGateError(f"stage is not running: {stage_name}")
    attempt = strict_one(con, "SELECT attempt_id FROM v18_pipeline_stage_attempts WHERE run_id=? AND stage_name=? AND status='running' ORDER BY attempt_no DESC LIMIT 1", (run_id, stage_name), purpose="stage attempt finish")
    if not attempt:
        raise StageGateError(f"stage attempt missing: {stage_name}")
    con.execute(
        "UPDATE v18_pipeline_stage_attempts SET status=?, result_json=?, output_digest=?, error_text=?, finished_at=? WHERE attempt_id=?",
        (status, json_dumps(result), _digest(result), error_text[:4000] if error_text else None, finished, attempt["attempt_id"]),
    )

def assert_stages_complete(con: sqlite3.Connection, *, run_id: str, stage_names: Sequence[str]) -> None:
    placeholders = ",".join("?" for _ in stage_names)
    rows = strict_many(
        con,
        f"SELECT stage_name,status,required FROM v18_pipeline_stages WHERE run_id=? AND stage_name IN ({placeholders})",
        (run_id, *stage_names),
        purpose="stage gate",
    )
    found = {str(row["stage_name"]): str(row["status"]) for row in rows}
    bad = [name for name in stage_names if found.get(name) != "completed"]
    if bad:
        raise StageGateError(f"required stages incomplete for {run_id}: {bad}")


def source_key(*, source_device: str, source_event_id: str | None, source_sha256: str | None, occurred_at: str, source_path: str | None = None) -> str:
    if not source_device:
        raise GovernanceError("source_device is mandatory for durable source identity")
    occurrence = source_event_id or f"{occurred_at}|{source_path or ''}"
    return _digest({"device": source_device, "event": occurrence, "sha256": source_sha256 or ""})


def work_scope_key(*, person_id: str, source_key_value: str) -> str:
    """Return the durable inbox/work identity for one memory owner.

    A device-side event id is globally meaningful only to the device; it is not
    a permission to deduplicate two users' memory streams together.
    """
    if not person_id:
        raise ScopeError("person_id is required for work identity")
    return _digest({"person_id": person_id, "source_key": source_key_value})


def register_event(*, scope: Scope, modality: str, source_device: str, source_event_id: str | None,
                   source_sha256: str | None, time: EventTime, source_path: str | None, payload: Mapping[str, Any],
                   status: str = "accepted") -> dict[str, Any]:
    """Record/return an immutable event envelope.

    Duplicate delivery returns the existing event only when every immutable
    field agrees. A conflicting replay is committed to quarantine *after* the
    read transaction has closed: raising from inside ``write_transaction``
    would roll back the quarantine together with the rejected write.
    """
    ensure_v18_schema()
    values = time.normalized()
    skey = source_key(source_device=source_device, source_event_id=source_event_id, source_sha256=source_sha256,
                      occurred_at=values["occurred_at"], source_path=source_path)
    fingerprint = _digest({"scope": scope.person_id, "source": skey, "modality": modality})
    payload_json = json_dumps(dict(payload))
    payload_sha256 = _digest(payload)
    conflict: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    with connect() as con, write_transaction(con):
        existing = strict_one(
            con,
            "SELECT * FROM event_envelopes_v176 WHERE event_fingerprint=?",
            (fingerprint,),
            purpose="event envelope dedupe",
        )
        if existing:
            conflicts: list[str] = []
            if str(existing.get("payload_sha256") or "") != payload_sha256:
                conflicts.append("payload_sha256")
            if str(existing.get("person_id") or "") != scope.person_id:
                conflicts.append("person_id")
            if (existing.get("live_session_id") or None) != (scope.live_session_id or None):
                conflicts.append("live_session_id")
            if conflicts:
                conflict = {
                    "existing_event_id": str(existing["event_id"]),
                    "conflicts": conflicts,
                    "incoming": dict(payload),
                }
            else:
                result = {"event_id": existing["event_id"], "created": False, "source_key": skey}
        else:
            event_id = new_id("evt")
            con.execute(
                """INSERT INTO event_envelopes_v176(
                     event_id,event_fingerprint,modality,source_device,source_event_id,source_sha256,
                     occurred_at,captured_at,received_at,processed_at,person_id,live_session_id,source_path,
                     payload_json,payload_sha256,status,pipeline_version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, fingerprint, modality, source_device, source_event_id, source_sha256,
                 values["occurred_at"], values["captured_at"], values["received_at"], values["processed_at"],
                 scope.person_id, scope.live_session_id, source_path, payload_json, payload_sha256, status,
                 V18_SCHEMA_VERSION, now_iso(), now_iso()),
            )
            result = {"event_id": event_id, "created": True, "source_key": skey}
    if conflict is not None:
        quarantine(
            category="event_envelope_conflict",
            reason="immutable event fingerprint received conflicting " + ", ".join(conflict["conflicts"]),
            raw_payload=conflict,
            source_table="event_envelopes_v176",
            source_id=conflict["existing_event_id"],
            person_id=scope.person_id,
        )
        raise GovernanceError("event envelope conflict; incoming event quarantined")
    if result is None:
        raise GovernanceError("event envelope write produced no result")
    return result


def claim_work(*, work_type: str, scope: Scope, source_key_value: str, lease_seconds: int = 120,
               max_attempts: int = 5) -> dict[str, Any] | None:
    """Atomically claim durable work; errors are retryable then quarantined.

    Returns ``None`` if another worker owns a valid lease or the item is
    terminal.  It is safe under multiple SQLite processes because the decision
    is made inside ``BEGIN IMMEDIATE``.
    """
    ensure_v18_schema()
    now = datetime.now(timezone.utc)
    now_s = iso_utc(now)
    expiry = iso_utc(now + timedelta(seconds=max(10, int(lease_seconds))))
    token = new_id("lease")
    # Work belongs to one memory owner.  A phone/event identifier may be
    # reused across profiles, so a raw source hash must never be the lease key.
    scoped_source_key = work_scope_key(person_id=scope.person_id, source_key_value=source_key_value)
    work_key = _digest({"work_type": work_type, "person_id": scope.person_id, "source_key": source_key_value})
    with connect() as con, write_transaction(con):
        row = strict_one(con, "SELECT * FROM v18_work_leases WHERE work_key=?", (work_key,), purpose="work lease lookup")
        if row is None:
            # Compatibility with RC1 rows stored under the raw key.  Restrict
            # the bridge to the same owner; it must never revive another owner.
            row = strict_one(con, "SELECT * FROM v18_work_leases WHERE work_type=? AND source_key=? AND person_id=?", (work_type, source_key_value, scope.person_id), purpose="legacy work lease lookup")
            if row:
                work_key = str(row["work_key"])
        if row:
            state = str(row["state"])
            if state in {"completed", "quarantined", "cancelled"}:
                return None
            # Corrupt scheduling values used to raise from this function and
            # leave the item indefinitely retryable.  A lease with no reliable
            # clock is unsafe to steal or execute, so quarantine it visibly.
            try:
                retry_after = row.get("retry_after")
                if retry_after and parse_iso_utc(str(retry_after)) > now:
                    return None
                existing_expiry = row.get("lease_expires_at")
                if state == "leased" and existing_expiry and parse_iso_utc(str(existing_expiry)) > now:
                    return None
                attempt_count = int(row.get("attempt_count") or 0)
                configured_max_attempts = int(row.get("max_attempts") or max_attempts)
            except Exception as exc:
                detail = f"invalid lease schedule or attempt counter: {exc}"[:2000]
                con.execute(
                    """UPDATE v18_work_leases
                       SET state='quarantined', lease_token=NULL, lease_expires_at=NULL,
                           updated_at=?, error_text=?
                       WHERE work_key=?""",
                    (now_s, detail, work_key),
                )
                quarantine_in_transaction(
                    con, category="work_lease_metadata_invalid", reason=detail,
                    source_table="v18_work_leases", source_id=work_key,
                    person_id=scope.person_id, raw_payload={"row": row},
                )
                return None
            if attempt_count >= configured_max_attempts:
                con.execute("UPDATE v18_work_leases SET state='quarantined', updated_at=?, error_text=COALESCE(error_text,'max retries exhausted') WHERE work_key=?", (now_s, work_key))
                quarantine_in_transaction(con, category="work_max_retries", reason="work exceeded retry budget", source_table="v18_work_leases", source_id=work_key, person_id=scope.person_id)
                return None
            con.execute(
                """UPDATE v18_work_leases SET state='leased',attempt_count=attempt_count+1,lease_token=?,lease_expires_at=?,retry_after=NULL,updated_at=? WHERE work_key=?""",
                (token, expiry, now_s, work_key),
            )
        else:
            con.execute(
                """INSERT INTO v18_work_leases(work_key,work_type,person_id,live_session_id,source_key,state,attempt_count,max_attempts,retry_after,lease_token,lease_expires_at,result_json,error_text,created_at,updated_at)
                   VALUES(?,?,?,?,?,'leased',1,?,?,?,?,?,?,?,?)""",
                (work_key, work_type, scope.person_id, scope.live_session_id, scoped_source_key, max_attempts, None, token, expiry, "{}", None, now_s, now_s),
            )
    return {"work_key": work_key, "lease_token": token, "lease_expires_at": expiry, "attempt_count": (int(row.get("attempt_count") or 0) + 1) if row else 1, "scoped_source_key": scoped_source_key}


def finish_work(*, work_key: str, lease_token: str, status: str, result: Any | None = None, error_text: str | None = None,
                retry_delay_seconds: int = 30) -> None:
    if status not in {"completed", "retryable_error", "quarantined", "cancelled"}:
        raise GovernanceError(f"invalid work status {status!r}")
    with connect() as con, write_transaction(con):
        row = strict_one(con, "SELECT * FROM v18_work_leases WHERE work_key=?", (work_key,), purpose="finish work lookup")
        if not row or row.get("lease_token") != lease_token or row.get("state") != "leased":
            raise LeaseError("work is not held by this lease")
        retry_after = None
        if status == "retryable_error":
            retry_after = iso_utc(datetime.now(timezone.utc) + timedelta(seconds=max(1, int(retry_delay_seconds))))
        con.execute(
            """UPDATE v18_work_leases SET state=?, retry_after=?, lease_token=NULL, lease_expires_at=NULL,
                  result_json=?, error_text=?, updated_at=?, completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END
               WHERE work_key=?""",
            (status, retry_after, json_dumps(result or {}), error_text[:4000] if error_text else None, now_iso(), status, now_iso(), work_key),
        )
        if status == "quarantined":
            quarantine_in_transaction(con, category="work_quarantined", reason=error_text or "quarantined", source_table="v18_work_leases", source_id=work_key, person_id=row["person_id"], raw_payload=result)


def record_artifact_version_in_transaction(con: sqlite3.Connection, *, artifact_table: str, artifact_id: str,
                                            identity_key: str, scope: Scope, source_payload: Any,
                                            metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Version an artifact using an already-open writer transaction.

    Every multi-table writer must call this variant.  Opening a second
    connection from inside a ``BEGIN IMMEDIATE`` transaction is not merely
    inefficient in SQLite: it can deadlock the post-stop/longitudinal run and
    leave its primary rows committed without their provenance.
    """
    if not artifact_table or not artifact_id or not identity_key:
        raise GovernanceError("artifact table, id and logical identity are required")
    source_digest = _digest(source_payload)
    latest = strict_one(
        con,
        """SELECT * FROM v18_artifact_versions
           WHERE artifact_table=? AND identity_key=? AND person_id=?
           ORDER BY version DESC LIMIT 1""",
        (artifact_table, identity_key, scope.person_id),
        purpose="artifact version lookup",
    )
    if latest and latest["source_digest"] == source_digest and int(latest["active"]) == 1 and latest["status"] == "active":
        return {"artifact_version_id": latest["artifact_version_id"], "version": latest["version"], "created": False}
    version = int(latest["version"]) + 1 if latest else 1
    if latest and int(latest["active"]) == 1:
        con.execute(
            "UPDATE v18_artifact_versions SET active=0,status='superseded',superseded_at=? WHERE artifact_version_id=?",
            (now_iso(), latest["artifact_version_id"]),
        )
    avid = new_id("artifact_v18")
    con.execute(
        """INSERT INTO v18_artifact_versions(artifact_version_id,artifact_table,artifact_id,identity_key,person_id,live_session_id,source_digest,version,active,status,created_at,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,1,'active',?,?)""",
        (avid, artifact_table, artifact_id, identity_key, scope.person_id, scope.live_session_id, source_digest, version, now_iso(), json_dumps(dict(metadata or {}))),
    )
    # A changed source can be revalidated, but a same-payload re-run cannot
    # silently revive a contradicted/tombstoned source.
    if latest is None or latest["source_digest"] != source_digest:
        con.execute(
            "DELETE FROM v18_source_tombstones WHERE source_table=? AND source_id=? AND person_id=?",
            (artifact_table, artifact_id, scope.person_id),
        )
    return {
        "artifact_version_id": avid, "version": version, "created": True,
        "superseded": latest["artifact_version_id"] if latest and int(latest["active"]) == 1 else None,
    }


def record_artifact_version(*, artifact_table: str, artifact_id: str, identity_key: str, scope: Scope,
                            source_payload: Any, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Version a derived object in a standalone durable transaction."""
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        return record_artifact_version_in_transaction(
            con, artifact_table=artifact_table, artifact_id=artifact_id,
            identity_key=identity_key, scope=scope, source_payload=source_payload,
            metadata=metadata,
        )


def link_artifact_in_transaction(con: sqlite3.Connection, *, child_table: str, child_id: str,
                                 parent_table: str, parent_id: str, scope: Scope,
                                 relation_type: str, source_version: str | None = None) -> None:
    """Record lineage through an existing writer transaction."""
    if not all((child_table, child_id, parent_table, parent_id, relation_type)):
        raise GovernanceError("complete lineage endpoints and relation_type are required")
    con.execute(
        """INSERT INTO artifact_lineage_v176(lineage_id,child_table,child_id,parent_table,parent_id,person_id,relation_type,source_version,invalidated_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,NULL,?)
           ON CONFLICT(child_table,child_id,parent_table,parent_id,relation_type) DO NOTHING""",
        (new_id("lineage"), child_table, child_id, parent_table, parent_id, scope.person_id, relation_type, source_version, now_iso()),
    )


def link_artifact(*, child_table: str, child_id: str, parent_table: str, parent_id: str, scope: Scope,
                  relation_type: str, source_version: str | None = None) -> None:
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        link_artifact_in_transaction(
            con, child_table=child_table, child_id=child_id, parent_table=parent_table,
            parent_id=parent_id, scope=scope, relation_type=relation_type,
            source_version=source_version,
        )


def set_projection_active_in_transaction(con: sqlite3.Connection, *, projection_kind: str,
                                         source_table: str, source_id: str, person_id: str,
                                         active: bool, reason: str | None = None) -> None:
    """Set a projection gate using the caller's transaction."""
    if not all((projection_kind, source_table, source_id, person_id)):
        raise ScopeError("projection state requires kind, source and person scope")
    con.execute(
        """INSERT INTO v18_source_projection_state(projection_id,projection_kind,source_table,source_id,person_id,active,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(projection_kind,source_table,source_id,person_id) DO UPDATE SET
             active=excluded.active,reason=excluded.reason,updated_at=excluded.updated_at""",
        (new_id("projection"), projection_kind, source_table, source_id, person_id,
         1 if active else 0, reason, now_iso(), now_iso()),
    )


def invalidate_descendants(*, root_table: str, root_id: str, scope: Scope, reason: str, run_id: str | None = None) -> dict[str, Any]:
    """Invalidate a root and every derived descendant across all projections.

    A source-level tombstone is intentionally stronger than one projection kind:
    old readers cannot keep a contradicted hook, case or deep-vision addendum
    live merely because they use a different projection label.
    """
    ensure_v18_schema()
    invalidation_id = new_id("invalidate")
    affected: list[dict[str, str]] = []
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO v18_invalidations(invalidation_id,root_table,root_id,person_id,reason,run_id,status,affected_json,created_at)
               VALUES(?,?,?,?,?,?, 'processing','[]',?)""",
            (invalidation_id, root_table, root_id, scope.person_id, reason, run_id, now_iso()),
        )
        queue: list[tuple[str, str]] = [(root_table, root_id)]
        seen = set(queue)
        while queue:
            current_table, current_id = queue.pop(0)
            con.execute(
                """INSERT INTO v18_source_tombstones(source_table,source_id,person_id,invalidation_id,reason,invalidated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(source_table,source_id,person_id) DO UPDATE SET
                     invalidation_id=excluded.invalidation_id, reason=excluded.reason, invalidated_at=excluded.invalidated_at""",
                (current_table, current_id, scope.person_id, invalidation_id, reason, now_iso()),
            )
            con.execute(
                "UPDATE v18_artifact_versions SET active=0,status='invalidated',invalidated_at=? WHERE artifact_table=? AND artifact_id=? AND person_id=? AND active=1",
                (now_iso(), current_table, current_id, scope.person_id),
            )
            con.execute(
                "UPDATE v18_source_projection_state SET active=0,reason=?,updated_at=? WHERE source_table=? AND source_id=? AND person_id=? AND active=1",
                (reason, now_iso(), current_table, current_id, scope.person_id),
            )
            affected.append({"table": current_table, "id": current_id})
            children = strict_many(
                con,
                """SELECT child_table,child_id FROM artifact_lineage_v176
                   WHERE parent_table=? AND parent_id=? AND person_id=? AND invalidated_at IS NULL""",
                (current_table, current_id, scope.person_id),
                purpose="lineage traversal",
            )
            for child in children:
                child_key = (str(child["child_table"]), str(child["child_id"]))
                con.execute(
                    """UPDATE artifact_lineage_v176 SET invalidated_at=?
                       WHERE parent_table=? AND parent_id=? AND child_table=? AND child_id=? AND person_id=? AND invalidated_at IS NULL""",
                    (now_iso(), current_table, current_id, child_key[0], child_key[1], scope.person_id),
                )
                if child_key not in seen:
                    seen.add(child_key)
                    queue.append(child_key)
        con.execute("UPDATE v18_invalidations SET status='completed',affected_json=?,completed_at=? WHERE invalidation_id=?", (json_dumps(affected), now_iso(), invalidation_id))
    return {"invalidation_id": invalidation_id, "affected": affected}

def is_artifact_active(con: sqlite3.Connection, *, table: str, artifact_id: str, person_id: str) -> bool:
    tombstone = strict_one(con, "SELECT 1 AS blocked FROM v18_source_tombstones WHERE source_table=? AND source_id=? AND person_id=?", (table, artifact_id, person_id), purpose="artifact tombstone check")
    if tombstone:
        return False
    row = strict_one(con, "SELECT active,status FROM v18_artifact_versions WHERE artifact_table=? AND artifact_id=? AND person_id=? ORDER BY version DESC LIMIT 1", (table, artifact_id, person_id), purpose="artifact active check")
    return bool(row and int(row["active"]) == 1 and row["status"] == "active")

def build_context_manifest(*, scope: Scope, purpose: str, items: Sequence[ContextItem], max_chars: int,
                           run_id: str | None = None, max_item_chars: int = 1600) -> dict[str, Any]:
    """Build a bounded, provenance-preserving context instead of raw truncation.

    Each omitted or shortened item is represented by a retrievable source ref,
    exact hash and explicit ``truncated`` marker.  The caller can reject an
    incomplete context for critical tasks rather than pretending it is whole.
    """
    if not isinstance(purpose, str) or not purpose.strip():
        raise ContextBudgetError("context purpose is required")
    if max_chars <= 0 or max_item_chars <= 0:
        raise ContextBudgetError("context budgets must be positive")
    filtered: list[ContextItem] = []
    excluded_future: list[dict[str, Any]] = []
    for item in items:
        if item.person_id != scope.person_id:
            raise ScopeError(f"cross-owner context item: {item.source_table}/{item.source_id}")
        when = parse_iso_utc(item.occurred_at)
        if scope.as_of and when > parse_iso_utc(scope.as_of):
            excluded_future.append({"source_table": item.source_table, "source_id": item.source_id, "occurred_at": item.occurred_at, "reason": "after_as_of"})
            continue
        filtered.append(item)
    # The same source can be found through SQL recall, a life-model projection
    # and an active-context cache.  Rendering it three times wastes the budget
    # and artificially increases its persuasive weight in the LLM prompt.
    # Keep one deterministic representative and preserve a manifest audit ref.
    grouped: dict[tuple[str, str, str], list[ContextItem]] = {}
    for item in filtered:
        grouped.setdefault((item.source_table, item.source_id, item.version or ""), []).append(item)
    deduplicated: list[dict[str, Any]] = []
    unique_items: list[ContextItem] = []
    for key, group in grouped.items():
        ordered = sorted(
            group,
            key=lambda x: (x.importance, parse_iso_utc(x.occurred_at), len(x.text or "")),
            reverse=True,
        )
        chosen = ordered[0]
        unique_items.append(chosen)
        for duplicate in ordered[1:]:
            deduplicated.append({
                "source_table": duplicate.source_table,
                "source_id": duplicate.source_id,
                "version": duplicate.version,
                "occurred_at": duplicate.occurred_at,
                "reason": "duplicate_source_ref",
            })
    unique_items.sort(key=lambda x: (parse_iso_utc(x.occurred_at), x.source_table, x.source_id, x.version or ""))
    rendered: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    used = 0
    for item in unique_items:
        raw_text = item.text or ""
        allowed = min(max_item_chars, max(0, max_chars - used))
        if allowed <= 0:
            omitted.append({"source_table": item.source_table, "source_id": item.source_id, "occurred_at": item.occurred_at, "content_sha256": _digest(raw_text), "reason": "budget_exhausted"})
            continue
        text = raw_text[:allowed]
        shortened = len(text) < len(raw_text)
        ref = {
            "source_table": item.source_table,
            "source_id": item.source_id,
            "occurred_at": iso_utc(parse_iso_utc(item.occurred_at)),
            "evidence_kind": item.evidence_kind,
            "confidence": item.confidence,
            "importance": item.importance,
            "version": item.version,
            "metadata": dict(item.metadata or {}),
            "retrievable": bool(item.retrievable),
            "text": text,
            "content_sha256": _digest(raw_text),
            "truncated": shortened,
        }
        rendered.append(ref)
        used += len(text)
        if shortened:
            omitted.append({"source_table": item.source_table, "source_id": item.source_id, "occurred_at": item.occurred_at, "content_sha256": ref["content_sha256"], "reason": "item_budget"})
    manifest = {
        "schema_version": V18_SCHEMA_VERSION,
        "purpose": purpose,
        "scope": {"person_id": scope.person_id, "live_session_id": scope.live_session_id, "as_of": scope.as_of_utc, "mode": scope.mode},
        "items": rendered,
        "omitted_refs": omitted,
        "excluded_future_refs": excluded_future,
        "deduplicated_refs": deduplicated,
        "incomplete": bool(omitted),
        "requested_budget_chars": max_chars,
        "rendered_chars": used,
    }
    ensure_v18_schema()
    context_id = new_id("context")
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO v18_context_manifests(context_id,run_id,person_id,live_session_id,as_of,purpose,requested_budget_chars,rendered_chars,incomplete,manifest_json,manifest_sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (context_id, run_id, scope.person_id, scope.live_session_id, scope.as_of_utc, purpose, max_chars, used, 1 if omitted else 0, json_dumps(manifest), _digest(manifest), now_iso()),
        )
    manifest["context_id"] = context_id
    return manifest


def require_complete_context(manifest: Mapping[str, Any], *, purpose: str) -> None:
    if bool(manifest.get("incomplete")):
        raise ContextBudgetError(f"{purpose} requires complete context; manifest={manifest.get('context_id')}")


def verify_same_owner(con: sqlite3.Connection, *, table: str, id_column: str, object_id: str, person_id: str,
                      owner_column: str = "person_id") -> dict[str, Any]:
    row = strict_one(con, f"SELECT * FROM {table} WHERE {id_column}=?", (object_id,), purpose=f"owner lookup {table}")
    if not row:
        raise ScopeError(f"missing {table}/{object_id}")
    if str(row.get(owner_column) or "") != person_id:
        raise ScopeError(f"cross-owner mutation denied for {table}/{object_id}")
    return row


def canonical_time(row: Mapping[str, Any], *fields: str) -> str | None:
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            try:
                return iso_utc(parse_iso_utc(value))
            except TimestampError:
                continue
    return None


def assert_live_session_owner(con: sqlite3.Connection, *, live_session_id: str, person_id: str) -> None:
    row = strict_one(con, "SELECT person_id,status FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,), purpose="live session owner")
    if not row:
        raise ScopeError(f"unknown live_session_id {live_session_id}")
    if str(row["person_id"]) != person_id:
        raise ScopeError("live_session_id does not belong to supplied person_id")


def register_conversation_scope_in_transaction(con: sqlite3.Connection, *, conversation_id: str, person_id: str,
                                              evidence_kind: str, evidence: Mapping[str, Any] | None = None) -> None:
    """Record ownership proof using the caller's transaction.

    This avoids opening a second SQLite writer while an assembler, longitudinal
    run, or replay already holds a transaction.  The legacy implementation
    opened a nested connection and could fail with ``database is locked`` under
    normal post-stop concurrency.
    """
    if evidence_kind not in {"explicit_export", "turn_owner", "manual", "migration"}:
        raise ScopeError(f"unsupported conversation ownership proof: {evidence_kind}")
    if not conversation_id or not person_id:
        raise ScopeError("conversation ownership proof requires conversation_id and person_id")
    con.execute(
        """INSERT INTO v18_conversation_scopes(conversation_id,person_id,evidence_kind,evidence_json,active,created_at,updated_at)
           VALUES(?,?,?,?,1,?,?)
           ON CONFLICT(conversation_id,person_id) DO UPDATE SET
             evidence_kind=excluded.evidence_kind,evidence_json=excluded.evidence_json,active=1,updated_at=excluded.updated_at""",
        (conversation_id, person_id, evidence_kind, json_dumps(dict(evidence or {})), now_iso(), now_iso()),
    )


def register_conversation_scope(*, conversation_id: str, person_id: str, evidence_kind: str, evidence: Mapping[str, Any] | None = None) -> None:
    """Record an explicit owner proof for a conversation in a durable writer."""
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        register_conversation_scope_in_transaction(
            con, conversation_id=conversation_id, person_id=person_id,
            evidence_kind=evidence_kind, evidence=evidence,
        )


def conversation_in_scope(con: sqlite3.Connection, *, conversation_id: str, person_id: str, allow_legacy_turn_proof: bool = True) -> bool:
    row = strict_one(
        con,
        "SELECT active FROM v18_conversation_scopes WHERE conversation_id=? AND person_id=?",
        (conversation_id, person_id),
        purpose="conversation owner scope",
    )
    if row:
        return bool(int(row["active"]))
    if not allow_legacy_turn_proof:
        return False
    # Temporary compatibility proof: at least one explicitly attributed turn.
    # A conversation with only unknown speakers is deliberately excluded rather
    # than attributed to the default profile.
    proof = strict_one(
        con,
        "SELECT 1 AS ok FROM turns WHERE conversation_id=? AND person_id=? LIMIT 1",
        (conversation_id, person_id),
        purpose="legacy conversation owner proof",
    )
    return bool(proof)


def set_projection_active(*, projection_kind: str, source_table: str, source_id: str, person_id: str,
                          active: bool, reason: str | None = None) -> None:
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        set_projection_active_in_transaction(
            con, projection_kind=projection_kind, source_table=source_table,
            source_id=source_id, person_id=person_id, active=active, reason=reason,
        )


def projection_is_active(con: sqlite3.Connection, *, projection_kind: str, source_table: str, source_id: str, person_id: str) -> bool:
    tombstone = strict_one(
        con,
        "SELECT 1 AS blocked FROM v18_source_tombstones WHERE source_table=? AND source_id=? AND person_id=?",
        (source_table, source_id, person_id),
        purpose="projection tombstone check",
    )
    if tombstone:
        return False
    row = strict_one(
        con,
        "SELECT active FROM v18_source_projection_state WHERE projection_kind=? AND source_table=? AND source_id=? AND person_id=?",
        (projection_kind, source_table, source_id, person_id),
        purpose="projection active check",
    )
    if row is not None:
        return bool(int(row["active"]))
    artifact = strict_one(
        con,
        "SELECT active,status FROM v18_artifact_versions WHERE artifact_table=? AND artifact_id=? AND person_id=? ORDER BY version DESC LIMIT 1",
        (source_table, source_id, person_id),
        purpose="projection artifact check",
    )
    # Legacy material without a V18 version remains visible only until an
    # explicit tombstone/projection state is recorded; V18 artifacts obey their
    # lifecycle even when no dedicated projection row was created.
    return True if artifact is None else bool(int(artifact["active"]) == 1 and artifact["status"] == "active")


def redact_external_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Carry only external-sync fields that must be indexed/retracted."""
    return {
        "source_version": payload.get("source_version"),
        "truth_status": payload.get("truth_status", "active"),
        "valid_until": payload.get("valid_until"),
        "person_id": payload.get("person_id"),
    }
