# MemoryLight Omega Audio Elite V12 — Brain 2.0 Foundation

Cette version ajoute la fondation V4→V12 au socle V3.3.3.

## Principe central

Le système ne doit plus seulement faire `phrase → analyse → carte mémoire`.
Il doit construire :

```text
situation → état interne → parole/action → réaction → résultat → cas similaire → prédiction → vérification
```

## Ce qui reste source de vérité

- SQLite canonique
- raw assets
- conversations / turns / source_spans
- memory_cards / memory_evidence / memory_facets / memory_links
- truth_status : observed / inferred / consolidated / predicted / falsified / obsolete

Qdrant, Graphiti et Mem0 restent des index/adapters utiles, mais ne sont pas la vérité canonique.

## Nouvelles couches V12

### V4 — Nettoyage mémoire
- `v12_canonical_facets`
- `v12_quality_findings`
- `v12_quarantine`
- séparation signal / candidate_pattern / confirmed_pattern

### V5 — Episode Engine
- `episodes`
- `episode_evidence`
- `episode_links`
- `situation_episodes`
- `interaction_episodes`
- `speech_acts`

### V6 — State & Thought Engine
- `internal_state_snapshots`
- `thought_hypotheses`
- `state_transitions`

### V7 — Action / Choice / Outcome Engine
- `action_intentions`
- `action_outcomes`
- `choice_episodes`

### V8 — Relationship & Social Engine
- `relationship_models`

### V9 — Causality & Contradiction Engine
- `causal_edges`
- `contradiction_events`

### V10 — Pattern & Loop Engine
- `behavior_signals`
- `candidate_patterns`
- `confirmed_patterns`
- `loop_patterns`
- `self_model_dimensions`

### V11 — Personal Language Prediction
- `personal_language_patterns`
- `phrase_templates`

### V12 — Prediction Engine
- `prediction_cases`
- `predictions`
- `prediction_results`
- `simulation_branches`
- `calibration_scores`
- `recommended_actions`

## Commandes

```powershell
mlomega-audio init-db
mlomega-audio ingest-transcript examples/example_conversation.json
mlomega-audio v12-build
mlomega-audio v12-overview
mlomega-audio v12-predict next_action "Je veux savoir si le système est assez complet"
mlomega-audio v12-verify <prediction_id> "résultat réel observé"
```

## Limite honnête

La V12 met en place le squelette complet : tables, matérialisation, cas prédictifs, prédiction initiale et calibration.
Elle ne remplace pas l'apprentissage réel sur des centaines/milliers d'épisodes. Plus il y aura de conversations et outcomes vérifiés, plus les prédictions deviendront solides.
