from __future__ import annotations

"""Durable synchronization ledger for external memory stores.

SQLite is the canonical memory. Qdrant/LanceDB, Graphiti and Mem0 are secondary
indexes. This module makes every secondary write explicit, retryable and
inspectable so ingestion/correction can never silently leave a partial memory.
"""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .db import connect, upsert
from .utils import json_dumps, json_loads, now_iso, stable_id

SYNC_SCHEMA_VERSION = "3.3.1-durable-sync-ledger"

PENDING_STATUSES = {"pending", "failed"}
TERMINAL_STATUSES = {"succeeded", "dead"}


def _job_payload_hash(payload: dict[str, Any] | None) -> str:
    return stable_id("payload", payload or {})


def ensure_sync_job(
    con,
    *,
    backend: str,
    operation: str,
    target_table: str,
    target_id: str,
    conversation_id: str | None = None,
    priority: int = 50,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 5,
) -> str:
    """Create or refresh the durable job for a secondary-memory update."""
    payload_hash = _job_payload_hash(payload)
    job_id = stable_id("sync_job", backend, operation, target_table, target_id, payload_hash)
    now = now_iso()
    existing = con.execute("SELECT status, attempt_count FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
    status = existing["status"] if existing else "pending"
    attempt_count = int(existing["attempt_count"] if existing else 0)
    if existing and status in TERMINAL_STATUSES and not operation.startswith("delete"):
        return job_id
    if status in TERMINAL_STATUSES and operation.startswith("delete"):
        status = "pending"
    upsert(con, "sync_jobs", {
        "job_id": job_id,
        "backend": backend,
        "operation": operation,
        "target_table": target_table,
        "target_id": target_id,
        "conversation_id": conversation_id,
        "priority": int(priority),
        "status": status,
        "attempt_count": attempt_count,
        "max_attempts": int(max_attempts),
        "next_attempt_at": now if status in PENDING_STATUSES else None,
        "locked_at": None,
        "lock_token": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "external_ref_json": json_dumps({}),
        "payload_json": json_dumps(payload or {}),
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }, "job_id")
    return job_id


def schedule_vector_sync(con, *, reason: str, target_table: str = "all_memory", target_id: str = "global", conversation_id: str | None = None, payload: dict[str, Any] | None = None) -> str:
    from .config import get_settings
    settings = get_settings()
    return ensure_sync_job(
        con,
        backend=f"vector:{settings.vector_backend}",
        operation="upsert_incremental",
        target_table=target_table,
        target_id=target_id,
        conversation_id=conversation_id,
        payload={"reason": reason, **(payload or {})},
        priority=20,
    )


def schedule_external_sync(con, *, conversation_id: str, backend: str, reason: str, payload: dict[str, Any] | None = None) -> str:
    if backend not in {"graphiti", "mem0"}:
        raise ValueError(f"Backend externe inconnu: {backend}")
    return ensure_sync_job(
        con,
        backend=backend,
        operation="upsert_conversation",
        target_table="conversations",
        target_id=conversation_id,
        conversation_id=conversation_id,
        payload={"reason": reason, **(payload or {})},
        priority=30,
    )


def schedule_post_ingest_sync(con, *, conversation_id: str) -> list[str]:
    """Queue every mandatory secondary index after canonical SQLite commit."""
    return [
        schedule_vector_sync(con, reason="post_ingest", conversation_id=conversation_id, payload={"conversation_id": conversation_id}),
        schedule_external_sync(con, conversation_id=conversation_id, backend="graphiti", reason="post_ingest"),
        schedule_external_sync(con, conversation_id=conversation_id, backend="mem0", reason="post_ingest"),
    ]


def begin_sync_job(job_id: str) -> str:
    token = uuid4().hex
    now = now_iso()
    with connect() as con:
        row = con.execute("SELECT status, attempt_count, max_attempts FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise RuntimeError(f"sync_job introuvable: {job_id}")
        if row["status"] in TERMINAL_STATUSES:
            return token
        attempt_count = int(row["attempt_count"] or 0) + 1
        status = "running" if attempt_count <= int(row["max_attempts"] or 5) else "dead"
        con.execute(
            """UPDATE sync_jobs
               SET status=?, attempt_count=?, last_attempt_at=?, locked_at=?, lock_token=?, updated_at=?, error_message=NULL
               WHERE job_id=?""",
            (status, attempt_count, now, now, token, now, job_id),
        )
        con.commit()
        if status == "dead":
            raise RuntimeError(f"sync_job {job_id} a dépassé max_attempts")
    return token


def complete_sync_job(job_id: str, *, result: dict[str, Any] | None = None) -> None:
    now = now_iso()
    with connect() as con:
        con.execute(
            """UPDATE sync_jobs
               SET status='succeeded', last_success_at=?, locked_at=NULL, lock_token=NULL,
                   next_attempt_at=NULL, external_ref_json=?, error_message=NULL, updated_at=?
               WHERE job_id=?""",
            (now, json_dumps(result or {}), now, job_id),
        )
        con.commit()


def fail_sync_job(job_id: str, *, error: BaseException) -> None:
    now = now_iso()
    with connect() as con:
        row = con.execute("SELECT attempt_count, max_attempts FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
        attempts = int(row["attempt_count"] or 0) if row else 1
        max_attempts = int(row["max_attempts"] or 5) if row else 5
        status = "dead" if attempts >= max_attempts else "failed"
        con.execute(
            """UPDATE sync_jobs
               SET status=?, locked_at=NULL, lock_token=NULL, next_attempt_at=?, error_message=?, updated_at=?
               WHERE job_id=?""",
            (status, now, str(error)[:2000], now, job_id),
        )
        con.commit()


def run_tracked_sync_job(job_id: str, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    begin_sync_job(job_id)
    try:
        result = work()
    except Exception as exc:
        fail_sync_job(job_id, error=exc)
        raise
    complete_sync_job(job_id, result=result)
    return result


def run_or_create_sync_job(
    *,
    backend: str,
    operation: str,
    target_table: str,
    target_id: str,
    conversation_id: str | None,
    payload: dict[str, Any] | None,
    work: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    with connect() as con:
        existing = con.execute(
            """SELECT job_id FROM sync_jobs
               WHERE backend=? AND operation=? AND target_table=? AND target_id=?
                 AND status IN ('pending','failed')
               ORDER BY priority ASC, updated_at ASC LIMIT 1""",
            (backend, operation, target_table, target_id),
        ).fetchone()
        job_id = existing["job_id"] if existing else ensure_sync_job(
            con,
            backend=backend,
            operation=operation,
            target_table=target_table,
            target_id=target_id,
            conversation_id=conversation_id,
            payload=payload,
        )
        con.commit()
    return run_tracked_sync_job(job_id, work)


def list_sync_jobs(*, status: str | None = None, backend: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if backend:
        clauses.append("backend=?")
        params.append(backend)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = list(con.execute(
            f"SELECT * FROM sync_jobs{where} ORDER BY priority ASC, updated_at DESC LIMIT ?",
            (*params, int(limit)),
        ))
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = json_loads(item.pop("payload_json", None), {})
        item["external_ref"] = json_loads(item.pop("external_ref_json", None), {})
        out.append(item)
    return out


def run_pending_sync_jobs(*, limit: int = 20, backend: str | None = None) -> list[dict[str, Any]]:
    clauses = ["status IN ('pending','failed')"]
    params: list[Any] = []
    if backend:
        clauses.append("backend=?")
        params.append(backend)
    with connect() as con:
        jobs = list(con.execute(
            f"SELECT * FROM sync_jobs WHERE {' AND '.join(clauses)} ORDER BY priority ASC, updated_at ASC LIMIT ?",
            (*params, int(limit)),
        ))
    results: list[dict[str, Any]] = []
    for job in jobs:
        payload = json_loads(job["payload_json"], {}) or {}
        if str(job["backend"]).startswith("vector:"):
            from .vector_sync import _sync_vectors_untracked
            result = run_tracked_sync_job(job["job_id"], lambda payload=payload: _sync_vectors_untracked(limit=payload.get("limit"), conversation_id=payload.get("conversation_id"), incremental=payload.get("incremental", True)))
        elif job["backend"] == "graphiti":
            from .external_memory import _sync_graphiti_untracked
            result = run_tracked_sync_job(job["job_id"], lambda cid=job["conversation_id"]: _sync_graphiti_untracked(str(cid)))
        elif job["backend"] == "mem0":
            from .external_memory import _sync_mem0_untracked
            result = run_tracked_sync_job(job["job_id"], lambda cid=job["conversation_id"]: _sync_mem0_untracked(str(cid)))
        else:
            raise RuntimeError(f"Backend de sync non supporté: {job['backend']}")
        results.append({"job_id": job["job_id"], "backend": job["backend"], "result": result})
    return results

# V18 remediation: atomic leases, backoff and terminal-state protection.
from .v18_sync import install_sync_jobs as _install_v18_sync_jobs
_globals_v18_sync_jobs = _install_v18_sync_jobs(__import__(__name__, fromlist=['*']))
globals().update(_globals_v18_sync_jobs)
