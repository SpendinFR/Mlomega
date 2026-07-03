# V13.1 Brain 2.0 Complete Final — Plan Coverage

This version implements the full V4→V13 plan as an auditable software architecture.

## Core doctrine

The system is not `audio → transcript → summary → vector query` anymore. The V13.1 flow is:

```text
observed life
→ situations
→ internal states
→ speech/actions/choices
→ reactions/outcomes
→ patterns/loops
→ similar cases
→ simulation
→ prediction
→ verification
→ correction
→ intervention
```

The raw evidence remains sacred: raw assets, conversations, turns, source spans, source items, lifestream segments, speakers, timestamps and raw JSON are not overwritten by interpretation.

## What was missing in earlier V13 and is now explicit

V12/V13 already had many foundation tables. V13.1 adds the missing explicit objects from the plan:

```text
v13_plan_requirements
v13_component_coverage
v13_engine_runs
v13_engine_outputs
audio_prosody_events
episode_boundaries
choice_options
choice_criteria
causal_hypotheses
counter_evidence_items
social_roles
trust_history
conflict_loops
repair_patterns
pattern_contexts
pattern_counterexamples
next_phrase_cases
style_state_snapshots
language_ngrams
similar_case_retrieval_runs
prediction_target_scores
model_revisions
trajectory_interventions
v13_complete_contract_checks
```

These prevent the important concepts from being hidden only inside JSON blobs.

## The 16 engines now have explicit contracts

```text
1. capture_engine
2. language_signature_engine
3. episode_builder
4. context_resolver
5. internal_state_engine
6. social_model_engine
7. causality_engine
8. contradiction_engine
9. pattern_miner
10. choice_model_engine
11. outcome_tracker
12. similar_case_retrieval
13. prediction_engine
14. simulation_engine
15. calibration_engine
16. intervention_engine
```

Each engine has a strict JSON schema, a persisted run table, a persisted output table, and a plan-audit mapping to the tables it is responsible for.

## LLM policy

The complete cognitive layer is Qwen/Ollama-first. It does not use regex as a psychological fallback.

If the LLM is disabled, `v13-build --allow-evidence-only` records missing engine outputs and materializes only non-inferential structures such as language n-grams, boundaries from known episode rows, calibration statistics from verified predictions, and choice options already present in structured data.

In strict mode, missing or invalid Qwen output is a hard error.

## Commands

```powershell
mlomega-audio v13-audit-plan
mlomega-audio v13-build <conversation_id>
mlomega-audio v13-build <conversation_id> --allow-evidence-only
mlomega-audio v13-overview
mlomega-audio v13-predict next_action "contexte actuel" --person-id me
mlomega-audio v13-verify <prediction_id> "résultat observé"
```

## Audit guarantee

`mlomega-audio v13-audit-plan` checks every required table and every engine dependency. The expected result for a correct install is `partial_or_missing: []`.

## Honest boundary

This is the complete software foundation and engine contract for the Brain 2.0 system. It becomes truly predictive only after ingesting enough user history and verifying predictions over time. It does not pretend to know thoughts or futures without evidence.
