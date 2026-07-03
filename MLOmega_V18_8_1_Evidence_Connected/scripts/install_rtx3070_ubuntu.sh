#!/usr/bin/env bash
set -euo pipefail

python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel setuptools
pip install -e ".[all]"

echo "Démarre les services: docker compose up -d qdrant neo4j ollama"
echo "Puis: ollama pull qwen3:8b"
echo "Puis: mlomega-audio doctor-elite --fail"
