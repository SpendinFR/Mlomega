# V15.1 BrainLive Longitudinal Final

V15.1 turns the V15 BrainLive foundation into a stricter personal predictive layer.
It keeps Brain2 as the deep consolidation engine and adds the missing live/day engines:

- Routine Miner: extracts repeated time/location/people/action patterns from BrainLive sessions, world states and vision observations.
- Longitudinal Hypothesis Engine: compares repeated situations across days/weeks and creates testable life hypotheses + next watch windows.
- Outcome Evaluator: compares BrainLive forecasts with later live observations; without LLM it records an unscored observed window rather than faking a verdict.
- User Disagreement Interpreter: uses LLM + active Brain2/BrainLive context; no canned explanations.
- Personal Affordance Matcher: compares vision observations with active needs, routine cards and life hypotheses.
- Daily/Nightly Scheduler: BrainLive works during the day; Brain2 remains the nightly deep layer.
- Replay Bridge: BrainLive does not duplicate Brain2/V13 replay; it links BrainLive short-horizon outcomes to existing V13 replay events.

## Strict no-regex/no-fake-psychology policy

V15.1 removes the old keyword fallback. If Ollama/Qwen is disabled or unavailable, BrainLive does not pretend to infer manipulation, fatigue, flow, intention or emotion from hardcoded words. It stores neutral raw observations/statistical patterns or marks rows as `llm_required` / `unscored` for later processing.

## New commands

```bash
mlomega-audio brainlive-mine-routines --person-id me
mlomega-audio brainlive-hypotheses-run --person-id me
mlomega-audio brainlive-outcomes-auto --person-id me
mlomega-audio brainlive-match-affordances <live_session_id>
mlomega-audio brainlive-scheduler-tick --person-id me --kind daytime
mlomega-audio brainlive-scheduler-tick --person-id me --kind nightly
mlomega-audio brainlive-replay --person-id me
```

Add `--no-llm` only for audit/dev. In that mode BrainLive remains strict and avoids fake interpretations.

## Intended loop

```text
Day:
  live sessions + turns + vision frames
  -> active contexts
  -> BrainLive runs
  -> forecasts / interventions / outcomes
  -> routine mining / affordance matching / outcome scoring

Night:
  BrainLive daily maintenance
  -> Brain2 V14 day consolidation
  -> self-model/pattern mirror/relationship/open-loop updates
  -> next day active context is stronger
```

## Relationship with Brain2 replay

Brain2 already has V13 replay/calibration tables. V15.1 adds a bridge instead of duplicating that layer. BrainLive contributes short-horizon prediction/outcome data; Brain2 remains the canonical deep replay/simulation engine.
