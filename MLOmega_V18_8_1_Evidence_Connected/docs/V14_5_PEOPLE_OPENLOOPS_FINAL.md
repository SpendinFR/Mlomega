# V14.5 — People Identity Hypotheses + Personal Open Loop Solution Tracker

V14.5 adds two final active-memory capabilities without replacing V13/V14/V14.2/V14.3/V14.4.

## 1. People Identity Hypotheses

The system can now create pending hypotheses about unknown speakers and relationship context:

- suspected name or person hint;
- possible relation to the user, such as brother, family, close friend, colleague, client;
- familiarity level;
- topics often discussed;
- how the user behaves with that person;
- states and loops that person appears to trigger;
- evidence and counter-evidence;
- recommended confirmation action.

It never confirms a voice identity by itself. `name-voice` and `enroll-voice` remain the trust boundary.

Commands:

```powershell
mlomega-audio v14-people-hypotheses
mlomega-audio v14-5-run <conversation_id>
```

## 2. Personal Open Loop / Solution Tracker

The system can now track casual but important sentences such as:

- “I would like to do X”;
- “I do not understand why this happens”;
- “I keep blocking here”;
- “I want to change this”;
- “I need to decide X”;
- “I expected Y but got Z”.

Those become active objects:

- desires;
- goals;
- confusions;
- blocked loops;
- expectations;
- active questions;
- suspected blockers;
- solution candidates;
- next best actions.

The tracker updates them over time when new conversations provide progress, contradiction, resolution or new evidence.

Commands:

```powershell
mlomega-audio v14-open-loops --person-id me
mlomega-audio v14-5-run <conversation_id> --person-id me
mlomega-audio v14-5-audit
```

## Flow integration

`flow-watch` now calls V14.5 after each ingested conversation:

```text
audio/transcript
→ ingestion
→ V13 strict
→ latent outcomes
→ V14.4 auto verification
→ V13.4 autonomous insights
→ V14 Pattern Mirror
→ V14.5 people/open-loop tracking
→ V14.3 scheduler + self-model export
```

## Self-model export

`export-self-model` now includes:

- pending identity/relation hypotheses;
- relationship context profiles;
- active desires/questions/blockages;
- solution candidates;
- next best actions.

```powershell
mlomega-audio export-self-model --person-id me --format markdown
```
