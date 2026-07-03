"""Executable V18 release-readiness audit.

This is deliberately a data integrity gate, not a cosmetic doctor command.
It checks whether the V18 primitives are actually being used coherently in the
current SQLite database: resumable pipelines, retained-output manifests,
projection invalidation, durable leases and legacy-migration findings.

It does not claim to validate GPU/LLM/network media runtime.  Those are kept as
explicit external gates rather than being turned into a misleading green status.
"""
from __future__ import annotations

from dataclasses import dataclass
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
import os

from .db import connect, write_transaction
from .governance_v18 import ensure_v18_schema, strict_many, strict_one
from .integrity_v176 import iso_utc, new_id, parse_iso_utc
from .utils import json_dumps, now_iso
from .v18_owner_scope import legacy_implicit_owner_enabled


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS v18_release_audit_runs(
  audit_run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('ok','attention','fail')),
  strict INTEGER NOT NULL DEFAULT 0 CHECK(strict IN (0,1)),
  stale_after_seconds INTEGER NOT NULL,
  issue_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  warning_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v18_release_audit_created
  ON v18_release_audit_runs(created_at DESC);
"""


@dataclass(frozen=True)
class AuditIssue:
    code: str
    severity: str  # info | warning | error
    message: str
    refs: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "refs": [dict(ref) for ref in self.refs],
        }


def ensure_v18_release_audit_schema() -> None:
    ensure_v18_schema()
    # Keep this import local: migration imports the governance kernel and a
    # module-level cycle would make the doctor unavailable during init-db.
    from .v18_migration import ensure_v18_migration_schema

    ensure_v18_migration_schema()
    with connect() as con, write_transaction(con):
        con.executescript(SCHEMA)


def _table_exists(con, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _parse_or_none(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    return parse_iso_utc(str(value))


def _issues_for_pipeline_runs(con, *, cutoff: datetime) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    rows = strict_many(
        con,
        """SELECT r.run_id,r.pipeline_name,r.person_id,r.status,r.started_at,
                  s.stage_name,s.status AS stage_status,s.required,s.started_at AS stage_started_at,
                  m.person_id AS manifest_person_id,m.complete AS manifest_complete
           FROM v18_pipeline_runs r
           LEFT JOIN v18_pipeline_stages s ON s.run_id=r.run_id
           LEFT JOIN v18_pipeline_output_manifests m ON m.run_id=r.run_id
           ORDER BY r.started_at,r.run_id,s.stage_name""",
        purpose="release audit pipeline integrity",
    )
    bad_manifest: list[dict[str, Any]] = []
    bad_stage: list[dict[str, Any]] = []
    bad_required_stage: list[dict[str, Any]] = []
    bad_run_time: list[dict[str, Any]] = []
    for row in rows:
        if row.get("pipeline_name") == "brainlive_post_stop" and row.get("status") == "completed":
            complete = row.get("manifest_complete")
            if complete is None or int(complete) != 1 or row.get("manifest_person_id") != row.get("person_id"):
                bad_manifest.append(
                    {
                        "run_id": row["run_id"],
                        "person_id": row["person_id"],
                        "manifest_person_id": row.get("manifest_person_id"),
                        "manifest_complete": complete,
                    }
                )
        if int(row.get("required") or 0) == 1 and row.get("stage_status") not in {"completed", None}:
            bad_required_stage.append({
                "run_id": row["run_id"], "pipeline_name": row.get("pipeline_name"),
                "stage_name": row.get("stage_name"), "status": row.get("stage_status"),
            })
        if row.get("stage_status") == "running":
            raw = row.get("stage_started_at")
            try:
                started = _parse_or_none(raw)
                stale = started is None or started <= cutoff
            except Exception as exc:
                bad_stage.append(
                    {"run_id": row["run_id"], "stage_name": row.get("stage_name"), "started_at": raw, "reason": f"invalid timestamp: {exc}"}
                )
            else:
                if stale:
                    bad_stage.append(
                        {"run_id": row["run_id"], "stage_name": row.get("stage_name"), "started_at": raw, "reason": "stale running stage"}
                    )
        if row.get("status") in {"started", "running"}:
            try:
                started = _parse_or_none(row.get("started_at"))
                if started is None or started <= cutoff:
                    bad_run_time.append(
                        {"run_id": row["run_id"], "pipeline_name": row["pipeline_name"], "started_at": row.get("started_at")}
                    )
            except Exception as exc:
                bad_run_time.append(
                    {"run_id": row["run_id"], "pipeline_name": row["pipeline_name"], "started_at": row.get("started_at"), "reason": f"invalid timestamp: {exc}"}
                )
    if bad_manifest:
        issues.append(
            AuditIssue(
                "post_stop_completed_without_retained_manifest",
                "error",
                "A completed post-stop run lacks a complete owner-matching retained-output manifest; cleanup must remain blocked.",
                tuple(bad_manifest),
            )
        )
    if bad_required_stage:
        issues.append(
            AuditIssue(
                "required_pipeline_stage_not_completed",
                "error",
                "A required pipeline stage is skipped, failed or otherwise incomplete; its outputs cannot unlock a production release.",
                tuple(bad_required_stage),
            )
        )
    if bad_stage:
        issues.append(
            AuditIssue(
                "stale_or_malformed_pipeline_stage",
                "error",
                "A running pipeline stage is stale or has malformed time metadata; recover or quarantine it before release.",
                tuple(bad_stage),
            )
        )
    if bad_run_time:
        issues.append(
            AuditIssue(
                "stale_or_malformed_pipeline_run",
                "warning",
                "An active pipeline run has exceeded the recovery threshold; it needs an explicit resume/recovery decision.",
                tuple(bad_run_time),
            )
        )
    return issues


def _issues_for_leases(con, *, cutoff: datetime) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    if not _table_exists(con, "v18_work_leases"):
        return issues
    rows = strict_many(
        con,
        """SELECT work_key,work_type,person_id,state,retry_after,lease_expires_at,attempt_count,max_attempts
           FROM v18_work_leases WHERE state IN ('leased','retryable_error')""",
        purpose="release audit work leases",
    )
    invalid: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for row in rows:
        field = "lease_expires_at" if row["state"] == "leased" else "retry_after"
        value = row.get(field)
        try:
            instant = _parse_or_none(value)
            if instant is None:
                raise ValueError("missing schedule timestamp")
        except Exception as exc:
            invalid.append({"work_key": row["work_key"], "person_id": row["person_id"], "field": field, "value": value, "reason": str(exc)})
            continue
        if instant <= cutoff:
            stale.append({"work_key": row["work_key"], "person_id": row["person_id"], "state": row["state"], field: value})
    if invalid:
        issues.append(
            AuditIssue(
                "lease_schedule_invalid",
                "error",
                "A nonterminal work lease has invalid or missing schedule time and could become permanently stuck.",
                tuple(invalid),
            )
        )
    if stale:
        issues.append(
            AuditIssue(
                "lease_recovery_required",
                "warning",
                "Expired work leases/retries require deterministic recovery or quarantine; they were not silently treated as complete.",
                tuple(stale),
            )
        )
    return issues


def _issues_for_invalidation(con) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    artifact_rows = strict_many(
        con,
        """SELECT a.artifact_version_id,a.artifact_table,a.artifact_id,a.person_id
           FROM v18_artifact_versions a
           JOIN v18_source_tombstones t
             ON t.source_table=a.artifact_table AND t.source_id=a.artifact_id AND t.person_id=a.person_id
           WHERE a.active=1 AND a.status='active'""",
        purpose="release audit active tombstoned artifacts",
    )
    if artifact_rows:
        issues.append(
            AuditIssue(
                "tombstoned_artifact_still_active",
                "error",
                "An invalidated source still has an active artifact version.",
                tuple(artifact_rows),
            )
        )
    projection_rows = strict_many(
        con,
        """SELECT p.projection_kind,p.source_table,p.source_id,p.person_id
           FROM v18_source_projection_state p
           JOIN v18_source_tombstones t
             ON t.source_table=p.source_table AND t.source_id=p.source_id AND t.person_id=p.person_id
           WHERE p.active=1""",
        purpose="release audit active tombstoned projections",
    )
    if projection_rows:
        issues.append(
            AuditIssue(
                "tombstoned_source_still_projected",
                "error",
                "An invalidated source remains active in a live/retrieval projection.",
                tuple(projection_rows),
            )
        )
    return issues


def _issues_for_migration(con) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    if not _table_exists(con, "v18_legacy_migration_runs"):
        return issues
    latest = strict_one(
        con,
        "SELECT migration_run_id,mode,status,created_at FROM v18_legacy_migration_runs ORDER BY created_at DESC,migration_run_id DESC LIMIT 1",
        purpose="release audit legacy migration latest run",
    )
    conversation_count = 0
    if _table_exists(con, "conversations"):
        conversation_count = int(con.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"])
    if conversation_count and not latest:
        issues.append(
            AuditIssue(
                "legacy_database_not_inspected",
                "warning",
                "The database contains historical conversations but no V18 legacy-migration inspection has been recorded.",
                ({"conversation_count": conversation_count},),
            )
        )
        return issues
    if not latest:
        return issues
    findings = strict_many(
        con,
        """SELECT severity,category,source_table,source_id,person_id,action
           FROM v18_legacy_migration_findings
           WHERE migration_run_id=? AND (severity='error' OR action='manual_review')
           ORDER BY severity DESC,category,source_table,source_id""",
        (latest["migration_run_id"],),
        purpose="release audit legacy migration findings",
    )
    if findings:
        severity = "error" if any(row["severity"] == "error" and row["action"] != "quarantined" for row in findings) else "warning"
        issues.append(
            AuditIssue(
                "legacy_migration_unresolved_findings",
                severity,
                "The latest migration inspection still contains structural findings requiring review or explicit quarantine.",
                tuple(findings),
            )
        )
    return issues


def _issues_for_owner_scope(con) -> list[AuditIssue]:
    """Detect impossible owner pairings in V18’s own durable tables."""
    issues: list[AuditIssue] = []
    manifest_rows = strict_many(
        con,
        """SELECT m.run_id,m.person_id AS manifest_person_id,r.person_id AS run_person_id
           FROM v18_pipeline_output_manifests m
           JOIN v18_pipeline_runs r ON r.run_id=m.run_id
           WHERE m.person_id<>r.person_id""",
        purpose="release audit manifest owner scope",
    )
    if manifest_rows:
        issues.append(
            AuditIssue(
                "pipeline_manifest_cross_owner",
                "error",
                "A retained-output manifest belongs to a different owner than its pipeline run.",
                tuple(manifest_rows),
            )
        )
    # An active conversation scope is a proof; contradictory active proofs
    # must be reviewed rather than allowing every downstream reader to choose.
    scope_rows = strict_many(
        con,
        """SELECT conversation_id,GROUP_CONCAT(person_id) AS owners,COUNT(*) AS n
           FROM v18_conversation_scopes WHERE active=1
           GROUP BY conversation_id HAVING COUNT(*)>1""",
        purpose="release audit duplicate active conversation scopes",
    )
    if scope_rows:
        issues.append(
            AuditIssue(
                "conversation_has_multiple_active_owners",
                "error",
                "A conversation has multiple simultaneous active owner proofs.",
                tuple(scope_rows),
            )
        )
    return issues



def _issues_for_legacy_owner_guards() -> list[AuditIssue]:
    """Verify that remaining legacy default-owner helpers cannot execute live."""
    issues: list[AuditIssue] = []
    source_dir = Path(__file__).resolve().parent
    missing: list[dict[str, Any]] = []
    for path in sorted(source_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "def _default_user(" not in text:
            continue
        if "reject_implicit_owner_fallback(__name__)" not in text:
            missing.append({"module": path.name})
    if missing:
        issues.append(
            AuditIssue(
                "legacy_default_owner_not_fail_closed",
                "error",
                "A legacy default-owner helper is reachable without the V18 explicit-owner fail-closed guard.",
                tuple(missing),
            )
        )
    if legacy_implicit_owner_enabled():
        issues.append(
            AuditIssue(
                "legacy_default_owner_escape_hatch_enabled",
                "error",
                "MLOMEGA_ALLOW_LEGACY_IMPLICIT_OWNER is enabled; release mode must never permit implicit owner selection.",
                ({"environment": "MLOMEGA_ALLOW_LEGACY_IMPLICIT_OWNER", "value": os.environ.get("MLOMEGA_ALLOW_LEGACY_IMPLICIT_OWNER")},),
            )
        )
    return issues


def _issues_for_legacy_forecast_lifecycle(con) -> list[AuditIssue]:
    """Ensure V14 forecasts cannot remain selectable by text status alone."""
    issues: list[AuditIssue] = []
    from .v18_legacy_forecasts import (
        LIFECYCLE_TABLE,
        ensure_legacy_forecast_lifecycle_schema,
        reconcile_legacy_forecasts,
    )

    ensure_legacy_forecast_lifecycle_schema(con)
    try:
        reconcile_legacy_forecasts(con=con)
    except Exception as exc:
        return [
            AuditIssue(
                "legacy_forecast_lifecycle_reconcile_failed",
                "error",
                "V14 forecast lifecycle reconciliation failed; legacy forecasts are not safe for live selection.",
                ({"error": str(exc)[:1000]},),
            )
        ]
    if not _table_exists(con, LIFECYCLE_TABLE):
        return [
            AuditIssue(
                "legacy_forecast_lifecycle_missing",
                "error",
                "The V18 lifecycle ledger for V14 forecasts is missing.",
            )
        ]
    invalid = strict_many(
        con,
        f"""SELECT source_table,source_id,person_id,lifecycle_state,due_at,expires_at
              FROM {LIFECYCLE_TABLE}
              WHERE lifecycle_state='open' AND (due_at IS NULL OR expires_at IS NULL OR due_at>=expires_at)""",
        purpose="release audit legacy forecast lifecycle deadlines",
    )
    if invalid:
        issues.append(
            AuditIssue(
                "legacy_forecast_open_without_bounded_deadline",
                "error",
                "A V14 forecast is marked open without a valid V18 due/expiry window; it must be indeterminate or terminal.",
                tuple(invalid),
            )
        )
    due = strict_many(
        con,
        f"""SELECT source_table,source_id,person_id,due_at,expires_at
              FROM {LIFECYCLE_TABLE}
              WHERE lifecycle_state='due'""",
        purpose="release audit legacy forecast due outcomes",
    )
    if due:
        issues.append(
            AuditIssue(
                "legacy_forecast_due_needs_outcome",
                "warning",
                "V14 forecasts reached their due time and are excluded from live use until an explicit V18 outcome closes them.",
                tuple(due),
            )
        )
    return issues


def _issues_for_llm_decision_runs(con, *, cutoff: datetime) -> list[AuditIssue]:
    """Detect durable hot decisions that could silently stop progressing."""
    if not _table_exists(con, "v18_llm_decision_runs"):
        return []
    issues: list[AuditIssue] = []
    rows = strict_many(
        con,
        """SELECT decision_run_id,person_id,live_session_id,state,attempt_count,max_attempts,
                  next_attempt_at,lease_expires_at,error_kind,updated_at
             FROM v18_llm_decision_runs
             WHERE state IN ('pending','claimed','running','retryable_error','repair_requested')""",
        purpose="release audit LLM decision lifecycle",
    )
    stalled: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for row in rows:
        state = str(row.get("state") or "")
        field = "lease_expires_at" if state in {"claimed", "running"} else "next_attempt_at"
        value = row.get(field)
        # Pending/repair runs intentionally have no delay and are due now.
        if state in {"pending", "repair_requested"} and not value:
            if _parse_or_none(row.get("updated_at")) and _parse_or_none(row.get("updated_at")) <= cutoff:
                stalled.append({"decision_run_id": row["decision_run_id"], "state": state, "updated_at": row.get("updated_at")})
            continue
        try:
            instant = _parse_or_none(value)
            if instant is None:
                raise ValueError("missing schedule")
        except Exception as exc:
            malformed.append({"decision_run_id": row["decision_run_id"], "state": state, "field": field, "value": value, "reason": str(exc)})
            continue
        if instant <= cutoff:
            stalled.append({"decision_run_id": row["decision_run_id"], "state": state, field: value, "attempt_count": row.get("attempt_count")})
    if malformed:
        issues.append(AuditIssue("llm_decision_schedule_invalid", "error", "A durable LLM decision has invalid retry/lease scheduling and may be stuck invisibly.", tuple(malformed)))
    if stalled:
        issues.append(AuditIssue("llm_decision_recovery_required", "warning", "A hot LLM decision is due or has an expired lease; the service worker must reclaim, retry or quarantine it.", tuple(stalled)))
    return issues

def _issues_for_llm_schema_boundaries() -> list[AuditIssue]:
    """Fail a release if an active Python LLM call drops its executable schema.

    This is intentionally a source-level gate.  It covers every direct
    ``require_json`` call in the shipped package, including V13--V17 engines,
    and prevents a future bridge from quietly reintroducing parse-only output.
    Semantic proof checks remain at active writers (hot/decomposed paths).
    """
    missing: list[dict[str, Any]] = []
    source_dir = Path(__file__).resolve().parent
    for path in sorted(source_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            missing.append({"module": path.name, "line": exc.lineno, "reason": "source syntax invalid"})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "require_json":
                continue
            schema = next((kw.value for kw in node.keywords if kw.arg == "schema_hint"), None)
            if schema is None or (isinstance(schema, ast.Constant) and schema.value is None):
                missing.append({"module": path.name, "line": node.lineno, "reason": "require_json without executable schema_hint"})
    if not missing:
        return []
    return [AuditIssue(
        "llm_schema_boundary_missing",
        "error",
        "At least one shipped LLM call lacks an executable schema_hint; strict JSON validation is not universal.",
        tuple(missing),
    )]


def _issues_for_deep_audio(con) -> list[AuditIssue]:
    """Verify that V18.5 offline-audio refinement did not fail or lose its Brain2 revision.

    The check is intentionally DB-only: raw media may later be purged after a
    successful retention gate, while the refined transcript and immutable
    conversation remain the durable evidence.
    """
    if not _table_exists(con, "brainlive_deep_audio_artifacts_v185"):
        return []
    issues: list[AuditIssue] = []
    failed = strict_many(
        con,
        """SELECT artifact_id,person_id,package_date,bundle_id,error_text,updated_at
             FROM brainlive_deep_audio_artifacts_v185 WHERE status='error'""",
        purpose="release audit V18.5 deep audio failures",
    )
    if failed:
        issues.append(AuditIssue(
            "deep_audio_refinement_error", "error",
            "An offline WhisperX/Pyannote/SpeechBrain bundle refinement failed; Brain2 must not be treated as fully deep-audio refined.",
            tuple(failed),
        ))
    if _table_exists(con, "brainlive_deep_audio_runs_v185"):
        failed_runs = strict_many(
            con,
            """SELECT run_id,person_id,package_date,live_session_id,error_text,updated_at
                 FROM brainlive_deep_audio_runs_v185 WHERE status='error'""",
            purpose="release audit V18.5 deep audio runs",
        )
        if failed_runs:
            issues.append(AuditIssue(
                "deep_audio_run_error", "error",
                "A V18.5 deep-audio run remains in error and needs retry, correction or explicit quarantine.",
                tuple(failed_runs),
            ))
    if _table_exists(con, "conversations") and _table_exists(con, "brainlive_brain2_event_exports_v1514"):
        incomplete = strict_many(
            con,
            """SELECT a.artifact_id,a.bundle_id,a.refined_conversation_id,c.channel,e.export_status
                 FROM brainlive_deep_audio_artifacts_v185 a
                 LEFT JOIN conversations c ON c.conversation_id=a.refined_conversation_id
                 LEFT JOIN brainlive_brain2_event_exports_v1514 e
                   ON e.bundle_id=a.bundle_id AND e.conversation_id=a.refined_conversation_id
                 WHERE a.status='completed'
                   AND (a.refined_conversation_id IS NULL OR c.conversation_id IS NULL
                        OR c.channel!='brainlive_event_bundle_deep_audio_v185'
                        OR e.export_status!='exported')""",
            purpose="release audit V18.5 deep audio export lineage",
        )
        if incomplete:
            issues.append(AuditIssue(
                "deep_audio_refinement_lineage_incomplete", "error",
                "A completed deep-audio artifact does not point to an active refined Brain2 export.",
                tuple(incomplete),
            ))
        parallel = strict_many(
            con,
            """SELECT bundle_id,COUNT(*) AS active_exports
                 FROM brainlive_brain2_event_exports_v1514
                 WHERE export_status IN ('active','ok','exported')
                 GROUP BY bundle_id HAVING COUNT(*)>1""",
            purpose="release audit parallel active bundle exports",
        )
        if parallel:
            issues.append(AuditIssue(
                "multiple_active_bundle_conversations", "error",
                "A bundle has more than one active Brain2 export; live and refined transcript revisions could be double-counted.",
                tuple(parallel),
            ))
        if _table_exists(con, "v18_conversation_scopes"):
            stale_scope = strict_many(
                con,
                """SELECT e.bundle_id,e.conversation_id,e.export_status,cs.person_id
                     FROM brainlive_brain2_event_exports_v1514 e
                     JOIN v18_conversation_scopes cs ON cs.conversation_id=e.conversation_id AND cs.active=1
                     WHERE e.export_status='superseded'""",
                purpose="release audit superseded conversation scope",
            )
            if stale_scope:
                issues.append(AuditIssue(
                    "superseded_bundle_conversation_still_scoped", "error",
                    "A superseded live bundle export remains active in the global Brain2 scope and can be counted twice.",
                    tuple(stale_scope),
                ))
    return issues


def _issues_for_runtime_safety_flags() -> list[AuditIssue]:
    """Release must not claim green while disabling V18 correctness guards."""
    checks = [
        ("MLOMEGA_V18_DECOMPOSED_LIVE", True, "decomposed_live_disabled", "The decomposed V18 live boundary is disabled; the historical monolithic path can bypass stage provenance and semantic contracts."),
        ("MLOMEGA_V18_STRICT_LLM_CONTRACTS", True, "strict_llm_contracts_disabled", "Executable LLM contracts are disabled; syntactically parseable but invalid outputs can cross a live boundary."),
        ("MLOMEGA_V18_ALLOW_INCOMPLETE_CONTEXT_INFERENCE", False, "incomplete_context_inference_enabled", "Inference from an incomplete context manifest is enabled; omitted evidence may be treated as available."),
    ]
    issues: list[AuditIssue] = []
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    for name, expected_true, code, message in checks:
        raw = os.environ.get(name)
        value = (raw or ("true" if expected_true else "false")).strip().lower()
        actual = value in truthy
        # Unknown text is unsafe for a release gate rather than guessed.
        invalid = value not in truthy | falsy
        unsafe = invalid or actual != expected_true
        if unsafe:
            issues.append(AuditIssue(code, "error", message, ({"environment": name, "value": raw, "expected": "true" if expected_true else "false"},)))
    return issues

def audit_v18_release(*, stale_after_seconds: int = 600, strict: bool = False, persist: bool = True) -> dict[str, Any]:
    """Return a durable release gate report for the currently selected DB.

    ``strict`` upgrades warnings to a failing status, useful in CI after a
    migration or before enabling raw-media cleanup.  The default reports
    warnings as ``attention`` so an empty development database remains usable.
    """
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must be non-negative")
    ensure_v18_release_audit_schema()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=int(stale_after_seconds))
    with connect() as con:
        issues = (
            _issues_for_pipeline_runs(con, cutoff=cutoff)
            + _issues_for_leases(con, cutoff=cutoff)
            + _issues_for_invalidation(con)
            + _issues_for_migration(con)
            + _issues_for_owner_scope(con)
            + _issues_for_legacy_forecast_lifecycle(con)
            + _issues_for_legacy_owner_guards()
            + _issues_for_llm_decision_runs(con, cutoff=cutoff)
            + _issues_for_llm_schema_boundaries()
            + _issues_for_deep_audio(con)
            + _issues_for_runtime_safety_flags()
        )
    errors = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    status = "fail" if errors or (strict and warnings) else ("attention" if warnings else "ok")
    report = {
        "schema": "v18_release_audit",
        "status": status,
        "strict": bool(strict),
        "stale_after_seconds": int(stale_after_seconds),
        "cutoff": iso_utc(cutoff),
        "issue_count": len(issues),
        "error_count": errors,
        "warning_count": warnings,
        "issues": [issue.as_dict() for issue in issues],
        "runtime_limitations": [
            "This audit does not prove real ASR/VAD/VLM/LLM/GPU/network behavior.",
            "Phone Bridge and Dashboard remain separate source-delivery gates when their code is available.",
            "A Bridge deployment must use the documented post-stop retention gate before raw cleanup.",
        ],
    }
    if persist:
        with connect() as con, write_transaction(con):
            con.execute(
                """INSERT INTO v18_release_audit_runs(
                     audit_run_id,status,strict,stale_after_seconds,issue_count,error_count,warning_count,report_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("release_audit"), status, 1 if strict else 0,
                    int(stale_after_seconds), len(issues), errors, warnings,
                    json_dumps(report), now_iso(),
                ),
            )
    return report
