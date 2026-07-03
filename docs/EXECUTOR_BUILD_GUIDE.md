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
Statut : terminé — commit : 42a810a — tests : pytest MLOmega_V18_8_1_Evidence_Connected/tests/test_v18_8_1_evidence_connected.py (6 passed) Créer l'arborescence handoff §3.1 ; copier `MLOmega_V18_8_1_Evidence_Connected` → `src/` + fichiers racine nécessaires ; vérifier que `pytest tests/test_v18_8_1_evidence_connected.py` passe AVANT toute modification (baseline). Geler `runtime_v18_7`/`operations_v18_7` (en-tête « gelé, ne pas diverger de v18_8 »).

**[x] E2. Contrats.**
Statut : terminé — commit : bcbac85 — tests : pytest tests/v19/test_contracts.py (2 passed) `packages/contracts/schemas/*.schema.json` (8 contrats handoff §3.4, champ `contracts_version` partout) → modèles pydantic v2 générés/écrits dans `packages/contracts/python/` → stubs C# dans `csharp/`. Test round-trip python→JSON→python pour chaque contrat. Aucune dépendance vers le cœur ni vers un SDK.

**[x] E3. SessionHub**
Statut : terminé — commit : 2da84fc — tests : pytest tests/v19/test_sessionhub.py (2 passed) (`services/live-pc/sessionhub.py`). Sessions (`session_id` = uuid horodaté, jamais réutilisé), ClockSync (échange de timestamps monotones, offset stocké par session), token de session éphémère émis à l'appairage (remplace le token statique pour le canal XR ; le bridge V18.8 existant garde le sien). Test : deux clients simulés, offsets cohérents.

**[x] E4. VideoIngress + gateway** (`services/live-pc/gateway.py`). Interface `VideoIngress` (async itérateur de `(frame_bgr, FrameEnvelope)`), impl `AiortcIngress`. Queue = 1 : variable « dernière frame » + compteur de drops, jamais de liste. Bench intégré : P50/P95 décodage. Test : `webrtc_frame_queue_bounded`.
Statut : terminé — commit : 47a4190 — tests : `pytest tests/v19 -m "contracts or transport"` (5 passed), `pytest tests/v19` (7 passed).

**[x] E5. fake_xr_device** (`simulators/fake_xr_device.py`). Client aiortc qui rejoue un MP4 + JSONL de pose ; options : fps, perte réseau simulée, rotation 90° (mode capture-only). Produit des `FrameEnvelope` valides (frame_id croissants, monotonic ns).
Statut : terminé — commit : 47a4190 — tests : `pytest tests/v19 -m "contracts or transport"` (5 passed), `pytest tests/v19` (7 passed).

**[x] E6. delivery_adapter** (`services/live-pc/delivery_adapter.py`). Boucle : lit `brainlive_intervention_delivery_queue` (`delivery_status='queued'`, tri priority desc) → convertit en `UIIntent` (`producer='brainlive'`, `component='context_card'` par défaut, `evidence_refs` depuis `evidence_json`, `delivery_id` reporté) → push WebSocket/DataChannel vers renderers connectés → à l'accusé : `record_delivery_feedback(delivery_id=..., feedback_type='delivered', feedback_source='xr_adapter')` puis relaie chaque `UIReceipt` (`displayed/seen/acted/dismissed`) vers la même fonction. Les UIIntent d'UltraLive/VisionRT ne passent **pas** par cette queue (réflexes directs) ; seul BrainLive H1 y passe. Lire `v18_delivery.py` en entier avant (fonction de poll existante à réutiliser si présente).
Statut : terminé — commit : 47a4190 — tests : `pytest tests/v19 -m "contracts or transport"` (5 passed), `pytest tests/v19` (7 passed).

