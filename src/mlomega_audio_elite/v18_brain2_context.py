"""V18 scoped Brain2 context addenda.

Derived visual observations must not be appended into the source dialogue.
This adapter exposes them to the strict V13 engines as explicitly labelled,
versioned context records limited to the conversation/episode window.
"""
from __future__ import annotations
from datetime import timedelta
import hashlib
from typing import Any

from .db import connect
from .governance_v18 import canonical_time, conversation_in_scope, projection_is_active, strict_many
from .integrity_v176 import parse_iso_utc
from .utils import json_dumps


def _scope_owner(con: Any, conversation_id: str) -> str | None:
    rows = strict_many(
        con,
        "SELECT person_id FROM v18_conversation_scopes WHERE conversation_id=? AND active=1 ORDER BY updated_at DESC",
        (conversation_id,),
        purpose="V18 context addendum owner",
    )
    if len(rows) == 1:
        return str(rows[0]["person_id"])
    return None


def _read_addenda(con: Any, *, conversation_id: str, owner: str | None, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    if not owner:
        return []
    # The addenda table is installed by the V18 post-stop output layer.  A
    # caller may legitimately run V13/V14 on an older or fresh database before
    # that layer has created any deep-vision output, so absence means "no system
    # addenda", never a failed cognitive run.
    present = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain2_context_addenda_v18'"
    ).fetchone()
    if not present:
        return []
    clauses = ["person_id=?", "conversation_id=?", "status='active'"]
    params: list[Any] = [owner, conversation_id]
    if start:
        clauses.append("event_time>=?"); params.append(start)
    if end:
        clauses.append("event_time<=?"); params.append(end)
    rows = strict_many(
        con,
        "SELECT * FROM brain2_context_addenda_v18 WHERE " + " AND ".join(clauses) + " ORDER BY event_time,addendum_id LIMIT 120",
        tuple(params),
        purpose="V18 context addenda",
    )
    safe: list[dict[str, Any]] = []
    for row in rows:
        source_table = str(row.get("source_table") or "")
        source_id = str(row.get("source_id") or "")
        kind = "deep_vision" if source_table == "brainlive_deep_vision_observations_v161" else "context"
        if not projection_is_active(con, projection_kind=kind, source_table=source_table, source_id=source_id, person_id=owner):
            continue
        safe.append({
            "addendum_id": row["addendum_id"], "source_table": source_table, "source_id": source_id,
            "event_time": row["event_time"], "evidence_role": row["evidence_role"],
            "text": row["text"], "metadata_json": row.get("metadata_json"),
            "scope": {"person_id": owner, "conversation_id": conversation_id},
        })
    return safe


def active_brain2_conversation_ids(
    con: Any,
    *,
    person_id: str,
    limit: int = 120,
) -> list[str]:
    """Return the canonical active conversations for a Brain2 global reader.

    A V18.5 deep-audio re-export intentionally preserves the fast live
    conversation for audit but deactivates its scope and export.  Global V13/V14
    readers must therefore use this resolver instead of scanning ``turns`` or
    ``conversations`` directly, otherwise the same scene would be analysed
    twice.  Ordinary scoped conversations remain eligible when they have no
    BrainLive export record.
    """
    if not person_id:
        raise ValueError("active Brain2 conversation selection requires person_id")
    if limit < 1:
        raise ValueError("active Brain2 conversation limit must be positive")
    exports_present = bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brainlive_brain2_event_exports_v1514'"
    ).fetchone())
    export_filter = ""
    if exports_present:
        export_filter = """ AND (
            NOT EXISTS(SELECT 1 FROM brainlive_brain2_event_exports_v1514 ex_any
                       WHERE ex_any.conversation_id=c.conversation_id)
            OR EXISTS(SELECT 1 FROM brainlive_brain2_event_exports_v1514 ex_active
                      WHERE ex_active.conversation_id=c.conversation_id
                        AND ex_active.export_status IN ('active','ok','exported'))
        )"""
    rows = strict_many(
        con,
        f"""SELECT DISTINCT c.conversation_id
            FROM conversations c
            JOIN v18_conversation_scopes cs ON cs.conversation_id=c.conversation_id
            WHERE cs.person_id=? AND cs.active=1 {export_filter}
            ORDER BY COALESCE(c.started_at,c.created_at) DESC,c.conversation_id DESC LIMIT ?""",
        (person_id, int(limit)),
        purpose="active Brain2 conversations",
    )
    return [str(row["conversation_id"]) for row in rows]


def active_brain2_conversation(
    con: Any, *, conversation_id: str, person_id: str
) -> bool:
    """True only for a scoped, non-superseded Brain2 conversation revision."""
    return str(conversation_id) in set(active_brain2_conversation_ids(con, person_id=person_id, limit=100000))


