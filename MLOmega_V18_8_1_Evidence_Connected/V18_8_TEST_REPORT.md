# Rapport de validation MLOmega V18.8.1

## Périmètre exécuté

- Intégrité V17.6 et audits V18.1, V18.2, V18.3 : 31 tests.
- V18.8 adaptive live : 6 tests.
- V18.8.1 evidence-connected : 6 tests.

**Total : 43 tests passés.**

## Contrôles nouveaux V18.8.1

- transition parole → silence : un seul signal de clôture sémantique, pas un appel Qwen par chunk silencieux ;
- frame réelle capturée → `vision_frames` → raw timeline → bundle → sélection deep ;
- frame introuvable : statut `blocked_visual_evidence_unavailable`, aucun faux succès deep vision ;
- dHash et changement de place : séparation de bundles sans VLM live obligatoire ;
- VLM deep simulé → addendum de contexte Brain2 scoped à la conversation.

## Limite assumée

Cette validation est locale et sans GPU/Windows/Android réel. L’installateur doit toujours confirmer les probes NVIDIA, Docker/WSL2, Ollama, Hugging Face/Pyannote, Qdrant et Phone Bridge avant `PRODUCTION_READY`.
