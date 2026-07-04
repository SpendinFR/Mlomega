# EXECUTOR_BUILD_GUIDE — MLOmega V19, construction pas à pas

Complément d'exécution de `docs/EXECUTOR_HANDOFF.md`. Le handoff dit **quoi** construire et pourquoi ; ce guide dit **comment**, étape par étape, avec les signatures réelles du code existant (extraites du dépôt le 2026-07-03 — recopiées, pas paraphrasées). Les références « guide §x » (TTL SceneCache, skills, composants UI, chaînes de scénarios, gates, tests, règles de vérité) résolvent dans `docs/GUIDE_V19_REFERENCE.md`. En cas de divergence entre ce guide et le code réel, **le code réel fait foi** : lire le module, consigner la divergence dans `docs/DECISIONS.md`, continuer.

Conventions : `E<n>` = étape ; chaque étape a Objectif / Créer / Brancher / Valider. Ne pas sauter d'étape. Chemins relatifs à la racine du monorepo V19 ; le cœur = `src/mlomega_audio_elite/`.

---

## 1. Les deux chaînes existantes (à connaître par cœur avant de coder)

### 1.1 Chaîne live de delivery (existante, à réutiliser telle quelle)

```
candidat d'intervention (H1)
  → v18_delivery.enqueue_delivery(...)        # point d'entrée UNIQUE, dédup + cooldown
  → table brainlive_intervention_delivery_queue (delivery_status='queued')
  → [V19 : delivery_adapter la consomme et pousse vers les renderers]
  → v18_8_live_policy.record_delivery_feedback(...)     # delivered/displayed/seen/acted/dismissed/ignored/failed
  → v18_8_live_policy.materialize_intervention_outcome_observation(...)  # réconciliation Brain2
```

### 1.2 Chaîne nocturne (existante — noms de stage réels du close-day)

```
close_brainlive_day(person_id=..., ...)          # v18_close_day.py
  stage "post_stop"      → run_brainlive_post_stop_deep_flow :
       assembly    → brainlive_event_assembler_v15_14.run_brainlive_event_assembly
                     (timeline brute multi-capteurs → bundles brainlive_event_bundles_v1514,
                      plafond 25 min via MLOMEGA_BRAINLIVE_MAX_BUNDLE_MINUTES)
       deep_audio  → brainlive_offline_deep_audio_v18_5.run_offline_deep_audio_for_bundles
                     (précédé de release_live_model_caches())
       deep_vision → brainlive_offline_deep_vision_v16_1.run_offline_deep_vision_for_bundles
                     (≤12 keyframes/bundle depuis vision_timeline_json, VLM par keyframe,
                      sortie brainlive_deep_vision_observations_v161, le tout sous
                      gpu_phase("post_stop_deep_vision", release_before=True, release_after=True))
  stage "longitudinal"   → brain2_longitudinal_cases_v17.run_longitudinal_consolidation
  stage "coordination"   → brainlive_brain2_coordination_v15_12.run_brainlive_brain2_coordination
  stage "life_model"     → brain2_life_model_updater_v15_13.run_brain2_life_model_update
  stage "live_ready"     → brainlive_personal_model_v15_9.build_brain2_live_personal_model
  puis record_output_manifest + assert_cleanup_eligible(required_stages=[les 5 ci-dessus])
```

Checkpoints/reprise : table générique `v18_pipeline_stages` (une ligne par `(run_id, stage_name)`, status ∈ running/completed/failed/retryable_error/skipped) + `v18_close_day_runs` (unique `(person_id, package_date)`). `_run_stage()` saute un stage déjà `completed`.

### 1.3 Comment la vidéo du jour est consolidée la nuit en V19 (décision de conception)

La nuit ne « regarde » **jamais** la vidéo image par image. Ce qui entre dans la consolidation nocturne est déjà curé pendant le jour :

1. **Keyframes** : le sélecteur de keyframes du PC (score de changement de scène, handoff §3.6) enregistre chaque keyframe retenue comme ligne `vision_frames` (`insert_only`, table immuable) avec `capture_mode='xr_keyframe'`, `live_session_id`, `image_path`, `image_sha256` + une ligne `raw_assets` pour le fichier. **Conséquence clé : l'assembleur de bundles et le deep vision existants les prennent en charge sans modification** — la chaîne assembly → deep_vision → Brain2 fonctionne telle quelle, la seule différence est que `vision_timeline_json` contient des keyframes choisies au lieu de photos périodiques.
2. **Clips de preuve** : sélectionnés en live par MemoryBridge (déclencheurs handoff §Lot 2) → `visual_evidence_assets_v19` + `visual_events_v19` (nouvelles tables).
3. **Résumés de session** : WorldBrain écrit `scene_session_summaries_v19` à la fin de chaque session.
4. **Tampon-jour** (optionnel) : sert uniquement au replay du jour ; il est **purgé** au close-day après extraction des clips retenus — il n'est jamais analysé exhaustivement.
5. La sélection uniforme actuelle (`select_keyframes_for_bundle` : premier/milieu/dernier + espacement régulier, max 12) reste le filet de sécurité ; comme les frames candidates sont déjà des keyframes de changement de scène, l'échantillonnage uniforme échantillonne du signal, plus du bruit.

Nouvelles phases nocturnes V19 (E15) insérées dans le close-day existant : `visual_consolidation` (changes/last-seen → `visual_events_v19`), `outcome_resolution` (auto-vérification des prédictions), `prediction_emission`, `self_schema`.

---

## 2. Référence de la surface d'intégration (signatures réelles)

### 2.1 `v18_delivery.py`

