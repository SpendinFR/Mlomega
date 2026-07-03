# V18 RC5 — Retrieval prédictif V17 dense, causal et calibré

## Ce que RC5 remplace

Le V17 historique matérialisait ses similarités avec un score lexical de type Jaccard, des poids fixes et un seuil fixe. Ce chemin n’est plus autorisé pour les **arêtes prédictives actives**.

RC5 utilise l’instance Qdrant déjà configurée par MLOmega. Il ne crée pas un deuxième serveur. Il crée au besoin une collection dédiée aux `observed cases` V17 afin de ne pas mélanger leurs vecteurs avec la mémoire audio/générale :

```text
MLOMEGA_QDRANT_URL=http://localhost:6333
MLOMEGA_QDRANT_COLLECTION=mlomega_audio_memory
MLOMEGA_V17_QDRANT_COLLECTION=mlomega_audio_memory_v17_cases
```

La collection porte une révision d’embedding. Changer de modèle ou de dimensions implique une nouvelle `MLOMEGA_V17_EMBEDDING_REVISION` et une reconstruction de projection, jamais un mélange silencieux de vecteurs.

## Contrat de production

Pour matérialiser une arête `brain2_case_similarity_edges_v17` en mode `predictive`, RC5 exige :

1. un observed case canonique actif, owner-scopé, versionné et daté ;
2. Qdrant, SentenceTransformers et un CrossEncoder disponibles ;
3. un filtrage Qdrant par `person_id`, `active`, `embedding_revision`, type d’entité et `observed_at < anchor.observed_at` ;
4. une revalidation locale de l’owner, du statut, de la version et du temps dans SQLite ;
5. un reranking cross-encoder ;
6. une calibration apprise sur des labels explicitement vérifiés, avec split chronologique ;
7. une précision de validation au moins égale à `MLOMEGA_V17_CALIBRATION_MIN_VALIDATION_PRECISION`.

Les outcomes, états après événement et texte de résultat sont exclus du texte prédictif. Le score dense, le score du reranker et la probabilité calibrée sont stockés séparément. `final_score` est la probabilité calibrée, pas un logit ou une confiance recopiée.

## Abstention, pas dégradation silencieuse

RC5 refuse de créer une arête prédictive si Qdrant, l’embedder, le reranker ou une calibration acceptée manquent. Les arêtes prédictives anciennes actives sont alors invalidées pour l’ancre concernée. Il n’existe pas de fallback Jaccard de production.

Avec une base neuve, l’état attendu est donc d’abord `abstained` : il faut au minimum le nombre de labels vérifiés configuré (30 par défaut) pour produire des arêtes actives. Cela est voulu : une prédiction non calibrée ne doit pas se déguiser en mémoire fiable.

## Projection, retrait et reconstruction

SQLite reste canonique. Qdrant est une projection reconstruisible :

- `v18_predictive_case_vector_manifest` conserve owner, revision, hash du payload, point Qdrant, état sync/retract/quarantine ;
- une correction ou invalidation de case génère un tombstone de projection ;
- le manifest de payload contient le texte prédictif, donc un changement de statut, de source ou de version force la réconciliation ;
- les appels Qdrant ont des indexes de payload requis pour owner, révision, statut et temps.

## Configuration requise

```dotenv
MLOMEGA_VECTOR_BACKEND=qdrant
MLOMEGA_QDRANT_URL=http://localhost:6333
MLOMEGA_QDRANT_COLLECTION=mlomega_audio_memory
MLOMEGA_V17_QDRANT_COLLECTION=mlomega_audio_memory_v17_cases
MLOMEGA_V17_EMBEDDING_REVISION=v18-rc5-predictive-1
MLOMEGA_EMBEDDING_BACKEND=sentence_transformers
MLOMEGA_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
MLOMEGA_RERANKER_BACKEND=sentence_transformers
MLOMEGA_RERANKER_MODEL=BAAI/bge-reranker-v2-m3
MLOMEGA_V17_CALIBRATION_MIN_SAMPLES=30
MLOMEGA_V17_CALIBRATION_MIN_VALIDATION_PRECISION=0.60
```

Les dépendances sont dans l’extra `vector` de `pyproject.toml` : `sentence-transformers`, `transformers`, `qdrant-client`.

## Validation restante sur l’environnement réel

RC5 a des tests déterministes avec backend Qdrant/embedding/reranker simulé. Ils prouvent les invariants de code, mais ne remplacent pas :

- un premier index réel de tes cases V17 ;
- la création/vérification des payload indexes Qdrant ;
- la vérification de la dimension de collection face au modèle choisi ;
- l’entraînement de calibration à partir de labels réellement vérifiés ;
- les métriques chronologiques sur un historique tenu à part.

L’absence de ces preuves doit produire une abstention contrôlée, non une arête heuristique.
