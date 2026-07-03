# Run RTX 3070 — V3.1 strict

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel setuptools
pip install -e ".[all]"
cp .env.example .env
```

Services :

```bash
docker compose up -d qdrant neo4j ollama
ollama pull qwen3:8b
mlomega-audio doctor-elite --fail
```

Ingestion audio :

```bash
mlomega-audio init-db
mlomega-audio ingest-audio ./conversation.wav --language fr --speaker-map examples/speaker_map.json
```

Interrogation :

```bash
mlomega-audio query "voici le pattern que tu ne vois pas"
```

Pour pyannote, il faut un token HuggingFace dans `.env` et l'acceptation des modèles pyannote concernés dans ton compte.
