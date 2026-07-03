# V14.8 — Smart Clarification Inbox + Natural Correction Router

V14.8 is the final trust-boundary layer for Brain 2.0.

It does **not** replace V13 prediction, V14 pattern mirror, V14.5 people/open-loops, V14.6 interpersonal state or V14.7 proactive interventions. It centralizes the few cases where the system should ask the user instead of silently deciding.

## Core rule

The system asks rarely.

Most ambiguities are kept in `watching` so future conversations can resolve them automatically. The user is asked only when the uncertainty is important, sensitive, blocking or repeated.

Examples that should be asked only when necessary:

- `UNKNOWN_VOICE_003` might be Max.
- Max might be the user's brother.
- A phrase might have been a joke, not anger.
- The system may be wrong about the user's emotion.
- A person model may be too strong and needs a boundary.
- An intervention preference needs correction.

## Commands

```powershell
mlomega-audio v14-8-audit
mlomega-audio v14-clarification-run --person-id me
mlomega-audio v14-clarifications --person-id me
mlomega-audio v14-clarifications --person-id me --status watching
mlomega-audio v14-clarification-export --person-id me
mlomega-audio v14-answer <item_id> "Oui c'est Max, c'est mon frère"
mlomega-audio v14-clarification-policy --person-id me --patch '{"max_new_questions_per_run":2}'
```

## Flow integration

`flow-watch` now runs:

```text
ingestion
→ V13 strict
→ latent outcomes
→ V14.4 auto-verification
→ V13.4 autonomous insights
→ V14 Pattern Mirror
→ V14.5 people/openloops
→ V14.6 interpersonal state
→ V14.7 proactive interventions
→ V14.8 clarification inbox
→ V14.3 scheduler/export self-model
```

## Files

The clarification inbox can be exported to:

```text
.mlomega_audio_elite/exports/clarification_inbox_me.md
```

It is also included in the self-model export under `clarification_inbox`.

## Safety / quality

- No regex/keyword identity decisions.
- No automatic identity confirmation.
- No relationship truth is locked without explicit confirmation.
- If Qwen is unavailable, the run records an error and creates no fake clarification.
- Interventions remain separate from clarifications: questions are not alerts unless they matter for timing/action.
