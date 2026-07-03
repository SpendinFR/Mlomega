# DECISIONS

- 2026-07-03: Lot 1 implements the V19 transport seam with a simulator-first `VideoIngress`. Real XREAL/S25 hardware gates remain blocked in this container and must be validated on device before marking Lot 3 hardware steps complete.
- 2026-07-03: E10 is not marked complete in this Linux container because the exact PowerShell command `scripts/RUN_MLOMEGA_V19.ps1 -SimOnly` cannot be executed (`pwsh`/`powershell` is absent). The underlying SimOnly path it wraps was validated with `python scripts/simonly_demo_v19.py`: fake device → BrainLive UIIntent → companion-web simulator receipt → `brainlive_intervention_feedback_events_v188`. V19 contracts/transport tests, the V18 baseline tests actually present under `MLOmega_V18_8_1_Evidence_Connected/tests`, and the simulator ingress bench were also validated. No hardware benchmark is claimed.
- 2026-07-03: The V18 deep-audio baseline now has a narrow WAV-only stitching fallback when `ffmpeg` is absent. It is limited to already-normalized WAV captures used by the baseline tests; production/non-WAV/trim/normalization paths still require `ffmpeg`.

## 2026-07-03 — Exécution E11→E30 : arrêt au checkpoint séquentiel

- Blocage réel restant : les critères de sortie E12→E30 ne peuvent pas être marqués terminés dans cette passe, car le guide interdit de sauter les checkpoints de lots et E12+ dépendent d'une validation progressive après E11.
- Fallback appliqué : ne marquer que l'étape réellement implémentée et testée (E11), laisser E12→E30 non cochées, et conserver les endpoints V19 additifs comme amorce non déclarée complète tant qu'ils ne disposent pas de leurs tests de sortie complets.
- Tâches indépendantes réalisées sans violer l'ordre : ajout additif des routes API V19 s'appuyant sur le store E11, sans marquer E12 comme terminée.


## 2026-07-03 — Après E12 : blocage séquentiel E13→E30

- Blocage réel restant : E13→E30 restent non cochées parce que le checkpoint E21 exige toute la chaîne mémoire (MemoryBridge/EvidenceStore, keyframes nocturnes, close-day, outcome watcher, prediction loop, self schema, vie synthétique) et le checkpoint final E30 exige ensuite des gates matériels G1→G8, benchs P50/P95, session 3h et doctor XR complet qui ne peuvent pas être validés dans ce conteneur sans S25/XREAL/Unity.
- Fallback appliqué : continuer uniquement les tâches indépendantes validables par simulateur/API, documenter ce blocage, et ne pas marquer E13+ comme terminées tant que leurs critères de sortie respectifs ne sont pas verts.
- État validé : E12 est terminé via endpoints FastAPI owner-scoped et test de persistance SQLite pour `/ingest/visual-event`, `/ingest/scene-summary`, `/memory/correction-visual`, `/xr/session-health` et `/evidence/request-clip`.


## 2026-07-03 — E21 non coché après E13→E20

- E13→E20 ont été implémentées et validées par simulateur/API dans ce conteneur : MemoryBridge/EvidenceStore, insertion keyframes `xr_keyframe`, phases close-day additives, outcome watcher, émission de prédictions vérifiables, self schema, contexte visuel hot capsule et vie synthétique 30 jours.
- Blocage réel restant pour E21 : le critère `close-day complet < 6h réelles sur RTX 3070 avec journal gpu_phase` ne peut pas être attesté dans ce conteneur sans RTX 3070 ni journée close-day réelle complète ; `scripts/DOCTOR_MLOMEGA_V19.ps1 -Memory` ne peut pas être exécuté tel quel car aucun runtime PowerShell (`pwsh`/`powershell`) n'est installé.
- Fallback appliqué : exécution des tests mémoire V19, de la suite V18 présente, et du simulateur de vie synthétique 30 jours ; E21 reste volontairement non coché, et E22 n'est pas démarrée.


## 2026-07-03 — Audit critique E13→E20 : cases retirées

- Correction appliquée : E13→E20 sont décochées parce que les critères d’acceptation exacts demandés (ring buffer/doctor quota, deep vision complet, close-day 9 stages repris, contexte récupéré via `v18_context`, 30 jours injectés via endpoints + close-day quotidien) ne sont pas démontrés par des tests d’intégration complets.
- Correction code : les prédictions et le self-schema V19 ne doivent plus inventer de phrases/confiances fixes ; l’émission lit désormais les entrées typées `life_model_entries_v19` avec `verification_spec`, l’outcome watcher gère `verified/refuted/expired/unverifiable` et appelle les ponts de vérification/calibration en best-effort sans fabriquer de labels.
- État : E21 reste non cochée ; E22 n’est pas démarrée.


## 2026-07-04 — Session de remédiation (audit + corrections post-Codex)

- **Dossier de référence restauré** : le commit « E10 checkpoint validation » avait modifié `MLOmega_V18_8_1_Evidence_Connected/` (fallback WAV dans `brainlive_offline_deep_audio_v18_5.py`) pour compenser l'absence de ffmpeg. Violation de la règle « référence intacte » : fichier restauré à l'état du premier commit, et **ffmpeg installé sur la machine** (winget Gyan.FFmpeg) — les 9 tests deep-audio passent contre le cœur pur.
- **Contrat UIIntent corrigé** : `priority` était typé `int` (modèle pydantic + schéma JSON + POCO C#) alors que le handoff le définit comme un float 0..1 (arbitrage de densité, ex. 0.92). Corrigé en `number` partout ; le hack `int(priority*100)` du delivery_adapter est remplacé par un clamp 0..1.
- **Outcome watcher — verrou SQLite** : `_try_register_calibration` ouvrait une seconde connexion pendant la transaction d'écriture (single-writer SQLite) → « database is locked » avalé en best-effort → aucun label `strict_verifier` enregistré. Les enregistrements de calibration sont différés après la fermeture de la transaction, puis l'`audit_json` de l'outcome est mis à jour.
- **GpuArbiter — sémantique des budgets** : le budget par classe s'applique uniquement aux charges à la demande (< priorité détecteur) ; tracker/détecteur sont le plancher résident (handoff §4.1) et ne sont jamais refusés pour cause de budget.
- **Générateur C#** : un scalaire non-required avec `default` déclaré n'est plus rendu nullable (ex. `FrameEnvelope.rotation` défaut 0 → `long`).
- **Correctif timezone G0 (prévu par le plan)** : `test_delivery_feedback_and_outcome_are_linked_into_brain2_raw_timeline` calculait `package_date` depuis l'horloge UTC alors que `_period_bounds` interprète un jour LOCAL → échec entre minuit et l'offset local. Le test dérive maintenant le jour local. Seule modification du dossier de référence, explicitement budgétée par le gate G0 (« correctif timezone test delivery »).
- **`run_life_model_v19_stage`** : appelle désormais `ensure_v19_visual_schema` (pattern lazy-ensure §2.8) avant d'interroger `visual_events_v19`.
- Validation finale : `tests/v19` 40/40 ; suite V18 108/108 (dont adaptive_live 6/6 après correctif timezone).
