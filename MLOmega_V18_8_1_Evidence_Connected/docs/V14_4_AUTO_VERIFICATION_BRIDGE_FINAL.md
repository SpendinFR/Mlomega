# V14.4 — Auto Verification Bridge Final

## Goal

V14.4 removes the last important manual step in the prediction loop.

Before V14.4, the system could already discover latent outcomes in later conversations and it could manually verify predictions with `v13-verify`. The missing product bridge was:

```text
latent_outcome_link
→ prediction_result
→ model_revision
→ v13_replay_event
```

V14.4 automates that bridge after each ingested conversation.

## Flow

```text
flow-watch
→ ingest audio/transcript
→ V13 strict build
→ subtopics
→ latent outcome discovery
→ V14.4 auto verification bridge
→ V13.4 autonomous insights
→ V14 pattern mirror
→ V14.3 scheduler/export
```

## No regex policy

V14.4 does not decide if a prediction is correct using keyword rules. It selects structured `latent_outcome_links` where `source_table='predictions'`, then calls the existing strict V13 Qwen calibration engine used by `v13-verify`.

## Commands

Manual command still available:

```powershell
mlomega-audio v13-verify <prediction_id> "Finalement j'ai fait X"
```

Automatic bridge, normally run by `flow-watch`:

```powershell
mlomega-audio v14-auto-verify --conversation-id <conversation_id>
```

Audit:

```powershell
mlomega-audio v14-4-audit
mlomega-audio v14-autopilot-coverage
```

## Commands intentionally left manual

Some commands must remain manual because they are trust boundaries:

- `setup-me`: the system must not guess your identity.
- `name-voice`: the system must not invent real names for unknown voices.
- `memory-revise`: human correction should stay explicit.
- `sync-vectors --full`: full rebuild is a repair/admin operation, not a normal flow step.
