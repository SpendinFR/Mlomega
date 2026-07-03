# V15 BrainLive + Vision — H0/H1/H2 Personal Predictive Runtime

V15 ajoute une couche live additive au-dessus de Brain2. Brain2 reste le moteur profond, auditable, relationnel, prédictif et consolidé. BrainLive ne réécrit pas Brain2 : il charge ses sorties, observe le présent, prédit les prochains besoins/actions/mots/risques/opportunités, puis stocke outcomes et désaccords pour améliorer le modèle.

## Doctrine

Brain2 comprend qui tu es à travers le temps. BrainLive comprend ce que tu es en train de devenir dans les prochaines secondes/minutes/heures.

BrainLive répond en permanence à ces questions :

1. Qu'est-ce qui se passe maintenant ?
2. Qu'est-ce que l'utilisateur fait probablement ?
3. Pourquoi il le fait probablement ?
4. Qu'est-ce qu'il va probablement vouloir ou faire ensuite ?
5. Quel besoin, émotion ou boucle est derrière ?
6. Quelle opportunité, affordance ou risque est visible maintenant ?
7. Est-ce que parler maintenant améliore la trajectoire ?
8. Qu'est-ce que la suite réelle apprend sur l'utilisateur ?

## Horizons

- **H0** : 0-10 secondes, réflexe, opportunité fugace, mot immédiat, signal de danger non violent, timing.
- **H1** : 10 secondes-5 minutes, conversation, phrase à dire, place/objet/opportunité à saisir, besoin émergent.
- **H2** : 5 minutes-2 heures, routines, transitions, fatigue/flow, tâche repoussée, trajectoire de la journée.

Brain2 garde les horizons jour/semaine/mois/all-time. La commande `brainlive-nightly` crée le pont jour entre les sessions live et la consolidation V14.

## Vision dans le flux normal

La vision n'est pas un module isolé. `vision-ingest-frame` crée :

- `raw_assets(type='vision_frame')`
- `vision_frames`
- `vision_scene_observations` si une observation JSON est fournie
- `source_items(source_type='vision_frame')`
- `lifestream_segments(segment_kind='visual_context')` si un `conversation_id` est fourni

Ainsi, une image peut enrichir le même flux que les conversations audio/transcripts.

## Tables principales

### Vision

- `vision_frames`
- `vision_scene_observations`
- `vision_context_windows`

### BrainLive runtime

- `brainlive_sessions`
- `brainlive_turn_buffer`
- `brainlive_world_states`
- `brainlive_active_contexts`
- `brainlive_event_candidates`

### Prédiction personnelle courte

- `brainlive_need_predictions`
- `brainlive_affordances`
- `brainlive_short_horizon_forecasts`
- `brainlive_life_hypotheses`
- `brainlive_hypothesis_evidence`
- `brainlive_hypothesis_forecasts`

### Intervention et apprentissage

- `brainlive_intervention_candidates`
- `brainlive_intervention_deliveries`
- `brainlive_prediction_outcomes`
- `brainlive_user_disagreement_events`
- `brainlive_missed_opportunity_cards`

### Pont Brain2

- `brainlive_nightly_consolidation_runs`

## Commandes

```bash
mlomega-audio brainlive-audit
mlomega-audio brainlive-start --person-id me --title "journée" --active-people '["Max"]' --location-hint "bureau"
mlomega-audio brainlive-turn <session> "De toute façon tu fais toujours ça" --speaker-label Max --speaker-person-id Max --speaker-confidence 0.72
mlomega-audio vision-ingest-frame ./frame.jpg --live-session-id <session> --observation-json '{"scene_summary":"extérieur, place libre à droite","affordances":[{"label":"place calme","position":"droite"}],"confidence":0.7}'
mlomega-audio brainlive-context <session> --full
mlomega-audio brainlive-run <session> --mode deep_live
mlomega-audio brainlive-inbox --person-id me
mlomega-audio brainlive-outcome <forecast_id> "J'ai finalement pris la place à droite" --match-score 0.8
mlomega-audio brainlive-disagree "fatigue probable" "non je veux continuer"
mlomega-audio brainlive-nightly --person-id me
```

Pour tester sans Ollama :

```bash
mlomega-audio brainlive-run <session> --no-llm
```

## Architecture cognitive

BrainLive est volontairement plus ambitieux qu'un router rapide. Il charge un `active_context` depuis Brain2 :

- memory cards
- self-model dimensions
- predictions/future scenarios/trajectory warnings
- V14 Pattern Mirror
- V14.5 people/open loops/next best actions
- V14.6 relationship models/interpersonal loops/social aftereffects
- V14.7 policies/feedback
- V14.8 clarification items
- anciennes hypothèses BrainLive
- vision récente
- turns live récents

Puis il produit :

- world state actuel
- événements candidats
- besoins probables
- affordances personnelles
- forecasts H0/H1/H2
- hypothèses de vie
- interventions candidates
- watch-next/notes for Brain2

## Règle fondamentale

Tout charger en RAM/cache : oui. Tout envoyer au LLM tout le temps : non. V15 stocke riche, mais construit un packet de contexte actif pour chaque run. La V1 accepte la lenteur pour prouver la qualité. L'optimisation viendra ensuite par cache, réduction de prompt, triggers et modèles plus rapides.
