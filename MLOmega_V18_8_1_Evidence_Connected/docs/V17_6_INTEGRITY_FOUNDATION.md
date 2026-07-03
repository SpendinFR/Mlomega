# V17.6 — Integrity Foundation

## Purpose

V17.6 is the first corrective release after the V17.4.2 audit. It deliberately
fixes common causes, rather than adding another feature layer:

- a JSON-looking LLM response is no longer treated as a trustworthy record;
- a forecast has one temporal contract and one lifecycle;
- an outcome cannot be orphaned, cross-owner, or terminal twice;
- an occurrence has a durable event envelope distinct from its file hash;
- SQLite writers have bounded lock waiting, WAL and transaction primitives;
- invalid inputs have a quarantine trail instead of becoming an empty list or a
  fake certainty.

## Implemented invariants

| Invariant | V17.6 mechanism |
|---|---|
| H0/H1/H2 are one contract | `HorizonSpec`: H0 0–10s, H1 10s–5m, H2 5m–2h. Forecast creation and evaluator both use it. |
| Probability differs from epistemic confidence | `probability`, legacy `confidence`, `epistemic_confidence`, and `evidence_quality` are stored separately. Hotloop refuses to invent probability from confidence. |
| Forecast lifecycle terminates | `open → due → evaluated_correct/evaluated_incorrect/indeterminate/expired`, with transition audit rows. |
| An outcome belongs to a real forecast | SQLite triggers reject missing forecast, owner mismatch and a second canonical outcome. |
| Invalid LLM payloads do not become facts | Pydantic V2 contracts reject missing/extra fields, bad horizons and non-finite/bounded scores. Invalid payloads are quarantined. |
| Identical hash does not erase another occurrence | `event_envelopes_v176` dedupes an actual occurrence fingerprint, including device + event/capture time, not SHA-256 alone. |
| Lock failures are not immediate random failures | Connections enable `foreign_keys`, `busy_timeout=15s`, WAL and `synchronous=FULL`. |

## Audit coverage in this release

Directly addressed or structurally prepared:

- `M-P0-18`: forecast/outcome lifecycle, orphan outcome and status closure;
- `M-P0-22`: canonical horizon timing and millisecond timestamps;
- `M-P0-25`: executable contracts on the BrainLive and outcome boundaries;
- `M-P0-27`: SQLite lock/durability base and transaction primitive;
- `M-P1-76`: NaN/Infinity are rejected at strict contract boundaries;
- `M-P1-79`: outcome evidence is globally event-time sorted;
- `M-P1-97`: H0/H1/H2 drift eliminated in the new writer/evaluator path;
- `M-P1-98`: probability/confidence are no longer copied into each other;
- part of capture provenance / hash-only dedupe issues through `event_envelopes_v176`.

## Commands

```powershell
mlomega-audio integrity-v176-migrate
mlomega-audio integrity-v176-audit --fail
pytest -q
```

`integrity-v176-audit` is a gate, not an informational dashboard. A release
must not proceed with orphan outcomes, cross-owner outcomes, invalid H0/H1/H2
values, terminal forecasts that remain active, or unreviewed quarantine items.

## Intentional non-goals

V17.6 does **not** claim to solve the audit. It supplies the foundation required
for the remaining work. V17.7 must migrate capture/service/replay/post-stop to
`EventEnvelope`, `RunContext` and isolated namespaces. V18 must migrate V13–V17,
Life Model, feedback, vector/sync and descendant invalidation to owner-scoped,
versioned writers.
