# EXECUTOR_HANDOFF — MLOmega V19 (Exocortex XR Live)

Document de passation pour l'agent exécuteur (Codex). Rédigé le 2026-07-03 après audit complet en lecture seule de
`MLOmega_V18_8_1_Evidence_Connected/` et du `Guide_Maitre_MLOmega_V19_Transformation_XR_Live.docx`.

**Kit de passation complet (auto-suffisant, le docx original est optionnel) :**
1. Ce fichier — *quoi construire et pourquoi* : décisions, architecture, lots, budgets, sources.
2. `docs/EXECUTOR_BUILD_GUIDE.md` — *comment* : étapes E1→E30, signatures réelles du code existant, chaînes live/nocturne, pièges connus.
3. `docs/GUIDE_V19_REFERENCE.md` — sections normatives extraites du guide maître : **toute référence « guide §x » dans ces documents résout là-bas** (SceneCache/TTL, skills, composants UI, priorités de rendu, chaînes des scénarios, gates G0-G11, liste complète des tests, règles de vérité). En cas de contradiction, ce handoff prime.

**Règles de travail non négociables pour l'exécuteur :**
1. Le cœur `src/mlomega_audio_elite/` n'est **jamais réécrit**. On l'étend par adaptateurs et endpoints ciblés.
2. Chaque lot se termine par ses commandes de validation. On ne démarre pas un lot tant que les critères de fin du précédent ne sont pas verts.
3. Tout ce qui touche au matériel réel (XREAL, S25, GPU) doit d'abord fonctionner contre un **simulateur** fourni dans le même lot.
4. Local-first : aucune intégration cloud active par défaut. Tout provider cloud passe par une interface, est désactivé par défaut, visible dans l'UI.
5. Les contrats (`UIIntent`, `SceneDelta`, `HotSceneContext`, `EvidenceEvent`, `ReflexEvent`) vivent dans `packages/contracts/` et ne dépendent d'aucun SDK matériel ni d'aucun modèle IA.

---

## 1. État réel du dépôt V18.8.1 (résumé d'audit)

### 1.1 Ce qui fonctionne (à conserver tel quel)
- **Chaîne de preuve et gouvernance mémoire** : 115 tables SQLite, traçabilité owner/evidence stricte
  (`v18_life_model.py::_DIRECT_EVIDENCE_SOURCES` revalide toute référence LLM contre une liste blanche de ~30 tables sources — vrai garde-fou anti-hallucination de provenance). Niveaux de vérité `OBSERVED/INFERRED/CONSOLIDATED` déjà en place.
- **Contrats JSON stricts vers le LLM** (`llm_contracts_v15_18.py`) : schéma validé, rejet des champs non déclarés, échec dur plutôt que dégradation silencieuse.
- **Cycle delivery des interventions** (`v18_delivery.py`, `v18_8_live_policy.py`) : `candidate_id → delivery_id (déterministe, dédupliqué) → outcome (displayed/seen/acted/dismissed)`, cooldown anti-résurrection, feedback persisté comme preuve. **Déjà découplé du canal de livraison** → point d'ancrage idéal pour l'adaptateur XR.
- **Hot capsule** (`v18_context.py`, `v18_hot_capsule.py`) : contexte borné (budget 12k chars), `as_of` strict, omissions traçables (`omitted_refs`), accepte déjà `visual_context`/`world_state`. Squelette direct du futur `HotSceneContext`.
- **Gestion GPU par phases** (`runtime_v18_8.py`) : bascule live → post-stop avec `release_live_model_caches()`, `ollama_unload`, `torch.cuda.empty_cache()` aux frontières. Réelle, pas déclarative.
- **Installateur Windows** (`INSTALL_MLOMEGA_V18_8_WINDOWS.ps1`) : transactionnel (venv `.new` + swap atomique + rollback), manifest SHA256 (183 fichiers), validation token HF en ligne avant téléchargement, reprise post-reboot via tâche planifiée + DPAPI. Modèle à imiter pour V19.
- **Calibration prédictive** (`v18_predictive_retrieval.py`) : la seule brique épistémiquement correcte du dépôt — bins Precision/Recall/Brier sur labels vérifiés, **abstention explicite** si données insuffisantes.
- **Deep audio post-stop** (`brainlive_offline_deep_audio_v18_5.py`) : stitching ffmpeg, réconciliation locuteurs, validation stricte. Solide.

### 1.2 Ce qui est fragile ou incomplet
- **Jamais exécuté sur le matériel cible** : `V18_8_TEST_REPORT.md` déclare explicitement une validation logique seule (43 tests), sans GPU/Windows/Android réel. Le pic VRAM réel n'a jamais été mesuré.
- **Prédiction de vie = prompts géants** : `brain2_life_model_v15_10.py` agrège ~30 tables puis envoie **un unique prompt Qwen** (timeout 180-480 s) censé produire routines/besoins/hooks prédictifs. La similarité de cas (`brain2_longitudinal_cases_v17.py`) est un Jaccard pondéré à poids câblés en dur (0.22/0.24/0.16/0.14/0.14/0.10), jamais appris ni validé. Avec un Qwen 7-14B local, la sortie sera un résumé structuré correct mais **pas une prédiction calibrée** — la forme est garantie, pas le fond.
- **La boucle de calibration est vide** : la brique la plus solide (`v18_predictive_retrieval`) exige des labels humains vérifiés qui ne sont jamais collectés systématiquement.
- **Duplication active v18_7/v18_8** : `runtime_v18_7.py`/`runtime_v18_8.py` (4 lignes de diff), `operations_v18_7.py`/`operations_v18_8.py` (40 lignes de diff), et les deux sont importés en production → risque de correctif appliqué d'un seul côté.
- **VRAM Ollama non maîtrisée par le code Python** : `keep_alive` 20-30 min dans un process séparé ; si `ollama_unload` échoue silencieusement, WhisperX large-v3 + qwen3-vl:8b + qwen3.5:9b peuvent se cumuler et saturer 8 Go.
- **Transport live = fichiers HTTP** : audio WAV 4 s / image JPEG 30 s / GPS 60 s en multipart vers le port 8766, queue SQLite, pump vers inbox, debounce 12-90 s avant LLM. Latence réelle d'une intervention : **30 s à ~2 min**. Incompatible avec tout usage XR temps réel.
- **Sécurité bridge minimale** : token statique unique `X-MLomega-Token`, pas de TLS natif, pas de scoping device/session, pas de backpressure réseau.
- **Dette diverse** : `cli.py` ~150 sous-commandes V12→V18.8 ; `docs/RUN_RTX3070.md` obsolète et trompeur (Neo4j, qwen3:8b) ; `docker-compose.yml` ≡ `docker-compose.core-v18_8.yml` avec conteneur nommé `mlomega-v18-7-qdrant` ; requirements non-lock résiduels.

### 1.3 Verdict de viabilité (projet 1)
Le projet mémoire est **viable comme infrastructure** (ingestion, preuve, scope, non-régression : niveau ingénierie sérieux) et **pas encore viable comme moteur de prédiction de vie**. Pour tenir l'objectif « prédire mon avenir », le Lot 2 doit impérativement : (a) fermer la boucle de vérification **par observation** — le système capte l'audio/vidéo/GPS en continu, il peut donc vérifier lui-même ses prédictions contre ce qu'il observe ensuite, sans solliciter l'utilisateur (principe déjà posé par le dépôt : `auto_verification_v14_4.py` et la règle V18.8.1 « les observations ultérieures restent la source principale ; un retour explicite reste facultatif ») ; (b) découper les prompts géants en sous-tâches vérifiables (extraction factuelle ≠ inférence spéculative) ; (c) rendre la similarité de cas apprenable. Sans ces trois points, le Life Model produira du texte plausible, pas des prédictions.

