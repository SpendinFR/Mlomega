from __future__ import annotations

"""V18.4 runtime hardening primitives.

This module contains the small set of cross-cutting invariants that must be
shared by the active BrainLive, V13--V17 bridges and service workers:

* source-addressable, idempotent vision occurrences;
* bounded episode capsules with immutable provenance;
* durable LLM decision runs and retry/replay state;
* semantic evidence checks before any cognitive artefact is persisted.

It intentionally does not replace V13--V17 business logic.  It gives their
existing calls one durable, auditable boundary.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
from typing import Any, Iterable, Mapping

from .db import connect, init_db, write_transaction
from .governance_v18 import Scope, ensure_v18_schema
from .integrity_v176 import ContractValidationError, iso_utc, parse_iso_utc
from .utils import json_dumps, json_loads, now_iso, stable_id


VERSION = "18.4.0-runtime-hardening"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS v18_vision_occurrence_map(
  occurrence_key TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  source_event_id TEXT,
  source_sha256 TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  source_path TEXT,
  frame_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  source_item_id TEXT NOT NULL,
  observation_id TEXT,
  state TEXT NOT NULL CHECK(state IN ('captured','completed','quarantined')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v18_vision_occurrence_scope
  ON v18_vision_occurrence_map(person_id,live_session_id,source_event_id,captured_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v18_vision_event_scope
  ON v18_vision_occurrence_map(person_id,live_session_id,source_event_id)
  WHERE source_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS v18_episode_capsules(
  capsule_id TEXT PRIMARY KEY,
  capsule_key TEXT NOT NULL UNIQUE,
  person_id TEXT NOT NULL,
  live_session_id TEXT,
  source_key TEXT NOT NULL,
  as_of TEXT NOT NULL,
  episode_start_at TEXT,
  episode_end_at TEXT,
  input_budget_chars INTEGER NOT NULL,
  output_budget_tokens INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','context_incomplete','superseded','quarantined')),
  summary_text TEXT,
  turns_json TEXT NOT NULL DEFAULT '[]',
  references_json TEXT NOT NULL DEFAULT '[]',
  omissions_json TEXT NOT NULL DEFAULT '[]',
  capsule_json TEXT NOT NULL DEFAULT '{}',
  capsule_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v18_capsule_scope
  ON v18_episode_capsules(person_id,live_session_id,source_key,created_at DESC);

CREATE TABLE IF NOT EXISTS v18_llm_decision_runs(
  decision_run_id TEXT PRIMARY KEY,
  decision_key TEXT NOT NULL UNIQUE,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  capsule_id TEXT NOT NULL,
  capsule_hash TEXT NOT NULL,
  execution_mode TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  model TEXT,
  generation_json TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL CHECK(state IN ('pending','claimed','running','succeeded','retryable_error','repair_requested','quarantined','terminal_error','cancelled')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  repair_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  next_attempt_at TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  raw_output_text TEXT,
  raw_output_sha256 TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  error_kind TEXT,
  error_text TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  FOREIGN KEY(capsule_id) REFERENCES v18_episode_capsules(capsule_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_llm_run_claim
  ON v18_llm_decision_runs(state,next_attempt_at,lease_expires_at,person_id,live_session_id);

CREATE TABLE IF NOT EXISTS v18_llm_decision_attempts(
  attempt_id TEXT PRIMARY KEY,
  decision_run_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN ('initial','repair','retry')),
  state TEXT NOT NULL CHECK(state IN ('started','succeeded','retryable_error','quarantined','terminal_error')),
  raw_output_text TEXT,
  raw_output_sha256 TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  error_kind TEXT,
  error_text TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(decision_run_id,attempt_no),
  FOREIGN KEY(decision_run_id) REFERENCES v18_llm_decision_runs(decision_run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS v18_hot_delivery_links(
  delivery_link_id TEXT PRIMARY KEY,
  decision_run_id TEXT,
  hot_intervention_id TEXT,
  delivery_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(delivery_id),
  UNIQUE(decision_run_id, hot_intervention_id)
);

CREATE TABLE IF NOT EXISTS v18_capsule_prompt_renderings(
  capsule_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  input_budget_chars INTEGER NOT NULL,
  rendered_input_chars INTEGER NOT NULL,
  output_budget_tokens INTEGER NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  incomplete INTEGER NOT NULL DEFAULT 0,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(capsule_id) REFERENCES v18_episode_capsules(capsule_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_capsule_prompt_scope
  ON v18_capsule_prompt_renderings(person_id,live_session_id,updated_at DESC);

CREATE TABLE IF NOT EXISTS v18_llm_evidence_requests(
  evidence_request_id TEXT PRIMARY KEY,
  decision_run_id TEXT NOT NULL,
  capsule_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  live_session_id TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('requested','resolved','unavailable','cancelled')),
  request_reason TEXT,
  resolution_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  UNIQUE(decision_run_id,source_table,source_id),
  FOREIGN KEY(decision_run_id) REFERENCES v18_llm_decision_runs(decision_run_id),
  FOREIGN KEY(capsule_id) REFERENCES v18_episode_capsules(capsule_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_evidence_request_pending
  ON v18_llm_evidence_requests(state,person_id,live_session_id,created_at);
"""


