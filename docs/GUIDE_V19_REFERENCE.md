# GUIDE_V19_REFERENCE — sections normatives extraites du guide maître

Extraction fidèle (2026-07-03) des sections du `Guide_Maitre_MLOmega_V19_Transformation_XR_Live.docx` que `EXECUTOR_HANDOFF.md` et `EXECUTOR_BUILD_GUIDE.md` citent. **Toute référence « guide §x » dans ces deux documents résout ici.** Le docx original devient une lecture de fond optionnelle. Quand le handoff amende le guide (ex. deux profils LLM, plan B GStreamer), **le handoff prime**.

---

## §7.1 — Identifiants non interchangeables

| ID | Durée | Sens | Interdit |
|---|---|---|---|
| session_id | session lunettes | unité de corrélation XREAL/S25/PC/MLOmega | le réutiliser après redémarrage |
| frame_id | immuable | image/pose capturée à un instant monotone | le générer côté PC après décodage sans trace source |
| track_id | secondes/minutes | suivi local ou global d'une région visible | le traiter comme identité durable |
| entity_id | jours/mois | objet, lieu, personne, document, tâche durable | le créer à partir d'une seule bbox faible confiance |
| evidence_id | durable | frame/clip/audio/hash/modèle lié à un fait | le supprimer sans réviser les événements dérivés |
| delivery_id | durable | une intervention BrainLive et son feedback | l'utiliser pour un cue réflexe UL0 |
| ui_intent_id | TTL court | instruction sémantique de rendu | le conserver indéfiniment à l'écran |

## §8.3 — Sélection de preuve (déclencheurs)

| Déclencheur | Ce qui est conservé | Destination |
|---|---|---|
| Commande explicite « garde / rejoue / c'est quoi ? » | frame focus + clip pré/post-événement + audio associé | EvidenceStore puis MLOmega |
| Changement fiable | keyframe avant/après + relation Entity/Track + map quality | WorldBrain → EvidenceEvent → MLOmega |
| Objet personnel posé/trouvé | frame + clip court + relation last_seen | `visual_event` MLOmega + Brain2 si répétition |
| Intervention BrainLive affichée | UIIntent + receipt + contexte source (pas la vidéo entière) | v18_delivery / feedback Brain2 |
| Cue UL0 non critique | agrégat compteur/latence, pas de clip par défaut | observabilité / reflex audit |

## §8.4 — Modes dégradés

| Panne | Comportement XR | Comportement mémoire |
|---|---|---|
| PC/LAN indisponible | UI locale, SceneCache récent, tracks/zoom/UL0-device continuent ; badge « PC indisponible » | ring buffer local chiffré ; synchro evidence au retour seulement |
| Transport vidéo dégradé | baisser bitrate/résolution, garder DataChannel, abandonner frames anciennes | aucun backlog de fichiers live |
| GPU PC saturé | VisionRT prioritaire ; VLM/OCR secondaire suspendu ; UI expire proprement | WhisperX/deep vision/Brain2 reportés |
| Track perdu | le composant se fane puis disparaît | aucun entity swap ; WorldBrain garde le dernier-vu avec âge |

## §9.1 — SceneCache : sous-caches et TTL

| Sous-cache | Contenu | TTL / règle |
|---|---|---|
| tracks | bbox/masques, vélocités, visibilité, âge | très court ; disparaît quand l'ancre est perdue |
| entities_hot | nom/alias, type, dernier track/frame, confiance | réconcilié via SceneDelta ; pas de nom si confiance d'identité basse |
| spatial_hot | bearing/last_seen session, map quality | pas de flèche précise si qualité sous seuil |
| task_hot | but, étape, outils, preuve/plan actif | une tâche active à la fois en UI |
| translation_hot | speaker track, texte partiel/final, langue, âge | expire au changement de tour ou délai excessif |
| ui_state | intents visibles, suppression, densité, préférences | TTL obligatoire ; reset session possible |

## §9.2 — Banque Ultra-Live

| Skill | Entrées | Sortie UI | Où |
|---|---|---|---|
| StableTrackSkill | ROI, LocalTrack, pose/optical flow | contour/label accroché au présent | S25 |
| LensWindowSkill | centre de vue, zoom, crop stabilisé | fenêtre zoom/clarification texte ou détail | S25 ; OCR PC enrichit |
| SubtitleSkill | ASR streaming, langue, speaker track | sous-titre partiel/final, traduction live | S25 et/ou PC LAN, sans BrainLive |
| HandActionSkill | mains, objets, mouvement, task_hot | highlight outil/zone, prochaine interaction probable | S25 si léger, PC UL0-LAN si lourd |
| MotionProximitySkill | tracks, optical flow, IMU, croissance apparente | cue périphérique, direction, urgence | S25 |
| RoadWorldSkill | caméra fixe véhicule, IMU, vitesse, tracks | risque/trajectoire informative courte | S25 + PC selon capteurs |
| FocusSearchSkill | focus/rayon, query courte, SceneCache | prépare cible, demande VisionRT, spinner discret | S25 |
| ChangeAttentionSkill | keyframes, scene motion, map quality | point d'intérêt sobre ou EvidenceRequest | PC UL0-LAN |

