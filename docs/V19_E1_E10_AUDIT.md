# Audit V19 — E1 à E10

Date: 2026-07-03

## E1 — Squelette monorepo

- **Fichiers qui l'implémentent**: `packages/`, `services/live-pc/`, `apps/companion-web/`, `simulators/fake_xr_device.py`, `scripts/RUN_MLOMEGA_V19.ps1`, `configs/profiles/rtx3070.yaml`, `tests/v19/`.
- **Fonctionnalité opérationnelle**: l'arborescence V19 existe et sépare les contrats, le service live PC, le renderer web, les simulateurs, les scripts Windows et les tests.
- **Simulé, stubé ou absent**: le squelette ne contient pas encore les services complets des lots 2/3; il prépare seulement les points d'extension.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_scripts_profile.py` vérifie la présence des scripts/profils attendus et l'absence du port legacy 8766.
- **Pourquoi terminé**: E1 est un jalon de structure; les dossiers et fichiers nécessaires au Lot 1 sont présents et utilisés par les tests V19.

## E2 — Contrats V19

- **Fichiers qui l'implémentent**: `packages/contracts/schemas/*.schema.json`, `packages/contracts/python/models.py`, `packages/contracts/csharp/*.cs`, `tests/v19/test_contracts.py`.
- **Fonctionnalité opérationnelle**: les contrats `FrameEnvelope`, `LocalTrack`, `SceneDelta`, `ReflexEvent`, `UIIntent`, `UIReceipt`, `HotSceneContext` et `EvidenceEvent` ont une version `v19.0` et des modèles Python stricts round-trippables JSON.
- **Simulé, stubé ou absent**: les stubs C# sont des DTO de contrat; aucun runtime Unity/Snap réel n'est livré à ce stade.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_contracts.py`.
- **Pourquoi terminé**: le critère Lot 1 est un contrat stable et testable, pas une intégration device réelle.

## E3 — SessionHub

- **Fichiers qui l'implémentent**: `services/live-pc/sessionhub.py`, `tests/v19/test_sessionhub.py`.
- **Fonctionnalité opérationnelle**: création de sessions XR avec `session_id` unique, token éphémère, authentification par token et calcul d'offset ClockSync à partir des timestamps monotones.
- **Simulé, stubé ou absent**: stockage en mémoire seulement; pas encore de persistance ni de révocation/rotation périodique avancée.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_sessionhub.py`.
- **Pourquoi terminé**: les primitives minimales exigées pour appairer un client simulé et mesurer l'offset sont présentes.

## E4 — Ingress vidéo / transport simulé

- **Fichiers qui l'implémentent**: `services/live-pc/gateway.py`, `simulators/fake_xr_device.py`, `tests/v19/test_transport.py`.
- **Fonctionnalité opérationnelle**: interface `VideoIngress`, adaptateur `AiortcIngress` générique, file `LatestFrameQueue` mono-slot et compteur de frames droppées.
- **Simulé, stubé ou absent**: aucun serveur WebRTC aiortc complet n'est branché; l'ingress accepte un itérateur simulé ou compatible PyAV.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_transport.py`.
- **Pourquoi terminé**: le jalon Lot 1 demande le transport simulé et la queue bornée à 1; ces garanties sont testées.

## E5 — Simulateur XR

- **Fichiers qui l'implémentent**: `simulators/fake_xr_device.py`, `services/live-pc/gateway.py`, `tests/v19/test_transport.py`, `scripts/simonly_demo_v19.py`.
- **Fonctionnalité opérationnelle**: le fake device émet des frames et enveloppes V19 consommables par l'ingress et par la démo SimOnly.
- **Simulé, stubé ou absent**: aucune caméra, lunettes, IMU ou téléphone réel; les poses/frames sont synthétiques.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_transport.py` et `PYTHONPATH=.:src python scripts/simonly_demo_v19.py`.
- **Pourquoi terminé**: E5 est explicitement un simulateur permettant de valider le chemin sans matériel.

## E6 — Delivery adapter

- **Fichiers qui l'implémentent**: `services/live-pc/delivery_adapter.py`, `src/mlomega_audio_elite/v18_delivery.py`, `src/mlomega_audio_elite/v18_8_live_policy.py`, `tests/v19/test_delivery_adapter.py`, `scripts/simonly_demo_v19.py`.
- **Fonctionnalité opérationnelle**: lecture de `brainlive_intervention_delivery_queue`, conversion en `UIIntent`, marquage `delivered`, persistance des `UIReceipt` dans `brainlive_intervention_feedback_events_v188` via la politique V18.8.
- **Simulé, stubé ou absent**: avant cet audit, le rendu WebSocket réel n'était pas branché; seul un `RendererHub` mémoire et le simulateur Python couvraient le flux.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_delivery_adapter.py` et `PYTHONPATH=.:src python scripts/simonly_demo_v19.py`.
- **Pourquoi terminé**: la partie queue/feedback était réelle; la limite restante était la connexion renderer, corrigée par le branchement WebSocket de ce patch.

