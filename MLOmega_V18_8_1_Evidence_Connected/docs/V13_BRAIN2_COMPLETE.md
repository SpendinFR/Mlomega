# V13 Brain 2.0 Complete Cognitive Cycle

Cette version ajoute au socle V12.1 une couche V13 qui suit le plan complet :

`vie observée → situations → états internes → paroles/actions → réactions → résultats → patterns → simulation → prédiction → vérification → correction`.

## Différence majeure avec V12.1

V12.1 posait les tables et matérialisait une première fondation. V13 ajoute un cycle cognitif strict : chaque épisode peut passer par plusieurs rôles LLM/Qwen, chacun avec schéma JSON et preuves.

Rôles V13 :

- `context_resolver` : qui, à qui, où, contexte, enjeu, déclencheur, tension non résolue.
- `state_thought_action_modeler` : état interne multidimensionnel, pensées probables, actes de parole, actions, options et critères.
- `causality_skeptic` : causalité, contradictions, contre-preuves, inférences faibles.
- `prediction_simulator` : prédictions par cible, probabilité, confiance, cas similaires, branches futures, vérification.
- `intervention_designer` : warnings, scénarios, conditions de sortie, plans d’intervention.
- `calibrator` : correction après résultat observé.

## Tables V13 ajoutées

- `v13_cognitive_cycles`
- `v13_llm_extractions`
- `v13_dynamic_models`
- `v13_user_model_snapshots`
- `v13_case_clusters`
- `v13_prediction_explanations`
- `v13_memory_contract_checks`
- `v13_replay_events`
- `v13_intervention_plans`
- `v13_plan_audit_rows`

## Principe anti-squelette

Par défaut, V13 exige Qwen/Ollama pour exécuter le vrai cycle cognitif. Si Qwen n’est pas disponible, la commande échoue au lieu de faire semblant.

Pour audit ou test uniquement :

```powershell
mlomega-audio v13-build --allow-evidence-only
```

Ce mode ne prétend pas être le cerveau complet. Il indique explicitement les rôles LLM manquants.

## Commandes

```powershell
mlomega-audio v13-audit-plan
mlomega-audio v13-build <conversation_id>
mlomega-audio v13-build <conversation_id> --allow-evidence-only
mlomega-audio v13-overview
mlomega-audio v13-predict next_action "contexte actuel" --person-id me
mlomega-audio v13-predict next_trajectory "si je ne change rien" --person-id me
mlomega-audio v13-verify <prediction_id> "résultat observé"
```

## Cibles de prédiction

- `next_word`
- `next_phrase`
- `next_message`
- `next_emotion`
- `next_thought`
- `next_action`
- `next_choice`
- `next_reaction`
- `next_outcome`
- `next_loop`
- `next_risk`
- `next_relationship_move`
- `next_project_move`
- `next_client_outcome`
- `next_life_event`
- `next_trajectory`
- `next_state`
- `next_intervention`
- `next_contradiction`
- `next_trigger`

## Contrat de vérité

- Le brut reste sacré.
- Les inférences restent des hypothèses.
- Les prédictions sont probabilistes.
- Chaque prédiction doit contenir preuves, contre-preuves, assumptions, interventions et plan de vérification.
- La calibration se fait après observation réelle.

## Graphiti / Mem0

Graphiti et Mem0 restent des couches secondaires possibles, pas le cœur du cerveau 2.0. La vérité canonique reste dans SQLite + preuves + cycles V13.