```python
def enqueue_delivery(*, live_session_id: str, source_key: str, candidate: Mapping[str, Any],
                     decision_run_id: str | None = None, hot_intervention_id: str | None = None,
                     tick_id: str | None = None, con: Any | None = None,
                     schema_ready: bool = False) -> dict[str, Any]
# retour: {"status": "skipped"|"suppressed"|"deduplicated"|"queued", "delivery_id": str|None, ...}
def ensure_delivery_schema() -> None
```
`candidate` — champs lus : au moins un de `message`/`text`/`say`/`intervention_message` ; `decision` ∈ `{"queue","speak_now","proactive","notify"}` ; optionnels `action_type`, `cooldown_key`, `recommended_timing`, `candidate_id`, `urgency`/`priority`/`expected_gain` (clampé 0..1).
Queue : `brainlive_intervention_delivery_queue(delivery_id PK, live_session_id, tick_id, candidate_id, horizon, message, action_type DEFAULT 'notify', delivery_status DEFAULT 'queued', priority REAL, evidence_json, created_at, delivered_at, displayed_at, seen_at, feedback_at, feedback_type, feedback_note, updated_at)`.
Dédup : `brainlive_intervention_delivery_dedupes` — clé = `stable_id("v18_delivery", person_id, live_session_id, source_key, fingerprint)` → **`source_key` est obligatoire et significatif**.

### 2.2 `v18_8_live_policy.py`

```python
def record_delivery_feedback(*, delivery_id: str, feedback_type: str, feedback_source: str,
                             note: str | None = None, evidence: Mapping[str, Any] | None = None,
                             observed_at: str | None = None) -> dict[str, Any]
# feedback_type ∈ {"delivered","displayed","seen","acted","dismissed","ignored","failed"} sinon ValueError
def materialize_intervention_outcome_observation(*, delivery_id: str, outcome_status: str,
                             observed_later_summary: str | None = None, did_help: bool | None = None,
                             evidence: Mapping[str, Any] | None = None,
                             observed_at: str | None = None) -> dict[str, Any]
# outcome_status ∈ {"observation_pending","feedback_explicit","reconciled_helped","reconciled_not_helped","unresolved"}
```
Tables : `brainlive_intervention_feedback_events_v188`, `brainlive_intervention_outcomes_v188` (UNIQUE `(delivery_id, outcome_status)`).

### 2.3 `v18_context.py` — ⚠️ pattern de patch

`build_active_context` **n'est pas défini ici** : l'original vit dans `brainlive_v15.py`
(`build_active_context(live_session_id: str, *, active_people=None, refresh_minutes=10, limit=20) -> dict`)
et `v18_context.install(module)` le remplace par un wrapper. Le wrapper lit `raw["context"]["visual_context"]` (issu de `vision_scene_observations`) et `raw["context"]["world_state"]` (dernière ligne de `brainlive_world_states`) et les aplatit en `ContextItem` (importance 0.8).
**Point d'extension** : ajouter un champ dans le mapping `_FIELD_TABLE` (`champ → table source`) de `v18_context.py`, et alimenter la table source correspondante. **Interdit** : re-patcher par-dessus, modifier le wrapper.

### 2.4 `v18_hot_capsule.py`

```python
def build_hot_capsule_payload(*, episode, manifest, fused, route, target_ms: int) -> tuple[dict, dict]
```
Budget entrée : `hot_input_budget(manifest)` — défaut 12 000 chars (env `MLOMEGA_V18_HOT_CAPSULE_MAX_CHARS`, borné 1500-20000) ; sortie 900 tokens (env `MLOMEGA_V18_HOT_OUTPUT_TOKENS`, 160-1400). Réduction itérative `_reduce_once` avec journal `manifest.omitted_refs`.
**Règle** : tout champ V19 (scène/focus/traduction) doit être comptabilisé par `_measure()` et réductible — jamais de clé hors budget.

### 2.5 `auto_verification_v14_4.py`

```python
def auto_verify_latent_outcome_predictions(*, conversation_id=None, person_id=None, limit=50,
                                           min_confidence=0.55, skip_already_verified=True) -> dict
def ensure_v14_4_schema() -> None
def autopilot_coverage() -> dict ; def audit_v14_4(*, persist=True) -> dict
```
Source : table `latent_outcome_links` (créée par `brain2_flow_v13_3.ensure_brain2_flow_schema`), jointe à `predictions` via `lol.source_id = p.prediction_id` (`source_table='predictions'`). Tables de sortie : `v14_4_auto_verify_runs`, `v14_4_auto_verify_links`, `v14_4_autopilot_coverage`.

### 2.6 `v18_predictive_retrieval.py`

```python
def ensure_predictive_schema() -> None
def get_predictive_backend() -> DensePredictiveBackend        # .retrieve / .sync_cases / .score_pair
def register_verified_similarity_label(*, person_id, anchor_case_id, similar_case_id, label,
        label_source, verified_at, source_revision=None, notes=None, metadata=None) -> dict
# label_source CHECK ∈ ('human_verified','strict_verifier','import_verified')
def calibrate_predictive_similarity(*, person_id, backend=None, min_samples=None,
        min_validation_precision=None) -> CalibrationResult
def current_calibration(*, person_id, embedding_revision) -> CalibrationResult | None
```
Contraintes : `similar_case_id` antérieur à `anchor_case_id` ; calibration = split chronologique train/validation, statut `accepted` seulement si précision validation ≥ seuil. **L'outcome watcher V19 étiquette avec `label_source='strict_verifier'`.**

### 2.7 `v18_life_model.py`

`_DIRECT_EVIDENCE_SOURCES: dict[str, tuple[str, str, tuple[str, ...]]]` — format `table → (colonne_pk, colonne_owner, colonnes_temps_candidates)`, ex. `"brainlive_world_states": ("world_state_id", "person_id", ("state_time", "created_at"))`. 34 tables. Existent aussi `_CONVERSATION_EVIDENCE_SOURCES` et `_SESSION_EVIDENCE_SOURCES` (résolution owner via `brainlive_sessions`).
**Toute table V19 servant de preuve doit être ajoutée à l'un de ces trois dicts**, sinon `ScopeError("evidence table is not approved")`.
`v18_life_model.py` est une **librairie de gouvernance** (pas un orchestrateur) : `validate_stratum_evidence`, `install_canonical(module)`, `install_updater(module, canonical_module)`.