## E7 — Companion-web

- **Fichiers qui l'implémentent**: `apps/companion-web/index.html`, `apps/companion-web/app.js`, `services/live-pc/delivery_adapter.py`, `tests/v19/test_delivery_adapter.py`.
- **Fonctionnalité opérationnelle**: la page ouvre `ws://<host>:8706/ws`, affiche les `UIIntent` et renvoie des `UIReceipt` `displayed`/`dismissed`.
- **Simulé, stubé ou absent**: rendu volontairement minimal; pas de flux vidéo, sous-titres avancés, contours ou design system final.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_delivery_adapter.py` couvre maintenant la diffusion JSON WebSocket côté hub; le rendu DOM reste manuel/non automatisé.
- **Pourquoi terminé**: pour le Lot 1, companion-web sert de renderer de référence/debug minimal; le vrai serveur `/ws` est maintenant disponible.

## E8 — Dégradé / GPU / bench primitives

- **Fichiers qui l'implémentent**: `services/live-pc/degraded.py`, `services/live-pc/gpu_arbiter.py`, `services/live-pc/gateway.py`, `scripts/bench_v19_sim.py`, `docs/BENCH_RESULTS.md`.
- **Fonctionnalité opérationnelle**: états de dégradation et arbiter GPU minimal, mesure de décodage simulée via `DecodeBench`, bench simulé documenté.
- **Simulé, stubé ou absent**: pas de bench matériel RTX 3070 garanti dans ce conteneur; NVML et le contrôle Ollama réel dépendent de l'environnement hôte.
- **Test exact**: `PYTHONPATH=.:src python scripts/bench_v19_sim.py`.
- **Pourquoi terminé**: les primitives et le bench sim-only existent; le matériel réel est hors scope du conteneur.

## E9 — Scripts + profil

- **Fichiers qui l'implémentent**: `scripts/INSTALL_MLOMEGA_V19_WINDOWS.ps1`, `scripts/setup_profile.ps1`, `scripts/RUN_MLOMEGA_V19.ps1`, `scripts/DOCTOR_MLOMEGA_V19.ps1`, `scripts/BENCH_V19.ps1`, `configs/profiles/rtx3070.yaml`, `tests/v19/test_scripts_profile.py`.
- **Fonctionnalité opérationnelle**: scripts Windows présents, profils et ports V19 87xx définis, `RUN_MLOMEGA_V19.ps1 -SimOnly` enveloppe la démo Python.
- **Simulé, stubé ou absent**: les scripts PowerShell ne sont pas exécutables dans ce conteneur Linux sans `pwsh`; les checks Windows/hardware restent à valider sur machine cible.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19/test_scripts_profile.py`.
- **Pourquoi terminé**: la présence, la cohérence des ports et le câblage SimOnly sont testés; l'exécution Windows réelle est une validation d'environnement.

## E10 — Checkpoint Lot 1

- **Fichiers qui l'implémentent**: `scripts/simonly_demo_v19.py`, `services/live-pc/delivery_adapter.py`, `apps/companion-web/app.js`, `docs/BENCH_RESULTS.md`, `docs/DECISIONS.md`, `tests/v19/`.
- **Fonctionnalité opérationnelle**: la démo Python valide fake device → enqueue BrainLive → `UIIntent` → receipt simulé → feedback SQLite V18.8. Ce patch ajoute le serveur FastAPI/WebSocket `/ws` réel pour que companion-web reçoive les `UIIntent` et renvoie les `UIReceipt` sur le même canal.
- **Simulé, stubé ou absent**: `RUN_MLOMEGA_V19.ps1 -SimOnly` n'est pas exécuté dans ce conteneur Linux faute de PowerShell; pas de capture caméra/WebRTC matérielle; la partie companion-web est testée au niveau hub WebSocket, pas via navigateur screenshot.
- **Test exact**: `PYTHONPATH=.:src pytest -q tests/v19`, `PYTHONPATH=.:src python scripts/simonly_demo_v19.py`, `PYTHONPATH=.:src python scripts/bench_v19_sim.py`.
- **Pourquoi terminé malgré les limites**: le checkpoint est terminé en mode SimOnly parce que le chemin fonctionnel de bout en bout existe sans matériel, les contrats/transport/delivery sont verts, le feedback est persisté, et le manque identifié (WebSocket companion-web réel) est corrigé ici. La validation PowerShell et hardware reste une limite d'environnement documentée, pas une absence fonctionnelle du code Lot 1.
