# MLOmega V18.7.1 - Zero Surprise / Resumable

## Correctifs de compatibilité et de sécurité

- Le Bridge Phone ne sélectionne plus le dernier service global lors d'un `/session/stop`. Il lit le manifeste runtime de ce projet, vérifie le propriétaire et transmet le `service_run_id` exact à `brainlive-stop-service --close-day`. Si l'identité est ambiguë, il conserve le spool et les raw; aucune purge n'est autorisée.
- `doctor-elite` reste utilisable pour les anciens guides : sous le profil `CORE_BRAINLIVE_V18_7_PHONE`, il devient un alias de `doctor-core-v18-7` et n'exige plus Neo4j, Graphiti ou Mem0.
- Docker/WSL2 : si Docker Desktop est installé mais WSL2 non initialisé, INSTALL demande maintenant l'activation de WSL2 et enregistre une reprise après reboot.
- Qdrant est uniformisé sur l'image épinglée `qdrant/qdrant:v1.12.6`; le nom de conteneur reflète V18.7.

## Flux inchangé

`STOP` et l'arrêt Android font toujours le chemin complet : drain -> post-stop deep audio/vision/Brain2 -> longitudinal jour -> coordination -> Life Model -> live-ready -> gates -> nettoyage uniquement après succès complet.

## Limites honnêtes

La validation Windows réelle reste obligatoire pour le driver NVIDIA, CUDA, Docker/WSL2, l'accès Hugging Face/Pyannote, les downloads de modèles et les permissions Android. L'installateur bloque au lieu d'annoncer un faux succès si l'une de ces probes échoue.
