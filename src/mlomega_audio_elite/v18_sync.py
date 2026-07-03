"""V18 durable, owner-scoped secondary synchronization.

SQLite is canonical. Secondary stores are projections: each write is scoped to a
memory owner, claimed atomically, retryable with actual instant semantics, and
retractable.  Legacy callers that omit the owner are rejected instead of
silently exporting another person's data.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable
from uuid import uuid4

from .db import connect, write_transaction, upsert
from .governance_v18 import ScopeError, conversation_in_scope, ensure_v18_schema
from .integrity_v176 import iso_utc, parse_iso_utc
from .utils import json_dumps, json_loads, now_iso, stable_id

PENDING = {"pending", "failed"}
TERMINAL = {"succeeded", "dead", "cancelled", "superseded"}


class SyncNotRunnable(RuntimeError):
    """A durable sync job cannot legitimately run now."""


def _next_backoff(attempt: int) -> str:
    """Return a canonical UTC retry instant using bounded exponential backoff."""
    base = max(2, min(3600, 2 ** max(1, int(attempt))))
    return iso_utc(parse_iso_utc(now_iso()) + timedelta(seconds=base))


def vector_point_id(*, person_id: str, source_type: str, source_id: str) -> str:
    """Return a privacy-scoped identity for one vector point."""
    if not isinstance(person_id, str) or not person_id.strip():
        raise ValueError("vector point requires explicit person_id")
    if not source_type or not source_id:
        raise ValueError("vector point requires source_type and source_id")
    return stable_id("vector_point_v18", person_id, source_type, source_id)


def _due_at(value: Any, *, now_value: str | None = None) -> bool:
    """Compare retry timestamps as instants, never as SQLite text values."""
    if value is None or str(value).strip() == "":
        return True
    try:
        due = parse_iso_utc(str(value))
        current = parse_iso_utc(now_value or now_iso())
    except Exception as exc:
        raise SyncNotRunnable(f"sync job has invalid next_attempt_at: {value!r}") from exc
    return due <= current


def _require_person_id(person_id: str | None, *, purpose: str) -> str:
    if not isinstance(person_id, str) or not person_id.strip():
        raise ScopeError(f"{purpose} requires explicit person_id")
    return person_id.strip()


def _require_conversation_scope(con, *, conversation_id: str | None, person_id: str, purpose: str) -> None:
    if not conversation_id:
        return
    if not conversation_in_scope(
        con,
        conversation_id=str(conversation_id),
        person_id=person_id,
        allow_legacy_turn_proof=False,
    ):
        raise ScopeError(f"{purpose} denied: conversation is not explicitly scoped to person_id")


def _terminalize_job(job_id: str, *, reason: str) -> None:
    """Persist a fatal diagnosis in its own transaction before returning error."""
    with connect() as con, write_transaction(con):
        con.execute(
            """UPDATE sync_jobs
               SET status='dead', locked_at=NULL, lock_token=NULL,
                   error_message=?, updated_at=?
               WHERE job_id=? AND status IN ('pending','failed','running')""",
            (reason[:2000], now_iso(), job_id),
        )


def install_sync_jobs(module: Any) -> dict[str, Any]:
    """Install owner-aware V18 sync scheduling over the legacy module surface."""

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
        # This is deliberately safe within a caller-owned transaction. Schema
        # initialization happens before entering public runner transactions.
        payload = dict(payload or {})
        digest = stable_id("sync18", backend, operation, target_table, target_id, payload)
        job_id = stable_id("sync_job18", backend, operation, target_table, target_id, digest)
        now = now_iso()
        row = con.execute("SELECT status FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row:
            return job_id
        con.execute(
            """INSERT INTO sync_jobs(
                   job_id,backend,operation,target_table,target_id,conversation_id,
                   priority,status,attempt_count,max_attempts,next_attempt_at,
                   locked_at,lock_token,last_attempt_at,last_success_at,
                   external_ref_json,payload_json,error_message,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'pending',0,?, ?,NULL,NULL,NULL,NULL,'{}',?,NULL,?,?)""",
            (
                job_id,
                backend,
                operation,
                target_table,
                target_id,
                conversation_id,
                int(priority),
                int(max_attempts),
                now,
                json_dumps(payload),
                now,
                now,
            ),
        )
        return job_id

    def _scoped_payload(
        *, person_id: str, conversation_id: str | None, reason: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        return {
            "schema": "v18_sync_job",
            "person_id": person_id,
            "conversation_id": conversation_id,
            "reason": reason,
            **dict(payload or {}),
        }

    def schedule_vector_sync(
        con,
        *,
        reason: str,
        person_id: str,
        target_table: str = "all_memory",
        target_id: str = "global",
        conversation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        owner = _require_person_id(person_id, purpose="vector sync")
        _require_conversation_scope(
            con, conversation_id=conversation_id, person_id=owner, purpose="vector sync"
        )
        from .config import get_settings

        settings = get_settings()
        return ensure_sync_job(
            con,
            backend=f"vector:{settings.vector_backend}",
            operation="upsert_incremental",
            target_table=target_table,
            target_id=target_id,
            conversation_id=conversation_id,
            payload=_scoped_payload(
                person_id=owner,
                conversation_id=conversation_id,
                reason=reason,
                payload=payload,
            ),
            priority=20,
        )

    def schedule_external_sync(
        con,
        *,
        conversation_id: str,
        backend: str,
        reason: str,
        person_id: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        if backend not in {"graphiti", "mem0"}:
            raise ValueError(f"Backend externe inconnu: {backend}")
        owner = _require_person_id(person_id, purpose="external sync")
        _require_conversation_scope(
            con, conversation_id=conversation_id, person_id=owner, purpose="external sync"
        )
        return ensure_sync_job(
            con,
            backend=backend,
            operation="upsert_conversation",
            target_table="conversations",
            target_id=conversation_id,
            conversation_id=conversation_id,
            payload=_scoped_payload(
                person_id=owner,
                conversation_id=conversation_id,
                reason=reason,
                payload=payload,
            ),
            priority=30,
        )

    def schedule_post_ingest_sync(con, *, conversation_id: str, person_id: str) -> list[str]:
        """Queue only the enabled projections for the active deployment profile.

        V18.7 Core explicitly excludes Graphiti and Mem0.  Previous code still
        created disabled external jobs during every normal conversation, which
        was confusing and could later wake a legacy worker.  The canonical
        post-ingest path therefore always schedules Qdrant/vector work and
        adds an external projection only when it has been explicitly enabled.
        """
        owner = _require_person_id(person_id, purpose="post-ingest sync")
        jobs = [
            schedule_vector_sync(
                con, reason="post_ingest", person_id=owner,
                conversation_id=conversation_id, payload={"conversation_id": conversation_id},
            ),
        ]
        from .config import get_settings
        cfg = get_settings()
        # The V18.7 supported profile must remain graph-free even if a user has
        # stale legacy environment variables from an older checkout.  Otherwise
        # an inactive Graphiti/Mem0 installation can be accidentally scheduled
        # during a successful Brain2 post-stop and turn a recoverable day into
        # a misleading external-dependency failure.
        core_v187 = str(cfg.deployment_profile or "").upper().startswith(("CORE_BRAINLIVE_V18_7", "CORE_BRAINLIVE_V18_8"))
        if not core_v187:
            graph_enabled = str(cfg.graph_backend or "").lower() not in {"", "disabled", "none", "off"}
            if graph_enabled:
                jobs.append(schedule_external_sync(con, conversation_id=conversation_id, backend="graphiti", reason="post_ingest", person_id=owner))
            if bool(cfg.mem0_enabled):
                jobs.append(schedule_external_sync(con, conversation_id=conversation_id, backend="mem0", reason="post_ingest", person_id=owner))
        return jobs

    def begin_sync_job(job_id: str) -> str:
        token = uuid4().hex
        now = now_iso()
        terminal_reason: str | None = None
        with connect() as con, write_transaction(con):
            row = con.execute(
                "SELECT status,attempt_count,max_attempts,next_attempt_at,lock_token FROM sync_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise SyncNotRunnable(f"sync job introuvable: {job_id}")
            if str(row["status"]) in TERMINAL:
                raise SyncNotRunnable(f"sync job terminal: {job_id}")
            try:
                is_due = _due_at(row["next_attempt_at"], now_value=now)
            except SyncNotRunnable as exc:
                terminal_reason = str(exc)
                con.execute(
                    "UPDATE sync_jobs SET status='dead',lock_token=NULL,locked_at=NULL,error_message=?,updated_at=? WHERE job_id=?",
                    (terminal_reason[:2000], now, job_id),
                )
                is_due = False
            if terminal_reason is None and not is_due:
                raise SyncNotRunnable(f"sync job backoff actif: {job_id}")
            if terminal_reason is None:
                attempts = int(row["attempt_count"] or 0) + 1
                if attempts > int(row["max_attempts"] or 5):
                    terminal_reason = f"sync job max attempts: {job_id}"
                    con.execute(
                        "UPDATE sync_jobs SET status='dead',lock_token=NULL,locked_at=NULL,error_message=?,updated_at=? WHERE job_id=?",
                        (terminal_reason, now, job_id),
                    )
                else:
                    cur = con.execute(
                        """UPDATE sync_jobs
                           SET status='running',attempt_count=?,last_attempt_at=?,locked_at=?,lock_token=?,
                               error_message=NULL,updated_at=?
                           WHERE job_id=? AND status IN ('pending','failed') AND lock_token IS NULL""",
                        (attempts, now, now, token, now, job_id),
                    )
                    if cur.rowcount != 1:
                        raise SyncNotRunnable(f"sync job déjà acquis/non éligible: {job_id}")
        if terminal_reason is not None:
            raise SyncNotRunnable(terminal_reason)
        return token

    def complete_sync_job(
        job_id: str, *, result: dict[str, Any] | None = None, token: str | None = None
    ) -> None:
        now = now_iso()
        with connect() as con, write_transaction(con):
            where = "job_id=? AND status='running'"
            params: list[Any] = [now, json_dumps(result or {}), now, job_id]
            if token:
                where += " AND lock_token=?"
                params.append(token)
            cur = con.execute(
                f"""UPDATE sync_jobs
                   SET status='succeeded',last_success_at=?,locked_at=NULL,lock_token=NULL,
                       next_attempt_at=NULL,external_ref_json=?,error_message=NULL,updated_at=?
                   WHERE {where}""",
                tuple(params),
            )
            if cur.rowcount != 1:
                raise SyncNotRunnable(f"sync completion without active lease: {job_id}")

    def fail_sync_job(job_id: str, *, error: BaseException, token: str | None = None) -> None:
        with connect() as con, write_transaction(con):
            row = con.execute(
                "SELECT attempt_count,max_attempts,status,lock_token FROM sync_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not row or str(row["status"]) != "running" or (token and row["lock_token"] != token):
                raise SyncNotRunnable(f"sync failure without active lease: {job_id}")
            attempts = int(row["attempt_count"] or 0)
            maximum = int(row["max_attempts"] or 5)
            status = "dead" if attempts >= maximum else "failed"
            next_at = None if status == "dead" else _next_backoff(attempts)
            con.execute(
                "UPDATE sync_jobs SET status=?,locked_at=NULL,lock_token=NULL,next_attempt_at=?,error_message=?,updated_at=? WHERE job_id=?",
                (status, next_at, str(error)[:2000], now_iso(), job_id),
            )

    def run_tracked_sync_job(job_id: str, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        token = begin_sync_job(job_id)
        try:
            result = work() or {}
        except Exception as exc:
            fail_sync_job(job_id, error=exc, token=token)
            raise
        complete_sync_job(job_id, result=result, token=token)
        return result

    def _validate_job_scope(job: Any, payload: dict[str, Any]) -> str:
        owner = _require_person_id(payload.get("person_id"), purpose="durable sync job")
        payload_cid = payload.get("conversation_id")
        row_cid = job["conversation_id"]
        if payload_cid and row_cid and str(payload_cid) != str(row_cid):
            raise ScopeError("sync job conversation_id differs from its payload")
        cid = str(row_cid or payload_cid or "") or None
        if cid:
            with connect() as con:
                _require_conversation_scope(
                    con, conversation_id=cid, person_id=owner, purpose="durable sync job"
                )
        return owner

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
        ensure_v18_schema()
        payload = dict(payload or {})
        # All public V18 secondary projections are owner-scoped. This check
        # deliberately happens before job creation, so ownerless jobs cannot
        # later be picked up by a worker and leak data.
        owner = _require_person_id(payload.get("person_id"), purpose="tracked secondary sync")
        with connect() as con:
            _require_conversation_scope(
                con, conversation_id=conversation_id, person_id=owner, purpose="tracked secondary sync"
            )
        with connect() as con, write_transaction(con):
            jid = ensure_sync_job(
                con,
                backend=backend,
                operation=operation,
                target_table=target_table,
                target_id=target_id,
                conversation_id=conversation_id,
                payload=payload,
            )
        try:
            return run_tracked_sync_job(jid, work)
        except SyncNotRunnable as exc:
            return {"job_id": jid, "status": "not_runnable", "reason": str(exc)}

    def run_pending_sync_jobs(*, limit: int = 20, backend: str | None = None) -> list[dict[str, Any]]:
        """Run only due, owner-scoped jobs; one failed job never aborts the batch."""
        clauses = ["status IN ('pending','failed')", "lock_token IS NULL"]
        params: list[Any] = []
        if backend:
            clauses.append("backend=?")
            params.append(backend)
        with connect() as con:
            candidates = list(
                con.execute(
                    f"SELECT * FROM sync_jobs WHERE {' AND '.join(clauses)} ORDER BY priority ASC,updated_at ASC LIMIT ?",
                    (*params, max(int(limit) * 4, int(limit))),
                )
            )
        jobs: list[Any] = []
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                if _due_at(candidate["next_attempt_at"]):
                    jobs.append(candidate)
            except SyncNotRunnable as exc:
                _terminalize_job(str(candidate["job_id"]), reason=str(exc))
                results.append(
                    {
                        "job_id": candidate["job_id"],
                        "backend": candidate["backend"],
                        "status": "invalid_schedule",
                        "reason": str(exc),
                    }
                )
            if len(jobs) >= int(limit):
                break
        # Core V18.7 deliberately has no Graphiti/Mem0 runtime.  Terminalize
        # stale jobs left by an older install rather than importing optional
        # modules during a normal resume or silently retrying them forever.
        from .config import get_settings

        core_v187 = str(get_settings().deployment_profile or "").strip().upper().startswith(("CORE_BRAINLIVE_V18_7", "CORE_BRAINLIVE_V18_8"))
        for job in jobs:
            payload = json_loads(job["payload_json"], {}) or {}
            if core_v187 and str(job["backend"]).lower() in {"graphiti", "mem0"}:
                reason = "secondary backend disabled by CORE_BRAINLIVE_V18_8 profile"
                _terminalize_job(str(job["job_id"]), reason=reason)
                results.append(
                    {
                        "job_id": job["job_id"],
                        "backend": job["backend"],
                        "status": "disabled_core_profile",
                        "reason": reason,
                    }
                )
                continue
            try:
                owner = _validate_job_scope(job, payload)
                cid = str(job["conversation_id"] or payload.get("conversation_id") or "") or None
                if str(job["backend"]).startswith("vector:"):
                    from .vector_sync import _sync_vectors_untracked

                    result = run_tracked_sync_job(
                        str(job["job_id"]),
                        lambda p=payload, o=owner: _sync_vectors_untracked(
                            limit=p.get("limit"),
                            conversation_id=p.get("conversation_id"),
                            incremental=p.get("incremental", True),
                            person_id=o,
                        ),
                    )
                elif job["backend"] == "graphiti":
                    from .external_memory import _sync_graphiti_untracked

                    result = run_tracked_sync_job(
                        str(job["job_id"]),
                        lambda c=cid, o=owner: _sync_graphiti_untracked(str(c), person_id=o),
                    )
                elif job["backend"] == "mem0":
                    from .external_memory import _sync_mem0_untracked

                    result = run_tracked_sync_job(
                        str(job["job_id"]),
                        lambda c=cid, o=owner: _sync_mem0_untracked(str(c), person_id=o),
                    )
                else:
                    raise RuntimeError(f"Backend de sync non supporté: {job['backend']}")
                results.append({"job_id": job["job_id"], "backend": job["backend"], "result": result})
            except ScopeError as exc:
                _terminalize_job(str(job["job_id"]), reason=f"invalid_scope: {exc}")
                results.append(
                    {"job_id": job["job_id"], "backend": job["backend"], "status": "invalid_scope", "reason": str(exc)}
                )
            except SyncNotRunnable as exc:
                results.append(
                    {"job_id": job["job_id"], "backend": job["backend"], "status": "not_runnable", "reason": str(exc)}
                )
            except Exception as exc:
                # run_tracked_sync_job already stores retry/dead state. Continue
                # with independent jobs and make failure observable to caller.
                results.append(
                    {"job_id": job["job_id"], "backend": job["backend"], "status": "failed", "error": str(exc)[:2000]}
                )
        return results

    return {
        "ensure_sync_job": ensure_sync_job,
        "schedule_vector_sync": schedule_vector_sync,
        "schedule_external_sync": schedule_external_sync,
        "schedule_post_ingest_sync": schedule_post_ingest_sync,
        "begin_sync_job": begin_sync_job,
        "complete_sync_job": complete_sync_job,
        "fail_sync_job": fail_sync_job,
        "run_tracked_sync_job": run_tracked_sync_job,
        "run_or_create_sync_job": run_or_create_sync_job,
        "run_pending_sync_jobs": run_pending_sync_jobs,
        "SyncNotRunnable": SyncNotRunnable,
    }


def install_vector(module: Any) -> dict[str, Any]:
    """Install V18 vector projection with explicit owner filtering/tombstones."""
    old_iter = module._iter_memory_rows

    def ensure_vector_sync_manifest_schema() -> None:
        legacy = getattr(module, "_v17_ensure_vector_sync_manifest_schema", None)
        if legacy:
            legacy()
        with connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS vector_sync_manifest_v18(
                    point_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, person_id TEXT NOT NULL, conversation_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1, truth_status TEXT NOT NULL DEFAULT 'active',
                    source_version TEXT, synced_at TEXT NOT NULL, retracted_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_vector_manifest18_source
                  ON vector_sync_manifest_v18(source_type,source_id,person_id);
                CREATE INDEX IF NOT EXISTS idx_vector_manifest18_active
                  ON vector_sync_manifest_v18(person_id,active);
                """
            )
            con.commit()

    def _active_payload(payload: dict[str, Any]) -> tuple[bool, str]:
        status = str(
            payload.get("truth_status")
            or payload.get("status")
            or payload.get("lifecycle_status")
            or "active"
        ).lower()
        active = status not in {
            "obsolete",
            "invalidated",
            "deleted",
            "retracted",
            "superseded",
            "quarantined",
            "failed",
            "error",
        }
        return active, status

    def _sync_vectors_untracked(
        *,
        person_id: str,
        limit: int | None = None,
        conversation_id: str | None = None,
        incremental: bool = True,
    ) -> dict[str, Any]:
        owner_scope = _require_person_id(person_id, purpose="vector projection")
        ensure_vector_sync_manifest_schema()
        if conversation_id:
            with connect() as con:
                _require_conversation_scope(
                    con,
                    conversation_id=conversation_id,
                    person_id=owner_scope,
                    purpose="vector projection",
                )
        settings = module.get_settings()
        embedder = module.get_embedder()
        store = module.get_vector_store(vector_size=embedder.dims)
        scanned = synced = skipped = retracted = skipped_owner = skipped_ownerless = 0
        by_type: dict[str, int] = {}
        batch: list[Any] = []
        manifests: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal synced, batch, manifests
            if not batch:
                return
            store.upsert(batch)
            with connect() as con, write_transaction(con):
                for manifest in manifests:
                    upsert(con, "vector_sync_manifest_v18", manifest, "point_id")
            synced += len(batch)
            batch = []
            manifests = []

        for payload in old_iter(limit=limit, conversation_id=conversation_id):
            scanned += 1
            owner = str(payload.get("person_id") or "")
            if not owner:
                skipped_ownerless += 1
                continue
            if owner != owner_scope:
                skipped_owner += 1
                continue
            source_type = str(payload["source_type"])
            source_id = str(payload["source_id"])
            point_id = vector_point_id(
                person_id=owner_scope, source_type=source_type, source_id=source_id
            )
            active, status = _active_payload(payload)
            digest = stable_id("vector18", payload)
            with connect() as con:
                previous = con.execute(
                    "SELECT payload_sha256,active FROM vector_sync_manifest_v18 WHERE point_id=?",
                    (point_id,),
                ).fetchone()
            if previous and previous["payload_sha256"] == digest and bool(previous["active"]) == active and incremental:
                skipped += 1
                continue
            if not active:
                vec = [0.0] * embedder.dims
                tombstone = {
                    "source_type": source_type,
                    "source_id": source_id,
                    "person_id": owner_scope,
                    "active": False,
                    "truth_status": status,
                    "text": "",
                }
                batch.append(module.VectorPoint(point_id=point_id, vector=vec, payload=tombstone))
                retracted += 1
            else:
                text = str(payload.get("text") or "")
                if not text:
                    continue
                clean = {
                    **payload,
                    "person_id": owner_scope,
                    "active": True,
                    "truth_status": status,
                    "source_version": payload.get("source_version") or digest,
                }
                batch.append(
                    module.VectorPoint(point_id=point_id, vector=embedder.embed(text), payload=clean)
                )
                by_type[source_type] = by_type.get(source_type, 0) + 1
            manifests.append(
                {
                    "point_id": point_id,
                    "source_type": source_type,
                    "source_id": source_id,
                    "payload_sha256": digest,
                    "person_id": owner_scope,
                    "conversation_id": payload.get("conversation_id"),
                    "active": 1 if active else 0,
                    "truth_status": status,
                    "source_version": payload.get("source_version") or digest,
                    "synced_at": now_iso(),
                    "retracted_at": None if active else now_iso(),
                    "metadata_json": json_dumps(
                        {"backend": settings.vector_backend, "model": embedder.model_name}
                    ),
                }
            )
            if len(batch) >= 64:
                flush()
        flush()
        return {
            "backend": settings.vector_backend,
            "collection": settings.qdrant_collection,
            "model": embedder.model_name,
            "dims": embedder.dims,
            "person_id": owner_scope,
            "scanned": scanned,
            "synced": synced,
            "skipped_unchanged": skipped,
            "skipped_cross_owner": skipped_owner,
            "skipped_ownerless": skipped_ownerless,
            "retracted": retracted,
            "incremental": incremental,
            "conversation_id": conversation_id,
            "by_type": by_type,
        }

    def sync_vectors(
        limit: int | None = None,
        conversation_id: str | None = None,
        full: bool = False,
        *,
        person_id: str,
    ) -> dict[str, Any]:
        owner = _require_person_id(person_id, purpose="sync_vectors")
        ensure_v18_schema()
        if conversation_id:
            with connect() as con:
                _require_conversation_scope(
                    con, conversation_id=conversation_id, person_id=owner, purpose="sync_vectors"
                )
        settings = module.get_settings()
        return module.run_or_create_sync_job(
            backend=f"vector:{settings.vector_backend}",
            operation="upsert_full" if full else "upsert_incremental",
            target_table="all_memory" if conversation_id is None else "conversation",
            target_id=owner if conversation_id is None else conversation_id,
            conversation_id=conversation_id,
            payload={
                "schema": "v18_sync_job",
                "person_id": owner,
                "limit": limit,
                "conversation_id": conversation_id,
                "incremental": not full,
                "reason": "manual_full" if full else "manual_incremental",
            },
            work=lambda: _sync_vectors_untracked(
                limit=limit,
                conversation_id=conversation_id,
                incremental=not full,
                person_id=owner,
            ),
        )

    return {
        "ensure_vector_sync_manifest_schema": ensure_vector_sync_manifest_schema,
        "_sync_vectors_untracked": _sync_vectors_untracked,
        "sync_vectors": sync_vectors,
    }