**[x] E7. companion-web** (`apps/companion-web/`). Une page : WebSocket vers delivery_adapter, rendu des UIIntent (cards/sous-titres/contours sur flux optionnel), clic → UIReceipt. Sert de renderer de référence pour tous les tests.
Statut : terminé — commit : 47a4190 — tests : `pytest tests/v19 -m "contracts or transport"` (5 passed), `pytest tests/v19` (7 passed).

**[x] E8. GpuArbiter + degraded** (`services/live-pc/gpu_arbiter.py`, `degraded.py`). NVML (pynvml) : VRAM totale/utilisée par phase ; API `request(job_class) -> grant/deny/preempt` selon priorités handoff §4.1 ; vérification post-`ollama_unload` (re-mesure VRAM, alerte si pas libérée). États dégradés → événements poussés aux renderers (StatusBar).
Statut : terminé — commit : 47a4190 — tests : `pytest tests/v19 -m "contracts or transport"` (5 passed), `pytest tests/v19` (7 passed).

**[x] E9. Scripts + profil.** `INSTALL_MLOMEGA_V19_WINDOWS.ps1` (préflight, `.venv-live`, MODEL_MANIFEST, ne touche pas `.venv`), `setup_profile.ps1` (questions → `configs/user_profile.yaml`, cf. handoff §3.5), `RUN_MLOMEGA_V19.ps1 -SimOnly|-Xr`, `DOCTOR_MLOMEGA_V19.ps1` (ports, GPU, Qdrant, Ollama, contrats, queue delivery, profil), `BENCH_V19.ps1`. Ports V19 : préfixe 87xx hors 8766.
Statut : terminé — commit : 76509be — tests : `pytest tests/v19` (9 passed).

**E10. Checkpoint Lot 1.** `pytest tests/v19 -m "contracts or transport"` vert ; `pytest tests/test_v18_*` vert inchangé ; démo : `RUN -SimOnly` → fake device → UIIntent test → companion-web → receipt visible dans `brainlive_intervention_feedback_events_v188`. Bench ingress consigné dans `docs/BENCH_RESULTS.md`. **Revue avant Lot 2.**

---

## 4. Étapes — LOT 2 (Mémoire profonde)

**[x] E11. Tables V19** (`src/mlomega_audio_elite/v19_visual_store.py`). SCHEMA propre + `ensure_v19_visual_schema()` (pattern §2.8) : `visual_evidence_assets_v19`, `visual_events_v19`, `world_entity_links_v19`, `scene_session_summaries_v19`, `ui_interaction_outcomes_v19` (colonnes : handoff §Lot 2 + toujours `person_id`, `live_session_id`, temps UTC + `created_at`). Puis **enregistrer chaque table de preuve dans `_DIRECT_EVIDENCE_SOURCES`** (format §2.7) — ex. `"visual_events_v19": ("visual_event_id", "person_id", ("occurred_at", "created_at"))`. Test : insertion + `validate_stratum_evidence` accepte une ref vers ces tables.
Statut : terminé — commit : 68a9386 — tests : `pytest tests/v19/test_memory_v19.py -q` (2 passed)

**[x] E12. Endpoints** (`api.py`, style §2.9, additif en fin de fichier) : `/ingest/visual-event` (EvidenceEvent JSON → `visual_events_v19` + asset), `/ingest/scene-summary`, `/memory/correction-visual`, `/xr/session-health`, `/evidence/request-clip`. Chaque payload porte `memory_owner_id` explicite (règle §2.10).
Statut : terminé — commit : 5fd18d805586b0928c8ee5c4c8620aa07aa8af2e — tests : `pytest tests/v19/test_memory_v19.py -q` (3 passed) ; `pytest tests/v19 -m memory -q` (3 passed, 10 deselected).

**E13. MemoryBridge + EvidenceStore** (`services/live-pc/memory_bridge.py`, `evidence_store.py`). Déclencheurs de sélection (handoff §Lot 2) → clip depuis ring buffer/tampon-jour → sha256 → POST `/ingest/visual-event`. Tampon-jour : encodage basse résolution continu, purge au close-day, quota doctor.

