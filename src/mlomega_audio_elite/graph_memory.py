from __future__ import annotations

import json
from sqlite3 import Connection

from .db import upsert
from .utils import normalize_text, now_iso, stable_id


def entity_id(entity_type: str, name: str) -> str:
    return stable_id("ent", entity_type, normalize_text(name))


def ensure_entity(con: Connection, entity_type: str, name: str, aliases: list[str] | None = None, metadata: dict | None = None) -> str:
    eid = entity_id(entity_type, name)
    row = con.execute("SELECT entity_id, aliases_json, metadata_json, created_at FROM entities WHERE entity_id=?", (eid,)).fetchone()
    if row:
        old_aliases = set(json.loads(row["aliases_json"] or "[]"))
        old_aliases.update(aliases or [])
        metadata_now = json.loads(row["metadata_json"] or "{}")
        metadata_now.update(metadata or {})
        upsert(con, "entities", {
            "entity_id": eid,
            "type": entity_type,
            "name": name,
            "canonical_name": normalize_text(name),
            "aliases_json": json.dumps(sorted(old_aliases), ensure_ascii=False),
            "metadata_json": json.dumps(metadata_now, ensure_ascii=False),
            "created_at": row["created_at"],
            "updated_at": now_iso(),
        }, "entity_id")
    else:
        upsert(con, "entities", {
            "entity_id": eid,
            "type": entity_type,
            "name": name,
            "canonical_name": normalize_text(name),
            "aliases_json": json.dumps(aliases or [], ensure_ascii=False),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }, "entity_id")
    return eid


def add_relation(
    con: Connection,
    from_entity: str,
    relation_type: str,
    to_entity: str,
    *,
    valid_from: str | None = None,
    valid_until: str | None = None,
    confidence: float = 0.75,
    evidence_type: str | None = None,
    evidence_id: str | None = None,
    context: dict | None = None,
    invalidate_previous: bool = False,
) -> str:
    if invalidate_previous and valid_from:
        con.execute(
            """UPDATE relations SET valid_until=?
               WHERE from_entity_id=? AND relation_type=? AND valid_until IS NULL""",
            (valid_from, from_entity, relation_type),
        )
    rid = stable_id("rel", from_entity, relation_type, to_entity, valid_from, evidence_id)
    upsert(con, "relations", {
        "relation_id": rid,
        "from_entity_id": from_entity,
        "relation_type": relation_type,
        "to_entity_id": to_entity,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "confidence": confidence,
        "evidence_type": evidence_type,
        "evidence_id": evidence_id,
        "context_json": json.dumps(context or {}, ensure_ascii=False),
        "created_at": now_iso(),
    }, "relation_id")
    return rid
