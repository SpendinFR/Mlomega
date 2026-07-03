"""V18 owner-scoped safety gate for Graphiti and Mem0 projections.

Canonical SQLite is the source of truth.  External adapters are explicitly
scoped projections and are disabled by default when their adapter cannot prove
update/delete semantics.  This module prevents cross-owner exports and makes
all projection status visible in the V18 ledger.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from .db import connect, write_transaction
from .governance_v18 import ScopeError, conversation_in_scope, ensure_v18_schema
from .utils import json_dumps, now_iso, stable_id


def _append_only_allowed() -> bool:
    return os.environ.get("MLOMEGA_V18_ALLOW_APPEND_ONLY_EXTERNAL", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _owner_for_conversation(con, conversation_id: str) -> str:
    rows = con.execute(
        "SELECT person_id FROM v18_conversation_scopes WHERE conversation_id=? AND active=1",
        (conversation_id,),
    ).fetchall()
    owners = {str(row["person_id"]) for row in rows if row["person_id"]}
    if len(owners) != 1:
        raise ScopeError("external sync requires exactly one explicit conversation owner")
    return next(iter(owners))


def _require_scope(conversation_id: str, person_id: str) -> str:
    if not isinstance(person_id, str) or not person_id.strip():
        raise ScopeError("external sync requires explicit person_id")
    owner = person_id.strip()
    with connect() as con:
        # Both checks matter: no fallback to a turn owner, and no export if a
        # legacy DB recorded two mutually incompatible ownership claims.
        if not conversation_in_scope(
            con,
            conversation_id=conversation_id,
            person_id=owner,
            allow_legacy_turn_proof=False,
        ):
            raise ScopeError("external sync denied: conversation is not explicitly scoped to person_id")
        canonical_owner = _owner_for_conversation(con, conversation_id)
    if canonical_owner != owner:
        raise ScopeError("external sync denied: supplied person_id differs from conversation owner")
    return owner


def _mark(
    backend: str,
    conversation_id: str,
    person_id: str,
    *,
    active: bool,
    status: str,
    detail: dict[str, Any],
) -> None:
    """Record one external projection without cross-owner overwrite."""
    ensure_v18_schema()
    sync_id = stable_id("external18", backend, "conversations", conversation_id, person_id)
    now = now_iso()
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO v18_external_sync_manifest(
                   external_sync_id,backend,source_table,source_id,person_id,
                   source_version,payload_sha256,truth_status,active,
                   external_ref_json,synced_at,retracted_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(backend,source_table,source_id,person_id) DO UPDATE SET
                 source_version=excluded.source_version,
                 payload_sha256=excluded.payload_sha256,
                 truth_status=excluded.truth_status,
                 active=excluded.active,
                 external_ref_json=excluded.external_ref_json,
                 synced_at=excluded.synced_at,
                 retracted_at=excluded.retracted_at""",
            (
                sync_id,
                backend,
                "conversations",
                conversation_id,
                person_id,
                detail.get("source_version"),
                stable_id(detail),
                status,
                1 if active else 0,
                json_dumps(detail),
                now,
                None if active else now,
            ),
        )


def install(module: Any) -> dict[str, Any]:
    """Replace legacy public and private exporters with scoped V18 wrappers."""
    legacy_graphiti = module._sync_graphiti_untracked
    legacy_mem0 = module._sync_mem0_untracked

    def _gate(
        backend: str,
        conversation_id: str,
        person_id: str,
        legacy_fn: Callable[[str], dict[str, Any]],
    ) -> dict[str, Any]:
        owner = _require_scope(conversation_id, person_id)
        if not _append_only_allowed():
            result = {
                "status": "disabled_for_safety",
                "backend": backend,
                "conversation_id": conversation_id,
                "person_id": owner,
                "reason": "append-only adapter has no verified update/delete/tombstone contract",
            }
            _mark(backend, conversation_id, owner, active=False, status="disabled", detail=result)
            return result
        try:
            result = legacy_fn(conversation_id) or {}
        except Exception as exc:
            _mark(
                backend,
                conversation_id,
                owner,
                active=False,
                status="failed",
                detail={"error": str(exc)[:2000], "person_id": owner},
            )
            raise
        detail = {
            "result": result,
            "source_version": stable_id(result),
            "person_id": owner,
            "conversation_id": conversation_id,
        }
        _mark(backend, conversation_id, owner, active=True, status="active", detail=detail)
        return {**result, "owner": owner, "v18_projection": "tracked_append_only_opt_in"}

    def _sync_graphiti_untracked(conversation_id: str, *, person_id: str) -> dict[str, Any]:
        return _gate("graphiti", conversation_id, person_id, legacy_graphiti)

    def _sync_mem0_untracked(conversation_id: str, *, person_id: str) -> dict[str, Any]:
        return _gate("mem0", conversation_id, person_id, legacy_mem0)

    def sync_graphiti(conversation_id: str, *, person_id: str) -> dict[str, Any]:
        owner = _require_scope(conversation_id, person_id)
        ensure_v18_schema()
        return module.run_or_create_sync_job(
            backend="graphiti",
            operation="upsert_conversation",
            target_table="conversations",
            target_id=conversation_id,
            conversation_id=conversation_id,
            payload={
                "schema": "v18_sync_job",
                "person_id": owner,
                "conversation_id": conversation_id,
                "reason": "manual_external_projection",
            },
            work=lambda: _sync_graphiti_untracked(conversation_id, person_id=owner),
        )

    def sync_mem0(conversation_id: str, *, person_id: str) -> dict[str, Any]:
        owner = _require_scope(conversation_id, person_id)
        ensure_v18_schema()
        return module.run_or_create_sync_job(
            backend="mem0",
            operation="upsert_conversation",
            target_table="conversations",
            target_id=conversation_id,
            conversation_id=conversation_id,
            payload={
                "schema": "v18_sync_job",
                "person_id": owner,
                "conversation_id": conversation_id,
                "reason": "manual_external_projection",
            },
            work=lambda: _sync_mem0_untracked(conversation_id, person_id=owner),
        )

    def sync_external_all(conversation_id: str, *, person_id: str) -> dict[str, Any]:
        owner = _require_scope(conversation_id, person_id)
        return {
            "conversation_id": conversation_id,
            "person_id": owner,
            "graphiti": sync_graphiti(conversation_id, person_id=owner),
            "mem0": sync_mem0(conversation_id, person_id=owner),
        }

    return {
        "_sync_graphiti_untracked": _sync_graphiti_untracked,
        "_sync_mem0_untracked": _sync_mem0_untracked,
        "sync_graphiti": sync_graphiti,
        "sync_mem0": sync_mem0,
        "sync_external_all": sync_external_all,
    }