### 2.8 `db.py`

Pattern : `db.py` porte un `SCHEMA` central appliqué par `init_db()` (`executescript`, cache `_INITIALIZED_DB_PATHS`) ; **les modules périphériques définissent chacun leur propre `SCHEMA` DDL + `ensure_*_schema()` lazy** appelé en tête de leurs fonctions publiques. → Les tables V19 vivent dans leurs modules (`v19_*.py`) avec `ensure_v19_*_schema()`, `CREATE TABLE IF NOT EXISTS` additifs. Ne pas toucher au `SCHEMA` central.
Helpers : `connect()`, `write_transaction(con, *, immediate=True)`, `upsert(con, table, values, pk)` (refuse `IMMUTABLE_FACT_TABLES`), `insert_only(...)`.
⚠️ `vision_frames` est dans `IMMUTABLE_FACT_TABLES` → **`insert_only` uniquement**.
Schémas utiles (définis dans `brainlive_v15.py`) :
- `vision_frames(frame_id PK, source_asset_id, conversation_id, live_session_id, captured_at NOT NULL, image_path, image_sha256, width, height, device_source, capture_mode DEFAULT 'manual', metadata_json, created_at)`
- `vision_scene_observations(observation_id PK, frame_id FK NOT NULL, live_session_id, conversation_id, model NOT NULL, scene_summary, location_hint, people_count, spatial_context, social_context_hint, visible_text_json, objects_json, risks_json, affordances_json, possible_user_activities_json, personal_relevance_json, confidence, raw_json, created_at)`
- `brainlive_world_states(world_state_id PK, live_session_id FK NOT NULL, person_id NOT NULL, state_time NOT NULL, where_am_i, who_is_active_json, what_is_happening, probable_activity_json, active_emotional_state, active_mode, audio_context_json, visual_context_json, evidence_json, counter_evidence_json, confidence, created_at)`
- `brainlive_active_contexts(...)` (⚠️ nom réel, pas `active_contexts`)
- `raw_assets(asset_id PK, type, path, sha256, captured_at, source, metadata_json, created_at)`

### 2.9 `api.py`

FastAPI, instance globale `app` (fallback `app=None` si absent), **pas d'APIRouter, pas d'auth**, `init_db()` au startup. Style à répliquer :
```python
@app.post("/ingest/transcript")
async def upload_transcript(file: UploadFile = File(...)):
    ...
    return {"conversation_id": conv_id}
```

### 2.10 `ingest.py`

```python
def ingest_transcript(data: dict, source_path: Path | None = None) -> str   # entrée canonique
```
`_resolve_memory_owner(...)` exige `metadata.memory_owner_id` (ou `owner_person_id`/`person_id`) explicite, sinon `ScopeError`. Scope enregistré via `governance_v18.register_conversation_scope_in_transaction(...)`. **Tout ingest V19 fournit `memory_owner_id` explicitement.**

### 2.11 `v18_close_day.py` / runtime

```python
def close_brainlive_day(*, person_id, live_session_id=None, service_run_id=None, package_date=None,
                        use_llm=True, force=False, post_stop_result=None) -> dict
def close_day_status(*, person_id, package_date=None)
```
Ajout d'une phase (liste **en dur**, pas de registre) : (1) `def do_xxx(): ...` dans le corps, (2) `_run_stage(run_id=run_id, name="xxx", fn=do_xxx)` à la bonne position, (3) ajouter le nom dans `_status_ok()` et `_stage_identifier()`, (4) l'ajouter dans `expected=[...]` et `required_stages=[...]` de `assert_cleanup_eligible`.
GPU : `gpu_phase(name, *, release_before=False, release_after=True)` (context manager), `release_live_model_caches()`, `ollama_unload(model=...)` dans `llm.py`.
⚠️ **Piège d'import** : plusieurs modules importent `from .runtime_v18_7 import ...` alors que les définitions vivent aussi dans `runtime_v18_8.py`. Ne pas « corriger » à la volée : suivre l'import existant du module qu'on étend, consigner dans DECISIONS.md.

### 2.12 Deep vision (chaîne à réutiliser en E14)

```python
def run_offline_deep_vision_for_bundles(person_id="me", *, package_date=None, live_session_id=None,
        model=None, timeout_per_image=None, max_keyframes_per_bundle: int = 12,
        transcript_char_threshold=None, limit_bundles=200, append_to_brain2=True,
        fail_on_vlm_error=False, use_vlm=True) -> dict
def select_keyframes_for_bundle(bundle: dict, *, max_keyframes: int = 12, silent_bias=True) -> list[dict]
```
Entrée : bundles `brainlive_event_bundles_v1514.vision_timeline_json` ; VLM choisi par priorité `model` > `MLOMEGA_OFFLINE_VLM_MODEL` > `MLOMEGA_VLM_HEAVY_MODEL` > `MLOMEGA_VLM_MODEL` > `settings.ollama_model`, appelé via `ollama_generate` (base64, `format:"json"`, temp 0.0), déchargé via `ollama_unload`. Sorties : `brainlive_deep_vision_runs_v161`, `brainlive_deep_vision_observations_v161`, `brainlive_deep_vision_brain2_exports_v161`.
CLI : `brainlive-close-day`, `brainlive-resume-close-day`, `brainlive-deep-vision-run/-audit`, `brainlive-deep-audio-run/-audit`, `brain2-life-model-update`.

---

## 3. Étapes — LOT 1 (Fondation)

