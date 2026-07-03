# Windows local setup — MemoryLight Omega Audio Elite V3.3.3

Cette version branche Mem0 en local sur Ollama + Qdrant. L'objectif est d'éviter que Mem0 tente OpenAI ou un provider cloud par défaut.

## Prérequis Windows

- Windows 10/11 récent.
- GPU NVIDIA + driver à jour si tu veux WhisperX/pyannote en CUDA.
- Docker Desktop lancé, avec backend WSL2.
- Ollama installé ou installable via winget.
- Token HuggingFace avec acceptation des modèles pyannote.

## Installation tout-en-un

Depuis PowerShell, dans le dossier du projet :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_install_all.ps1
```

Ou double-clique / lance :

```bat
INSTALL_WINDOWS.bat
```

Le script fait :

1. installe/valide Git, FFmpeg, Python 3.11, Ollama, Docker Desktop via winget si possible ;
2. crée `.env` depuis `.env.windows.example` ;
3. configure Ollama pour `qwen3:8b` ;
4. configure Mem0 local : LLM Ollama + embedder Ollama `nomic-embed-text` + vector store Qdrant `mlomega_mem0` ;
5. crée `.venv` ;
6. installe PyTorch CUDA, puis `pip install -e .[all]` ;
7. lance Qdrant + Neo4j via Docker Compose ;
8. pull `qwen3:8b` et `nomic-embed-text` dans Ollama ;
9. précharge embeddings, reranker, WhisperX, alignement français, pyannote et SpeechBrain quand le token HuggingFace est présent ;
10. lance `mem0-config`, `mem0-doctor` et `doctor-elite --fail`.

Pour changer le modèle Qwen :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_install_all.ps1 -OllamaModel "qwen3:8b"
```

Pour fournir le token HuggingFace sans prompt :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_install_all.ps1 -HfToken "hf_xxx"
```

## Premier test

Place un audio court à la racine sous le nom `conversation.wav`, puis :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_first_test.ps1 -AudioPath .\conversation.wav
```

Ou :

```bat
RUN_FIRST_TEST.bat -AudioPath .\conversation.wav
```

Ce script exécute :

```powershell
mlomega-audio doctor-elite --fail
mlomega-audio init-db
mlomega-audio ingest-audio .\conversation.wav --language fr --speaker-map .\examples\speaker_map.json
mlomega-audio memory-overview
mlomega-audio sync-jobs
mlomega-audio query "qu'est-ce qui a été dit dans cette conversation ?"
```

## Commandes utiles

Voir la config Mem0 réelle :

```powershell
. .\scripts\load_env.ps1
.\.venv\Scripts\mlomega-audio.exe mem0-config
```

Tester seulement Mem0 local :

```powershell
. .\scripts\load_env.ps1
.\.venv\Scripts\mlomega-audio.exe mem0-doctor --fail --show-config
```

Relancer les syncs ratées :

```powershell
.\.venv\Scripts\mlomega-audio.exe sync-pending
```

## Notes importantes

- Ollama Windows expose normalement `http://localhost:11434`.
- Qdrant et Neo4j tournent via Docker Compose.
- Mem0 utilise une collection Qdrant séparée : `mlomega_mem0`.
- La mémoire principale du projet reste dans SQLite + Qdrant `mlomega_audio_memory`.
- Le premier lancement peut télécharger plusieurs modèles lourds.
