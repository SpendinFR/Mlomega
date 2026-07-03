"""V17.6 integrity kernel.

This module is intentionally small and dependency-central.  It provides the
invariants that the older feature modules were missing: strict LLM boundaries,
canonical forecast lifecycle, typed temporal values, explicit quarantine, and
transaction-safe writes.

The module is additive: it migrates an existing V17.4 database without deleting
historical rows.  V17.7/V18 will migrate the remaining writers to this kernel.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .db import connect, init_db
from .utils import json_dumps, now_iso


INTEGRITY_SCHEMA_VERSION = "17.6.0"


class IntegrityError(RuntimeError):
    """Base error for an invariant breach that must not look like missing data."""


class ContractValidationError(IntegrityError):
    """An LLM/VLM payload failed an executable contract."""


class OwnershipError(IntegrityError):
    """A caller attempted to mutate an object owned by another person/session."""


class LifecycleError(IntegrityError):
    """A state transition is not legal for the current object."""


class TimestampError(IntegrityError):
    """A time is absent, invalid, ambiguous, or violates an ordering invariant."""


class Horizon(str, Enum):
    H0 = "H0"
    H1 = "H1"
    H2 = "H2"


@dataclass(frozen=True)
class HorizonSpec:
    horizon: Literal["H0", "H1", "H2"]
    min_seconds: int
    max_seconds: int

    def due_at(self, occurred_at: str) -> str:
        dt = parse_iso_utc(occurred_at)
        return iso_utc(dt + timedelta(seconds=self.max_seconds))


# One source of truth.  Documentation, live materialisation, evaluation and
# calibration must use these exact ranges.
HORIZON_SPECS: dict[Horizon, HorizonSpec] = {
    Horizon.H0: HorizonSpec(Horizon.H0, 0, 10),
    Horizon.H1: HorizonSpec(Horizon.H1, 10, 300),
    Horizon.H2: HorizonSpec(Horizon.H2, 300, 7200),
}


FORECAST_ACTIVE_STATES = {"open", "due"}
FORECAST_TERMINAL_STATES = {
    "evaluated_correct",
    "evaluated_incorrect",
    "expired",
    "indeterminate",
    "quarantined",
    "invalidated",
    "superseded",
}
FORECAST_STATES = FORECAST_ACTIVE_STATES | FORECAST_TERMINAL_STATES


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class WorldStateContract(StrictContract):
    where_am_i: str | None = None
    what_is_happening: str | None = None
    active_mode: Literal["conversation", "work", "routine", "transition", "social", "rest", "unknown", "other"]
    probable_activity: list[Any]
    active_emotional_state: str | None = None
    confidence: float
    # V18.4 makes even the world-state assertion source-addressable.  A model
    # may still say unknown, but it cannot present an unreferenced situation as
    # an observed fact.
    evidence: list[Any]
    counter_evidence: list[Any]

    @field_validator("confidence")
    @classmethod
    def _valid_confidence(cls, value: float) -> float:
        return unit_interval(value, field="world_state.confidence")


class EventContract(StrictContract):
    event_type: str
    summary: str
    urgency_score: float
    novelty_score: float
    tension_score: float
    relationship_relevance_score: float
    opportunity_score: float
    evidence: list[Any]
    counter_evidence: list[Any]

    @field_validator(
        "urgency_score",
        "novelty_score",
        "tension_score",
        "relationship_relevance_score",
        "opportunity_score",
    )
    @classmethod
    def _valid_score(cls, value: float) -> float:
        return unit_interval(value, field="event score")


class NeedPredictionContract(StrictContract):
    need_label: str
    need_type: str
    horizon: Literal["H0", "H1", "H2"]
    why_now: str | None = None
    confidence: float
    evidence: list[Any]
    counter_evidence: list[Any]

    @field_validator("confidence")
    @classmethod
    def _valid_confidence(cls, value: float) -> float:
        return unit_interval(value, field="need_prediction.confidence")


class AffordanceContract(StrictContract):
    affordance_label: str
    world_element: str | None = None
    position_hint: str | None = None
    personal_relevance: str | None = None
    matched_need_label: str | None = None
    personal_fit: float
    time_sensitivity: float
    confidence: float
    evidence: list[Any]
    counter_evidence: list[Any]

    @field_validator("personal_fit", "time_sensitivity", "confidence")
    @classmethod
    def _valid_score(cls, value: float) -> float:
        return unit_interval(value, field="affordance score")


class ForecastContract(StrictContract):
    horizon: Literal["H0", "H1", "H2"]
    forecast_type: Literal["need", "action", "words", "emotion", "risk", "opportunity", "trajectory"]
    predicted_need: str | None = None
    predicted_action: str | None = None
    predicted_words: str | None = None
    predicted_emotion: str | None = None
    predicted_risk: str | None = None
    predicted_opportunity: str | None = None
    if_intervene_future: str | None = None
    if_silent_future: str | None = None
    expected_gain: float
    probability: float
    confidence: float
    evidence: list[Any]
    counter_evidence: list[Any]

    @field_validator("expected_gain", "probability", "confidence")
    @classmethod
    def _valid_score(cls, value: float, info) -> float:
        return unit_interval(value, field=f"forecast.{info.field_name}")

    @model_validator(mode="after")
    def _has_prediction_target(self) -> "ForecastContract":
        if not any(
            [
                self.predicted_need,
                self.predicted_action,
                self.predicted_words,
                self.predicted_emotion,
                self.predicted_risk,
                self.predicted_opportunity,
            ]
        ):
            raise ValueError("forecast requires at least one predicted field")
        return self


class LifeHypothesisContract(StrictContract):
    hypothesis_type: str
    statement: str
    scope: str
    time_pattern: str | None = None
    location_pattern: str | None = None
    trigger_contexts: list[Any]
    candidate_needs: list[Any]
    candidate_emotions: list[Any]
    candidate_risks: list[Any]
    candidate_opportunities: list[Any]
    predicted_next_window: str | None = None
    confidence: float
    evidence: list[Any]
    counter_evidence: list[Any]

    @field_validator("confidence")
    @classmethod
    def _valid_confidence(cls, value: float) -> float:
        return unit_interval(value, field="life_hypothesis.confidence")


class InterventionContract(StrictContract):
    message: str
    intervention_type: str
    recommended_timing: str
    urgency: float
    confidence: float
    expected_gain: float
    risk_if_silent: float
    risk_if_said: float
    intrusion_score: float
    autonomy_risk: float
    cooldown_key: str | None = None
    evidence: list[Any]
    counter_evidence: list[Any]

    @field_validator(
        "urgency",
        "confidence",
        "expected_gain",
        "risk_if_silent",
        "risk_if_said",
        "intrusion_score",
        "autonomy_risk",
    )
    @classmethod
    def _valid_score(cls, value: float) -> float:
        return unit_interval(value, field="intervention score")


class BrainLiveOutputContract(StrictContract):
    world_state: WorldStateContract
    events: list[EventContract]
    need_predictions: list[NeedPredictionContract]
    affordances: list[AffordanceContract]
    forecasts: list[ForecastContract]
    life_hypotheses: list[LifeHypothesisContract]
    interventions: list[InterventionContract]
    watch_next: list[Any]
    notes_for_brain2: list[Any]


class OutcomeEvaluationContract(StrictContract):
    forecast_id: str = Field(min_length=1)
    was_prediction_correct: bool | None
    match_score: float | None
    observed_after: str
    lesson: dict[str, Any]
    evidence: list[Any]
    counter_evidence: list[Any]
    missed_opportunity: dict[str, Any]

    @field_validator("match_score")
    @classmethod
    def _valid_match_score(cls, value: float | None) -> float | None:
        return None if value is None else unit_interval(value, field="outcome.match_score")


class OutcomeBatchContract(StrictContract):
    evaluations: list[OutcomeEvaluationContract]


def parse_iso_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TimestampError("missing ISO-8601 timestamp")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimestampError(f"invalid timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        raise TimestampError(f"timestamp must include timezone: {value!r}")
    return dt.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def unit_interval(value: Any, *, field: str) -> float:
    # bool is accepted by float() but is never a valid probability/confidence.
    if isinstance(value, bool):
        raise ContractValidationError(f"{field} must be a finite number, not bool")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(numeric):
        raise ContractValidationError(f"{field} must be finite")
    if not 0.0 <= numeric <= 1.0:
        raise ContractValidationError(f"{field} must be within [0, 1]")
    return numeric


def validate_brainlive_output(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return BrainLiveOutputContract.model_validate(payload).model_dump(mode="python")
    except (ValidationError, ContractValidationError, ValueError) as exc:
        raise ContractValidationError(f"invalid BrainLive output: {exc}") from exc


def validate_outcome_batch(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return OutcomeBatchContract.model_validate(payload).model_dump(mode="python")
    except (ValidationError, ContractValidationError, ValueError) as exc:
        raise ContractValidationError(f"invalid outcome batch: {exc}") from exc


def horizon_due_at(occurred_at: str, horizon: str | Horizon) -> str:
    try:
        h = Horizon(horizon)
    except ValueError as exc:
        raise LifecycleError(f"unsupported horizon {horizon!r}") from exc
    return HORIZON_SPECS[h].due_at(occurred_at)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(con: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _table_columns(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


INTEGRITY_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS integrity_schema_migrations_v176(
  migration_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_envelopes_v176(
  event_id TEXT PRIMARY KEY,
  event_fingerprint TEXT NOT NULL UNIQUE,
  modality TEXT NOT NULL,
  source_device TEXT NOT NULL,
  source_event_id TEXT,
  source_sha256 TEXT,
  occurred_at TEXT NOT NULL,
  captured_at TEXT,
  received_at TEXT,
  processed_at TEXT,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  source_path TEXT,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('accepted','quarantined','superseded')),
  pipeline_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_envelopes_owner_time_v176
  ON event_envelopes_v176(person_id, occurred_at, modality);
CREATE INDEX IF NOT EXISTS idx_event_envelopes_session_time_v176
  ON event_envelopes_v176(live_session_id, occurred_at);

CREATE TABLE IF NOT EXISTS pipeline_runs_v176(
  run_id TEXT PRIMARY KEY,
  pipeline_name TEXT NOT NULL,
  person_id TEXT,
  live_session_id TEXT,
  mode TEXT NOT NULL CHECK(mode IN ('live','post_stop','replay','maintenance','migration')),
  as_of TEXT,
  status TEXT NOT NULL CHECK(status IN ('started','completed','failed','quarantined','partial','cancelled')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error_code TEXT,
  error_text TEXT,
  metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_v176_scope ON pipeline_runs_v176(person_id, live_session_id, started_at);

CREATE TABLE IF NOT EXISTS data_quarantine_v176(
  quarantine_id TEXT PRIMARY KEY,
  run_id TEXT,
  source_table TEXT,
  source_id TEXT,
  person_id TEXT,
  category TEXT NOT NULL,
  reason TEXT NOT NULL,
  raw_payload_json TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_quarantine_open_v176 ON data_quarantine_v176(resolved_at, category, created_at);

CREATE TABLE IF NOT EXISTS artifact_lineage_v176(
  lineage_id TEXT PRIMARY KEY,
  child_table TEXT NOT NULL,
  child_id TEXT NOT NULL,
  parent_table TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  source_version TEXT,
  invalidated_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(child_table, child_id, parent_table, parent_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_parent_v176 ON artifact_lineage_v176(parent_table, parent_id, invalidated_at);

CREATE TABLE IF NOT EXISTS brainlive_forecast_transitions_v176(
  transition_id TEXT PRIMARY KEY,
  forecast_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  previous_state TEXT,
  next_state TEXT NOT NULL,
  outcome_id TEXT,
  actor TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_transition_forecast_v176 ON brainlive_forecast_transitions_v176(forecast_id, created_at);
"""