---

## 2. Verdict sur le guide V19

Le guide est de bonne qualité : ses invariants sont conservés intégralement — quatre horizons (UL0 / VisionRT / BrainLive / Brain2) dont aucun ne bloque le précédent, UIIntent sémantique (jamais de coordonnées pixel imposées par le PC), frame → observation → preuve → événement → mémoire (jamais frame → mémoire), truth_level sur toute sortie visible, queue vidéo = 1 frame, gates matériels G0→G11, licences isolées.

**Corrections apportées par cet handoff** (vérifiées le 2026-07-03) :
1. **aiortc décode H.264 en CPU pur (PyAV/FFmpeg)** — le RTX 3070 n'est pas utilisé au décodage et des latences cumulatives sont documentées par la communauté. Décision : aiortc reste le choix du premier jalon (simplicité, BSD), mais derrière une interface `VideoIngress` avec **bench de sortie obligatoire** (P95 decode < 33 ms à 720p30) ; plan B au même contrat : GStreamer `webrtcbin` + `nvh264dec`.
2. **Ultralytics YOLO est AGPL-3.0** — acceptable en usage strictement personnel, mais à isoler derrière `VisionModelProvider` avec alternative Apache-2.0 documentée (YOLOX ou RTMDet export ONNX) pour ne jamais verrouiller le projet.
3. **Deux profils LLM au lieu d'un** : le guide suppose qwen3.5:9b partout. Sur 8 Go de VRAM partagés avec VisionRT, un 9B q4 (~6 Go) ne cohabite pas avec le live. Décision : `LLMProvider` expose deux profils — `live` (modèle ~4B quantisé, ≤3 Go, réponses H1 courtes) et `deep` (9b+ la nuit, GPU entièrement libéré du live). C'est une config, pas un fork de code.
4. **XREAL Eye : incertitudes matérielles réelles** — la doc caméra dit « XREAL One series » sans citer explicitement One Pro ; capteurs bruts (gray cams/IMU direct) réservés au programme Enterprise SDK ; capture RGB exige les permissions Android `RECORD_AUDIO` + `FOREGROUND_SERVICE_MEDIA_PROJECTION`. Conséquence : le gate G1 (sample officiel sur le vrai matériel) est **le premier livrable du Lot 3**, et tout le reste du lot est développé contre le simulateur.
5. **Mot d'éveil + gestes précisés** : keyword spotting sherpa-onnx (même dépendance que l'ASR streaming, nom personnalisable) ; gestes via MediaPipe Tasks Hands + Gesture Recognizer sur S25.
6. **Mode capture-only / téléphone-only ajouté au périmètre** (absent du guide) : lunettes accrochées verticalement → détection d'orientation (gravité IMU ou heuristique visuelle) → rotation des frames avant tout traitement ; visualiseur web compagnon servi par le PC qui rejoue le flux d'UIIntent — lisible sur n'importe quel téléphone (iPhone inclus), sert aussi de simulateur d'affichage pour tous les tests.

---

## 3. Architecture cible

### 3.1 Monorepo V19

```
MLOmega_V19/
  src/mlomega_audio_elite/        # cœur V18.8.1 copié tel quel + adaptateurs V19 (nouveaux fichiers uniquement)
  packages/contracts/             # source de vérité : JSON Schema + modèles pydantic + génération C# (Unity)
  services/live-pc/               # nouveau venv .venv-live : sessionhub, gateway, visionrt, audiort,
                                  #   worldbrain, ultralive-lan, memory-bridge, delivery-adapter, gpu-arbiter
  apps/xr-mobile/                 # Unity 6 LTS + XREAL SDK + plugin Kotlin (webrtc, sherpa-onnx, mediapipe)
  apps/companion-web/             # visualiseur UIIntent (téléphone-only, capture-only, debug)
  configs/                        # profils matériels, policies, MODEL_MANIFEST (licence/sha/version), retention
  scripts/                        # INSTALL/RUN/STOP/DOCTOR/BENCH _MLOMEGA_V19 .ps1
  simulators/                     # fake-xr-device, sim-s25, mock-llm, scénarios enregistrés
  tests/                          # contrats, non-régression V18, e2e simulés
  docs/
```

Deux environnements Python séparés : `.venv` (cœur V18.8, torch cu121, intouché) et `.venv-live` (services live, ONNX Runtime GPU). Ils ne partagent que SQLite, Qdrant, Ollama et les contrats.

### 3.2 Les trois couches et leur budget de latence

| Couche | Où | Contenu | Budget |
|---|---|---|---|
| **Ultra-Live** | S25 (UL0-device) + PC sans LLM (UL0-LAN) | tracks locaux, zoom/LensWindow, gestes, wake word, sous-titres/traduction streaming, proximité/mouvement, next-action | cue device < 100 ms ; sous-titre partiel < 1 s |
| **Live contextuel** | PC | VisionRT (détection/OCR/VLM ciblé), WorldBrain (scène/session map/changes), BrainLive H0/H1/H2 + HotSceneContext + delivery XR | « c'est quoi ? » < 3 s ; suggestion BrainLive 2-10 s, jamais bloquante |
| **Mémoire profonde** | PC nuit / post-stop | ingestion continue, indexation différée, deep audio/vision, Brain2, Life Model, consolidation, prédiction + calibration | asynchrone, GPU entièrement libéré du live |

Séparation stricte des flux : capture → ingestion (queue bornée) → mémoire (preuves sélectionnées uniquement) ; recherche et contexte chaud lisent la mémoire, jamais l'inverse en live.

**Clarification importante — Ultra-Live ne contient AUCUN LLM ni VLM, local ou cloud.** La reconnaissance ultra-live (tracks, mains, mouvement, proximité, zoom, sous-titres) repose sur des petits calculateurs spécialisés (MediaPipe, détecteur ONNX nano, ASR streaming) qui tournent en < 100 ms sur l'appareil ou le LAN. Un modèle cloud ne rend PAS ce chemin plus rapide : l'aller-retour réseau Internet (80-300 ms variables) le rendrait plus lent et moins fiable. Le cloud est un levier de **profondeur** (qualité des réponses sémantiques VisionRT/BrainLive, 1-10 s), jamais un levier de **réflexe**. C'est pour cela que le réflexe est local par conception et que le cloud est un provider optionnel des couches supérieures uniquement.

### 3.3 Adaptateurs obligatoires (interfaces stables, une implémentation par cible)