**[x] E1. Squelette monorepo.**
Statut : terminé — commit : 173184f (import) + 4598428 (référence restaurée à l'état pristine) — tests : suite V18 108/108 contre `src/` racine. Créer l'arborescence handoff §3.1 ; copier `MLOmega_V18_8_1_Evidence_Connected` → `src/` + fichiers racine nécessaires ; vérifier que `pytest tests/test_v18_8_1_evidence_connected.py` passe AVANT toute modification (baseline). Geler `runtime_v18_7`/`operations_v18_7` (en-tête « gelé, ne pas diverger de v18_8 »).

**[x] E2. Contrats.**
Statut : terminé — commit : 173184f + 4598428 (fix `priority` int→float 0..1 partout ; POCOs C# générés avec champs via `generate_csharp.py`) — tests : test_contracts + test_csharp_generator verts. `packages/contracts/schemas/*.schema.json` (8 contrats handoff §3.4, champ `contracts_version` partout) → modèles pydantic v2 générés/écrits dans `packages/contracts/python/` → stubs C# dans `csharp/`. Test round-trip python→JSON→python pour chaque contrat. Aucune dépendance vers le cœur ni vers un SDK.

**[x] E3. SessionHub**
Statut : terminé — commit : 2d2a7d8 — tests : test_sessionhub (ClockSync offset/RTT validés numériquement). (`services/live-pc/sessionhub.py`). Sessions (`session_id` = uuid horodaté, jamais réutilisé), ClockSync (échange de timestamps monotones, offset stocké par session), token de session éphémère émis à l'appairage (remplace le token statique pour le canal XR ; le bridge V18.8 existant garde le sien). Test : deux clients simulés, offsets cohérents.

**[x] E4. VideoIngress + gateway** (`services/live-pc/gateway.py`). Interface `VideoIngress` (async itérateur de `(frame_bgr, FrameEnvelope)`), impl `AiortcIngress`. Queue = 1 : variable « dernière frame » + compteur de drops, jamais de liste. Bench intégré : P50/P95 décodage. Test : `webrtc_frame_queue_bounded`.
Statut : terminé — commit : 4598428 (vrai `AiortcIngress` WebRTC ; le stub initial 2d2a7d8 renommé `IterableIngress`) — tests : test_transport + test_transport_webrtc 3/3 (boucle aiortc réelle) ; bench réel P95 décodage 0,81 ms (8c24192).

**[x] E5. fake_xr_device** (`simulators/fake_xr_device.py`). Client aiortc qui rejoue un MP4 + JSONL de pose ; options : fps, perte réseau simulée, rotation 90° (mode capture-only). Produit des `FrameEnvelope` valides (frame_id croissants, monotonic ns).
Statut : terminé — commit : 4598428 (rejeu MP4+pose réel en H.264 via aiortc, options fps/loss/rotate90 ; scénario de test dans `simulators/scenarios/`) — tests : test_transport_webrtc 3/3.

**[x] E6. delivery_adapter** (`services/live-pc/delivery_adapter.py`). Boucle : lit `brainlive_intervention_delivery_queue` (`delivery_status='queued'`, tri priority desc) → convertit en `UIIntent` (`producer='brainlive'`, `component='context_card'` par défaut, `evidence_refs` depuis `evidence_json`, `delivery_id` reporté) → push WebSocket/DataChannel vers renderers connectés → à l'accusé : `record_delivery_feedback(delivery_id=..., feedback_type='delivered', feedback_source='xr_adapter')` puis relaie chaque `UIReceipt` (`displayed/seen/acted/dismissed`) vers la même fonction. Les UIIntent d'UltraLive/VisionRT ne passent **pas** par cette queue (réflexes directs) ; seul BrainLive H1 y passe. Lire `v18_delivery.py` en entier avant (fonction de poll existante à réutiliser si présente).
Statut : terminé — commit : 2d2a7d8 + 4598428 (priority float clampée 0..1) — tests : test_delivery_adapter + démo intégration jusqu'à `brainlive_intervention_feedback_events_v188`.

**[x] E7. companion-web** (`apps/companion-web/`). Une page : WebSocket vers delivery_adapter, rendu des UIIntent (cards/sous-titres/contours sur flux optionnel), clic → UIReceipt. Sert de renderer de référence pour tous les tests.
Statut : terminé — commit : 2d2a7d8 — tests : receipts displayed/dismissed vérifiés via la démo SimOnly.

**[x] E8. GpuArbiter + degraded** (`services/live-pc/gpu_arbiter.py`, `degraded.py`). NVML (pynvml) : VRAM totale/utilisée par phase ; API `request(job_class) -> grant/deny/preempt` selon priorités handoff §4.1 ; vérification post-`ollama_unload` (re-mesure VRAM, alerte si pas libérée). États dégradés → événements poussés aux renderers (StatusBar).
Statut : terminé — commit : 4598428 (budgets par classe + `verify_ollama_unload` /api/ps + machine à états degraded réelle) — tests : test_gpu_arbiter 5/5, test_degraded verts.

**[x] E9. Scripts + profil.** `INSTALL_MLOMEGA_V19_WINDOWS.ps1` (préflight, `.venv-live`, MODEL_MANIFEST, ne touche pas `.venv`), `setup_profile.ps1` (questions → `configs/user_profile.yaml`, cf. handoff §3.5), `RUN_MLOMEGA_V19.ps1 -SimOnly|-Xr`, `DOCTOR_MLOMEGA_V19.ps1` (ports, GPU, Qdrant, Ollama, contrats, queue delivery, profil), `BENCH_V19.ps1`. Ports V19 : préfixe 87xx hors 8766.
Statut : terminé — commit : 4598428 (INSTALL transactionnel réel, DOCTOR avec checks GPU/Qdrant/Ollama/contrats, setup_profile interactif + -Defaults) — tests : test_scripts_profile vert ; DOCTOR exécuté sur machine cible : OK, 4 WARN, 0 FAIL (RTX 3070 détectée).

**[x] E10. Checkpoint Lot 1.**
Statut : terminé — commit : 8c24192 — tests : tests/v19 40/40 ; V18 108/108 ; bench WebRTC réel machine cible : P95 décodage 0,81 ms < 33 ms (critère tenu, cf. `docs/BENCH_RESULTS.md`). `pytest tests/v19 -m "contracts or transport"` vert ; `pytest tests/test_v18_*` vert inchangé ; démo : `RUN -SimOnly` → fake device → UIIntent test → companion-web → receipt visible dans `brainlive_intervention_feedback_events_v188`. Bench ingress consigné dans `docs/BENCH_RESULTS.md`. **Revue avant Lot 2.**

---

## 4. Étapes — LOT 2 (Mémoire profonde)

**[x] E11. Tables V19** (`src/mlomega_audio_elite/v19_visual_store.py`). SCHEMA propre + `ensure_v19_visual_schema()` (pattern §2.8) : `visual_evidence_assets_v19`, `visual_events_v19`, `world_entity_links_v19`, `scene_session_summaries_v19`, `ui_interaction_outcomes_v19` (colonnes : handoff §Lot 2 + toujours `person_id`, `live_session_id`, temps UTC + `created_at`). Puis **enregistrer chaque table de preuve dans `_DIRECT_EVIDENCE_SOURCES`** (format §2.7) — ex. `"visual_events_v19": ("visual_event_id", "person_id", ("occurred_at", "created_at"))`. Test : insertion + `validate_stratum_evidence` accepte une ref vers ces tables.
Statut : terminé — commit : 6f61715 + 4598428 (3 tables brain2_*_models ajoutées + evidence sources) — tests : test_memory_v19 verts (validate_stratum_evidence accepte les refs v19).

**[x] E12. Endpoints** (`api.py`, style §2.9, additif en fin de fichier) : `/ingest/visual-event` (EvidenceEvent JSON → `visual_events_v19` + asset), `/ingest/scene-summary`, `/memory/correction-visual`, `/xr/session-health`, `/evidence/request-clip`. Chaque payload porte `memory_owner_id` explicite (règle §2.10).
Statut : terminé — commit : 4e6c03f — tests : endpoints FastAPI via TestClient, 422 si `memory_owner_id` absent.

**[x] E13. MemoryBridge + EvidenceStore** (`services/live-pc/memory_bridge.py`, `evidence_store.py`). Déclencheurs de sélection (handoff §Lot 2) → clip depuis ring buffer/tampon-jour → sha256 → POST `/ingest/visual-event`. Tampon-jour : encodage basse résolution continu, purge au close-day, quota doctor.
Statut : terminé — commit : 9be3afa + 4598428 — tests : test_memory_v19 (e13) vert ; déclencheurs enrichis + tampon-jour purgé par la consolidation.

**[x] E14. Pont keyframes → chaîne nocturne existante** (le pont central du projet). Le sélecteur de keyframes PC enregistre chaque keyframe : (1) fichier image → `raw_assets` ; (2) ligne `vision_frames` via **`insert_only`** (`capture_mode='xr_keyframe'`, `live_session_id`, `image_sha256`) — cf. §1.3. Vérifier ensuite avec le simulateur que `run_brainlive_event_assembly` intègre ces frames dans `vision_timeline_json` d'un bundle et que `run_offline_deep_vision_for_bundles` les analyse (si l'assembleur ne lit pas `vision_frames` pour la timeline vision, lire `collect_live_raw_timeline` et brancher au bon endroit — ADR obligatoire). Test : session simulée → bundle → deep vision → `brainlive_deep_vision_observations_v161` non vide.
Statut : terminé — commit : 9be3afa (`v19_keyframes.py` : insert_only, capture_mode='xr_keyframe', raw_assets) — tests : test_memory_v19 vert ; chaîne bundle→deep vision avec VLM réel = vérification différée au close-day final (décision utilisateur).

**[x] E15. Nouvelles phases close-day** (`v18_close_day.py`, pattern exact §2.11 — seule modification autorisée de ce fichier). Après `post_stop`, avant `longitudinal` : stage `visual_consolidation` (module `v19_visual_consolidation.py` : ChangeEvents WorldBrain → `visual_events_v19` ; résumés session → `scene_session_summaries_v19` ; purge tampon-jour après extraction). Après `life_model` : stages `outcome_resolution`, `prediction_emission`, `self_schema` (E16-E18). Chaque stage ajouté dans `_status_ok()`, `_stage_identifier()`, `expected`, `required_stages`. Test : `brainlive-close-day` complet sur données simulées, `close_day_status` liste les 9 stages `completed` ; relance = tous `resumed_stage`.
Statut : terminé — commit : 9be3afa + 4598428 (stage `life_model_v19` ajouté entre `outcome_resolution` et `prediction_emission` — 10 stages au total) — tests : pattern 4-endroits audité conforme §2.11 ; V18 108/108.

**[x] E16. Outcome watcher** (`v19_outcome_watcher.py`). Prédictions ouvertes (avec `verification_spec`) × preuves du jour (transcripts, `visual_events_v19`, GPS, routines) → résolution `verified/refuted/expired/unverifiable` + evidence_refs de résolution → écrit `prediction_outcomes_v19` ; alimente la calibration via `register_verified_similarity_label(..., label_source='strict_verifier')` (contrainte §2.6 : le cas similaire doit être antérieur à l'ancre) ; appelle `auto_verify_latent_outcome_predictions` pour la voie conversationnelle existante. Échantillon d'audit journalisé.
Statut : terminé — commit : 9be3afa + 4598428 + 8c24192 (fix verrou SQLite : labels de calibration différés hors transaction) — tests : test_prediction_auto_verified_by_observation vert (outcome `verified` + label `strict_verifier` sans entrée utilisateur).

**[x] E17. Prediction emission + Life Model durable** (`v19_prediction_loop.py`, `v19_life_model_store.py`). Life Model V19 = magasin d'entrées typées (handoff Lot 2 : dimensions × axes temporels, statuts `active/weakening/contradicted/superseded`, historique). Mise à jour = deltas LLM en 3 étapes contractées (réutiliser `llm_contracts_v15_18`), appliquées par le store — jamais de régénération complète. L'updater V15.13 existant continue de tourner (stage `life_model`) ; le store V19 le complète, il ne le remplace pas (ADR si conflit). Émission : 3-7 prédictions avec `verification_spec`, pénalité si invérifiable.
Statut : terminé — commit : 4598428 (store branché au close-day, deltas incrémentaux, transitions weakening/contradicted) — tests : test_life_model_update_is_incremental + test_life_model_entry_weakens_without_confirmation verts.

**[x] E18. Self schema** (`v19_self_schema.py`). Projection depuis life model store + patterns confirmés + `causal_edges` + `prediction_outcomes_v19` → table `self_schema_v19` (entrées : type aime/veut/a_fait/causal/conditionnel, evidence_refs, taux d'occurrence). Endpoint `GET /self-schema` + projection compacte dans le hot capsule (E19).
Statut : terminé — commit : 9be3afa + 4598428 — tests : test_self_schema_conditional_pattern_has_evidence vert (occurrence_rate + evidence_refs obligatoires).

**[x] E19. Hot capsule + contexte visuel.** (1) `v19_visual_context.py` : pousse l'état WorldBrain courant dans `brainlive_world_states` (schéma §2.8) et les observations dans `vision_scene_observations` — le wrapper `v18_context` les reprend automatiquement ; ajouter les champs nouveaux (`self_schema_hot`, `scene_focus`) dans `_FIELD_TABLE`. (2) Extension `v18_hot_capsule` : champs additifs comptabilisés par `_measure()` et réductibles (§2.4). Test : hot capsule avec scène simulée respecte le budget et journalise les omissions.
Statut : terminé — commit : 4598428 (`v19_visual_context` réécrit sur les vraies tables `brainlive_v15` — schéma shadow supprimé, cf. DECISIONS 2026-07-04) — tests : test v19_visual_context contre les tables réelles vert ; budget hot capsule respecté.

**[x] E20. Vie synthétique** (`simulators/synthetic_life.py`) : 30 jours générés (routines, déplacements, objets, rencontres, conversations) injectés par les endpoints → close-day par jour → au moins une routine détectée, une prédiction `verified`, une `refuted`, un pattern conditionnel dans le self schema. C'est le test d'acceptation du lot.
Statut : terminé — commit : 4598428 (générateur seedé 30 jours : personnes/conversations/lieux/objets déplacés/routines à ~80% d'adhérence/événements rares) — tests : scénarios alimentent les 4 tests nommés, tous verts.

**[x] E21. Checkpoint Lot 2.** `pytest tests/v19 -m memory` vert ; tests V18 verts ; close-day complet < 6h réelles sur RTX 3070 (données synthétiques) avec journal `gpu_phase` ; doctor `-Memory` vert. **Revue avant Lot 3.**
Statut : terminé — commit : 8c24192 — tests : tests/v19 40/40 ; V18 108/108 ; doctor OK (4 WARN services éteints). **Exception actée (décision utilisateur 2026-07-04) : le close-day réel complet avec Ollama/Qdrant allumés est différé après le Lot 3, en test final de bout en bout.**

---

## 5. Étapes — LOT 3 (Live/XR/mobile)

**E22. Gate G1 matériel (peut démarrer dès la fin du Lot 1, en parallèle du Lot 2).** Unity 6 LTS + XREAL SDK 3.1.0, sample officiel sur S25 réel : Eye RGB, pose, rendu stéréo, permissions (`RECORD_AUDIO`, `FOREGROUND_SERVICE_MEDIA_PROJECTION`), coupure/reprise. Si la caméra Eye est inaccessible : plan B `one-xr` (pose) + caméra S25 (même pipeline), ADR, et continuer.

**E23. App Unity noyau.** `XRDeviceAdapter` (interface C#) + `XrealDeviceAdapter`, `SimulatedDeviceAdapter`, `PhoneOnlyAdapter` ; `XrSessionController`, `EyeCaptureSource` (frame_id + monotonic), `PosePublisher`, `ClockSync` (protocole E3).
Statut : code livré — commit : 6c67d5d — tests : EditMode écrits (exécution à la première ouverture Unity) ; validation matérielle couplée au gate G1. Contrats synchronisés dans `apps/xr-mobile/Assets/Scripts/Contracts/` (Newtonsoft, cf. ADR DECISIONS §E23) + outil de sync Editor ; `ClockSync` reproduit numériquement `sessionhub.py` (offsets -5ms/+8ms de `tests/v19/test_sessionhub.py`) ; `PhoneOnlyAdapter` = cible téléphone-only de premier rang (`IsStereo=false`) ; sélection d'adaptateur via `MLOmegaConfig` alignée sur `configs/user_profile.yaml`. Pas de [x] (validation Unity/matériel utilisateur après ouverture).

**E24. Transport mobile.** Plugin Kotlin `LiveTransportPlugin` (GetStream webrtc-android) : H.264 low-latency + Opus 20 ms + DataChannel fiable/ordonné (contrats E2 sérialisés JSON), reconnexion, bitrate adaptatif. Valider contre le gateway E4 : frame_id/pose intacts côté PC, UIIntent retour affiché sur le bon track.
Statut : code livré — commit `feat(v19-e24)` — tests : `pytest tests/v19` 50/50 verts (dont `test_sessionhub_http` 8/8, `test_transport_webrtc` unifié 4/4, `test_e24_roundtrip` : frame_id/pose intacts + UIIntent renvoyé avec le bon `target_track_id` + UIReceipt jusqu'à `record_delivery_feedback`) ; V18 inchangés. Serveur HTTP SessionHub (`sessionhub_http.py`, port 8710) + signaling unifié `POST /webrtc/offer` (token exigé) réutilisé par `fake_xr_device` et le futur client Android. Plugin Android (`apps/xr-mobile/android/livetransport/`, GetStream **1.3.10** épinglée) + bridge Unity `LiveTransportBridge.cs`. **Compilation Android + validation S25 différées matériel** (pas d'Android SDK ici ; ADR `docs/DECISIONS.md` §E24). Pas de [x] (validation matériel utilisateur).

**E25. SceneCache + UIIntentBroker + UIRuntime.** Sous-caches et TTL (guide V19 §9.1), priorités de rendu (handoff Lot 3), design system liquid glass — chaque composant émet ses `UIReceipt` vers le DataChannel (repris par delivery_adapter E6 pour la voie BrainLive). StatusBar permanente.
Statut : code livré — commit `feat(v19-e25)` — tests : EditMode écrits (exécution à la première ouverture Unity ; pas d'Unity dans cet environnement) ; validation visuelle éditeur + matériel différée. `SceneCache` (6 sous-caches + `SceneCacheConfig`, §9.1) + `UIIntentBroker` (échelle §13.2, TTL, fade track perdu, densité, dédup, `ui_intent_drop_reason` §15.3) déjà mergés ; ce lot ajoute : shader URP `LiquidGlass.shader` (verre translucide + rim d'accent de vérité + grain) alimenté par un flou Kawase dual-filter dans une `ScriptableRendererFeature` RenderGraph (`GlassBlurFeature`, texture globale partagée `_MLOmegaGlassBlur`, fallback translucide plat si absente — ADR §E25) ; `UITheme` (tokens) ; les 10 composants §13.1 (`ObjectOutline`, `PersonTag`, `Subtitle`, `LensWindow`, `OffscreenArrow`, `ContextCard`, `TaskCard`, `VirtualScreen`, `CorrectionChip`, `StatusBar`) sur base `UIComponentBase` (cycle admit→display→fade→recycle + receipts §13.3 : `displayed` à l'affichage, `seen` après dwell prudent, `acted`/`dismissed`/`corrected` ; vérité §17.2 : badge « probable », âge last-seen, étiquette hypothèse, pas de nom sous seuil d'identité, pas de flèche sous seuil de carte) ; `UIRuntime` (mapping composant→type + pooling + ancrage SceneCache + sink) ; `UIReceiptTransportSink` (file bornée `ReceiptOutbox`, flush à la reconnexion, drop du plus ancien) ; `Editor/E25SceneBuilder.cs` (scène démo + `E25DemoDriver` injectant un intent de chaque composant). Tests EditMode : `UIComponentRegistryTests`, `UITruthTests`, `UIReceiptLifecycleTests`, `UIReceiptOutboxTests`. **Compilation Unity + validation visuelle éditeur/S25 différées** (pas d'éditeur Unity ici). Pas de [x] (validation Unity/matériel utilisateur après ouverture).

**E26. Ultra-Live device.** `ReflexScheduler` + skills : StableTrack, LensWindow (zoom gestes), MotionProximity, FocusSearch ; `GesturePipeline` MediaPipe (pincer=zoom, paume=menu, balayage=cacher) ; `AsrKwsService` sherpa-onnx (VAD + zipformer FR/EN + wake word configurable). Test clé : PC coupé → zoom/tracks/gestes/wake word intacts.
Statut : code livré — commits : f027975 (Kotlin reflexvision : MediaPipe gestes + sherpa-onnx ASR/KWS, machine à états JVM-testée) + 9b49201 (couche reflex Unity : scheduler §9.3, 6 skills via broker, LocalTrackStore + TemplateTracker NCC, bridges avec sim éditeur) + tests/docs (ce commit) — tests : JVM Kotlin (GestureStateMachine, KeywordEncoder) + EditMode `ReflexOfflineTests` (test clé offline : transport déconnecté → intents toujours émis ; mapping scheduler ; TemplateTracker sur motif synthétique ; agrégation ReflexEvent avec flush immédiat en critique) — écrits, exécution au premier clic Unity ; compilation Kotlin + validation S25 différées matériel.

**[x] E27. VisionRT + AudioRT PC** (`services/live-pc/visionrt.py`, `audiort.py`). Détecteur ONNX adaptatif 5-15 fps + tracker toutes frames (politique handoff §3.6, cadences en config) ; OCR ROI ; VLM crop un job à la fois via GpuArbiter ; sortie `SceneDelta` liée à `source_frame_id`. AudioRT : VAD + faster-whisper streaming + LID + traduction → `UIIntent subtitle` partiels/finaux sans LLM.
Statut : terminé — commits `feat(v19-e27): ...` (branche `feat/v19-e27-visionrt-audiort`) — tests : **exécutés et verts sur la machine cible** (E27 sans dépendance matériel externe). `tracking.py` ByteTrack maison (2-passes IoU + Kalman) ; `visionrt.py` YOLOX-nano ONNX Apache-2.0 (Megvii, sha256-épinglé, détecteur réel P50 9,9 ms / P95 10,5 ms CPU), cadence adaptative motion-driven (106/300 frames détecteur au bench), OCR ROI rapidocr, VLM crop un-job Ollama (dégradé `vlm_unavailable` testé Ollama éteint), keyframes → `v19_keyframes` (pont E14), SceneDelta liée `source_frame_id` + focus `what_is/find/ocr` → UIIntent §17.2 ; `audiort.py` webrtcvad + faster-whisper small int8 **sur RTX 3070 (device=cuda, ~200-380 ms/segment)** + Argos Translate fr↔en (MIT, sans LLM, fr→en vérifié) + sous-titres réflexe DataChannel direct ; `live_pipeline.py` orchestration + dégradé §3.6 + `/metrics`. GpuArbiter : classe `asr` dédiée ajoutée au plancher réflexe protégé + budgets profil. Tests : `test_tracking`, `test_visionrt` (détection person réelle + cadence + keyframe→`vision_frames`), `test_audiort` (VAD + whisper réel + traduction), `test_e27_pipeline` (fake_xr_device→pipeline→SceneDelta `source_frame_id` cohérent via WebRTC réel + `what_is` dégradé Ollama off). **`pytest tests/v19 -q` = 66/66 verts ; V18 108/108 inchangés.** Bench `--vision` réel dans `docs/BENCH_RESULTS.md`. Modèles fetch via `scripts/fetch_models_v19.py` (models/ git-ignoré), manifest à jour (URL/sha/licence).

**[x] E28. WorldBrain + spatial** (`worldbrain.py`, `spatial.py` impl `SpatialMapProvider` V19.A). Entities/observations/relations/last-seen/ChangeEvents/map_quality ; keyframe selector (E14 branché) ; `brainlive_scene_adapter.py` → HotSceneContext → politique BrainLive existante → `enqueue_delivery` (§2.1, `source_key` = scène+sujet).
Statut : terminé — commits `feat(v19-e28): ...` (branche `feat/v19-e28-worldbrain`) — tests : **exécutés et verts sur la machine cible** (E28 sans dépendance matériel). `worldbrain.py` : promotion track→`WorldEntity` (≥3 obs confirmées ≥ conf 0.35, seuils config ; 1 bbox faible → pas d'entité), `Observation`/`Relation` (on_top_of/near/holds géométriques)/`ChangeEvent` (appeared/disappeared/moved before-after)/`SceneSession`, last-seen avec âge ; persistance sur les **vraies** tables (last-seen+changes → `visual_events_v19` via `store_visual_event` owner-scopé ; résumé session → `scene_session_summaries_v19` ; état courant → `brainlive_world_states`/`vision_scene_observations` via `v19_visual_context`) + SQLite service-local pour la session (aucune table cœur). `spatial.py` `PoseKeyframeMap` V19.A : zones par clustering de poses, bearings relatifs, **map_quality mesurée** (densité×fraîcheur×cohérence), `bearing_to`→None sous seuil (jamais de fausse flèche). `brainlive_scene_adapter.py` : `HotSceneContext` budget dur + omissions traçables ; situations §12.4 (personne connue/objet retrouvé/tâche active) → **`enqueue_delivery` direct** (`decision='notify'`, `source_key` scène+sujet, evidence) → `delivery_adapter` E6. Câblage `live_pipeline.py` (VisionRT→WorldBrain, pose→spatial, transcript→adapter, end_session, métriques `/metrics`). ADR §E28 (promotion, seuils map_quality, choix `enqueue_delivery` vs hot-loop). Tests : `test_e28_worldbrain.py` (12 cas : promotion/rejet bbox faible, last-seen+âge, moved, relations, holds, map_quality basse→bearing None, bearing qualifié, persistance réelle visual_events_v19 + brainlive_world_states, résumé session, scene_adapter→queue `brainlive_intervention_delivery_queue` avec source_key/evidence, budget HotSceneContext). **`pytest tests/v19 -q` = 78/78 verts ; V18 inchangés.** Seuils par défaut : `promote_min_observations=3`, `promote_min_confidence=0.35`, `min_map_quality_for_bearing=0.35`, `hot_budget_chars=4000`.

**[x] E29. Scénarios + capture-only.** Les 16 scénarios contre `simulators/scenarios/` + companion-web d'abord, matériel ensuite ; `OrientationGuard` (rotation IMU) + profil `phone_only` de bout en bout.
Statut : terminé — commits : 240b448 (pack 16 scénarios + runner in-process/webrtc + rotation PC) + ce commit (OrientationGuard Unity, loader de profil §3.5, e2e phone_only, fix WebSocket delivery_adapter) — tests : `test_e29_scenarios` 3/3 (16/16 scénarios PASS contre le vrai pipeline ; profil validé ; phone_only → queue → viewer → receipt en table) ; transport 23/23 après fix. **Périmètre honnête** : chaînes LIVE prouvées en simulation ; la profondeur mémoire/LLM des scénarios dépendants de Brain2/Qwen relève du test final close-day (E30 fusionnée, décision utilisateur). Partie Unity (OrientationGuard) : compilation différée matériel comme E22-E26.

**E30. Checkpoint final.** Gates G1→G8 réels ; benchs P50/P95 publiés (`docs/BENCH_RESULTS.md`) ; session 3h sans fuite VRAM ; doctor `-Full -Xr -Vision -World -Delivery` vert ; tests V18 verts ; démo capture-only sur second téléphone.

---

## 6. Pièges connus (résumé — chacun a déjà coûté une erreur à quelqu'un)

1. `build_active_context` est patché (`install`), l'original est dans `brainlive_v15` → étendre via `_FIELD_TABLE`, jamais re-patcher.
2. `vision_frames` est immuable → `insert_only`, jamais `upsert`.
3. Tout ingest sans `memory_owner_id` explicite → `ScopeError`.
4. Toute preuve d'une table non listée dans les trois dicts de `v18_life_model` → `ScopeError`.
5. Les stages du close-day sont une liste en dur : 4 endroits à modifier (fn, `_status_ok`, `_stage_identifier`, `expected`/`required_stages`).
6. Plusieurs modules importent `runtime_v18_7` (pas `v18_8`) → suivre l'import du module étendu, ne pas « réparer ».
7. `enqueue_delivery` exige `source_key` significatif (clé de dédup) et `decision` dans l'ensemble autorisé.
8. `label_source` de la calibration est contraint par CHECK SQL → `strict_verifier` pour l'outcome watcher.
9. Les champs ajoutés au hot capsule doivent passer par `_measure()`/réduction — une clé hors budget casse le contrat d'omission traçable.
10. `ollama_unload` peut échouer silencieusement → le GpuArbiter re-mesure la VRAM après chaque unload.
11. Le nom réel est `brainlive_active_contexts`, pas `active_contexts` ; les sorties deep vision vont dans `brainlive_deep_vision_observations_v161`, pas `vision_scene_observations`.
12. La table de queue delivery s'appelle `brainlive_intervention_delivery_queue` — ne pas en créer une seconde.