def _reconcile_legacy_forecast_lifecycle(con: sqlite3.Connection) -> None:
    """Repair only deterministic legacy forecast/outcome relationships.

    Legacy V17.4 rows could retain ``status='open'`` after an outcome had been
    stored.  We do not delete historic rows or invent an outcome: we select the
    earliest owner-matching outcome as canonical, close the forecast from that
    evidence, and quarantine duplicate/orphan/cross-owner outcome rows.
    The operation is idempotent.
    """
    tables = {str(r["name"]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not {"brainlive_short_horizon_forecasts", "brainlive_prediction_outcomes"}.issubset(tables):
        return
    rows = con.execute(
        """
        SELECT
          o.outcome_id,o.forecast_id,o.person_id AS outcome_person_id,
          o.was_prediction_correct,o.created_at AS outcome_created_at,
          f.person_id AS forecast_person_id,f.lifecycle_state,f.status,
          f.canonical_outcome_id
        FROM brainlive_prediction_outcomes o
        LEFT JOIN brainlive_short_horizon_forecasts f ON f.forecast_id=o.forecast_id
        WHERE o.forecast_id IS NOT NULL
        ORDER BY o.forecast_id, COALESCE(o.created_at,''), o.outcome_id
        """
    ).fetchall()
    valid_by_forecast: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        outcome_id = str(row["outcome_id"])
        forecast_id = str(row["forecast_id"])
        if row["forecast_person_id"] is None:
            quarantine_in_transaction(
                con,
                category="legacy_orphan_forecast_outcome",
                reason="Outcome references no forecast; retained but unusable.",
                source_table="brainlive_prediction_outcomes",
                source_id=outcome_id,
                person_id=row["outcome_person_id"],
            )
            continue
        if str(row["forecast_person_id"]) != str(row["outcome_person_id"]):
            quarantine_in_transaction(
                con,
                category="legacy_cross_owner_forecast_outcome",
                reason="Outcome owner differs from forecast owner; retained but unusable.",
                source_table="brainlive_prediction_outcomes",
                source_id=outcome_id,
                person_id=row["outcome_person_id"],
            )
            continue
        valid_by_forecast.setdefault(forecast_id, []).append(row)

    for forecast_id, outcomes in valid_by_forecast.items():
        canonical = outcomes[0]
        prior = str(canonical["lifecycle_state"] or canonical["status"] or "open")
        was_correct = canonical["was_prediction_correct"]
        next_state = "evaluated_correct" if was_correct == 1 else "evaluated_incorrect" if was_correct == 0 else "indeterminate"
        if canonical["canonical_outcome_id"] != canonical["outcome_id"] or prior in FORECAST_ACTIVE_STATES:
            con.execute(
                """
                UPDATE brainlive_short_horizon_forecasts
                SET canonical_outcome_id=?, lifecycle_state=?, status=?, updated_at=?
                WHERE forecast_id=?
                """,
                (canonical["outcome_id"], next_state, next_state, now_iso(), forecast_id),
            )
            con.execute(
                """
                INSERT INTO brainlive_forecast_transitions_v176(
                  transition_id,forecast_id,person_id,previous_state,next_state,
                  outcome_id,actor,reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("fc_transition"),
                    forecast_id,
                    canonical["forecast_person_id"],
                    prior,
                    next_state,
                    canonical["outcome_id"],
                    "migration_v176",
                    "reconciled legacy forecast/outcome lifecycle",
                    now_iso(),
                ),
            )
        for duplicate in outcomes[1:]:
            quarantine_in_transaction(
                con,
                category="legacy_duplicate_forecast_outcome",
                reason=f"Canonical outcome is {canonical['outcome_id']}; duplicate retained but excluded.",
                source_table="brainlive_prediction_outcomes",
                source_id=duplicate["outcome_id"],
                person_id=duplicate["outcome_person_id"],
            )


def ensure_integrity_schema() -> None:
    """Install V17.6 additive migrations and compatibility protections.

    This function deliberately does not delete legacy data.  Rows that cannot be
    safely backfilled remain visible but are marked/quarantined rather than being
    reinterpreted as valid V17.6 forecasts.
    """
    init_db()
    with connect() as con:
        con.executescript(INTEGRITY_SCHEMA)
        existing = {str(row["name"]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "brainlive_short_horizon_forecasts" in existing:
            for name, ddl in {
                "occurred_at": "TEXT",
                "due_at": "TEXT",
                "lifecycle_state": "TEXT",
                "epistemic_confidence": "REAL",
                "evidence_quality": "REAL",
                "canonical_outcome_id": "TEXT",
                "dedupe_key_v176": "TEXT",
                "schema_version": "TEXT",
                "invalidated_at": "TEXT",
                "superseded_by": "TEXT",
            }.items():
                _add_column_if_missing(con, "brainlive_short_horizon_forecasts", name, ddl)
            con.execute(
                """
                UPDATE brainlive_short_horizon_forecasts
                SET lifecycle_state=CASE
                    WHEN status IN ('open','active','watching') THEN 'open'
                    WHEN status IN ('evaluated_correct','evaluated_incorrect','expired','indeterminate','quarantined','invalidated','superseded') THEN status
                    ELSE 'quarantined'
                END
                WHERE lifecycle_state IS NULL OR lifecycle_state=''
                """
            )
            con.execute("UPDATE brainlive_short_horizon_forecasts SET occurred_at=created_at WHERE occurred_at IS NULL OR occurred_at=''")
            # Do not fabricate exact V17.6 deadlines for legacy rows.  Old
            # timestamps may lack a timezone and their horizon contract was
            # different (H0 used 90 seconds).  They remain legacy/read-only
            # until an explicit migration/backfill verifies their provenance.
            con.execute("UPDATE brainlive_short_horizon_forecasts SET schema_version=COALESCE(schema_version, 'legacy_pre_17_6')")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bl_forecast_dedupe_v176 ON brainlive_short_horizon_forecasts(dedupe_key_v176) WHERE dedupe_key_v176 IS NOT NULL")
            con.execute("CREATE INDEX IF NOT EXISTS idx_bl_forecast_lifecycle_v176 ON brainlive_short_horizon_forecasts(person_id, lifecycle_state, due_at, occurred_at)")

        if "brainlive_short_horizon_forecasts" in existing:
            # V17.6 rows are append-only lifecycle objects, not arbitrary JSON
            # rows.  SQLite cannot add CHECK constraints to an existing table,
            # so compatibility triggers enforce the non-negotiable invariants.
            con.executescript(
                """
                DROP TRIGGER IF EXISTS trg_bl_forecast_validate_insert_v176;
                CREATE TRIGGER trg_bl_forecast_validate_insert_v176
                BEFORE INSERT ON brainlive_short_horizon_forecasts
                WHEN NEW.schema_version='17.6.0' AND (
                  NEW.horizon NOT IN ('H0','H1','H2')
                  OR NEW.lifecycle_state NOT IN ('open','due','evaluated_correct','evaluated_incorrect','expired','indeterminate','quarantined','invalidated','superseded')
                  OR NEW.status<>NEW.lifecycle_state
                  OR NEW.occurred_at IS NULL OR NEW.due_at IS NULL
                  OR julianday(NEW.occurred_at) IS NULL OR julianday(NEW.due_at) IS NULL
                  OR julianday(NEW.due_at)<=julianday(NEW.occurred_at)
                  OR NEW.probability IS NULL OR NEW.probability<0 OR NEW.probability>1
                  OR NEW.epistemic_confidence IS NULL OR NEW.epistemic_confidence<0 OR NEW.epistemic_confidence>1
                  OR NOT EXISTS(SELECT 1 FROM brainlive_sessions s WHERE s.live_session_id=NEW.live_session_id AND s.person_id=NEW.person_id)
                )
                BEGIN
                  SELECT RAISE(ABORT, 'invalid V17.6 forecast invariant');
                END;

                DROP TRIGGER IF EXISTS trg_bl_forecast_validate_update_v176;
                CREATE TRIGGER trg_bl_forecast_validate_update_v176
                BEFORE UPDATE OF lifecycle_state,status,canonical_outcome_id ON brainlive_short_horizon_forecasts
                WHEN OLD.schema_version='17.6.0' AND (
                  NEW.lifecycle_state NOT IN ('open','due','evaluated_correct','evaluated_incorrect','expired','indeterminate','quarantined','invalidated','superseded')
                  OR NEW.status<>NEW.lifecycle_state
                  OR (NEW.lifecycle_state IN ('evaluated_correct','evaluated_incorrect','indeterminate') AND NEW.canonical_outcome_id IS NULL)
                  OR (NEW.lifecycle_state IN ('open','due') AND NEW.canonical_outcome_id IS NOT NULL)
                )
                BEGIN
                  SELECT RAISE(ABORT, 'illegal V17.6 forecast lifecycle transition');
                END;
                """
            )

        if "brainlive_prediction_outcomes" in existing and "brainlive_short_horizon_forecasts" in existing:
            # First repair deterministic historical lifecycle contradictions, then
            # prevent a new orphan, cross-owner, duplicate, or temporally
            # impossible outcome from reaching the table.
            _reconcile_legacy_forecast_lifecycle(con)
            con.executescript(
                """
                DROP TRIGGER IF EXISTS trg_bl_outcome_requires_forecast_v176;
                CREATE TRIGGER trg_bl_outcome_requires_forecast_v176
                BEFORE INSERT ON brainlive_prediction_outcomes
                WHEN NEW.forecast_id IS NOT NULL AND (
                  NOT EXISTS(SELECT 1 FROM brainlive_short_horizon_forecasts f WHERE f.forecast_id=NEW.forecast_id)
                  OR EXISTS(SELECT 1 FROM brainlive_short_horizon_forecasts f WHERE f.forecast_id=NEW.forecast_id AND f.person_id<>NEW.person_id)
                  OR EXISTS(SELECT 1 FROM brainlive_short_horizon_forecasts f WHERE f.forecast_id=NEW.forecast_id AND f.canonical_outcome_id IS NOT NULL)
                  OR EXISTS(SELECT 1 FROM brainlive_short_horizon_forecasts f
                            WHERE f.forecast_id=NEW.forecast_id
                              AND f.schema_version='17.6.0'
                              AND (julianday(NEW.observed_after) IS NULL OR julianday(NEW.observed_after)<julianday(f.occurred_at)))
                )
                BEGIN
                  SELECT RAISE(ABORT, 'invalid forecast outcome: missing, cross-owner, duplicate, or predated');
                END;

                DROP TRIGGER IF EXISTS trg_bl_outcome_record_transition_v176;
                CREATE TRIGGER trg_bl_outcome_record_transition_v176
                BEFORE INSERT ON brainlive_prediction_outcomes
                WHEN NEW.forecast_id IS NOT NULL
                BEGIN
                  INSERT INTO brainlive_forecast_transitions_v176(
                    transition_id,forecast_id,person_id,previous_state,next_state,
                    outcome_id,actor,reason,created_at
                  )
                  SELECT
                    lower(hex(randomblob(16))), f.forecast_id, NEW.person_id,
                    COALESCE(f.lifecycle_state,f.status,'open'),
                    CASE WHEN NEW.was_prediction_correct=1 THEN 'evaluated_correct'
                         WHEN NEW.was_prediction_correct=0 THEN 'evaluated_incorrect'
                         ELSE 'indeterminate' END,
                    NEW.outcome_id, 'outcome_writer', 'outcome recorded', NEW.created_at
                  FROM brainlive_short_horizon_forecasts f
                  WHERE f.forecast_id=NEW.forecast_id;
                END;

                DROP TRIGGER IF EXISTS trg_bl_outcome_closes_forecast_v176;
                CREATE TRIGGER trg_bl_outcome_closes_forecast_v176
                AFTER INSERT ON brainlive_prediction_outcomes
                WHEN NEW.forecast_id IS NOT NULL
                BEGIN
                  UPDATE brainlive_short_horizon_forecasts
                  SET
                    canonical_outcome_id=NEW.outcome_id,
                    lifecycle_state=CASE
                      WHEN NEW.was_prediction_correct=1 THEN 'evaluated_correct'
                      WHEN NEW.was_prediction_correct=0 THEN 'evaluated_incorrect'
                      ELSE 'indeterminate'
                    END,
                    status=CASE
                      WHEN NEW.was_prediction_correct=1 THEN 'evaluated_correct'
                      WHEN NEW.was_prediction_correct=0 THEN 'evaluated_incorrect'
                      ELSE 'indeterminate'
                    END,
                    updated_at=NEW.created_at
                  WHERE forecast_id=NEW.forecast_id;
                END;
                """
            )
        migration_id = "v176_integrity_kernel"
        con.execute(
            """
            INSERT INTO integrity_schema_migrations_v176(migration_id, applied_at, details_json)
            VALUES(?,?,?)
            ON CONFLICT(migration_id) DO UPDATE SET applied_at=excluded.applied_at, details_json=excluded.details_json
            """,
            (migration_id, now_iso(), json_dumps({"version": INTEGRITY_SCHEMA_VERSION})),
        )
        con.commit()


def quarantine_in_transaction(
    con: sqlite3.Connection,
    *,
    category: str,
    reason: str,
    raw_payload: Any | None = None,
    run_id: str | None = None,
    source_table: str | None = None,
    source_id: str | None = None,
    person_id: str | None = None,
) -> str:
    """Write a quarantine record using the caller's transaction."""
    existing = con.execute(
        """SELECT quarantine_id FROM data_quarantine_v176
           WHERE category=?
             AND COALESCE(source_table,'')=COALESCE(?, '')
             AND COALESCE(source_id,'')=COALESCE(?, '')
             AND COALESCE(run_id,'')=COALESCE(?, '')
             AND resolved_at IS NULL
           LIMIT 1""",
        (category, source_table, source_id, run_id),
    ).fetchone()
    if existing:
        return str(existing["quarantine_id"])
    qid = new_id("quarantine")
    con.execute(
        """
        INSERT INTO data_quarantine_v176(
          quarantine_id, run_id, source_table, source_id, person_id,
          category, reason, raw_payload_json, created_at, resolved_at, resolution_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            qid,
            run_id,
            source_table,
            source_id,
            person_id,
            category,
            reason,
            json_dumps(raw_payload) if raw_payload is not None else None,
            now_iso(),
            None,
            None,
        ),
    )
    return qid


def quarantine(
    *,
    category: str,
    reason: str,
    raw_payload: Any | None = None,
    run_id: str | None = None,
    source_table: str | None = None,
    source_id: str | None = None,
    person_id: str | None = None,
) -> str:
    ensure_integrity_schema()
    with connect() as con:
        qid = quarantine_in_transaction(
            con,
            category=category,
            reason=reason,
            raw_payload=raw_payload,
            run_id=run_id,
            source_table=source_table,
            source_id=source_id,
            person_id=person_id,
        )
        con.commit()
    return qid


def record_event_envelope(
    *,
    modality: str,
    source_device: str,
    person_id: str,
    occurred_at: str,
    payload: dict[str, Any],
    source_event_id: str | None = None,
    source_sha256: str | None = None,
    captured_at: str | None = None,
    received_at: str | None = None,
    processed_at: str | None = None,
    live_session_id: str | None = None,
    source_path: str | None = None,
    pipeline_version: str = INTEGRITY_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Insert an immutable envelope; dedupe by a true occurrence fingerprint.

    Hash-only dedupe is deliberately prohibited.  Two identical pieces of audio
    captured at different times are different occurrences.
    """
    ensure_integrity_schema()
    occurred = iso_utc(parse_iso_utc(occurred_at))
    if captured_at:
        captured = iso_utc(parse_iso_utc(captured_at))
    else:
        captured = None
    if not modality or not source_device or not person_id:
        raise IntegrityError("modality, source_device and person_id are required")
    payload_json = json_dumps(payload)
    fingerprint_parts = [source_device, modality, source_event_id or "", source_sha256 or "", occurred]
    fingerprint = _sha256_text("\x1f".join(fingerprint_parts))
    now = now_iso()
    payload_sha256 = _sha256_text(payload_json)
    with connect() as con:
        row = con.execute("SELECT * FROM event_envelopes_v176 WHERE event_fingerprint=?", (fingerprint,)).fetchone()
        if row:
            # A duplicate transport delivery is idempotent only when it carries
            # exactly the same immutable source payload and owner/session scope.
            # Reusing a device event id with changed metadata used to silently
            # return the old event, hiding a provenance conflict forever.
            conflicts: list[str] = []
            if str(row["payload_sha256"] or "") != payload_sha256:
                conflicts.append("payload_sha256")
            if str(row["person_id"] or "") != str(person_id):
                conflicts.append("person_id")
            if (row["live_session_id"] or None) != (live_session_id or None):
                conflicts.append("live_session_id")
            if conflicts:
                quarantine_in_transaction(
                    con,
                    category="event_envelope_conflict",
                    reason="immutable event fingerprint was replayed with conflicting " + ", ".join(conflicts),
                    raw_payload={"incoming": payload, "existing_event_id": row["event_id"], "conflicts": conflicts},
                    source_table="event_envelopes_v176",
                    source_id=str(row["event_id"]),
                    person_id=person_id,
                )
                con.commit()
                raise IntegrityError("event envelope conflict; incoming payload quarantined")
            return {"event_id": row["event_id"], "created": False, "event_fingerprint": fingerprint}
        event_id = new_id("evt")
        con.execute(
            """
            INSERT INTO event_envelopes_v176(
              event_id,event_fingerprint,modality,source_device,source_event_id,source_sha256,
              occurred_at,captured_at,received_at,processed_at,person_id,live_session_id,
              source_path,payload_json,payload_sha256,status,pipeline_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                fingerprint,
                modality,
                source_device,
                source_event_id,
                source_sha256,
                occurred,
                captured,
                received_at,
                processed_at,
                person_id,
                live_session_id,
                source_path,
                payload_json,
                payload_sha256,
                "accepted",
                pipeline_version,
                now,
                now,
            ),
        )
        con.commit()
    return {"event_id": event_id, "created": True, "event_fingerprint": fingerprint}


def create_forecast(
    con: sqlite3.Connection,
    *,
    live_session_id: str,
    person_id: str,
    event_id: str | None,
    run_id: str | None,
    payload: dict[str, Any],
    occurred_at: str,
    source: str,
) -> dict[str, Any]:
    """Persist one forecast through a single validated, idempotent writer."""
    try:
        forecast = ForecastContract.model_validate(payload)
    except (ValidationError, ContractValidationError, ValueError) as exc:
        raise ContractValidationError(f"invalid forecast: {exc}") from exc
    occurred = iso_utc(parse_iso_utc(occurred_at))
    session = con.execute(
        "SELECT person_id,status FROM brainlive_sessions WHERE live_session_id=?", (live_session_id,)
    ).fetchone()
    if not session:
        raise IntegrityError(f"unknown live_session_id {live_session_id!r}")
    if str(session["person_id"]) != person_id:
        raise OwnershipError("forecast person_id does not own live_session_id")
    horizon = Horizon(forecast.horizon)
    spec = HORIZON_SPECS[horizon]
    prediction_text = (
        forecast.predicted_action
        or forecast.predicted_need
        or forecast.predicted_risk
        or forecast.predicted_opportunity
        or forecast.predicted_words
        or forecast.predicted_emotion
        or ""
    )
    dedupe_key = _sha256_text(
        "\x1f".join(
            [
                live_session_id,
                run_id or "no_run",
                horizon.value,
                forecast.forecast_type,
                prediction_text,
                occurred,
                source,
            ]
        )
    )
    existing = con.execute(
        "SELECT * FROM brainlive_short_horizon_forecasts WHERE dedupe_key_v176=?", (dedupe_key,)
    ).fetchone()
    if existing:
        return dict(existing)
    now = now_iso()
    evidence_quality = min(1.0, 0.2 * len(forecast.evidence) + 0.1 * len(forecast.counter_evidence))
    fid = new_id("blfc")
    values = {
        "forecast_id": fid,
        "live_session_id": live_session_id,
        "event_id": event_id,
        "person_id": person_id,
        "horizon": horizon.value,
        "forecast_type": forecast.forecast_type,
        "predicted_need": forecast.predicted_need,
        "predicted_action": forecast.predicted_action,
        "predicted_words": forecast.predicted_words,
        "predicted_emotion": forecast.predicted_emotion,
        "predicted_risk": forecast.predicted_risk,
        "predicted_opportunity": forecast.predicted_opportunity,
        "if_intervene_future": forecast.if_intervene_future,
        "if_silent_future": forecast.if_silent_future,
        "expected_gain": forecast.expected_gain,
        "probability": forecast.probability,
        # Legacy confidence retains the epistemic value for consumers that have
        # not yet moved to the explicit V17.6 column.
        "confidence": forecast.confidence,
        "evidence_json": json_dumps(forecast.evidence),
        "counter_evidence_json": json_dumps(forecast.counter_evidence),
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "occurred_at": occurred,
        "due_at": spec.due_at(occurred),
        "lifecycle_state": "open",
        "epistemic_confidence": forecast.confidence,
        "evidence_quality": evidence_quality,
        "canonical_outcome_id": None,
        "dedupe_key_v176": dedupe_key,
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "invalidated_at": None,
        "superseded_by": None,
    }
    cols = list(values)
    con.execute(
        f"INSERT INTO brainlive_short_horizon_forecasts({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
        tuple(values[c] for c in cols),
    )
    con.execute(
        """
        INSERT INTO brainlive_forecast_transitions_v176(
          transition_id,forecast_id,person_id,previous_state,next_state,outcome_id,actor,reason,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (new_id("fc_transition"), fid, person_id, None, "open", None, source, "forecast created", now),
    )
    return values


def record_forecast_outcome(
    con: sqlite3.Connection,
    *,
    forecast_id: str,
    person_id: str,
    observed_after: str,
    was_prediction_correct: bool | None,
    match_score: float | None,
    outcome_window: str,
    actor: str,
    user_feedback: str | None = None,
    lesson: dict[str, Any] | None = None,
    evidence: list[Any] | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Close a forecast exactly once, owner-scoped and in the same transaction."""
    forecast = con.execute(
        "SELECT * FROM brainlive_short_horizon_forecasts WHERE forecast_id=?", (forecast_id,)
    ).fetchone()
    if not forecast:
        raise LifecycleError(f"forecast not found: {forecast_id}")
    if str(forecast["person_id"]) != person_id:
        raise OwnershipError("outcome person_id does not own forecast")
    current_state = str(forecast["lifecycle_state"] or forecast["status"] or "open")
    if current_state not in FORECAST_ACTIVE_STATES or forecast["canonical_outcome_id"]:
        raise LifecycleError(f"forecast {forecast_id} is not active: {current_state}")
    if match_score is not None:
        match_score = unit_interval(match_score, field="outcome.match_score")
    # Outcome evidence is an observation; it must not claim an event before the
    # forecast's creation/occurrence time.
    observed_dt = parse_iso_utc(observed_after)
    occurred_dt = parse_iso_utc(str(forecast["occurred_at"] or forecast["created_at"]))
    if observed_dt < occurred_dt:
        raise TimestampError("outcome observation predates forecast occurrence")
    now = now_iso()
    outcome_id = new_id("blout")
    con.execute(
        """
        INSERT INTO brainlive_prediction_outcomes(
          outcome_id,live_session_id,forecast_id,candidate_id,person_id,observed_after,
          outcome_window,was_prediction_correct,match_score,user_feedback,lesson_json,evidence_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            outcome_id,
            forecast["live_session_id"],
            forecast_id,
            candidate_id,
            person_id,
            iso_utc(observed_dt),
            outcome_window,
            None if was_prediction_correct is None else (1 if was_prediction_correct else 0),
            match_score,
            user_feedback,
            json_dumps(lesson or {}),
            json_dumps(evidence or []),
            now,
        ),
    )
    # The schema trigger closes the forecast and writes a transition.  Re-read to
    # return the canonical state, not an assumption made in Python.
    row = con.execute(
        "SELECT * FROM brainlive_short_horizon_forecasts WHERE forecast_id=?", (forecast_id,)
    ).fetchone()
    return {"outcome_id": outcome_id, "forecast": dict(row) if row else None, "actor": actor}


def mark_forecast_expired(con: sqlite3.Connection, *, forecast_id: str, person_id: str, actor: str, reason: str) -> None:
    row = con.execute("SELECT * FROM brainlive_short_horizon_forecasts WHERE forecast_id=?", (forecast_id,)).fetchone()
    if not row:
        raise LifecycleError(f"forecast not found: {forecast_id}")
    if str(row["person_id"]) != person_id:
        raise OwnershipError("cannot expire another owner's forecast")
    current = str(row["lifecycle_state"] or row["status"] or "open")
    if current not in FORECAST_ACTIVE_STATES:
        return
    now = now_iso()
    con.execute(
        """
        UPDATE brainlive_short_horizon_forecasts
        SET lifecycle_state='expired',status='expired',updated_at=?
        WHERE forecast_id=?
        """,
        (now, forecast_id),
    )
    con.execute(
        """
        INSERT INTO brainlive_forecast_transitions_v176(
          transition_id,forecast_id,person_id,previous_state,next_state,outcome_id,actor,reason,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (new_id("fc_transition"), forecast_id, person_id, current, "expired", None, actor, reason, now),
    )


def transition_due_forecasts(con: sqlite3.Connection, *, as_of: str, person_id: str | None = None) -> int:
    as_of_dt = parse_iso_utc(as_of)
    params: list[Any] = [iso_utc(as_of_dt)]
    where = "lifecycle_state='open' AND due_at IS NOT NULL AND due_at<=?"
    if person_id:
        where += " AND person_id=?"
        params.append(person_id)
    candidates = con.execute(
        f"SELECT forecast_id,person_id FROM brainlive_short_horizon_forecasts WHERE {where}", tuple(params)
    ).fetchall()
    if not candidates:
        return 0
    now = now_iso()
    for row in candidates:
        con.execute(
            "UPDATE brainlive_short_horizon_forecasts SET lifecycle_state='due',status='due',updated_at=? WHERE forecast_id=?",
            (now, row["forecast_id"]),
        )
        con.execute(
            """
            INSERT INTO brainlive_forecast_transitions_v176(
              transition_id,forecast_id,person_id,previous_state,next_state,outcome_id,actor,reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (new_id("fc_transition"), row["forecast_id"], row["person_id"], "open", "due", None, "scheduler", "forecast due", now),
        )
    return len(candidates)


def active_forecast_sql(alias: str = "f") -> str:
    """Reusable safe selector for live/context code.

    Unbackfilled legacy forecasts are intentionally not live-active: their old
    horizon contract and event time may be unknown, so retaining ``status=open``
    would make them immortal context pollution.  They remain queryable for audit
    and explicit migration, but only V17.6 lifecycle rows can influence a live
    decision.
    """
    state = f"COALESCE({alias}.lifecycle_state, CASE WHEN {alias}.status IN ('open','active','watching') THEN 'open' ELSE {alias}.status END)"
    return f"({state} IN ('open','due') AND {alias}.schema_version='{INTEGRITY_SCHEMA_VERSION}' AND {alias}.due_at IS NOT NULL)"


def integrity_audit_v176() -> dict[str, Any]:
    """Report executable invariants for the V17.6 core migration."""
    ensure_integrity_schema()
    with connect() as con:
        tables = {str(r["name"]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        checks: dict[str, int] = {}
        if "brainlive_short_horizon_forecasts" in tables:
            checks["invalid_horizon"] = int(
                con.execute("SELECT COUNT(*) AS c FROM brainlive_short_horizon_forecasts WHERE horizon NOT IN ('H0','H1','H2') OR horizon IS NULL").fetchone()["c"]
            )
            checks["invalid_lifecycle_state"] = int(
                con.execute(
                    "SELECT COUNT(*) AS c FROM brainlive_short_horizon_forecasts WHERE lifecycle_state IS NULL OR lifecycle_state NOT IN ('open','due','evaluated_correct','evaluated_incorrect','expired','indeterminate','quarantined','invalidated','superseded')"
                ).fetchone()["c"]
            )
            checks["terminal_forecast_still_active"] = int(
                con.execute(
                    "SELECT COUNT(*) AS c FROM brainlive_short_horizon_forecasts WHERE canonical_outcome_id IS NOT NULL AND lifecycle_state IN ('open','due')"
                ).fetchone()["c"]
            )
            checks["v176_forecast_missing_due_at"] = int(
                con.execute(
                    "SELECT COUNT(*) AS c FROM brainlive_short_horizon_forecasts WHERE schema_version='17.6.0' AND (due_at IS NULL OR occurred_at IS NULL)"
                ).fetchone()["c"]
            )
        if "brainlive_prediction_outcomes" in tables and "brainlive_short_horizon_forecasts" in tables:
            checks["orphan_outcome"] = int(
                con.execute(
                    "SELECT COUNT(*) AS c FROM brainlive_prediction_outcomes o LEFT JOIN brainlive_short_horizon_forecasts f ON f.forecast_id=o.forecast_id WHERE o.forecast_id IS NOT NULL AND f.forecast_id IS NULL"
                ).fetchone()["c"]
            )
            checks["cross_owner_outcome"] = int(
                con.execute(
                    "SELECT COUNT(*) AS c FROM brainlive_prediction_outcomes o JOIN brainlive_short_horizon_forecasts f ON f.forecast_id=o.forecast_id WHERE o.person_id<>f.person_id"
                ).fetchone()["c"]
            )
        checks["open_quarantine_records"] = int(
            con.execute("SELECT COUNT(*) AS c FROM data_quarantine_v176 WHERE resolved_at IS NULL").fetchone()["c"]
        )
        critical = {k: v for k, v in checks.items() if k not in {"open_quarantine_records"} and v}
        status = "violations_detected" if critical else "quarantine_review_required" if checks.get("open_quarantine_records", 0) else "ok"
        return {
            "version": INTEGRITY_SCHEMA_VERSION,
            "status": status,
            "checks": checks,
            "critical_violations": critical,
            "release_blockers": critical | ({"open_quarantine_records": checks["open_quarantine_records"]} if checks.get("open_quarantine_records") else {}),
        }
