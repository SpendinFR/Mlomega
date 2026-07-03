# V14.1 — Brain 2.0 Router + Selection/Ranking Final

## Pourquoi cette version existe

V14 Pattern Mirror ne doit jamais remplacer la mémoire brute ni le moteur prédictif.

La bonne architecture est maintenant explicite :

- **Raw Recall** : pour les faits, dates, lieux, personnes, paroles exactes.
- **V13 Prediction Engine** : pour ce qui va probablement être dit/fait/ressenti/choisi.
- **V14 Pattern Mirror** : pour les boucles longues, contradictions, signaux faibles et trajectoires.
- **V14.1 Router/Selection** : choisit automatiquement quelle couche interroger et quels éléments doivent remonter.

## Aucune regex dans cette couche

`brain2_router_v14_1.py` ne fait pas de routing par regex ou mots-clés bricolés.

- Qwen route la question par contrat JSON strict.
- Le moteur sélectionne ensuite les tables structurées pertinentes.
- Le ranking utilise type de table, score, confiance, temps, et liens d'objets.
- La réponse finale sépare fait / inférence / prédiction / manque de contexte.

## Commandes

### Audit

```powershell
mlomega-audio v14-1-audit
```

### Poser une question naturelle

```powershell
mlomega-audio v14-ask "J'étais où le 8 mai 2020 ?" --person-id me
mlomega-audio v14-ask "Que va probablement faire Max si je lui dis X ?" --person-id me
mlomega-audio v14-ask "Qu'est-ce que je suis en train de refaire comme boucle ?" --person-id me
mlomega-audio v14-ask "Que prédis-tu de mon avenir proche perso, relationnel et pro ?" --person-id me
```

### Voir le routing choisi

```powershell
mlomega-audio v14-route "Que va faire Max si je lui dis X ?" --person-id me
```

### Voir ce que le système sélectionne avant de répondre

```powershell
mlomega-audio v14-select "Quels projets ai-je laissés ouverts ?" --person-id me
```

## Ce qui remonte selon la question

### Question factuelle

Exemple : “J’étais où tel jour ?”

Le routeur doit sélectionner :

- `conversations`
- `turns`
- `source_spans`
- `episodes`
- timestamps
- speakers
- lieux explicites/inférés

### Question prédictive

Exemple : “Que va faire Max si je lui dis X ?”

Le routeur doit sélectionner :

- `predictions`
- `prediction_results`
- `similar_case_scores`
- `interaction_episodes`
- `relationship_models`
- `action_outcomes`
- `choice_episodes`
- `v14_trajectory_forecasts`

### Question miroir long terme

Exemple : “Qu’est-ce que je répète sans le voir ?”

Le routeur doit sélectionner :

- `v14_pattern_mirror_cards`
- `v14_periodic_self_snapshots`
- `v14_long_horizon_threads`
- `v14_repetition_chains`
- `v14_blindspot_hypotheses`

### Question mixte / avenir proche

Exemple : “Que prédis-tu de mon avenir perso, relationnel, pro ?”

Le routeur peut combiner :

- raw facts récents
- intentions ouvertes
- outcomes absents
- choix récurrents
- tensions relationnelles
- patterns récents
- snapshots V14
- forecasts à surveiller

## Règle de réponse

Chaque réponse doit distinguer :

- ce qui est factuel,
- ce qui est inféré,
- ce qui est prédit,
- ce qui manque,
- ce qui peut changer la trajectoire.
