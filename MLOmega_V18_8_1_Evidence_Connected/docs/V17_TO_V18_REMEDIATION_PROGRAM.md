# Programme de correction V17.6 → V17.7 → V18

Ce programme transforme le registre d’audit en dépendances de correction. Il ne
faut pas faire 150 patches isolés : les mêmes défauts traversent capture,
BrainLive, post-stop, V13/V14, Life Model, V17 et les syncs.

> Les 20 corrections déjà faites hors de ce dépôt ne sont pas supposées
> présentes ici. Chaque lot ci-dessous doit être fusionné sur le dernier arbre
> de code réel et validé par ses tests de contrat, pas recopié aveuglément.

## Règle de séquencement

1. **Un fait source ne doit jamais être détruit.**
2. **Aucun dérivé ne peut survivre à l’invalidation de sa source.**
3. **Aucun moteur ne lit hors de son owner, de son scope ou de son `as_of`.**
4. **Un résultat LLM/VLM invalide est quarantiné, jamais normalisé en fait.**
5. **Une étape suivante ne s’exécute pas lorsque son prérequis critique est
   partiel.**

Les versions suivent ce graphe :

```text
V17.6  Integrity kernel
  └── V17.7  Event/run/context/post-stop isolation
        └── V18  Brain2/Life Model/V17/sync retraction & calibration
```

## V17.6 — Integrity kernel (ce lot)

### Causes racines traitées

- `M-P0-18`, `M-P0-22`, `M-P0-25`, `M-P0-27`;
- `M-P1-76`, `M-P1-79`, `M-P1-81`, `M-P1-97`, `M-P1-98`;
- fondation de provenance pour `M-P0-01…06`, `M-P0-15`, `M-P0-24`.

### Livrables

- `integrity_v176.py`: Pydantic strict, `HorizonSpec`, validation de nombres
  finis, EventEnvelope, quarantaine, lineage et audit;
- colonnes/migrations additives du lifecycle forecast;
- outcomes orphelins/cross-owner/dupliqués bloqués par SQLite;
- fermeture transactionnelle des forecasts;
- sorties BrainLive/Outcome invalides rejetées avant persistance;
- `busy_timeout`, WAL, `synchronous=FULL`, primitive de transaction;
- manifeste de release et tests de non-régression.

### Gate V17.6

```text
integrity-v176-audit.status == ok
pytest V17.6 == green
aucune forecast V17.6 sans occurred_at/due_at
aucun outcome orphelin/cross-owner/doublon
```

## V17.7 — Event, RunContext, Context Gateway et post-stop réparable

Cette version absorbe les problèmes qui viennent d’un flux de faits non
immuable ou mal borné. Elle doit utiliser les primitives V17.6 au lieu de les
réimplémenter.

### Workstream 1 — Capture/service idempotents

Couvre `M-P0-01…07`, `M-P1-75` et les points Annexes audio/image/GPS :

- protocole `temp → fsync → manifest ready → rename`;
- EventEnvelope obligatoire pour audio, image, transcript et GPS;
- machine d’état `pending → leased → processing → accepted | retry_wait |
  quarantined`, avec nombre de tentatives, next_retry_at et dead-letter;
- curseur durable VAD, aucun `segments[:30]` silencieux;
- échec de découpe = quarantaine du segment, jamais ASR du fichier complet;
- ASR/VAD/VLM non disponible = résultat incomplet bloquant, pas `ok`;
- JSON, JSONL et transcript streaming explicitement distingués;
- sidecar lié cryptographiquement au média et temps source obligatoire;
- clusters inconnus persistants, owners/sessions explicites.

### Workstream 2 — RunContext, `as_of` et replay isolé

Couvre `M-P0-19`, `M-P0-20`, `M-P1-78`, `M-P1-80` :

- `RunContext(run_id, mode, person_id, session_id, as_of, namespace)`;
- replay dans namespace/base séparé, jamais dans `brainlive_turn_buffer`
  production;
- les bornes replay deviennent des clauses SQL strictes;
- les données rejouées conservent `occurred_at`, jamais `now()`;
- toute lecture de contexte reçoit `as_of` et refuse le futur.

### Workstream 3 — Context Gateway

Couvre `M-P0-16`, `M-P0-17`, `M-P1-77`, `M-P1-96` :

- épisode = seuls ses tours + fenêtre justifiée avant/après;
- résumés versionnés avec références source, pas une coupe de chaîne JSON;
- budget token appliqué par sélection/récupération, avec statut
  `incomplete_context` explicite;
- tâches H0/H1/H2 décomposées : perception, décision de route, prédiction et
  intervention avec contrats séparés;
- aucune exception SQL ne devient `[]` dans les chemins critiques.

### Workstream 4 — Assembler et post-stop versionnés

Couvre `M-P0-08…13`, `M-P0-24`, `M-P0-26` :

