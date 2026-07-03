# Stack élite stricte V3.1

Cette build n'a plus de chemin simplifié : chaque couche annoncée doit être présente et opérationnelle.

## Audio
- WhisperX : transcription, alignement et timestamps mot-à-mot.
- pyannote.audio : diarisation.
- SpeechBrain ECAPA : embeddings vocaux réels pour reconnaître `me`, Sarah, etc.

## Mémoire vectorielle
- Qwen/Qwen3-Embedding-0.6B ou modèle compatible SentenceTransformers.
- Qdrant comme backend principal, LanceDB possible si explicitement choisi.
- BGE reranker v2 M3 comme reranker obligatoire.
- Indexation des tours, analyses, souvenirs atomiques, états de réflexion, patterns et self-model facts.

## Graphe / mémoire agent
- Graphiti + Neo4j pour graphe temporel externe.
- Mem0 pour mémoire agent supplémentaire.
- Une erreur Graphiti/Mem0 stoppe l'ingestion au lieu de produire un résultat partiel.

## LLM local profond
- Ollama + Qwen3:8b par défaut.
- Le `ConversationMicroscope` appelle le LLM local pour produire JSON structuré : intention, émotion, pourquoi maintenant, expressions, idées, décisions, engagements.
- Le module déterministe/regex a été retiré du chemin d'ingestion.

## Doctor

```bash
mlomega-audio doctor-elite --fail
```

Cette commande vérifie les imports, CUDA, Ollama avec le modèle configuré, et Qdrant. Sur ta RTX 3070, c'est la commande qui dit si la machine est prête.


## V3.2.1 external sync

Graphiti reçoit maintenant les tours bruts, les `memory_cards` canoniques et les `memory_frames` typées comme épisodes séparés. Mem0 reçoit les `atomic_memories` historiques plus les couches V3.2 `memory_card` et `memory_frame` avec leurs métadonnées/facettes.