## §9.3 — Scheduler Ultra-Live (activation par signal, pas de « modes »)

```
centre de vue sur texte        → LensWindow + OCR ROI
main + objet proche            → HandAction + StableTrack
conversation multi-langue      → SubtitleSkill + speaker direction
mouvement rapide / proximité   → MotionProximity
caméra véhicule active         → RoadWorld
commande « où est … »          → FocusSearch + SceneCache → VisionRT si miss
changement de zone             → keyframe / WorldBrain change candidate
```

## §10.2 — Objets WorldBrain (champs minimum)

| Objet | Champs minimum | Utilité |
|---|---|---|
| WorldEntity | entity_id, kind, label, confidence, lifecycle | téléphone, porte, table, personne, document, tâche |
| Observation | observation_id, entity_id?, frame_id, track_id?, state, model, confidence, evidence | ce qui est réellement vu, daté, corrigeable |
| Relation | subject, predicate, object, observed_at, confidence, evidence | sur_table, près_de, utilise, bloque, pointe_vers |
| SceneSession | session_id, place_hint, map_quality, active_zone, keyframes | contexte spatial d'une visite/tâche |
| ChangeEvent | appeared/disappeared/moved/opened/changed, before/after evidence | Sherlock, colis, chargeur, pièce modifiée |
| SpatialAnchorCandidate | pose/frame/keypoints/bearing, covariance/quality | flèche seulement si qualité suffisante |

Niveaux : Frame (ms) → Active Scene (secondes/minutes) → Session Map (visite/tâche) → World Memory projection (jours/mois via MLOmega).

## §10.3 — Spatial : ordre réaliste

- **V19.A** : pose XREAL + tracks + relations écran + keyframes + directions de session (suffisant pour contours, last-seen, flèches prudentes).
- **V19.B** : relocalisation légère par keyframes/embeddings/repères ; qualité estimée, flèche refusée si insuffisante.
- **V19.C** : backend SLAM/VIO isolé (ORB-SLAM3 = GPLv3, isoler/WSL2).
- **V19.D** : persistance multi-session seulement après métriques de dérive/calibration/correction.

## §12.4 — UI par situation (BrainLive)

| Situation | Entrées contextuelles | UI proposée |
|---|---|---|
| Personne connue | identité multi-indice, relation pack, sujet, engagement | ContextCard latérale 2-3 lignes ; jamais sur le visage |
| Tension/sujet sensible | conversation, evidence mémoire | rappel sobre (« reste factuel », question ouverte) ; rien si confiance faible |
| Tâche pratique | plan, outils, étape, prior task events, focus | TaskCard une action + highlight VisionRT/UL0 |
| Objet perdu | last_seen, place, map quality, entity state | contour/flèche/last-seen selon niveau de vérité |
| Ennui/réunion | demande explicite | VirtualScreen/notes explicitement activé ; aucun faux signal « urgence » |

## §13.1 — Design system (composants)

| Composant | Usage | Ancre | Source typique |
|---|---|---|---|
| ObjectOutline | objet, outil, porte, zone | screen_track | UltraLive / VisionRT |
| PersonTag | personne connue | face_track offset | VisionRT + BrainLive |
| Subtitle | ASR et traduction | bas de vision / speaker offset | UltraLive |
| LensWindow | zoom/OCR/inspection | panel temporaire focus | UltraLive + VisionRT |
| OffscreenArrow | objet/lieu hors champ | head-edge | WorldBrain |
| ContextCard | souvenir/règle/relation | panel latéral | BrainLive |
| TaskCard | prochaine action/outil/étape | panel latéral | BrainLive + VisionRT |
| VirtualScreen | TV, replay, notes, écran travail | surface explicite | demande utilisateur |
| CorrectionChip | corriger entity/mémoire/UI | panel/voix | toutes sources |
| StatusBar | caméra, micro, réseau, PC, privacy, mode | head-locked discret | S25 |

## §13.2 — Priorité de rendu (UIIntentBroker, après opt-out utilisateur)

1. status/privacy/pause → 2. UltraLive critique ou focus explicite → 3. sous-titre/traduction active → 4. résultat VisionRT demandé → 5. tâche actuelle → 6. BrainLive contextualisé → 7. Free Guy/décoratif.
Un intent qui perd son track, son TTL ou sa confiance disparaît. Le renderer ne laisse jamais une boîte vieille collée à un autre objet.

## §13.3 — Sémantique des receipts