def conversation_context_addenda(
    con: Any,
    *,
    conversation_id: str,
    person_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    max_items: int = 24,
    max_chars: int = 24000,
) -> dict[str, Any]:
    """Return bounded, source-addressable system context for Brain2 engines.

    Deep VLM descriptions are useful to Brain2, but they are not dialogue.
    This envelope keeps them separate from ``turns`` and never silently cuts a
    description in half: an item either fits completely or is represented as
    an omitted source reference.  Callers pass the envelope as
    ``context_addenda`` to their contract/prompt.
    """
    if max_items < 1 or max_chars < 1:
        raise ValueError("context addenda limits must be positive")
    owner = person_id or _scope_owner(con, conversation_id)
    rows = _read_addenda(con, conversation_id=conversation_id, owner=owner, start=start, end=end)
    selected: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    chars = 0
    for row in rows:
        text = str(row.get("text") or "")
        raw_metadata = str(row.get("metadata_json") or "")
        # Metadata can contain a large raw provider payload.  It is provenance,
        # not prompt text: retain it only when compact and otherwise emit a
        # hash/reference explicitly instead of truncating it invisibly.
        metadata_included = raw_metadata if len(raw_metadata) <= 4096 else None
        candidate = {
            "addendum_id": row.get("addendum_id"),
            "source_table": row.get("source_table"),
            "source_id": row.get("source_id"),
            "event_time": row.get("event_time"),
            "evidence_role": row.get("evidence_role"),
            "text": text,
            "metadata_json": metadata_included,
            "metadata_omitted": metadata_included is None and bool(raw_metadata),
            "metadata_sha256": hashlib.sha256(raw_metadata.encode("utf-8")).hexdigest() if raw_metadata else None,
            "scope": row.get("scope"),
        }
        candidate_chars = len(json_dumps(candidate))
        if len(selected) >= max_items or chars + candidate_chars > max_chars:
            omitted.append({
                "addendum_id": row.get("addendum_id"),
                "source_table": row.get("source_table"),
                "source_id": row.get("source_id"),
                "event_time": row.get("event_time"),
                "evidence_role": row.get("evidence_role"),
                "reason": "context_addendum_budget",
            })
            continue
        selected.append(candidate)
        chars += candidate_chars
    return {
        "entries": selected,
        "budget": {
            "start": start,
            "end": end,
            "max_items": int(max_items),
            "max_chars": int(max_chars),
            "included_items": len(selected),
            "included_chars": chars,
            "omitted_refs": omitted,
            "context_incomplete": bool(omitted),
        },
        "evidence_role_policy": "context_addenda are sensor/system evidence, never user speech or a declared preference",
    }


def install(module: Any) -> dict[str, Any]:
    old_conversation_bundle = module._conversation_bundle
    old_episode_bundle = module._episode_bundle
    old_build = module.build_strict_v13_for_conversation
    old_all = module.build_strict_v13_all

    def _conversation_bundle(con: Any, conversation_id: str) -> dict[str, Any]:
        bundle = old_conversation_bundle(con, conversation_id)
        owner = _scope_owner(con, conversation_id)
        bundle["context_addenda"] = conversation_context_addenda(
            con, conversation_id=conversation_id, person_id=owner, max_items=24, max_chars=24000
        )
        bundle["context_scope"] = {"conversation_id": conversation_id, "person_id": owner, "addenda_are_system_context": True}
        return bundle

    def _episode_bundle(con: Any, episode_id: str) -> dict[str, Any]:
        bundle = old_episode_bundle(con, episode_id)
        ep = bundle.get("episode") or {}
        conv_id = str(ep.get("source_conversation_id") or "")
        owner = _scope_owner(con, conv_id) if conv_id else None
        start = canonical_time(ep, "start_time")
        end = canonical_time(ep, "end_time")
        # Add a small explicit visual context border.  It cannot expand into a
        # conversation-wide dump, and every addendum remains source-addressable.
        if start:
            try:
                start = (parse_iso_utc(start) - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
            except Exception:
                pass
        if end:
            try:
                end = (parse_iso_utc(end) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
            except Exception:
                pass
        bundle["context_addenda"] = conversation_context_addenda(
            con, conversation_id=conv_id, person_id=owner, start=start, end=end, max_items=12, max_chars=12000
        )
        bundle.setdefault("context_scope", {}).update({"person_id": owner, "addenda_local_window": True})
        return bundle

    def build_strict_v13_for_conversation(conversation_id: str, *, max_episodes: int | None = None, person_id: str | None = None) -> dict[str, Any]:
        if not person_id:
            raise ValueError("V18 strict V13 requires an explicit person_id")
        with connect() as con:
            if not conversation_in_scope(con, conversation_id=conversation_id, person_id=person_id):
                raise ValueError("conversation is not proven in supplied person scope")
        # Make the scoped addenda visible to the legacy builder through the
        # patched globals. Its own prompt boundary remains _safe_prompt_payload.
        module._conversation_bundle = _conversation_bundle
        module._episode_bundle = _episode_bundle
        return old_build(conversation_id, max_episodes=max_episodes, person_id=person_id)

    def build_strict_v13_all(*, max_episodes_per_conversation: int | None = None) -> dict[str, Any]:
        # Only conversations with a single explicit active owner can be run in
        # batch. Ambiguous legacy conversations are reported, not guessed.
        with connect() as con:
            rows = strict_many(con, "SELECT conversation_id,person_id FROM v18_conversation_scopes WHERE active=1 ORDER BY conversation_id,person_id", (), purpose="V18 batch strict scopes")
        seen: dict[str, list[str]] = {}
        for row in rows:
            seen.setdefault(str(row["conversation_id"]), []).append(str(row["person_id"]))
        results=[]; skipped=[]
        for cid,owners in seen.items():
            if len(set(owners)) != 1:
                skipped.append({"conversation_id":cid,"reason":"ambiguous_owner_scope"}); continue
            results.append(build_strict_v13_for_conversation(cid, max_episodes=max_episodes_per_conversation, person_id=owners[0]))
        return {"version":"18.0.0-strict-context", "conversations":len(results), "results":results, "skipped":skipped}

    return {
        "_conversation_bundle": _conversation_bundle,
        "_episode_bundle": _episode_bundle,
        "build_strict_v13_for_conversation": build_strict_v13_for_conversation,
        "build_strict_v13_all": build_strict_v13_all,
    }