**E14. Pont keyframes → chaîne nocturne existante** (le pont central du projet). Le sélecteur de keyframes PC enregistre chaque keyframe : (1) fichier image → `raw_assets` ; (2) ligne `vision_frames` via **`insert_only`** (`capture_mode='xr_keyframe'`, `live_session_id`, `image_sha256`) — cf. §1.3. Vérifier ensuite avec le simulateur que `run_brainlive_event_assembly` intègre ces frames dans `vision_timeline_json` d'un bundle et que `run_offline_deep_vision_for_bundles` les analyse (si l'assembleur ne lit pas `vision_frames` pour la timeline vision, lire `collect_live_raw_timeline` et brancher au bon endroit — ADR obligatoire). Test : session simulée → bundle → deep vision → `brainlive_deep_vision_observations_v161` non vide.

**E15. Nouvelles phases close-day** (`v18_close_day.py`, pattern exact §2.11 — seule modification autorisée de ce fichier). Après `post_stop`, avant `longitudinal` : stage `visual_consolidation` (module `v19_visual_consolidation.py` : ChangeEvents WorldBrain → `visual_events_v19` ; résumés session → `scene_session_summaries_v19` ; purge tampon-jour après extraction). Après `life_model` : stages `outcome_resolution`, `prediction_emission`, `self_schema` (E16-E18). Chaque stage ajouté dans `_status_ok()`, `_stage_identifier()`, `expected`, `required_stages`. Test : `brainlive-close-day` complet sur données simulées, `close_day_status` liste les 9 stages `completed` ; relance = tous `resumed_stage`.

