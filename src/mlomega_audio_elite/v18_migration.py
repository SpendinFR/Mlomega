"""V18 controlled migration of legacy V17 data.

The V17 database has many historical tables that cannot be made trustworthy by a
single ALTER TABLE.  This module deliberately distinguishes *inspection* from
*safe migration*: it only creates ownership proofs where a configured user
profile makes the owner unambiguous, and it quarantines ambiguous material
instead of assigning the default user.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .db import connect, write_transaction
from .governance_v18 import ensure_v18_schema, register_conversation_scope_in_transaction, strict_many
from .integrity_v176 import quarantine_in_transaction
from .utils import json_dumps, now_iso
from .integrity_v176 import new_id


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS v18_legacy_migration_runs(
  migration_run_id TEXT PRIMARY KEY,
  requested_person_id TEXT,
  mode TEXT NOT NULL CHECK(mode IN ('inspect','apply')),
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed','partial')),
  findings_count INTEGER NOT NULL DEFAULT 0,
  migrated_count INTEGER NOT NULL DEFAULT 0,
  quarantined_count INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '{}',
  error_text TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS v18_legacy_migration_findings(
  finding_id TEXT PRIMARY KEY,
  migration_run_id TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
  category TEXT NOT NULL,
  source_table TEXT,
  source_id TEXT,
  person_id TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  action TEXT NOT NULL CHECK(action IN ('none','scope_registered','quarantined','manual_review')),
  created_at TEXT NOT NULL,
  FOREIGN KEY(migration_run_id) REFERENCES v18_legacy_migration_runs(migration_run_id) ON DELETE CASCADE,
  UNIQUE(migration_run_id, category, source_table, source_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_v18_migration_findings_run ON v18_legacy_migration_findings(migration_run_id, severity, action);
"""


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    source_table: str
    source_id: str
    person_id: str | None
    detail: dict[str, Any]
    proposed_action: str


