# V14.3 — Brain 2.0 Self Model Scheduler Final

V14.3 ajoute la couche qui manquait pour l'usage 24/24 : le système ne demande plus à l'utilisateur de lancer les consolidations à la main.

## Flux automatique

Avec `mlomega-audio flow-watch --poll-seconds 60`, chaque audio/transcript déclenche déjà :

1. ingestion,
2. V13 strict,
3. sous-sujets,
4. outcomes latents,
5. insights autonomes,
6. V14 Pattern Mirror,
7. V14.3 scheduler.

Le scheduler vérifie ensuite si les consolidations sont dues :

- `hour`,
- `day`,
- `week`,
- `month`.

Il lance uniquement ce qui est dû. Il ne relance pas une consolidation lourde à chaque fichier si elle vient déjà d'être faite.

## Fichiers self-model

Quand une consolidation périodique réussit, V14.3 exporte automatiquement :

- `exports/self_model_me_....md`,
- `exports/self_model_me_....json`.

Ces fichiers contiennent le miroir lisible de l'utilisateur :

- identité centrale,
- état actuel,
- aujourd'hui / semaine / mois,
- traits actifs,
- besoins / valeurs / peurs,
- mots et expressions,
- pensées probables,
- émotions et transitions d'état,
- choix / décisions,
- intentions / actions / outcomes,
- relations et personnes déclencheuses,
- boucles / patterns / contradictions,
- prédictions / forecasts,
- angles morts / inconnues,
- index de preuves.

## Commandes

```powershell
mlomega-audio v14-auto-consolidate --person-id me --force
mlomega-audio v14-scheduler-status --person-id me
mlomega-audio export-self-model --person-id me --format markdown
mlomega-audio export-self-model --person-id me --format json
mlomega-audio v14-self-model --person-id me
mlomega-audio v14-3-audit
```

## Principe important

V14.3 ne remplace pas V13/V14/V14.2. Il orchestre et exporte :

- V13 prédit/simule,
- V14 consolide les patterns longs,
- V14.2 route et fusionne SQL + vectoriel,
- V14.3 automatise les consolidations et rend le self-model lisible.

Aucune regex n'est utilisée dans la nouvelle couche V14.3.