**E16. Outcome watcher** (`v19_outcome_watcher.py`). Prédictions ouvertes (avec `verification_spec`) × preuves du jour (transcripts, `visual_events_v19`, GPS, routines) → résolution `verified/refuted/expired/unverifiable` + evidence_refs de résolution → écrit `prediction_outcomes_v19` ; alimente la calibration via `register_verified_similarity_label(..., label_source='strict_verifier')` (contrainte §2.6 : le cas similaire doit être antérieur à l'ancre) ; appelle `auto_verify_latent_outcome_predictions` pour la voie conversationnelle existante. Échantillon d'audit journalisé.

**E17. Prediction emission + Life Model durable** (`v19_prediction_loop.py`, `v19_life_model_store.py`). Life Model V19 = magasin d'entrées typées (handoff Lot 2 : dimensions × axes temporels, statuts `active/weakening/contradicted/superseded`, historique). Mise à jour = deltas LLM en 3 étapes contractées (réutiliser `llm_contracts_v15_18`), appliquées par le store — jamais de régénération complète. L'updater V15.13 existant continue de tourner (stage `life_model`) ; le store V19 le complète, il ne le remplace pas (ADR si conflit). Émission : 3-7 prédictions avec `verification_spec`, pénalité si invérifiable.

**E18. Self schema** (`v19_self_schema.py`). Projection depuis life model store + patterns confirmés + `causal_edges` + `prediction_outcomes_v19` → table `self_schema_v19` (entrées : type aime/veut/a_fait/causal/conditionnel, evidence_refs, taux d'occurrence). Endpoint `GET /self-schema` + projection compacte dans le hot capsule (E19).

**E19. Hot capsule + contexte visuel.** (1) `v19_visual_context.py` : pousse l'état WorldBrain courant dans `brainlive_world_states` (schéma §2.8) et les observations dans `vision_scene_observations` — le wrapper `v18_context` les reprend automatiquement ; ajouter les champs nouveaux (`self_schema_hot`, `scene_focus`) dans `_FIELD_TABLE`. (2) Extension `v18_hot_capsule` : champs additifs comptabilisés par `_measure()` et réductibles (§2.4). Test : hot capsule avec scène simulée respecte le budget et journalise les omissions.

**E20. Vie synthétique** (`simulators/synthetic_life.py`) : 30 jours générés (routines, déplacements, objets, rencontres, conversations) injectés par les endpoints → close-day par jour → au moins une routine détectée, une prédiction `verified`, une `refuted`, un pattern conditionnel dans le self schema. C'est le test d'acceptation du lot.

**E21. Checkpoint Lot 2.** `pytest tests/v19 -m memory` vert ; tests V18 verts ; close-day complet < 6h réelles sur RTX 3070 (données synthétiques) avec journal `gpu_phase` ; doctor `-Memory` vert. **Revue avant Lot 3.**

---

## 5. Étapes — LOT 3 (Live/XR/mobile)

**E22. Gate G1 matériel (peut démarrer dès la fin du Lot 1, en parallèle du Lot 2).** Unity 6 LTS + XREAL SDK 3.1.0, sample officiel sur S25 réel : Eye RGB, pose, rendu stéréo, permissions (`RECORD_AUDIO`, `FOREGROUND_SERVICE_MEDIA_PROJECTION`), coupure/reprise. Si la caméra Eye est inaccessible : plan B `one-xr` (pose) + caméra S25 (même pipeline), ADR, et continuer.

**E23. App Unity noyau.** `XRDeviceAdapter` (interface C#) + `XrealDeviceAdapter`, `SimulatedDeviceAdapter`, `PhoneOnlyAdapter` ; `XrSessionController`, `EyeCaptureSource` (frame_id + monotonic), `PosePublisher`, `ClockSync` (protocole E3).

**E24. Transport mobile.** Plugin Kotlin `LiveTransportPlugin` (GetStream webrtc-android) : H.264 low-latency + Opus 20 ms + DataChannel fiable/ordonné (contrats E2 sérialisés JSON), reconnexion, bitrate adaptatif. Valider contre le gateway E4 : frame_id/pose intacts côté PC, UIIntent retour affiché sur le bon track.

**E25. SceneCache + UIIntentBroker + UIRuntime.** Sous-caches et TTL (guide V19 §9.1), priorités de rendu (handoff Lot 3), design system liquid glass — chaque composant émet ses `UIReceipt` vers le DataChannel (repris par delivery_adapter E6 pour la voie BrainLive). StatusBar permanente.

**E26. Ultra-Live device.** `ReflexScheduler` + skills : StableTrack, LensWindow (zoom gestes), MotionProximity, FocusSearch ; `GesturePipeline` MediaPipe (pincer=zoom, paume=menu, balayage=cacher) ; `AsrKwsService` sherpa-onnx (VAD + zipformer FR/EN + wake word configurable). Test clé : PC coupé → zoom/tracks/gestes/wake word intacts.

**E27. VisionRT + AudioRT PC** (`services/live-pc/visionrt.py`, `audiort.py`). Détecteur ONNX adaptatif 5-15 fps + tracker toutes frames (politique handoff §3.6, cadences en config) ; OCR ROI ; VLM crop un job à la fois via GpuArbiter ; sortie `SceneDelta` liée à `source_frame_id`. AudioRT : VAD + faster-whisper streaming + LID + traduction → `UIIntent subtitle` partiels/finaux sans LLM.

**E28. WorldBrain + spatial** (`worldbrain.py`, `spatial.py` impl `SpatialMapProvider` V19.A). Entities/observations/relations/last-seen/ChangeEvents/map_quality ; keyframe selector (E14 branché) ; `brainlive_scene_adapter.py` → HotSceneContext → politique BrainLive existante → `enqueue_delivery` (§2.1, `source_key` = scène+sujet).

**E29. Scénarios + capture-only.** Les 16 scénarios contre `simulators/scenarios/` + companion-web d'abord, matériel ensuite ; `OrientationGuard` (rotation IMU) + profil `phone_only` de bout en bout.

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