| Interface | Impl. V19 initiale | Impl. futures prévues |
|---|---|---|
| `XRDeviceAdapter` | XREAL One Pro + Eye (Unity) **et** `PhoneOnlyAdapter` (caméra + écran du téléphone, livré dès V19) | Snap Spectacles (client Lens Studio parlant les mêmes contrats via WebSocket), Meta, boîtier XR |
| `CameraPoseProvider` | pose 6DoF XREAL SDK | ARCore, caméra fixe |
| `AudioInputProvider` | micros lunettes / S25 | micro PC, BT |
| `MobileReflexRuntime` | S25 (Kotlin + Unity) | autre téléphone / boîtier XR |
| `LLMProvider` | Ollama (profils `live` 4B / `deep` 9b+) | OpenAI/Gemini/Anthropic opt-in |
| `VisionModelProvider` | YOLO-nano ONNX (+alt. YOLOX Apache) | tout détecteur/VLM |
| `ASRTranslationProvider` | faster-whisper PC + sherpa-onnx S25 | cloud opt-in |
| `SpatialMapProvider` | pose+keyframes (V19.A), reloc légère (V19.B) | SLAM isolé (GPLv3 → WSL2) |
| `UIRendererAdapter` | Unity URP world-space (liquid glass) | companion-web, futurs renderers |

### 3.4 Contrats (packages/contracts/ — indépendants du matériel et du modèle)

Champs minimum, conformes au guide §7 :

- `FrameEnvelope{session_id, frame_id, capture_monotonic_ns, captured_at_utc, pose, intrinsics?, rotation, source}`
- `LocalTrack{session_id, track_id, source_frame_id, kind, bbox_or_mask, velocity_screen, visibility, confidence, observed_at_monotonic_ns}`
- `SceneDelta{session_id, source_frame_id, entities[], relations[], changes[], map_quality, evidence_refs[], expires_at}`
- `ReflexEvent{session_id, source_frame_id, skill, prediction, horizon_ms, confidence, severity, evidence_refs[], aggregate_key}`
- `UIIntent{ui_intent_id, producer(ultralive|visionrt|brainlive), source_frame_id?, target_track_id?, entity_id?, component, anchor, content, truth_level(observed|probable|remembered|inferred|replay), confidence, priority, ttl_ms, ui_hint, evidence_refs[], delivery_id?}`
- `UIReceipt{ui_intent_id, delivery_id?, event(displayed|seen|acted|dismissed|corrected), observed_at, local_track_state, user_action?, source}`
- `HotSceneContext` = extension du hot capsule existant : session/lieu/map_quality + focus + entités visibles + personnes identifiées + activité/task + traduction active + changes/last-seen + ReflexEvents agrégés + mémoire Brain2 + evidence refs + omissions. Budget dur en caractères, `as_of` strict — réutiliser le mécanisme de `v18_hot_capsule.py`.
- `EvidenceEvent{event_type, occurred_at, session_id, entity, observation, place, truth_level, confidence, evidence[{frame_id, clip_id, sha256}], provenance{models, source_frame_id}}`

Identifiants non interchangeables : `session_id` (session lunettes), `frame_id` (immuable, généré côté capture), `track_id` (secondes/minutes, jamais une identité), `entity_id` (durable), `evidence_id`, `delivery_id`, `ui_intent_id` (TTL court). Horloge : `ClockSync` monotone S25↔PC à l'ouverture de session, offset re-mesuré périodiquement.

### 3.5 Profils de capacité et assistant de configuration (première exécution)

Le système n'est **jamais verrouillé à une combinaison matérielle**. Un fichier `configs/user_profile.yaml` (créé par un assistant interactif au premier `RUN_MLOMEGA_V19.ps1`, modifiable ensuite depuis l'UI et par simple édition) déclare :

```yaml
display:   xreal_one_pro | spectacles | phone_only | companion_web   # « as-tu des lunettes ? lesquelles ? »
capture:   xreal_eye | phone_camera | none (audio seul)
llm:       ollama_local (défaut) | openai | gemini | anthropic       # + modèle et clé si cloud
vision:    onnx_local (défaut) | provider cloud opt-in
asr:       local (défaut) | provider cloud opt-in
cloud_data_policy: local_only (défaut) | allow_crops | allow_transcripts   # granulaire, visible dans StatusBar
```

Règles : chaque valeur mappe une implémentation d'adaptateur du §3.3 — en changer ne recompile rien, ne migre rien ; cloud désactivé par défaut, chaque envoi cloud actif est signalé dans l'UI (indicateur permanent) et les données envoyées sont séparées des données locales ; le doctor valide le profil au démarrage (ex. `spectacles` sans client appairé → bascule proposée vers `phone_only`). Le **mode `phone_only` est une cible de premier rang, pas un fallback de debug** : le téléphone capture (caméra/micro) et affiche l'UI (mêmes UIIntent, rendu 2D), ce qui donne un produit utilisable sans aucune lunette et un banc de test permanent.

### 3.6 Politique de traitement du flux vidéo (qui consomme quoi, à quelle cadence)

Le flux vidéo n'est **pas** « chaque frame passe partout ». Chaque consommateur a sa propre cadence, découplée du flux ; le décodeur ne garde que la dernière frame (queue = 1, frames en retard abandonnées et comptées). Valeurs de départ pour 720p30 — à recalibrer par `BENCH_V19.ps1`, jamais en dur dans le code (tout dans `configs/profiles/rtx3070.yaml`) :