def ensure_runtime_hardening_schema() -> None:
    """Install additive V18.4 state without removing any V13--V17 table."""
    init_db()
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)


def _hash(value: Any) -> str:
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
    else:
        raw = json_dumps(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def bounded_text(value: Any, *, limit: int = 12000) -> str:
    text = "" if value is None else str(value)
    return text[: max(0, int(limit))]


def vision_occurrence_key(*, live_session_id: str | None, source_event_id: str | None,
                           captured_at: str, source_sha256: str, source_path: str) -> str:
    # A source-event identity is authoritative across transport retries.  The
    # receiver may assign a different local file path on every attempt, so a
    # path must never participate in this key when the capture already has a
    # stable Android/Phone event ID.  Reuse is still guarded by the content,
    # timestamp and scope checks in ``reserve_vision_occurrence``.
    if source_event_id:
        return stable_id(
            "v18visionoccurrence",
            live_session_id or "no-session",
            "source-event",
            str(source_event_id),
        )
    # Older/manual callers without an event ID retain content/time/path identity
    # so independent captures are not incorrectly collapsed.
    return stable_id(
        "v18visionoccurrence",
        live_session_id or "no-session",
        "legacy-content",
        captured_at,
        source_sha256,
        source_path,
    )


def reserve_vision_occurrence(
    con: Any,
    *,
    occurrence_key: str,
    person_id: str,
    live_session_id: str | None,
    source_event_id: str | None,
    source_sha256: str,
    captured_at: str,
    source_path: str,
    frame_id: str,
    asset_id: str,
    source_item_id: str,
) -> dict[str, Any]:
    """Reserve or return one immutable vision occurrence inside caller txn.

    The reservation and the facts must share the same transaction.  A retry
    sees the exact same IDs; a collision with altered source content is refused.
    """
    row = con.execute(
        "SELECT * FROM v18_vision_occurrence_map WHERE occurrence_key=?",
        (occurrence_key,),
    ).fetchone()
    if row:
        existing = dict(row)
        if (
            str(existing.get("person_id")) != str(person_id)
            or str(existing.get("live_session_id") or "") != str(live_session_id or "")
            or str(existing.get("source_sha256")) != str(source_sha256)
            or str(existing.get("captured_at")) != str(captured_at)
        ):
            raise ContractValidationError("vision source identity collision across scope/content/time")
        return {**existing, "reused": True}
    now = now_iso()
    con.execute(
        """INSERT INTO v18_vision_occurrence_map(
               occurrence_key,person_id,live_session_id,source_event_id,source_sha256,captured_at,source_path,
               frame_id,asset_id,source_item_id,observation_id,state,created_at,updated_at
             ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            occurrence_key, person_id, live_session_id, source_event_id, source_sha256, captured_at, source_path,
            frame_id, asset_id, source_item_id, None, "captured", now, now,
        ),
    )
    return {
        "occurrence_key": occurrence_key,
        "person_id": person_id,
        "live_session_id": live_session_id,
        "source_event_id": source_event_id,
        "source_sha256": source_sha256,
        "captured_at": captured_at,
        "source_path": source_path,
        "frame_id": frame_id,
        "asset_id": asset_id,
        "source_item_id": source_item_id,
        "observation_id": None,
        "state": "captured",
        "reused": False,
    }


def complete_vision_occurrence(con: Any, *, occurrence_key: str, observation_id: str | None = None) -> None:
    now = now_iso()
    con.execute(
        """UPDATE v18_vision_occurrence_map
           SET observation_id=COALESCE(?,observation_id), state='completed', updated_at=?
           WHERE occurrence_key=?""",
        (observation_id, now, occurrence_key),
    )


def persist_episode_capsule(
    *,
    person_id: str,
    live_session_id: str,
    source_key: str,
    as_of: str,
    turns: Iterable[Mapping[str, Any]],
    summary_text: str | None,
    references: Iterable[Mapping[str, Any]],
    omissions: Iterable[Mapping[str, Any]],
    input_budget_chars: int,
    output_budget_tokens: int,
    status: str = "ready",
    episode_start_at: str | None = None,
    episode_end_at: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an immutable, bounded live capsule and return its identity."""
    ensure_runtime_hardening_schema()
    as_of = iso_utc(parse_iso_utc(as_of))
    turns_list = [dict(v) for v in turns]
    refs_list = [dict(v) for v in references]
    omit_list = [dict(v) for v in omissions]
    capsule_payload = {
        "schema_version": VERSION,
        "person_id": person_id,
        "live_session_id": live_session_id,
        "source_key": source_key,
        "as_of": as_of,
        "episode_start_at": episode_start_at,
        "episode_end_at": episode_end_at,
        "summary": summary_text or "",
        "turns": turns_list,
        "references": refs_list,
        "omissions": omit_list,
        "input_budget_chars": int(input_budget_chars),
        "output_budget_tokens": int(output_budget_tokens),
        "extra": dict(extra or {}),
    }
    capsule_hash = _hash(capsule_payload)
    capsule_key = stable_id("v18capsule", person_id, live_session_id, source_key, capsule_hash)
    capsule_id = stable_id("v18capsuleid", capsule_key)
    now = now_iso()
    with connect() as con, write_transaction(con):
        current = con.execute("SELECT * FROM v18_episode_capsules WHERE capsule_key=?", (capsule_key,)).fetchone()
        if current:
            row = dict(current)
            return {**row, "created": False}
        con.execute(
            """INSERT INTO v18_episode_capsules(
                 capsule_id,capsule_key,person_id,live_session_id,source_key,as_of,episode_start_at,episode_end_at,
                 input_budget_chars,output_budget_tokens,status,summary_text,turns_json,references_json,omissions_json,
                 capsule_json,capsule_hash,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                capsule_id, capsule_key, person_id, live_session_id, source_key, as_of, episode_start_at, episode_end_at,
                int(input_budget_chars), int(output_budget_tokens), status, summary_text or "", json_dumps(turns_list),
                json_dumps(refs_list), json_dumps(omit_list), json_dumps(capsule_payload), capsule_hash, now, now,
            ),
        )
    return {"capsule_id": capsule_id, "capsule_key": capsule_key, "capsule_hash": capsule_hash, "created": True, **capsule_payload}



def record_capsule_prompt_rendering(
    *,
    capsule_id: str,
    person_id: str,
    live_session_id: str,
    input_budget_chars: int,
    rendered_input_chars: int,
    output_budget_tokens: int,
    prompt_payload: Mapping[str, Any],
    incomplete: bool,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the exact bounded prompt accounting used by a live decision.

    A declared budget is not enough: strict release checks can now prove that the
    serialized prompt was not larger than the configured boundary.
    """
    ensure_runtime_hardening_schema()
    budget = max(1, int(input_budget_chars))
    rendered = max(0, int(rendered_input_chars))
    if rendered > budget:
        raise ContractValidationError(f"capsule prompt exceeds budget: {rendered}>{budget}")
    now = now_iso()
    payload_hash = _hash(prompt_payload)
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO v18_capsule_prompt_renderings(
                   capsule_id,person_id,live_session_id,input_budget_chars,rendered_input_chars,
                   output_budget_tokens,prompt_sha256,incomplete,details_json,created_at,updated_at
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(capsule_id) DO UPDATE SET
                   input_budget_chars=excluded.input_budget_chars,
                   rendered_input_chars=excluded.rendered_input_chars,
                   output_budget_tokens=excluded.output_budget_tokens,
                   prompt_sha256=excluded.prompt_sha256,
                   incomplete=excluded.incomplete,
                   details_json=excluded.details_json,
                   updated_at=excluded.updated_at""",
            (
                capsule_id, person_id, live_session_id, budget, rendered,
                max(1, int(output_budget_tokens)), payload_hash, 1 if incomplete else 0,
                json_dumps(dict(details or {})), now, now,
            ),
        )
        row = dict(con.execute("SELECT * FROM v18_capsule_prompt_renderings WHERE capsule_id=?", (capsule_id,)).fetchone())
    return row


def record_llm_evidence_requests(
    *,
    decision_run_id: str,
    capsule_id: str,
    person_id: str,
    live_session_id: str,
    refs: Iterable[Mapping[str, Any]],
    reason: str = "model_requested_evidence",
) -> list[str]:
    """Record bounded, scope-bound evidence requests without fabricating facts."""
    ensure_runtime_hardening_schema()
    ids: list[str] = []
    now = now_iso()
    with connect() as con, write_transaction(con):
        for ref in list(refs)[:4]:
            if not isinstance(ref, Mapping):
                continue
            table = str(ref.get("source_table") or "")
            source_id = str(ref.get("source_id") or "")
            if not table or not source_id:
                raise ContractValidationError("evidence request needs source_table and source_id")
            request_id = stable_id("v18evidreq", decision_run_id, table, source_id)
            con.execute(
                """INSERT INTO v18_llm_evidence_requests(
                       evidence_request_id,decision_run_id,capsule_id,person_id,live_session_id,
                       source_table,source_id,state,request_reason,resolution_json,created_at,updated_at,resolved_at
                     ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(decision_run_id,source_table,source_id) DO NOTHING""",
                (request_id, decision_run_id, capsule_id, person_id, live_session_id,
                 table, source_id, "requested", reason, "{}", now, now, None),
            )
            ids.append(request_id)
    return ids


def resolve_llm_evidence_requests(
    *,
    decision_run_id: str,
    resolved: Iterable[Mapping[str, Any]],
) -> int:
    """Mark only the evidence rows actually recovered for a successor capsule.

    A requested reference is not considered resolved merely because a model
    asked for it.  The state transition is committed only after the gateway
    returned the exact scope/as_of-bound row used to build the successor.
    """
    ensure_runtime_hardening_schema()
    now = now_iso()
    count = 0
    with connect() as con, write_transaction(con):
        for item in list(resolved):
            if not isinstance(item, Mapping):
                continue
            table = str(item.get("source_table") or "")
            source_id = str(item.get("source_id") or "")
            if not table or not source_id:
                continue
            cur = con.execute(
                """UPDATE v18_llm_evidence_requests
                   SET state='resolved',resolution_json=?,updated_at=?,resolved_at=?
                   WHERE decision_run_id=? AND source_table=? AND source_id=? AND state='requested'""",
                (json_dumps(dict(item)), now, now, decision_run_id, table, source_id),
            )
            count += max(0, int(cur.rowcount or 0))
    return count


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _retry_at(delay_seconds: int) -> str:
    return iso_utc(_now_dt() + timedelta(seconds=max(1, int(delay_seconds))))


def ensure_llm_decision_run(
    *,
    person_id: str,
    live_session_id: str,
    source_key: str,
    capsule_id: str,
    capsule_hash: str,
    execution_mode: str,
    contract_version: str,
    model: str | None,
    generation: Mapping[str, Any] | None = None,
    max_attempts: int = 5,
) -> dict[str, Any]:
    ensure_runtime_hardening_schema()
    key = stable_id("v18llmdecision", person_id, live_session_id, source_key, capsule_hash, execution_mode, contract_version)
    now = now_iso()
    with connect() as con, write_transaction(con):
        row = con.execute("SELECT * FROM v18_llm_decision_runs WHERE decision_key=?", (key,)).fetchone()
        if row:
            return {**dict(row), "created": False}
        run_id = stable_id("v18llmrun", key)
        con.execute(
            """INSERT INTO v18_llm_decision_runs(
                 decision_run_id,decision_key,person_id,live_session_id,source_key,capsule_id,capsule_hash,
                 execution_mode,contract_version,model,generation_json,state,attempt_count,repair_count,max_attempts,
                 next_attempt_at,lease_token,lease_expires_at,raw_output_text,raw_output_sha256,result_json,error_kind,
                 error_text,created_at,updated_at,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, key, person_id, live_session_id, source_key, capsule_id, capsule_hash, execution_mode,
                contract_version, model, json_dumps(dict(generation or {})), "pending", 0, 0, max(1, int(max_attempts)),
                None, None, None, None, None, "{}", None, None, now, now, None,
            ),
        )
    return {"decision_run_id": run_id, "decision_key": key, "state": "pending", "created": True}


def claim_llm_decision_run(*, decision_run_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
    """Atomically claim a pending/retryable decision run without double execution."""
    ensure_runtime_hardening_schema()
    now_dt = _now_dt()
    now = iso_utc(now_dt)
    expiry = iso_utc(now_dt + timedelta(seconds=max(10, int(lease_seconds))))
    token = stable_id("v18llmlease", decision_run_id, now, hashlib.sha256(now.encode()).hexdigest())
    with connect() as con, write_transaction(con):
        row = con.execute("SELECT * FROM v18_llm_decision_runs WHERE decision_run_id=?", (decision_run_id,)).fetchone()
        if not row:
            return None
        state = str(row["state"])
        if state in {"succeeded", "quarantined", "terminal_error", "cancelled"}:
            return None
        next_at = row["next_attempt_at"]
        lease_until = row["lease_expires_at"]
        if state in {"retryable_error", "pending"} and next_at:
            try:
                if parse_iso_utc(str(next_at)) > now_dt:
                    return None
            except Exception:
                con.execute("UPDATE v18_llm_decision_runs SET state='quarantined',error_kind='invalid_schedule',error_text=?,updated_at=? WHERE decision_run_id=?", ("invalid next_attempt_at", now, decision_run_id))
                return None
        if state in {"claimed", "running"} and lease_until:
            try:
                if parse_iso_utc(str(lease_until)) > now_dt:
                    return None
            except Exception:
                con.execute("UPDATE v18_llm_decision_runs SET state='quarantined',error_kind='invalid_schedule',error_text=?,updated_at=? WHERE decision_run_id=?", ("invalid lease_expires_at", now, decision_run_id))
                return None
        attempts = int(row["attempt_count"] or 0)
        max_attempts = int(row["max_attempts"] or 1)
        if attempts >= max_attempts:
            con.execute("UPDATE v18_llm_decision_runs SET state='quarantined',error_kind='max_attempts',error_text=?,updated_at=? WHERE decision_run_id=?", ("LLM retry budget exhausted", now, decision_run_id))
            return None
        attempt_no = attempts + 1
        phase = "repair" if state == "repair_requested" else "retry" if attempts else "initial"
        con.execute(
            """UPDATE v18_llm_decision_runs
               SET state='running',attempt_count=?,lease_token=?,lease_expires_at=?,next_attempt_at=NULL,updated_at=?
               WHERE decision_run_id=?""",
            (attempt_no, token, expiry, now, decision_run_id),
        )
        con.execute(
            """INSERT INTO v18_llm_decision_attempts(
                 attempt_id,decision_run_id,attempt_no,phase,state,raw_output_text,raw_output_sha256,result_json,error_kind,error_text,started_at,finished_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (stable_id("v18llmattempt", decision_run_id, attempt_no), decision_run_id, attempt_no, phase, "started", None, None, "{}", None, None, now, None),
        )
        updated = dict(con.execute("SELECT * FROM v18_llm_decision_runs WHERE decision_run_id=?", (decision_run_id,)).fetchone())
    return {**updated, "lease_token": token, "attempt_no": attempt_no, "phase": phase}


def finish_llm_decision_run(
    *,
    decision_run_id: str,
    lease_token: str,
    outcome: str,
    result: Mapping[str, Any] | None = None,
    raw_output: str | None = None,
    error_kind: str | None = None,
    error_text: str | None = None,
    retry_delay_seconds: int = 30,
) -> dict[str, Any]:
    """Finish exactly one claimed attempt with a durable, inspectable outcome."""
    allowed = {"succeeded", "retryable_error", "repair_requested", "quarantined", "terminal_error"}
    if outcome not in allowed:
        raise ValueError(f"unsupported decision outcome: {outcome}")
    ensure_runtime_hardening_schema()
    raw = bounded_text(raw_output, limit=64_000) if raw_output else None
    raw_sha = _hash(raw) if raw is not None else None
    now = now_iso()
    with connect() as con, write_transaction(con):
        row = con.execute("SELECT * FROM v18_llm_decision_runs WHERE decision_run_id=?", (decision_run_id,)).fetchone()
        if not row or str(row["state"]) != "running" or str(row["lease_token"] or "") != str(lease_token):
            raise RuntimeError("LLM decision run is not held by this lease")
        attempt_no = int(row["attempt_count"] or 0)
        next_at = _retry_at(retry_delay_seconds) if outcome == "retryable_error" else None
        repair_count = int(row["repair_count"] or 0) + (1 if outcome == "repair_requested" else 0)
        final_state = outcome
        if outcome == "repair_requested" and repair_count > 1:
            final_state = "quarantined"
            error_kind = error_kind or "repair_budget_exhausted"
            error_text = error_text or "more than one semantic repair requested"
        completed_at = now if final_state in {"succeeded", "quarantined", "terminal_error"} else None
        con.execute(
            """UPDATE v18_llm_decision_runs
               SET state=?,repair_count=?,next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL,
                   raw_output_text=COALESCE(?,raw_output_text),raw_output_sha256=COALESCE(?,raw_output_sha256),
                   result_json=?,error_kind=?,error_text=?,updated_at=?,completed_at=COALESCE(?,completed_at)
               WHERE decision_run_id=?""",
            (
                final_state, repair_count, next_at, raw, raw_sha, json_dumps(dict(result or {})), error_kind,
                bounded_text(error_text, limit=4000) if error_text else None, now, completed_at, decision_run_id,
            ),
        )
        # Attempt rows predate the run-level ``repair_requested`` state.  A
        # repair request is operationally a retryable failed attempt, while the
        # parent run retains the stronger semantic state that forces exactly one
        # same-capsule repair.  Do not let the audit trail violate its own CHECK
        # constraint or turn an invalid model response into an uncaught SQLite
        # error.
        attempt_state = "retryable_error" if final_state == "repair_requested" else final_state
        con.execute(
            """UPDATE v18_llm_decision_attempts
               SET state=?,raw_output_text=?,raw_output_sha256=?,result_json=?,error_kind=?,error_text=?,finished_at=?
               WHERE decision_run_id=? AND attempt_no=?""",
            (
                attempt_state, raw, raw_sha, json_dumps(dict(result or {})), error_kind,
                bounded_text(error_text, limit=4000) if error_text else None, now, decision_run_id, attempt_no,
            ),
        )
        saved = dict(con.execute("SELECT * FROM v18_llm_decision_runs WHERE decision_run_id=?", (decision_run_id,)).fetchone())
    return saved


def classify_llm_exception(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "trunc" in name or "trunc" in text or "json decode" in text or "unterminated" in text or "expecting" in text and "json" in text:
        return "truncated_output"
    if "timeout" in name or "timed out" in text or "temporarily" in text or "connection" in text or "urlopen" in text:
        return "transient_runtime_error"
    if "contract" in name or "schema" in text or "validation" in text:
        return "invalid_contract"
    return "runtime_error"


def _manifest_refs(manifest: Mapping[str, Any] | None) -> dict[tuple[str, str], Mapping[str, Any]]:
    refs: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in (manifest or {}).get("items") or []:
        if not isinstance(item, Mapping):
            continue
        table = str(item.get("source_table") or "")
        source_id = str(item.get("source_id") or "")
        if table and source_id:
            refs[(table, source_id)] = item
    return refs


def _semantic_evidence_list(value: Any, *, refs: Mapping[tuple[str, str], Mapping[str, Any]], as_of: str, field: str) -> None:
    if not isinstance(value, list):
        raise ContractValidationError(f"{field} must be a list of manifest evidence references")
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ContractValidationError(f"{field}[{index}] must be an evidence reference object")
        table = str(item.get("source_table") or "")
        source_id = str(item.get("source_id") or "")
        if (table, source_id) not in refs:
            raise ContractValidationError(f"{field}[{index}] references unavailable evidence {table}/{source_id}")
        source = refs[(table, source_id)]
        if bool(source.get("truncated")) and not bool(source.get("retrievable", False)):
            raise ContractValidationError(f"{field}[{index}] references an unavailable truncated source")
        source_time = str(source.get("occurred_at") or "")
        if source_time and parse_iso_utc(source_time) > parse_iso_utc(as_of):
            raise ContractValidationError(f"{field}[{index}] references a future source")


def validate_semantic_output(
    payload: Mapping[str, Any],
    *,
    context_manifest: Mapping[str, Any] | None,
    person_id: str,
    as_of: str,
) -> dict[str, Any]:
    """Validate evidence, temporal scope and cross-field logic after Pydantic.

    The Pydantic contracts establish shape.  This validator establishes that a
    claim actually points to evidence available to this owner/as_of capsule.
    Empty collections remain legitimate; non-empty cognitive claims cannot cite
    invented IDs or opaque free-text evidence.
    """
    if not isinstance(payload, Mapping):
        raise ContractValidationError("semantic payload must be a mapping")
    as_of = iso_utc(parse_iso_utc(as_of))
    manifest_scope = (context_manifest or {}).get("scope") or {}
    if str(manifest_scope.get("person_id") or person_id) != str(person_id):
        raise ContractValidationError("context manifest owner mismatch")
    manifest_as_of = manifest_scope.get("as_of")
    if manifest_as_of and parse_iso_utc(str(manifest_as_of)) > parse_iso_utc(as_of):
        raise ContractValidationError("context manifest as_of exceeds decision as_of")
    refs = _manifest_refs(context_manifest)
    evidence_fields = ("events", "need_predictions", "affordances", "forecasts", "life_hypotheses", "interventions")
    for collection in evidence_fields:
        values = payload.get(collection) or []
        if not isinstance(values, list):
            raise ContractValidationError(f"{collection} must be a list")
        for index, item in enumerate(values):
            if not isinstance(item, Mapping):
                raise ContractValidationError(f"{collection}[{index}] must be an object")
            evidence = item.get("evidence")
            if not evidence:
                raise ContractValidationError(f"{collection}[{index}] must cite at least one manifest evidence reference")
            _semantic_evidence_list(evidence, refs=refs, as_of=as_of, field=f"{collection}[{index}].evidence")
            _semantic_evidence_list(item.get("counter_evidence") or [], refs=refs, as_of=as_of, field=f"{collection}[{index}].counter_evidence")
            if collection == "forecasts":
                horizon = str(item.get("horizon") or "")
                if horizon not in {"H0", "H1", "H2"}:
                    raise ContractValidationError(f"forecast {index} has invalid horizon")
                # A future target is permitted only when it is after the evidence
                # window; the canonical writer calculates due_at itself.
                for numeric in ("probability", "confidence", "expected_gain"):
                    val = item.get(numeric)
                    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)) or not 0.0 <= float(val) <= 1.0:
                        raise ContractValidationError(f"forecast {index} invalid {numeric}")
            if collection == "interventions":
                if str(item.get("recommended_timing") or "") not in {"now", "soon", "after_pause", "later", "watch_only"}:
                    raise ContractValidationError(f"intervention {index} has invalid timing")
    return dict(payload)


def validate_resolvable_semantic_output(
    payload: Mapping[str, Any],
    *,
    context_manifest: Mapping[str, Any] | None,
    person_id: str,
    live_session_id: str | None,
    as_of: str,
) -> dict[str, Any]:
    """Semantic validation plus proof that every cited row still resolves.

    ``validate_semantic_output`` checks the universal V13--V17 contract shape
    and the immutable manifest allow-list.  This stronger boundary is for
    active LLM writers: it additionally proves row existence, owner/session and
    temporal scope before any output can be persisted.
    """
    normalized = validate_semantic_output(
        payload,
        context_manifest=context_manifest,
        person_id=person_id,
        as_of=as_of,
    )
    world = normalized.get("world_state") or {}
    if isinstance(world, Mapping):
        world_claim = bool(
            world.get("where_am_i")
            or world.get("what_is_happening")
            or world.get("probable_activity")
            or str(world.get("active_mode") or "").strip().lower() not in {"", "unknown"}
        )
        validate_resolvable_manifest_evidence(
            world.get("evidence"), context_manifest=context_manifest,
            person_id=person_id, live_session_id=live_session_id, as_of=as_of,
            field="world_state.evidence", required=world_claim,
        )
        validate_resolvable_manifest_evidence(
            world.get("counter_evidence") or [], context_manifest=context_manifest,
            person_id=person_id, live_session_id=live_session_id, as_of=as_of,
            field="world_state.counter_evidence",
        )
    for collection in ("events", "need_predictions", "affordances", "forecasts", "life_hypotheses", "interventions"):
        for index, item in enumerate(normalized.get(collection) or []):
            if not isinstance(item, Mapping):
                continue
            validate_resolvable_manifest_evidence(
                item.get("evidence"), context_manifest=context_manifest,
                person_id=person_id, live_session_id=live_session_id, as_of=as_of,
                field=f"{collection}[{index}].evidence", required=True,
            )
            validate_resolvable_manifest_evidence(
                item.get("counter_evidence") or [], context_manifest=context_manifest,
                person_id=person_id, live_session_id=live_session_id, as_of=as_of,
                field=f"{collection}[{index}].counter_evidence",
            )
    return normalized


def validate_manifest_evidence(
    value: Any,
    *,
    context_manifest: Mapping[str, Any] | None,
    as_of: str,
    field: str = "evidence",
    required: bool = False,
) -> None:
    """Validate one evidence list against the immutable capsule manifest.

    This public helper is intentionally shared by the hot one-call path and the
    decomposed path.  It makes evidence IDs executable: a model cannot cite an
    invented row, an unavailable truncation or a future observation.
    """
    if (value is None or value == []) and not required:
        return
    refs = _manifest_refs(context_manifest)
    if not refs and required:
        raise ContractValidationError(f"{field} requires manifest evidence but the manifest is empty")
    _semantic_evidence_list(value, refs=refs, as_of=iso_utc(parse_iso_utc(as_of)), field=field)

_SAFE_SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _lookup_manifest_source(
    con: Any,
    *,
    source_table: str,
    source_id: str,
    person_id: str,
    live_session_id: str | None,
    as_of: str,
) -> None:
    """Prove that a manifest reference still resolves in the declared scope.

    The manifest is an allow-list, but a syntactically valid allow-listed ID is
    not enough if its backing row was deleted, belongs to another session, or is
    newer than the decision's ``as_of``.  This is intentionally generic because
    active V13--V17 projections use different primary-key names.
    """
    if not _SAFE_SQL_IDENT.fullmatch(source_table):
        raise ContractValidationError(f"unsafe manifest source table {source_table!r}")
    columns = [dict(row) for row in con.execute(f"PRAGMA table_info({source_table})").fetchall()]
    if not columns:
        raise ContractValidationError(f"manifest evidence table does not exist: {source_table}")
    names = {str(col.get("name") or "") for col in columns}
    id_columns = [str(col["name"]) for col in columns if int(col.get("pk") or 0) == 1]
    id_columns.extend(name for name in names if name.endswith("_id") and name not in id_columns)
    row: dict[str, Any] | None = None
    for column in id_columns:
        candidate = con.execute(f"SELECT * FROM {source_table} WHERE {column}=? LIMIT 1", (source_id,)).fetchone()
        if candidate:
            row = dict(candidate)
            break
    if row is None:
        raise ContractValidationError(f"manifest evidence no longer resolves: {source_table}/{source_id}")

    owner_columns = ("person_id", "owner_person_id", "subject_person_id")
    owner = next((row.get(key) for key in owner_columns if row.get(key) not in (None, "")), None)
    row_session = row.get("live_session_id")
    if owner is not None and str(owner) != str(person_id):
        raise ContractValidationError(f"manifest evidence belongs to another owner: {source_table}/{source_id}")
    if row_session is not None:
        if live_session_id is not None and str(row_session) != str(live_session_id):
            raise ContractValidationError(f"manifest evidence belongs to another live session: {source_table}/{source_id}")
        session = con.execute("SELECT person_id FROM brainlive_sessions WHERE live_session_id=?", (row_session,)).fetchone()
        if not session or str(session["person_id"] or "") != str(person_id):
            raise ContractValidationError(f"manifest evidence session owner mismatch: {source_table}/{source_id}")
    elif owner is None:
        # An active cognitive claim must never use an unscoped row as evidence.
        raise ContractValidationError(f"manifest evidence is not owner/session scoped: {source_table}/{source_id}")

    for time_key in ("occurred_at", "timestamp_start", "captured_at", "event_time", "created_at", "updated_at"):
        raw_time = row.get(time_key)
        if raw_time in (None, ""):
            continue
        try:
            if parse_iso_utc(str(raw_time)) > parse_iso_utc(as_of):
                raise ContractValidationError(f"manifest evidence is newer than decision as_of: {source_table}/{source_id}")
            break
        except ContractValidationError:
            raise
        except Exception as exc:
            raise ContractValidationError(f"manifest evidence has invalid time {source_table}/{source_id}:{time_key}") from exc


def validate_resolvable_manifest_evidence(
    value: Any,
    *,
    context_manifest: Mapping[str, Any] | None,
    person_id: str,
    live_session_id: str | None,
    as_of: str,
    field: str = "evidence",
    required: bool = False,
) -> None:
    """Validate evidence identity, existence, owner/session and temporal scope.

    This is the persistence boundary for the hot path.  It deliberately runs
    after the simple manifest allow-list validation so no LLM-output ID is ever
    interpolated into SQL unless it was first announced by the immutable capsule.
    """
    validate_manifest_evidence(value, context_manifest=context_manifest, as_of=as_of, field=field, required=required)
    if value in (None, []):
        return
    refs = _manifest_refs(context_manifest)
    with connect() as con:
        for item in list(value or []):
            if not isinstance(item, Mapping):
                raise ContractValidationError(f"{field} contains non-object evidence")
            table = str(item.get("source_table") or "")
            source_id = str(item.get("source_id") or "")
            # The first validation above guarantees this key was announced.
            if (table, source_id) not in refs:
                raise ContractValidationError(f"{field} references unannounced evidence")
            _lookup_manifest_source(
                con,
                source_table=table,
                source_id=source_id,
                person_id=person_id,
                live_session_id=live_session_id,
                as_of=as_of,
            )

