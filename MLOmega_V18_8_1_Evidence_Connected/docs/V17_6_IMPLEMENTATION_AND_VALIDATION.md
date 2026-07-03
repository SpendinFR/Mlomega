# V17.6.0 — Implementation and validation record

## Intent

This is the **Integrity Foundation** release. It corrects cross-cutting defects
that otherwise contaminate every later BrainLive/Brain2 layer. It is an additive
migration over the audited V17.4.2 codebase; it does not claim to resolve the
full audit.

## Code changes

- `integrity_v176.py`
  - executable Pydantic contracts for BrainLive output and outcome evaluation;
  - rejects missing/extra fields, invalid horizons, non-finite numbers and
    values outside `[0, 1]`;
  - canonical horizon definition: H0 0–10 seconds, H1 10 seconds–5 minutes,
    H2 5 minutes–2 hours;
  - durable occurrence envelope, quarantine, lineage and pipeline-run schema;
  - validated append-only forecast writer;
  - canonical forecast state machine and deterministic legacy reconciliation.
- `brainlive_v15.py`
  - validates output before any derived row is written;
  - quarantines invalid LLM outputs;
  - uses the canonical forecast writer and canonical outcome writer.
- `brainlive_hotloop_v15_6.py`
  - refuses to manufacture probability from confidence;
  - uses the canonical forecast writer and quarantines invalid hot forecasts.
- `brainlive_longitudinal_v15_1.py`
  - uses canonical horizon deadlines;
  - globally orders outcome evidence by event time;
  - evaluates only due, owner-scoped forecasts and closes them through one
    lifecycle path; a no-LLM path no longer creates fake permanent outcomes.
- `brainlive_personal_model_v15_9.py`
  - selects only valid V17.6 active forecasts; legacy `open` rows with unknown
    deadlines cannot remain as immortal live context.
- `db.py` and `utils.py`
  - bounded SQLite lock wait, WAL, full synchronous mode and transaction helper;
  - millisecond timestamps, random IDs, strict JSON helper, non-finite JSON
    rejection and safer identifiers for generic upsert.
- `cli.py`
  - `integrity-v176-migrate` creates BrainLive tables before declaring migration
    success; `integrity-v176-audit` verifies the actual migration scope.

## Database invariants

SQLite trigger protections are installed for V17.6 forecasts/outcomes:

1. forecast lifecycle is a known state and status matches lifecycle;
2. V17.6 forecast has time bounds, bounded probability/confidence and an owned
   live session;
3. outcome references an existing forecast owned by the same person;
4. outcome cannot predate the forecast occurrence;
5. one canonical outcome closes one forecast; duplicate historic outcomes are
   retained but quarantined;
6. a terminal forecast cannot remain active in live selection.

## Test evidence

`pytest -q tests` executes 11 tests:

1. probability and epistemic confidence remain distinct;
2. outcome closes lifecycle;
3. orphan and duplicate outcomes are blocked;
4. NaN and extra LLM fields are rejected;
5. dedupe applies to a run/occurrence, not hash-only content;
6. event envelope dedupes retries but preserves a later identical occurrence;
7. invalid BrainLive output is quarantined;
8. inter-source observations are globally sorted by event time;
9. DB triggers reject illegal lifecycle and predated outcome writes;
10. old `open` forecast with outcome is reconciled and duplicate is quarantined;
11. valid BrainLive output persists through the canonical writer.

The migration CLI was also run on a fresh database. It created the BrainLive
forecast table, all V17.6 integrity tables and five V17.6 triggers, and the
integrity audit returned no violations.

## Audit mapping

| Audit family | V17.6 status |
|---|---|
| forecast/outcome lifecycle, orphan outcome, stale open forecasts | addressed in this release |
| H0/H1/H2 drift and probability/confidence confusion | addressed on migrated writer/evaluator paths |
| NaN / malformed LLM JSON at BrainLive/outcome border | addressed on migrated paths |
| event identity/provenance base | primitive delivered; capture/service migration is V17.7 |
| lock handling / atomic writer foundation | foundation delivered; all multi-module jobs remain V17.7/V18 |
| replay/as-of isolation | V17.7 |
| context gateway / local episode contracts | V17.7 |
| post-stop re-export/invalidation | V17.7 |
| V13/V14 owner-scoped retrieval | V18 |
| Life Model, V17 retraction and sync reconciliation | V18 |

## Mandatory merge rule

This package was built from the audited V17.4.2 tree available in this runtime.
It must be merged onto the current branch containing the user's existing 20
corrections. Do not overwrite that work. For every overlap, keep the test and
merge through the canonical V17.6 writer/contract rather than maintaining two
parallel paths.