def _tables(con) -> set[str]:
    return {str(row["name"]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _self_profiles(con) -> set[str]:
    if "speaker_profiles" not in _tables(con):
        return set()
    return {
        str(row["person_id"])
        for row in con.execute("SELECT person_id FROM speaker_profiles WHERE COALESCE(is_user,0)=1").fetchall()
        if row["person_id"]
    }


def ensure_v18_migration_schema() -> None:
    ensure_v18_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)


def inspect_legacy_database(*, requested_person_id: str | None = None, limit: int = 5000) -> list[Finding]:
    """Return deterministic, non-mutating findings for legacy material.

    A conversation is eligible for a scope proof only if exactly one speaker
    profile explicitly marked ``is_user=1`` appears in its turns.  A discussion
    with two known people is not assumed to belong to whichever label happened
    to be written last.
    """
    ensure_v18_migration_schema()
    findings: list[Finding] = []
    with connect() as con:
        tables = _tables(con)
        self_ids = _self_profiles(con)
        if "conversations" in tables and "turns" in tables:
            rows = strict_many(
                con,
                """SELECT c.conversation_id, GROUP_CONCAT(DISTINCT NULLIF(TRIM(t.person_id),'')) AS turn_owners,
                          COUNT(t.turn_id) AS turn_count
                   FROM conversations c LEFT JOIN turns t ON t.conversation_id=c.conversation_id
                   GROUP BY c.conversation_id ORDER BY c.created_at,c.conversation_id LIMIT ?""",
                (max(1, int(limit)),),
                purpose="legacy conversation ownership inspection",
            )
            for row in rows:
                cid = str(row["conversation_id"])
                current = con.execute(
                    "SELECT person_id FROM v18_conversation_scopes WHERE conversation_id=? AND active=1", (cid,)
                ).fetchall()
                if current:
                    continue
                owners = {value.strip() for value in str(row.get("turn_owners") or "").split(",") if value and value.strip()}
                candidates = owners & self_ids
                # A requested profile narrows the *migration run*, not the
                # evidence.  Two user profiles in the same legacy conversation
                # remain ambiguous; filtering first would let an operator turn a
                # cross-owner record into an apparently valid proof.
                eligible_owner = next(iter(candidates)) if len(candidates) == 1 else None
                if eligible_owner and requested_person_id and eligible_owner != requested_person_id:
                    continue
                if eligible_owner:
                    findings.append(Finding(
                        "info", "legacy_conversation_scope_eligible", "conversations", cid, eligible_owner,
                        {"turn_count": int(row.get("turn_count") or 0), "turn_owners": sorted(owners), "self_profiles": sorted(self_ids)},
                        "scope_registered",
                    ))
                elif not owners:
                    findings.append(Finding(
                        "warning", "legacy_conversation_owner_missing", "conversations", cid, None,
                        {"turn_count": int(row.get("turn_count") or 0)}, "manual_review",
                    ))
                else:
                    findings.append(Finding(
                        "warning", "legacy_conversation_owner_ambiguous", "conversations", cid, None,
                        {"turn_count": int(row.get("turn_count") or 0), "turn_owners": sorted(owners), "self_profiles": sorted(self_ids)},
                        "quarantined",
                    ))

            duplicates = strict_many(
                con,
                """SELECT conversation_id,idx,COUNT(*) AS n
                   FROM turns GROUP BY conversation_id,idx HAVING COUNT(*)>1
                   ORDER BY conversation_id,idx LIMIT ?""",
                (max(1, int(limit)),),
                purpose="legacy duplicate turn index inspection",
            )
            for row in duplicates:
                findings.append(Finding(
                    "error", "legacy_duplicate_turn_index", "turns", f"{row['conversation_id']}:{row['idx']}", None,
                    {"conversation_id": row["conversation_id"], "idx": row["idx"], "count": row["n"]}, "quarantined",
                ))

        if {"brainlive_prediction_outcomes", "brainlive_short_horizon_forecasts"}.issubset(tables):
            outcomes = strict_many(
                con,
                """SELECT o.outcome_id,o.forecast_id,o.person_id AS outcome_person_id,f.person_id AS forecast_person_id
                   FROM brainlive_prediction_outcomes o
                   LEFT JOIN brainlive_short_horizon_forecasts f ON f.forecast_id=o.forecast_id
                   WHERE o.forecast_id IS NOT NULL AND (f.forecast_id IS NULL OR f.person_id<>o.person_id)
                   ORDER BY o.created_at,o.outcome_id LIMIT ?""",
                (max(1, int(limit)),),
                purpose="legacy forecast outcome integrity inspection",
            )
            for row in outcomes:
                category = "legacy_orphan_forecast_outcome" if row.get("forecast_person_id") is None else "legacy_cross_owner_forecast_outcome"
                findings.append(Finding(
                    "error", category, "brainlive_prediction_outcomes", str(row["outcome_id"]), str(row.get("outcome_person_id") or "") or None,
                    {"forecast_id": row.get("forecast_id"), "forecast_person_id": row.get("forecast_person_id")}, "quarantined",
                ))

        if "brain2_observed_cases_v17" in tables:
            ownerless = strict_many(
                con,
                "SELECT observed_case_id FROM brain2_observed_cases_v17 WHERE person_id IS NULL OR TRIM(person_id)='' ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
                purpose="legacy V17 owner inspection",
            )
            for row in ownerless:
                findings.append(Finding(
                    "error", "legacy_ownerless_observed_case", "brain2_observed_cases_v17", str(row["observed_case_id"]), None,
                    {}, "quarantined",
                ))
    return findings


def _write_findings(con, *, run_id: str, findings: Iterable[Finding], apply: bool) -> tuple[int, int]:
    migrated = quarantined = 0
    for finding in findings:
        action = "none"
        if apply and finding.proposed_action == "scope_registered" and finding.person_id:
            register_conversation_scope_in_transaction(
                con,
                conversation_id=finding.source_id,
                person_id=finding.person_id,
                evidence_kind="migration",
                evidence={"migration_run_id": run_id, **finding.detail},
            )
            action = "scope_registered"
            migrated += 1
        elif apply and finding.proposed_action == "quarantined":
            quarantine_in_transaction(
                con,
                category=finding.category,
                reason="V18 migration found ambiguous or structurally invalid legacy material.",
                raw_payload=finding.detail,
                run_id=run_id,
                source_table=finding.source_table,
                source_id=finding.source_id,
                person_id=finding.person_id,
            )
            action = "quarantined"
            quarantined += 1
        elif finding.proposed_action == "manual_review":
            action = "manual_review"
        con.execute(
            """INSERT INTO v18_legacy_migration_findings(
                 finding_id,migration_run_id,severity,category,source_table,source_id,person_id,detail_json,action,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(migration_run_id,category,source_table,source_id,person_id) DO UPDATE SET
                 detail_json=excluded.detail_json,action=excluded.action""",
            (new_id("migration_finding"), run_id, finding.severity, finding.category, finding.source_table,
             finding.source_id, finding.person_id, json_dumps(finding.detail), action, now_iso()),
        )
    return migrated, quarantined


def run_legacy_migration(*, requested_person_id: str | None = None, apply: bool = False, limit: int = 5000) -> dict[str, Any]:
    """Inspect or safely apply the V18 migration for an existing SQLite DB."""
    ensure_v18_migration_schema()
    run_id = new_id("legacy_migration")
    findings = inspect_legacy_database(requested_person_id=requested_person_id, limit=limit)
    with connect() as con, write_transaction(con):
        con.execute(
            """INSERT INTO v18_legacy_migration_runs(
                 migration_run_id,requested_person_id,mode,status,findings_count,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (run_id, requested_person_id, "apply" if apply else "inspect", "running", len(findings), now_iso()),
        )
        migrated, quarantined = _write_findings(con, run_id=run_id, findings=findings, apply=apply)
        status = "completed" if not any(f.severity == "error" for f in findings) else ("partial" if apply else "completed")
        con.execute(
            """UPDATE v18_legacy_migration_runs SET status=?,migrated_count=?,quarantined_count=?,result_json=?,finished_at=?
               WHERE migration_run_id=?""",
            (status, migrated, quarantined, json_dumps({
                "findings": len(findings), "by_category": {
                    category: sum(1 for f in findings if f.category == category)
                    for category in sorted({f.category for f in findings})
                },
            }), now_iso(), run_id),
        )
    return {
        "migration_run_id": run_id,
        "mode": "apply" if apply else "inspect",
        "findings_count": len(findings),
        "migrated_count": migrated,
        "quarantined_count": quarantined,
        "findings": [
            {"severity": f.severity, "category": f.category, "source_table": f.source_table,
             "source_id": f.source_id, "person_id": f.person_id, "action": f.proposed_action, "detail": f.detail}
            for f in findings
        ],
    }
