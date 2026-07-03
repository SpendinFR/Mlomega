# Memory Foundation V3.2

Objectif : rendre la mémoire suffisamment structurée pour qu'un futur moteur Humain 2.0 puisse l'exploiter sans réinterpréter tout le passé à chaque question.

## Principe

Chaque élément important devient une carte mémoire canonique (`memory_cards`) reliée à :

- une preuve brute (`source_spans`) ;
- un run d'extraction (`extraction_runs`) ;
- des preuves (`memory_evidence`) ;
- des facettes de recherche (`memory_facets`) ;
- des liens vers d'autres souvenirs (`memory_links`) ;
- éventuellement une frame typée (`memory_frames`).

## Niveaux de vérité

- `observed` : texte/audio/turn capturé.
- `inferred` : analyse produite par le LLM sur un tour précis.
- `consolidated` : mémoire construite à partir de plusieurs souvenirs.
- `external` : mémoire issue d'une intégration externe.

## Frames exploitables

Le microscope local doit produire des `memory_frames`, par exemple :

- `choice`
- `action`
- `plan`
- `belief`
- `desire`
- `fear`
- `constraint`
- `need`
- `boundary`
- `relationship_signal`
- `identity_signal`
- `contradiction_signal`
- `question`
- `request`

Ces frames ne prédisent rien encore. Elles rendent les conversations comparables.

## Facettes exploitables

Les `memory_facets` permettent de filtrer par :

- domaine de vie ;
- projet ;
- personne ;
- émotion ;
- besoin ;
- valeur ;
- risque ;
- zone de décision ;
- dynamique relationnelle ;
- horizon temporel ;
- état d'énergie ;
- style de communication.

## Requête future typique

```sql
SELECT c.*
FROM memory_cards c
JOIN memory_facets f1 ON f1.target_id = c.card_id AND f1.facet_type = 'project'
JOIN memory_facets f2 ON f2.target_id = c.card_id AND f2.facet_type = 'emotion'
WHERE c.truth_status IN ('observed', 'inferred', 'consolidated')
ORDER BY c.time_start;
```

## Pourquoi c'est le bon socle

Un moteur prédictif futur pourra comparer des situations parce que la mémoire n'est plus seulement textuelle. Elle est :

```text
temporelle
facettée
prouvée
typée
liée
canonique
traçable
```


## V3.2.1 external sync

Graphiti reçoit maintenant les tours bruts, les `memory_cards` canoniques et les `memory_frames` typées comme épisodes séparés. Mem0 reçoit les `atomic_memories` historiques plus les couches V3.2 `memory_card` et `memory_frame` avec leurs métadonnées/facettes.
