# Architecture mémoire élite — Audio Conversation Core V3.2

Objectif : transformer une conversation audio en mémoire exploitable à long terme, capable de suivre mots, expressions, voix, relation, sujet, intention, réflexion, boucles, frames de vie et cartes mémoire réutilisables.

## Pipeline

```text
AUDIO
→ WhisperX transcription + timestamps mot-à-mot
→ pyannote diarisation
→ SpeechBrain ECAPA voice identity
→ conversation turns normalisés
→ source_spans observés
→ Ollama/Qwen deep conversation microscope
→ extraction_runs audités
→ mots / expressions / idées / décisions / engagements / memory_frames / memory_facets
→ atomic memories
→ memory_cards canoniques
→ memory_evidence + memory_links + memory_facets
→ entities + temporal relations
→ consolidation reflection_states / edges
→ patterns / self_model
→ Qdrant/LanceDB vector memory
→ Graphiti/Neo4j temporal graph
→ Mem0 agent memory
→ retrieval vectoriel + reranker
```

## Couches mémoire

1. `raw_assets` : preuve brute.
2. `conversations` / `turns` : transcript avec locuteurs, timing, contexte.
3. `source_spans` : citation/source exacte, hashée, liée au tour.
4. `extraction_runs` : modèle, schéma, version, prompt hash, statut.
5. `word_signals` : mots saillants, rôle, pourquoi ils comptent.
6. `expression_signals` : expressions/tics personnels et sens propre à l'utilisateur.
7. `utterance_analyses` : intention profonde, émotion, pourquoi maintenant, attente cachée, règle de réponse.
8. `ideas`, `decisions`, `commitments` : idées, choix, promesses.
9. `memory_frames` : choix/action/plan/croyance/désir/peur/contrainte/besoin/signal relationnel.
10. `atomic_memories` : souvenirs atomiques réutilisables.
11. `memory_cards` : carte canonique centrale pour exploitation future.
12. `memory_evidence` : preuve rattachée à chaque carte/item.
13. `memory_facets` : classement filtrable par domaine, projet, personne, émotion, besoin, valeur, risque, temps.
14. `memory_links` : liens explicites entre souvenirs.
15. `entities` / `relations` : graphe temporel local.
16. `reflection_states` : état de réflexion par personne + sujet + période.
17. `reflection_edges` : stable_loop, stance_shift, contradiction possible.
18. `patterns` : boucles/signaux cachés consolidés.
19. `self_model_facts` : modèle de fonctionnement de l'utilisateur.
20. Qdrant/LanceDB : mémoire vectorielle.
21. Graphiti/Neo4j : graphe temporel externe.
22. Mem0 : mémoire agent additionnelle.

## Ce que la V3.2 doit permettre

- Retrouver une preuve exacte pour chaque souvenir.
- Filtrer par personne, sujet, émotion, besoin, projet, domaine de vie.
- Séparer observation, inférence et consolidation.
- Comparer plusieurs conversations sans réanalyser le brut.
- Relier un état consolidé à tous les souvenirs qui le justifient.
- Préparer le futur moteur Life Pattern / Humain 2.0.

## Principe de vérité

Le système ne dit jamais “je sais ton âme”. Il dit :

```text
D'après telles preuves, dans tels contextes, cette hypothèse a telle confiance.
```