| Consommateur | Où | Cadence | Ce qu'il voit |
|---|---|---|---|
| Renderer + LocalTrack | S25 | cadence d'affichage native | la texture caméra locale, zéro aller-retour réseau |
| Tracker global (ByteTrack/BoT-SORT) | PC (CPU) | toutes les frames décodées disponibles (~15-30 fps) | frame complète ; interpole les positions entre deux détections |
| Détecteur (YOLO-nano ONNX) | PC (GPU) | **adaptatif 5-15 fps** : haut si mouvement/nouvelle zone/focus, bas si scène statique | frame complète ; le tracker maintient les tracks entre deux passes |
| OCR | PC | à la demande uniquement (focus, LensWindow, commande) | ROI/crop, jamais plein écran |
| VLM ciblé | PC | à la demande, un job à la fois, préemptible | crop de la zone de focus |
| Sélecteur de keyframes | PC | événementiel : score de changement de scène (distance d'embedding + histogramme + événements pose) → max ~1 keyframe / qq secondes en mouvement, zéro si statique | frame complète → observations WorldBrain + candidates de preuve |
| Détection de changements | PC | à la re-visite d'une zone (keyframes relocalisées) | paires de keyframes avant/après |
| Ring buffer preuve | S25 | continu, court, chiffré | vidéo/audio bruts pré/post-événement pour extraction de clips |
| Tampon-jour (optionnel) | PC | continu basse résolution | replay du jour ; purgé au close-day |
| Mémoire (MLOmega) | PC | **jamais de frame directe** | uniquement keyframes/clips/événements sélectionnés par MemoryBridge |

Sous charge, le GpuArbiter dégrade dans cet ordre (et l'UI l'affiche) : détecteur → cadence plancher 5 fps ; détection de changements en pause ; VLM refusé ; jamais toucher au tracker ni aux sous-titres. Ce tableau est le contrat de traitement : un exécuteur qui « fait passer chaque frame dans le détecteur + OCR + VLM » a mal lu et fera fondre les 8 Go.

### 3.7 Règle de vérité (héritée du guide, appliquée partout)
- Toute UI porte son `truth_level` ; map_quality faible ⇒ pas de flèche spatiale ; identité faible ⇒ pas de nom (règle de justesse, pas de permission : on n'affiche pas un nom dont on n'est pas sûr).
- Personnes : le système identifie et profile librement les gens de ta vie (aucun gating de consentement — outil personnel non diffusé). Une personne non encore reconnue reste un track jusqu'à ce qu'assez d'indices l'identifient, puis devient une entité durable enrichie de sa relation. Pas de recherche d'identité sur Internet — non par éthique mais parce que l'archi est locale et n'a pas ce composant ; à activer un jour via un provider si tu le veux.
- Sherlock : « trace observée » ≠ « hypothèse » ; l'hypothèse est étiquetée comme telle (règle de justesse, pour ne pas confondre déduction et fait).
- Route/danger : information seulement, jamais « tu peux y aller » (règle de sécurité physique, conservée).

---

## 4. Stack technique et sources externes

Sources vérifiées le 2026-07-03. À ne consulter que si l'API concernée pose problème.

| Nom | URL | Licence | Rôle | Raison | Risque/limite |
|---|---|---|---|---|---|
| XREAL SDK 3.1.0 (Unity) | https://docs.xreal.com/ (download: developer.xreal.com/download) | Propriétaire | Rendu stéréo, pose 6DoF, caméra RGB Eye (YUV_420_888) | SDK officiel, seul accès au matériel | Doc Eye dit « One series » sans citer One Pro explicitement ; capteurs bruts = Enterprise SDK sur demande ; permissions `RECORD_AUDIO` + `FOREGROUND_SERVICE_MEDIA_PROJECTION` requises. **Valider gate G1 avant tout.** |
| GetStream/webrtc-android | https://github.com/GetStream/webrtc-android | Apache-2.0 | libwebrtc précompilé Android (vidéo H.264 + Opus + DataChannel) | Seul binding libwebrtc largement maintenu en 2026 | Roadmap dépendante de Stream ; figer la version au premier build reproductible |
| k2-fsa/sherpa-onnx | https://github.com/k2-fsa/sherpa-onnx | Apache-2.0 | ASR streaming + VAD + keyword spotting (wake word) on-device S25 | Actif, releases régulières, couvre ASR+KWS avec une seule dépendance | Modèles FR et EN streaming zipformer probablement séparés (pas de bilingue FR/EN confirmé) — choisir sur k2-fsa.github.io/sherpa/onnx/pretrained_models ; build JNI Android |
| aiortc | https://github.com/aiortc/aiortc | BSD-3-Clause | Réception WebRTC côté PC Python (premier jalon) | Pure Python, intégration directe pipeline vision | **Décodage H.264 CPU (PyAV), pas de NVDEC** ; bench P95 obligatoire ; plan B : GStreamer webrtcbin + nvh264dec (même interface `VideoIngress`) |
| Ultralytics YOLO (nano, export ONNX) | https://github.com/ultralytics/ultralytics | **AGPL-3.0** | Détection résidente VisionRT | Facilité export/track (BoT-SORT/ByteTrack) | AGPL : usage personnel OK, redistribution non ; alternative Apache-2.0 au même contrat : YOLOX ou RTMDet |
| MediaPipe Tasks Vision | https://ai.google.dev/edge/mediapipe | Apache-2.0 | Mains, gestes, pose, détection légère sur S25 | Officiel Google, modes live-stream avec timestamps | Ne jamais activer tous les détecteurs en parallèle (scheduler obligatoire) |
| PaddleOCR (export ONNX) | https://github.com/PaddlePaddle/PaddleOCR | Apache-2.0 | OCR ROI à la demande côté PC | Léger, multi-langue, convertible ONNX | Jamais plein écran en continu ; ROI uniquement |
| Skarian/one-xr | https://github.com/Skarian/one-xr | MIT | **Plan B du gate G1** : accès Android natif à l'IMU/pose et aux contrôles (luminosité, dimmer) des XREAL One/One Pro **sans le SDK Unity** | Seul repo public ciblant exactement notre matériel en Kotlin natif ; débloque un pipeline pose→WebRTC custom si le SDK Unity coince | Dernier commit 2026-02 (stable mais peu actif) ; IMU/pose seulement — la caméra Eye reste à valider séparément |
| Snap Spectacles — doc officielle SnapML | https://developers.snap.com/spectacles/about-spectacles-features/snapML | Propriétaire (plateforme) | Base de l'adaptateur Spectacles futur : SnapML exécute des modèles **ONNX/TFLite on-device**, Camera Module accessible, WebSocket/Fetch depuis une Lens (TypeScript) | Confirme que le client Spectacles peut parler nos contrats UIIntent/UIReceipt par WebSocket et faire de la détection locale | Runtime Lens Studio ≠ Unity : client entièrement distinct, seuls les contrats sont partagés |
| stspanho/spectacles-yolo-ml-example | https://github.com/stspanho/spectacles-yolo-ml-example | **Aucune licence déclarée** | Référence de pattern (ne pas copier le code) : YOLOv7-tiny → SnapML ONNX 224×224 sur Spectacles + pipeline d'entraînement | Exemple concret récent (2026-06) du portage détection on-device vers Spectacles | Sans LICENSE, s'inspirer du pattern uniquement, ne rien réutiliser tel quel |

Déjà en place (ne pas réinstaller) : Ollama ≥ 0.12.7 (`qwen3.5:9b`, `moondream`, `qwen3-vl:8b`), Qdrant v1.12.6 (Docker), torch 2.4.1+cu121, WhisperX 3.3.1 + pyannote 3.3.2 (token HF gated), faster-whisper, ffmpeg. À ajouter : `onnxruntime-gpu` (`.venv-live`), un modèle LLM live ~4B (ex. `qwen3:4b` q4) via Ollama, embeddings visuels légers (CLIP/DINOv2 small ONNX) pour la recherche visuelle et la reloc V19.B.

**Interdits au premier jalon** (guide §5.4, confirmé) : Neo4j/Graphiti/Mem0, Grounding DINO/SAM2/V-JEPA en boucle live, Redis/PostgreSQL/K8s, TensorRT (seulement si bench ONNX Runtime insuffisant, et alors dans un env isolé).

### 4.1 Budget VRAM RTX 3070 8 Go (à faire respecter par le GpuArbiter)

| Phase | Charges résidentes | Budget |
|---|---|---|
| Live jour (8h-22h) | decode + YOLO-nano FP16 (~0.3 Go) + faster-whisper small int8 (~1 Go) + OCR/embeddings à la demande (~0.7 Go) + LLM live 4B q4 (~2.5-3 Go, keep_alive court) | ≤ 5.5 Go, marge 2.5 Go |
| VLM ciblé (à la demande) | moondream ou crop qwen3-vl — **un job à la fois, préemptible**, LLM live déchargé si nécessaire | pic contrôlé |
| Nuit / post-stop | WhisperX large-v3 + qwen3-vl:8b + qwen3.5:9b **séquentiels**, live éteint | phases exclusives (mécanisme `runtime_v18_8` existant) |

Le GpuArbiter interroge `nvidia-smi`/NVML, applique les priorités du guide §10.4 et **vérifie** les `ollama_unload` (ne jamais faire confiance à un unload silencieux — cause de saturation identifiée à l'audit).

### 4.2 Chemin vers la version complète des scénarios (rien n'est plafonné par l'architecture)

Les versions « bornées » de certains scénarios au lancement sont des **configurations de départ, pas des plafonds**. Chaque limite a son déverrouillage, sans réécriture :

| Limite au lancement | Scénarios concernés | Déverrouillage | Coût du changement |
|---|---|---|---|
| Profondeur sémantique du VLM/LLM local (8 Go VRAM) | 3 (assistant), 5 (c'est quoi ça), 7 (aide tâche), 11 (Sherlock spontané) | Activer un provider cloud opt-in dans `user_profile.yaml` (crops/transcripts seulement, politique granulaire) **ou** GPU plus gros / modèle local plus grand plus tard | Un champ de config |
| Flèches spatiales limitées à la session | 8 (retrouver), 9 (navigation indoor), 10 (WorldBrain) | V19.B (relocalisation par keyframes/embeddings, prévue Lot 3) puis V19.C (backend SLAM isolé) — `SpatialMapProvider` déjà en place | Une implémentation de provider |
| Prédiction moyen/long terme faible au début | 1 (mémoire de vie) | Accumulation de mois de données + auto-vérification observationnelle + calibration : la qualité monte mécaniquement avec le temps | Aucun — c'est le temps qui paie |
| Sherlock/changements avec faux positifs | 10, 11 | Modèles de change detection plus fins + apprentissage des receipts (ce que tu ignores n'est plus montré) | Config + données |
| Aide tâche sans compréhension fine des gestes | 7 | Modèles action/main plus lourds côté UL0-LAN (le PC, pas le S25) quand le budget GPU le permet, ou provider cloud | Config |

Règle pour l'exécuteur : chaque fois qu'une limitation est codée, elle doit l'être **derrière l'interface provider correspondante avec le déverrouillage documenté** — jamais en dur dans la logique métier.

---

## 5. LOT 1 — Fondation V19

**Objectif** : monorepo, contrats, adaptateurs, simulateurs, transport WebRTC de bout en bout simulé, installateur + doctor V19, mode dégradé. À la fin du lot, un UIIntent produit par le PC s'affiche dans le companion-web à partir d'une vidéo rejouée — sans lunettes, sans S25, sans modèle lourd.

**Dossiers/fichiers principaux à créer**
- `packages/contracts/` : `schemas/*.schema.json` (les 8 contrats §3.4), `python/mlomega_contracts/models.py` (pydantic), `csharp/` (généré, ex. via NJsonSchema), `README.md` (règles d'évolution : additif uniquement, version `contracts_version` dans chaque message).
- `services/live-pc/` : `sessionhub.py` (sessions, ClockSync, auth par token de session éphémère — remplacer le token statique unique du bridge, DataChannel de contrôle), `gateway.py` (interface `VideoIngress` + impl `AiortcIngress`, queue vidéo = 1 frame, drop compté), `delivery_adapter.py` (consomme la queue `brainlive_intervention_delivery_queue` existante + push WebSocket/DataChannel vers renderers, reçoit les `UIReceipt` et les reboucle vers `v18_8_live_policy.record_delivery_feedback`), `gpu_arbiter.py` (NVML + vérification d'unload Ollama), `degraded.py` (états : PC absent, GPU saturé, réseau dégradé, batterie), `main.py` (FastAPI + workers asyncio).
- `apps/companion-web/` : page unique (HTML/JS ou Svelte léger) connectée au delivery_adapter en WebSocket ; affiche les UIIntent (cards, sous-titres, contours sur flux vidéo optionnel), renvoie des UIReceipt au clic. Sert de : simulateur lunettes, mode téléphone-only, outil de debug permanent.
- `simulators/` : `fake_xr_device.py` (rejoue un MP4 + trace de pose JSONL → WebRTC via aiortc client, réglable : fps, perte réseau, orientation 90° pour capture-only), `mock_llm.py` (impl `LLMProvider` à réponses fixes), `scenarios/` (2-3 vidéos courtes enregistrées au téléphone + poses synthétiques).
- `scripts/` : `INSTALL_MLOMEGA_V19_WINDOWS.ps1` (imite le style transactionnel V18.8 : préflight driver NVIDIA/Python 3.11/ffmpeg → **préserve `.venv` V18.8** → crée `.venv-live` → onnxruntime-gpu → `ollama pull` du modèle live 4B → MODEL_MANIFEST avec sha+licence → doctor final), `RUN_MLOMEGA_V19.ps1` (`-Xr`, `-SimOnly`), `STOP_`, `DOCTOR_MLOMEGA_V19.ps1` (`-Full -Xr -Vision -Delivery` : ports, GPU, Qdrant, Ollama, contrats, simulateur), `BENCH_V19.ps1` (ingress decode P50/P95, capture→UI ms).
- `.env.v19.example` ; `configs/profiles/` (rtx3070.yaml, degraded.yaml, sim.yaml) ; **assistant de première exécution** (`scripts/setup_profile.ps1`, appelé par RUN si `configs/user_profile.yaml` absent : lunettes ? lesquelles ? LLM local ou cloud ? lequel ? — écrit le profil §3.5, validé ensuite par doctor).
- Nettoyage ciblé (seul changement autorisé dans l'existant) : marquer `runtime_v18_7.py`/`operations_v18_7.py` comme gelés (commentaire d'en-tête + doc), corriger `docs/RUN_RTX3070.md` (bandeau « obsolète, voir V19 »), dédupliquer `docker-compose*.yml`. **Aucune modification de logique du cœur dans ce lot.**

**Données échangées** : `FrameEnvelope` (métadonnées via DataChannel, média via WebRTC), `UIIntent`/`UIReceipt` (DataChannel/WebSocket JSON), health/heartbeat SessionHub.

**Dépendances** : `.venv-live` = fastapi, uvicorn, aiortc, av, pydantic v2, websockets, pynvml, numpy, opencv-python-headless, pytest.

**Mocks/simulateurs** : fake_xr_device, mock_llm, companion-web (affichage).

**Commandes de validation**
```powershell
.\scripts\INSTALL_MLOMEGA_V19_WINDOWS.ps1 -PersonId me
.\scripts\DOCTOR_MLOMEGA_V19.ps1 -Full
.\scripts\RUN_MLOMEGA_V19.ps1 -PersonId me -SimOnly   # fake device + companion-web
.venv-live\Scripts\python -m pytest tests/v19 -m "contracts or transport"
.\scripts\BENCH_V19.ps1 -Ingress
```

**Tests réellement utiles** : round-trip de chaque contrat (python→JSON→C# stub→python) ; `webrtc_frame_queue_bounded` (la queue ne dépasse jamais 1, frames anciennes droppées) ; `ui_intent_ttl_expiry` ; `delivery_receipt_feedback_persisted` (un UIReceipt du companion-web finit dans les tables feedback V18.8) ; `degraded_pc_absent` (fake device continue, resync à la reconnexion) ; `install_preserves_venv_v18` ; non-régression : la suite `tests/test_v18_*` existante passe inchangée.

**Critères de fin** : vidéo rejouée → frame décodée au PC avec `frame_id`/pose corrects → UIIntent de test affiché dans companion-web → receipt persisté dans SQLite V18.8 ; doctor vert ; bench ingress documenté (si P95 decode > 33 ms à 720p30, ouvrir la piste GStreamer avant Lot 3).

**Risques** : décodage aiortc CPU (mitigé : interface + bench + plan B) ; conflits de ports avec bridge 8766 (choisir 87xx distincts) ; Windows/firewall (reprendre le scoping Private/Domain de l'installateur V18.8).

---

## 6. LOT 2 — Mémoire profonde V19

**Objectif** : brancher la vision sur la mémoire prouvée, rendre le Life Model réellement prédictif (auto-vérification observationnelle + calibration + schéma de soi), consolider la nuit. À la fin du lot, un événement visuel simulé devient un souvenir prouvé, corrigible, interrogeable, et le système émet des prédictions qu'il **vérifie lui-même** contre ce qu'il observe ensuite — sans solliciter l'utilisateur.

**Dossiers/fichiers principaux**
- Nouveaux endpoints dans `src/mlomega_audio_elite/api.py` : `/ingest/visual-event`, `/ingest/scene-summary`, `/memory/correction-visual`, `/xr/session-health`, `/evidence/request-clip`.
- Migrations dans `db.py` (additives, aucune rupture de `vision_frames`/`raw_assets`) : `visual_evidence_assets_v19`, `visual_events_v19`, `world_entity_links_v19`, `scene_session_summaries_v19`, `ui_interaction_outcomes_v19`, `brain2_spatial_routine_models`, `brain2_visual_task_models`, `brain2_ui_preference_models`, `prediction_outcomes_v19` (résolution observationnelle) et `self_schema_v19` (voir plus bas).
- `services/live-pc/memory_bridge.py` : sélection d'evidence (déclencheurs du guide §8.3 : commande explicite, changement fiable, objet personnel, intervention affichée), hash sha256, appel des endpoints ; **jamais** d'archivage vidéo par défaut.
- `services/live-pc/evidence_store.py` : clips/keyframes + retention policy visible + delete réel. Ajouter un **tampon-jour optionnel** : enregistrement continu basse résolution/bitrate côté PC (~6-12 Go/jour, quota disque surveillé par doctor), purgé à la consolidation nocturne après extraction des clips de preuve — c'est ce qui rend le replay « rejoue 14h30 » réel pour la journée en cours sans archiver toute la vie en vidéo.
- Adaptateur `src/mlomega_audio_elite/v19_visual_context.py` (nouveau fichier) : injecte `visual_context`/`world_state` de WorldBrain dans `v18_context.build_active_context` avec source refs et `as_of` strict — sans modifier la logique existante du module.
- Extension `v18_hot_capsule.py` → projection `HotSceneContext` (champ additif, budget dur conservé).
- **Boucle prédictive auto-vérifiée** (la correction la plus importante du projet). Principe : le système entend et voit tout ce que fait l'utilisateur — il vérifie donc **lui-même** ses prédictions contre les observations suivantes. Le feedback explicite de l'utilisateur est un canal de **correction rare** (CorrectionChip / une phrase), jamais une obligation ni un carburant.
  - `v19_prediction_loop.py` : chaque close-day, le Life Model émet des prédictions **concrètes, datées et observables** (JSON contraint : horizon, confiance, evidence_refs, et surtout `verification_spec` — le critère machine-vérifiable : entité/personne/lieu/action attendus + fenêtre temporelle + sources d'observation qui permettraient de trancher). Une prédiction sans `verification_spec` exploitable est pénalisée à l'émission : le système doit préférer les prédictions qu'il saura vérifier seul.
  - `v19_outcome_watcher.py` : à chaque close-day, confronte les prédictions ouvertes aux preuves captées depuis (transcripts, visual_events, GPS/lieux, routines, interactions) et les résout en `verified / refuted / expired / unverifiable`, avec evidence_refs de la résolution. **S'appuie sur le pont existant `auto_verification_v14_4.py`** (latent outcome → prediction verification, déjà dans le dépôt) étendu aux événements visuels V19. Ces labels automatiques alimentent la calibration `v18_predictive_retrieval` existante ; un échantillon aléatoire de résolutions est journalisé pour audit (contrôle du bruit d'auto-labellisation).
  - **Schéma de soi** (`self_schema_v19` + `v19_self_schema.py`) : projection nocturne, centrée personne, du « schéma complet de William » — préférences (aime/n'aime pas), désirs (veut/voudrait faire), historique (a fait/a connu/a obtenu), liens causaux avec preuve (« a obtenu X grâce à Y », en réutilisant la table `causal_edges` existante), et **patterns conditionnels** (« quand il fait Y dans le contexte C → Z suit », avec taux d'occurrence observé et evidence_refs), reliés entre aspects de vie (relations, routines, émotions, projets, langage). Reconstruit chaque nuit depuis les patterns confirmés + cas longitudinaux + résolutions de prédictions ; interrogeable par API et projeté (compact) dans le hot capsule. C'est ce schéma qui nourrit les prédictions — les schémas se répètent, le système exploite cette répétition.
  - **Life Model V19 = structure durable, pas une sortie de prompt** (exigence structurelle, non négociable). Le Life Model actuel est recompilé à chaque cycle par un unique prompt géant — c'est du texte régénéré, pas un modèle de vie. Le remplacer par `life_model_v19` : un magasin persistant et versionné d'entrées typées, organisé en **dimensions** (identité/valeurs, émotions, envies/objectifs, relations, routines, projets, santé/énergie, langage personnel) × **axes temporels** (passé consolidé court/moyen/long terme, présent, futur court/moyen/long terme). Chaque entrée porte : énoncé, dimension, portée temporelle, confiance, evidence_refs, `first_observed`, `last_confirmed`, statut (`active/weakening/contradicted/superseded`) et historique de révision. La mise à jour nocturne est **incrémentale** : le LLM ne reçoit que les faits nouveaux du jour + les entrées existantes concernées, et émet des **deltas** contractés (ajouter/confirmer/affaiblir/contredire/remplacer) — jamais une régénération complète. Une entrée jamais re-confirmée s'affaiblit avec le temps au lieu de rester vraie pour toujours. Le `self_schema_v19` ci-dessus est la projection interrogeable de ce magasin ; les prédictions et le hot capsule **lisent la structure**, pas la sortie d'un prompt.
  - Découpage des prompts en 3 étapes contractées — (1) extraction factuelle stricte (aucune inférence autorisée), (2) hypothèses/deltas avec evidence_refs obligatoires, (3) prédictions passées au filtre de calibration (abstention si non calibré). Réutiliser `llm_contracts_v15_18` tel quel.
  - Similarité de cas : conserver le Jaccard comme baseline mais journaliser les 6 sous-scores par cas ; dès ≥200 cas **auto-labellisés** par l'outcome watcher, ajuster les poids par régression logistique (scikit-learn, offline, nuit).
- Consolidation nocturne : étendre `v18_close_day.py` d'une phase `visual_consolidation` (deep vision sur keyframes sélectionnées via `brainlive_offline_deep_vision_v16_1` — réutilisé, pas réécrit) ordonnancée par le GpuArbiter après le deep audio.
- Replay : `v18_replay.py` étendu pour servir clip + audio + events par plage horaire (API consommée par companion-web puis XR).

**Données échangées** : `EvidenceEvent` (schéma canonique §3.4/guide 11.3), `SceneSessionSummary`, `UIReceipt` → `ui_interaction_outcomes_v19`, prédictions/labels JSON.

**Dépendances** : scikit-learn (nuit uniquement), rien d'autre de nouveau côté cœur.

**Mocks** : générateur d'événements visuels synthétiques (`simulators/synthetic_life.py` : 30 jours de vie simulée — routines, objets déplacés, rencontres — pour tester consolidation et prédiction sans attendre 30 jours réels).

**Commandes de validation**
```powershell
.venv\Scripts\python -m pytest tests/v19 -m memory
.venv\Scripts\mlomega-audio v19-visual-ingest-smoke --sim
.venv\Scripts\mlomega-audio v19-close-day --person-id me --dry-run
.\scripts\DOCTOR_MLOMEGA_V19.ps1 -Memory
```

**Tests réellement utiles** : `visual_event_evidence_integrity` (chaque event pointe une preuve hashée existante) ; `visual_correction_rebuilds_projection` (« ce n'est pas mon téléphone » → label suspendu + projections recalculées) ; `replay_clip_with_audio` ; `prediction_auto_verified_by_observation` (vie synthétique : prédiction « café demain matin » → événement GPS/visuel simulé le lendemain → résolution `verified` **sans aucune entrée utilisateur** → bins de calibration recalculés) ; `refuted_prediction_lowers_confidence` ; `unverifiable_prediction_penalized_at_emission` ; `self_schema_conditional_pattern_has_evidence` (chaque « quand Y → Z » du schéma de soi porte taux d'occurrence + evidence_refs) ; `life_model_three_stage_no_uncited_claims` (étape 2 rejetée si claim sans evidence_ref) ; `life_model_update_is_incremental` (deux nuits successives : la deuxième n'émet que des deltas, les entrées non concernées gardent leur historique intact) ; `life_model_entry_weakens_without_confirmation` (une entrée jamais re-confirmée passe en `weakening`, jamais supprimée silencieusement) ; `nightly_gpu_phases_exclusive` (jamais deux modèles lourds simultanés) ; 30 jours synthétiques → au moins une routine spatiale détectée avec preuves.

**Critères de fin** : chaîne complète sim → EvidenceEvent → souvenir corrigible → hot capsule enrichi ; prédictions émises puis **auto-résolues par observation** au close-day suivant, calibration alimentée sans intervention utilisateur ; schéma de soi générable et interrogeable (préférences, causaux, patterns conditionnels, tous avec preuves) ; close-day nocturne complet < 6h sur RTX 3070 avec journal des phases GPU ; tests V18 existants toujours verts.

**Risques** : bruit d'auto-labellisation (une résolution `verified` par correspondance trop laxiste empoisonne la calibration — mitigé : seuils de correspondance stricts, classe `unverifiable` assumée, échantillon d'audit journalisé) ; prompts 4B trop faibles pour l'étape hypothèses (mitigé : cette étape peut tourner la nuit sur le 9b) ; croissance disque des clips (retention policy + quota dans doctor).

---

## 7. LOT 3 — Live / XR / Mobile

**Objectif** : l'app Unity XREAL + le runtime S25, les calculateurs Ultra-Live, VisionRT/WorldBrain sur PC, la traduction live, les gestes, le wake word, l'UI liquid glass, le mode capture-only, et les scénarios de bout en bout. Développement contre simulateurs d'abord ; le vrai matériel valide par gates (G1→G8 du guide).

**Dossiers/fichiers principaux**
- `apps/xr-mobile/` (Unity 6 LTS, URP, XR Plugin Management, Input System, TextMeshPro) :
  - `Scripts/Core/` : `XrSessionController`, `EyeCaptureSource` (YUV→texture, `frame_id`+monotonic), `PosePublisher`, `ClockSync`.
  - `Scripts/Scene/` : `LocalTrackStore` (optical flow léger, association frame→track), `SceneCache` (sous-caches tracks/entities_hot/spatial_hot/task_hot/translation_hot/ui_state avec TTL du guide §9.1, persistance chiffrée).
  - `Scripts/Reflex/` : `ReflexScheduler` (activation par signal, jamais de « modes »), `UltraLiveSkillBank` : `StableTrackSkill`, `LensWindowSkill` (zoom gestuel « attraper le réel »), `SubtitleSkill`, `HandActionSkill`, `MotionProximitySkill`, `FocusSearchSkill`.
  - `Scripts/UI/` : `UIIntentBroker` (priorités §13.2 : privacy > UL critique > sous-titres > VisionRT demandé > tâche > BrainLive > décoratif ; TTL, densité), `UIRuntime` + design system liquid glass : `ObjectOutline`, `PersonTag`, `Subtitle`, `LensWindow`, `OffscreenArrow`, `ContextCard`, `TaskCard`, `VirtualScreen`, `CorrectionChip`, `StatusBar` (caméra/micro/PC/privacy toujours visibles). Style : panneaux translucides flous, bords lumineux doux, animations courtes ; chaque composant émet ses `UIReceipt`.
  - Plugin Kotlin `android/` : `LiveTransportPlugin` (GetStream webrtc-android : H.264 + Opus 20 ms + DataChannel fiable ; reconnexion, backpressure, bitrate adaptatif), `AsrKwsService` (sherpa-onnx : VAD + streaming zipformer FR/EN + keyword spotting, nom d'éveil configurable), `GesturePipeline` (MediaPipe Hands + Gesture Recognizer : pincer=zoom, paume=menu, balayage=cacher l'UI — mappage dans `configs/gestures.yaml`), `EvidenceRingBuffer` (vidéo/audio N minutes chiffrées, sélection de clips), `OrientationGuard` (capture-only : gravité IMU → rotation 90/180° des frames avant encodage, badge « capture-only » dans StatusBar).
  - `XRDeviceAdapter` : interface C# ; impls `XrealDeviceAdapter`, `SimulatedDeviceAdapter` (webcam/vidéo, éditeur Unity) et `PhoneOnlyAdapter` (caméra + écran du téléphone : mêmes SceneCache/UIIntentBroker/skills, rendu 2D plein écran au lieu de stéréo — livré dans ce lot comme cible de premier rang, utilisable sans lunettes). Toute la logique au-dessus est identique.
- `services/live-pc/` (suite) : `visionrt.py` (détection résidente + BoT-SORT/ByteTrack + OCR ROI + retrieval visuel + VLM ciblé un job à la fois, sortie `SceneDelta` liée à `source_frame_id`), `audiort.py` (VAD + faster-whisper streaming + LID + traduction — pipeline sans LLM conversationnel ; sous-titres partiels puis finaux), `worldbrain.py` (WorldEntity/Observation/Relation/SceneSession/ChangeEvent/last-seen/map_quality, guide §10.2), `spatial.py` (impl `SpatialMapProvider` V19.A : pose+keyframes+bearings ; V19.B reloc par embeddings si qualité mesurée le permet), `brainlive_scene_adapter.py` (WorldBrain+AudioRT → HotSceneContext → politique BrainLive existante show/prepare/ask/defer/silence), `ultralive_lan.py` (traduction streaming, HandAction lourd, ChangeAttention).
- Scénarios (guide §14, tous testables via simulateur + companion-web avant lunettes) : personne connue (PersonTag + ContextCard relationnelle), personne pas encore identifiée (track + sous-titres, promue en entité dès qu'identifiée), « c'est quoi ça ? », traduction live, zoom/OCR, objet perdu/sortie (visible→contour ; carte fiable→flèche ; sinon last-seen), aide tâche (TaskCard une étape), Sherlock (observé vs hypothèse), VirtualScreen, réflexe proximité/conduite (informatif seulement), replay, capture-only.

**Données échangées** : tout passe par les contrats du Lot 1 ; média par WebRTC ; `SceneDelta`/`UIIntent`/`UIReceipt`/`ReflexEvent` par DataChannel ; EvidenceEvents par memory_bridge (Lot 2).

**Dépendances** : Unity 6 LTS + XREAL SDK 3.1.0 ; Kotlin : getstream webrtc-android, sherpa-onnx (JNI), mediapipe tasks-vision ; PC : déjà en place (Lot 1) + modèles ONNX du MODEL_MANIFEST.

**Mocks/simulateurs** : `SimulatedDeviceAdapter` (Unity éditeur), `fake_xr_device` (Lot 1) pour le PC, companion-web comme renderer de secours, jeux de vidéos scénarisées (`simulators/scenarios/` : table avec objets, conversation deux personnes, panneau en langue étrangère, pièce avant/après changement).

**Commandes de validation**
```powershell
.\scripts\RUN_MLOMEGA_V19.ps1 -PersonId me -SimOnly      # scénarios simulés complets
.\scripts\RUN_MLOMEGA_V19.ps1 -PersonId me -Xr           # matériel réel
.\scripts\DOCTOR_MLOMEGA_V19.ps1 -Full -Xr -Vision -World -Delivery
.\scripts\BENCH_V19.ps1 -Full   # capture→render, capture→pc, command→pixel, vision_infer, P50/P95
.venv-live\Scripts\python -m pytest tests/v19 -m "live or scenarios"
```

**Tests réellement utiles** (sous-ensemble prioritaire de la liste §16.1 du guide) : `xr_eye_pose_frame_alignment` (G1, matériel réel) ; `ui_intent_track_attachment` (un résultat PC tardif se recolle au track vivant ou s'abstient) ; `ultralive_zoom_without_pc` et `ultralive_subtitle_without_brainlive` (coupure PC → réflexes intacts) ; `visionrt_object_query_targeted` ; `find_phone_visible` / `find_phone_last_seen` / `exit_unknown_no_false_arrow` ; `known_person_identity_confidence` (pas de nom sous seuil de confiance — justesse) ; `network_loss_degraded` ; `pc_vram_overload_defers_deep_work` ; `capture_only_reorients_frames` ; `ui_density_limit_privacy_pause` ; test de vérité visible (guide §16.2) : filmer monde + lunettes + horloge, corréler timestamps, publier P50/P95 par trajet dans `docs/BENCH_RESULTS.md`.

**Critères de fin** : les 8 premiers gates du guide (G1→G8) passés sur matériel réel ; cue UL0-device < 100 ms ; sous-titre partiel < 1 s ; « c'est quoi ça ? » < 3 s ; session 3h sans fuite VRAM ni surchauffe S25 bloquante ; capture-only fonctionnel avec companion-web sur un autre téléphone ; StatusBar privacy permanente ; scénarios 1-16 démontrés (en simulé au minimum, en réel pour ceux couverts par G1-G8).

**Risques et limitations matérielles** : accès caméra Eye sur One Pro non garanti par la doc publique (**G1 en tout premier** ; si le SDK Unity coince : plan B pose/contrôles via `one-xr` (MIT, Kotlin natif) + caméra du S25 en attendant, même pipeline) ; thermique/batterie S25 sur 14h (mitigé : profils de capture, encodage adaptatif, capture-only moins coûteux, recharge périodique — mesurer, ne pas promettre) ; 8 Go VRAM = budget §4.1 strict, le GpuArbiter est le seul arbitre ; SLAM complet hors périmètre V19 (flèches uniquement si map_quality suffisante) ; latence Wi-Fi variable (Wi-Fi 6 + PC Ethernet requis, mode dégradé sinon).

---

## 8. Discipline d'exécution (anti-bricolage — lecture obligatoire)

### 8.1 Inventaire des points de jonction avec le code existant

Tout branchement V19 → cœur V18.8.1 passe **exactement** par ces symboles, vérifiés à l'audit. Règle : **lire le module existant en entier avant de s'y brancher** ; ne jamais renommer, ne jamais modifier une signature existante, ne jamais dupliquer une fonction existante « pour aller plus vite ».

| Nouveau composant | Se branche sur (symbole exact) | Interdit |
|---|---|---|
| `delivery_adapter.py` | `v18_delivery.enqueue_delivery` + queue `brainlive_intervention_delivery_queue` ; feedback via `v18_8_live_policy.record_delivery_feedback` | contourner la queue H1 ; créer une 2e queue |
| Contexte visuel | nouveau module `v19_visual_context.py` ; attention : `build_active_context` est **patché** par `v18_context.install(module)` (l'original vit dans `brainlive_v15`) — étendre via le mapping `_FIELD_TABLE` de `v18_context.py` et alimenter `brainlive_world_states`/`vision_scene_observations`, jamais toucher au wrapper | modifier la logique interne de `v18_context.py` ou re-patcher par-dessus |
| `HotSceneContext` | extension additive de `v18_hot_capsule.build_hot_capsule_payload` (budget dur et `omitted_refs` conservés) | créer un hot context parallèle |
| `v19_outcome_watcher.py` | étend `auto_verification_v14_4.auto_verify_latent_outcome_predictions` (+ `ensure_v14_4_schema`) aux événements visuels | réécrire la vérification from scratch |
| Calibration | `v18_predictive_retrieval` (bins/abstention existants) | inventer un 2e mécanisme de score |
| Deep vision nocturne | `brainlive_offline_deep_vision_v16_1` réutilisé sur les keyframes sélectionnées | le mettre dans une boucle live |
| Phases nocturnes | `v18_close_day` (phases additives) + `runtime_v18_8.release_live_model_caches` + vérification `ollama_unload` | lancer un modèle lourd sans passer par les phases |
| Migrations | `db.py` : `CREATE TABLE IF NOT EXISTS` additifs, FK vers tables existantes par nom | toute modification de `vision_frames`, `raw_assets`, ou d'une colonne existante |
| Preuves | liste blanche `v18_life_model._DIRECT_EVIDENCE_SOURCES` (y ajouter les tables v19, ne rien retirer) | référencer une preuve hors liste blanche |

### 8.2 Règles de travail

- Ordre strict : Lot 1 → Lot 2 → Lot 3 (le gate G1 matériel peut courir en parallèle du Lot 2).
- **Fin de chaque lot = checkpoint bloquant** : doctor vert + suite de tests du lot + suite `tests/test_v18_*` inchangée et verte + revue (humaine ou seconde passe d'agent) avant d'ouvrir le lot suivant. Ne jamais enchaîner les trois lots en aveugle.
- Toute évolution de contrat est additive avec bump de `contracts_version` ; tout nouveau module a un smoke test et une sonde doctor.
- Ambiguïté réelle (deux implémentations défendables) → écrire un ADR court dans `docs/DECISIONS.md`, choisir l'option **réversible**, continuer. Aucun raccourci ou « TODO provisoire » silencieux : tout compromis doit apparaître dans DECISIONS.md.
- Ne jamais court-circuiter : frame → observation → preuve → événement → mémoire. Jamais de frame directement en mémoire, jamais de coordonnées pixel du PC vers les lunettes. Politique vidéo = §3.6, cadences en config, jamais en dur.
- Chaque décision d'affichage passe par UIIntentBroker (priorités, TTL, densité) et laisse un UIReceipt.
- Cloud : uniquement via les interfaces provider, opt-in explicite, indicateur UI, données séparées.
- En cas de doute sur une API matérielle : consulter uniquement les sources du §4, maximum trois par problème, et consigner la décision dans `docs/DECISIONS.md`.