- partition exclusive des raw items par bundle/version;
- bundle/export/conversation versionnés, un seul actif;
- ré-export = nouveau dérivé + invalidation en cascade, jamais append `idx`;
- scope `live_session_id` transmis à chaque étape post-stop;
- VLM/silent fallback classé hypothèse/quarantaine, jamais observation vraie;
- manifeste de conservation, vérification des attendus, backup/restauration
  avant purge;
- sync optionnelle hors commit critique du post-stop.

### Gate V17.7

```text
E2E 5 minutes + arrêt/reprise + double upload + fichier partiel = aucune perte ni double écriture
replay = zéro ligne production nouvelle
un bundle réexporté = descendants précédents non sélectionnables
cleanup raw = interdit sans manifest restaurable et post-stop complet
```

## V18 — Brain2, Life Model, V17, retrieval, sync et rétraction

V18 ne doit démarrer qu’après V17.7 : les moteurs aval ne peuvent pas devenir
corrects si leurs sources sont ambiguës.

### Workstream 5 — Ownership Chain et writers canoniques

Couvre `M-P0-14`, `F-P0-01`, `F-P0-09` et toutes les lectures `OR person_id
IS NULL` ambiguës :

- `person_id` non nullable sur tout dérivé nouveau;
- tables sans owner jointes via parent obligatoire; orphan = quarantine;
- writers centraux contrôlent owner/session/source/version;
- réponse clarification et feedback vérifient owner + état + version.

### Workstream 6 — V13/V14 context et retrieval fiable

Couvre `M-P0-16`, `M-P0-17`, `M-P1-82`, les défauts routeur/retrieval
cross-owner et fausse fusion SQL/vector :

- V13 reçoit un `EpisodeContext` local et valide ses relations source;
- V14 routeur/retrieval filtre owner + `as_of` avant ranking;
- une fusion SQL/vector est un merge par identité source, pas deux candidats;
- références LLM sont vérifiées, sinon quarantinées.

### Workstream 7 — Life Model et coordination rétractables

Couvre `F-P0-02…08`, `F-P1-01…04`, `M-P0-23` :

- `target_id` produit par writer canonique, pas fourni par LLM;
- une seule machine d’état effective pour canonical row, lifecycle, strates,
  hooks et bindings;
- `weaken/contradict/obsolete` change la sélection live et invalide les
  descendants;
- promotion de strate uniquement avec critères d’indépendance explicites;
- selectors utilisent `valid_until`, `status`, lifecycle et scope session;
- delta bornée `[period_start, period_end)` et `as_of`.

### Workstream 8 — V17 causal, recalculable et calibré

Couvre `M-P0-20`, `M-P0-21`, `M-P1-83…86`, `F-P0-08` :

- observed case versionné avec hash de sources, recomputation après correction;
- arêtes et patterns tombstonés/recalculés si source évolue;
- `predictive_retrieval`: cases antérieures à `as_of`, outcome exclu;
- `retrospective_similarity`: outcome permis mais séparé;
- embeddings dense + cosine + reranker; Jaccard secondaire explicable;
- calibration temporelle, indépendance jour/épisode, expiration des patterns.

### Workstream 9 — Sync et mémoire externe réconciliables

Couvre `M-P0-23`, `M-P1-87…92` :

- payload hash complet (truth/status/version/owner), pas texte seul;
- tombstones et retraction dans Qdrant/LanceDB/Mem0/Graphiti;
- jobs avec claim atomique, lease, backoff respecté, idempotence externe;
- Mem0 reçoit l’owner réel et une clé externe stable;
- sync secondaire non bloquante mais traçable/rejouable.

### Workstream 10 — Runtime/release/observabilité

Couvre `M-P1-93…95`, `M-P2-21…23` :

- manifest de version unique, lockfiles et doctor réellement bloquant;
- secrets sans défaut de démonstration activable;
- métriques de conservation, taux de retry, quarantaines, correction cascade,
  délais E2E et taux de non-régression;
- audit séparé Phone Bridge et Dashboard avant toute promesse de sécurité;
- tests unitaires, SQLite integration, chaos, concurrence et E2E.

### Gate V18

```text
multi-owner isolation : zéro fuite
replay causal : zéro future leak
correction source : zéro descendant/résultat externe encore actif
sync concurrente : une seule exécution par lease
calibration prospective mesurée sur split temporel tenu à part
```

## Ordre de traitement des 20 corrections déjà effectuées

Avant de fusionner chaque version, comparer les 20 changements existants à ces
workstreams. Une correction locale est conservée seulement si elle :

1. a un test de non-régression;
2. respecte les primitives V17.6 (`owner`, `occurred_at`, `as_of`, lifecycle,
   quarantaine);
3. ne réintroduit pas un writer parallèle ou une route legacy;
4. ne contourne pas les transitions canonique de la version suivante.

Une correction sans ces propriétés doit être intégrée comme test/contrat, puis
reconstruite dans le writer central plutôt que maintenue comme rustine.
