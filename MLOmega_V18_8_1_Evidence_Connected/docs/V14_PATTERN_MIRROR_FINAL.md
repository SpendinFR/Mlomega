# V14 Brain 2.0 Pattern Mirror Final

## Objectif

V14 est la couche qui remet le projet au bon niveau : elle ne sert pas seulement à stocker ou répondre. Elle doit montrer à William ce qu'un humain oublie, minimise ou ne relie pas dans le temps.

Le système cherche donc automatiquement :

- les boucles récurrentes ;
- les contradictions récentes ;
- les signaux faibles qui ressemblent à une ancienne erreur ;
- les personnes qui déclenchent certains états ;
- les décisions qui ressemblent à d'anciens choix ;
- les phrases qui annoncent souvent une action ou un blocage ;
- les trajectoires longues qui reviennent ;
- ce qui aurait pu être anticipé ;
- ce qu'il faut surveiller maintenant ;
- l'intervention qui peut casser une trajectoire.

## Flux autonome

Quand `flow-watch` détecte un audio/transcript :

```text
ingestion -> V13 build -> sous-sujets -> outcomes latents -> V13 insights -> V14 Pattern Mirror -> V14 daily consolidation
```

Donc après chaque nouvelle conversation, le système prépare automatiquement des hypothèses, prédictions, alertes et cartes de miroir longitudinal.

## Consolidation périodique

Commandes :

```powershell
mlomega-audio v14-today --person-id me
mlomega-audio v14-consolidate --period day --person-id me
mlomega-audio v14-consolidate --period week --person-id me
mlomega-audio v14-consolidate --period month --person-id me
mlomega-audio v14-snapshots --person-id me
```

Le but est d'obtenir :

```text
Aujourd'hui William est...
Cette semaine William a répété...
Ce mois-ci une trajectoire revient...
Ces personnes déclenchent...
Ces phrases annoncent souvent...
Ces décisions ressemblent à d'anciens choix...
Voici ce qu'il faut surveiller maintenant.
```

## Tables V14 principales

- `v14_pattern_mirror_cards` : cartes de boucles cachées / blindspots / signaux faibles.
- `v14_periodic_self_snapshots` : état jour/semaine/mois/all-time.
- `v14_people_trigger_maps` : personnes -> états déclenchés / boucles.
- `v14_repetition_chains` : chaînes répétées mot/action/choix/réaction.
- `v14_forecast_watch_queue` : prédictions à surveiller.
- `v14_trajectory_forecasts` : trajectoires probables.
- `v14_counterfactual_lessons` : ce qui aurait pu être anticipé.
- `v14_intervention_triggers` : messages utiles pour casser une boucle.

## Règle de vérité

V14 ne doit pas faire de psychologie générique. Chaque lecture doit avoir :

```text
hypothèse + probabilité + preuves + contre-preuves + contexte temporel + action possible
```

Si Qwen n'est pas disponible ou si les preuves sont insuffisantes, V14 échoue ou dit ce qui manque. Il ne remplit pas de fausses analyses.