| Événement | Source | Effet mémoire |
|---|---|---|
| displayed | UIRuntime | prouve que le message a atteint les lunettes |
| seen | regard/timer/interaction prudente | exposition, pas compréhension |
| acted | voix/geste/confirmation explicite | peut devenir signal outcome, jamais causalité automatique |
| dismissed/ignored | UIRuntime | réduit répétition, nourrit préférence UI |
| corrected | commande utilisateur | suspend/rectifie et déclenche révision |

## §14 — Chaînes d'exécution des scénarios

**14.1 Personne connue** : `track visage/voix → confirmation multi-frames/multi-indice → WorldBrain person entity → HotSceneContext + Brain2 relation pack → BrainLive policy → ContextCard courte → UIReceipt → Brain2 feedback`. Règle : identité haute confiance requise pour afficher un nom (justesse) ; conseil relationnel = hypothèse sourcée.

**14.2 Personne pas encore identifiée** : `person track → SubtitleSkill / AudioRT → WorldBrain person entity (provisoire) → promue en entité durable dès identification`. Mémoire identitaire autorisée ; pas de lookup Internet (composant absent de l'archi locale, activable via provider si souhaité).

**14.3 « C'est quoi ça ? »** : `commande/focus → S25 crop stabilisé → VisionRT detector/OCR/retrieval → VLM local ciblé seulement si insuffisant → SceneDelta → label/mini-card attaché au track → evidence sur demande`. Réponse classée observed/recognized/probable.

**14.4 Traduction live** : `audio → VAD → ASR streaming + LID → traduction streaming → SubtitleSkill UIIntent partiel/final → speaker association si stable → AudioRT/BrainLive reçoit le transcript en parallèle`. BrainLive ne ralentit jamais les sous-titres.

**14.5 Zoom/lecture** : `focus centre → LensWindow crop + stabilisation locale → OCR ROI PC → texte/traduction → UIIntent → utilisateur cache/épingle/corrige`. Ne jamais recréer des détails absents de l'image.

**14.6 Objet perdu / sortie / Maps** : `LOCATE(target) → SceneCache local-first → visible ? outline : WorldBrain last_seen/map_quality → flèche seulement si qualifiée : sinon last-seen card : sinon recherche VisionRT`. Jamais de flèche sans qualité de carte.

**14.7 Aide tâche** : `HELP(focus, goal) → TaskThread → plan/OCR + objets VisionRT + relations WorldBrain + historique BrainLive → TaskCard une étape → HandAction/Outline → receipt/correction`. Distinguer manuel, observation, hypothèse.

**14.8 Sherlock** : `keyframe relocalisée → ChangeWorker/VisionRT → observed change or visual trace → WorldBrain ChangeEvent → BrainLive seulement si pertinent → UI candidate / evidence replay`. « Trace sur la main » = observable ; « il a mangé le Nutella » = hypothèse, jamais un fait.

**14.9 Écran virtuel** : `commande utilisateur → VirtualScreen intent local → player local ou stream PC → surface XR ; StatusBar garde contrôles micro/caméra`. Rien archivé sauf choix explicite.

**14.10 Réflexe conduite/proximité** : `sensors → Motion/Road skill → trajectory/proximity/free-space → UIIntent haute priorité → cue périphérique / direction / gel de l'UI moins importante → ReflexEvent agrégé → evidence seulement si significatif`. Information, jamais « tu peux y aller ».

## §15.2 — Ordre de démarrage d'une session

1. PC : MLOmega/BrainLive existant, Qdrant, Ollama, puis services live ; vérifier VRAM, disque EvidenceStore, health.
2. S25 : app XR, réseau LAN, consentements/SceneCache autorisés.
3. Brancher XREAL/Eye : display, pose, Eye RGB, micro, batterie, indicateur caméra/micro.
4. `session_id` + ClockSync + WebRTC + DataChannel ; vérifier frame_id à l'arrivée PC.
5. Ultra-Live local immédiat ; VisionRT/WorldBrain/BrainLive se branchent sans bloquer le renderer.
6. StatusBar : caméra, micro, PC, LAN, ring buffer, mode UI, privacy pause.
7. Fin : flush EvidenceEvents choisis, stop explicite, close-day/post-stop selon policies, purge seulement après gates positives.

## §15.3 — Métriques d'observabilité

`capture_to_render_ms` · `capture_to_pc_ms` · `pc_to_ui_ms` · `vision_infer_ms` / `ocr_ms` / `vlm_queue_depth` · `command_to_pixel_ms` · `track_age_ms` / `track_switches` · `map_quality` / `relocalization_success` · `scene_cache_hit_rate` · `ui_intent_drop_reason` + receipt outcome · `gpu_vram_mb` / `dropped_frames`.

## §16 — Gates de construction

| Gate | Livrable | Test de sortie |
|---|---|---|
| G0 | socle figé : sauvegarde V18.8 + migration test | tests V18 verts, aucune table cassée |
| G1 | app Unity minimale XREAL : status, Eye frame, pose, stéréo | Eye/pose/rendu/batterie vérifiés sur S25 réel |
| G2 | FrameEnvelope, session clock, DataChannel, WebRTC, queue=1 | frame_id/pose reçus au PC + UIIntent test sur bon track |
| G3 | SceneCache, LocalTrack, UIIntentBroker, ObjectOutline simulé | le contour suit même PC coupé/retardé |
| G4 | Ultra-Live : LensWindow, tracks, cue mouvement, audio command, Subtitle | zoom/track/subtitle sans BrainLive ni VLM |
| G5 | VisionRT : detector/tracker, OCR ROI, « c'est quoi ? », SceneDelta | un résultat tardif se recolle au track ou s'abstient |
| G6 | WorldBrain : entities/observations/last-seen/session map/changes | find_phone_visible / find_phone_last_seen / exit_unknown |
| G7 | MLOmega visual bridge : EvidenceEvents, tables V19, correction, replay | un objet posé devient événement prouvé et corrigible |
| G8 | BrainLive XR : HotSceneContext, delivery adapter, receipts, ContextCard | intervention sourcée affichée + feedback + visible dans Brain2 |
| G9 | tâches + Free Guy : TaskThread, attention scheduler, densité | aide plan/outil sans surcharge de labels |
| G10 | spatial durable : reloc/SLAM expérimental + quality gate | flèche seulement quand les métriques de carte passent |
| G11 | skills avancées : Change/Sherlock, NextAction, branche route | métriques, failsafe et contrat UI avant activation |

## §16.1 — Liste complète des tests de non-régression

`xr_eye_pose_frame_alignment` · `webrtc_frame_queue_bounded` · `ui_intent_track_attachment` · `ultralive_zoom_without_pc` · `ultralive_subtitle_without_brainlive` · `visionrt_object_query_targeted` · `brainlive_hot_scene_context_manifest` · `brainlive_xr_delivery_receipt` · `visual_event_evidence_integrity` · `visual_correction_rebuilds_projection` · `find_phone_visible` · `find_phone_last_seen` · `exit_warm_offscreen` · `exit_unknown_no_false_arrow` · `known_person_identity_confidence` · `task_with_plan_and_tool` · `replay_clip_with_audio` · `network_loss_degraded` · `pc_vram_overload_defers_deep_work` · `street_informational_only` · `ui_density_limit_privacy_pause`

## §16.2 — Test de vérité visible

Filmer simultanément le monde, le mouvement de tête, le téléphone/lunettes et une horloge de référence. Corréler les timestamps device/PC, calculer P50/P95 par trajet. « Ça semble fluide » ne suffit pas : chaque UIIntent doit être relié à sa frame source, son track actuel, son âge et son résultat de rendu.

## §17.1 — Personnes (amendé par le handoff : pas de gating de consentement — outil personnel non diffusé)

Le système identifie et profile librement les personnes de la vie de l'utilisateur ; il n'y a pas de liste d'opt-in ni d'enrôlement obligatoire. Les règles restantes ci-dessous sont conservées **pour la justesse et le confort**, pas pour la permission :

| Règle | Implémentation |
|---|---|
| Reconnaissance progressive | une personne pas encore identifiée est un track ; dès qu'assez d'indices (visage + voix + contexte) convergent, elle devient une entité durable enrichie de sa relation |
| Pas de nom incertain | tant que la confiance d'identité est basse, aucun nom affiché (règle d'exactitude, pas de permission) |
| Correction simple | une phrase/geste suspend un label et crée une correction durable |
| Pas de recherche Internet d'identité | non par éthique mais parce que l'archi est locale et n'a pas ce composant ; activable un jour via un provider si souhaité |
| Embeddings chiffrés sur PC | protège tes propres données personnelles |

## §17.2 — Vérité UI (affichage par niveau)

| Niveau | Affichage |
|---|---|
| Observed + preuve fraîche | label/contour normal |
| Probable | texte discret « probable » ou demande de confirmation |
| Remembered / last seen | âge et lieu/condition visibles |
| Inferred | étiquette hypothèse, jamais présentée comme observation |
| Map quality faible | pas de flèche spatiale précise |
| Faible confiance identité | aucun nom / person tag |

## §17.3 — Sécurité

Route : UI informative seulement, jamais « tu peux traverser/tourner/c'est sûr ». Danger/proximité : attirer l'attention, montrer direction/sortie/espace libre ; aucune instruction de violence. Social : une lecture relationnelle reste une hypothèse sourcée, étiquetée comme telle (règle de justesse, pas de permission). Cognitif : densité réductible, Free Guy coupable, pause, session effaçable.
