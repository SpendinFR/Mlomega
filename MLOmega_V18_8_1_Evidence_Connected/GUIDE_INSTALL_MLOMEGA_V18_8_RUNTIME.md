# Guide MLOmega V18.8.1 — installer, lancer, arrêter et reprendre

## 1. Installation PC Windows

Place le ZIP V18.8.1 dans `C:\MLOmega`, extrait-le, ouvre **PowerShell administrateur** dans ce dossier puis lance :

```powershell
powershell -ExecutionPolicy Bypass -File .\INSTALL_MLOMEGA_V18_8_WINDOWS.ps1 -HfToken "hf_xxx" -PersonId "me"
```

L’installateur prépare ou vérifie : Python 3.11, FFmpeg, Docker Desktop/WSL2, Qdrant Docker, Ollama, les modèles configurés, `.venv` isolé, Silero VAD, faster-whisper, WhisperX, Pyannote, SpeechBrain, embeddings, reranker, SQLite, Phone Bridge et les probes réelles. Il peut demander un redémarrage pour WSL/Docker puis reprend son état. Il ne doit annoncer `PRODUCTION_READY` qu’après le smoke test complet.

Le pilote NVIDIA Windows doit déjà être opérationnel : `nvidia-smi` doit fonctionner. CUDA est fourni dans la `.venv` par les wheels PyTorch ; INSTALL ne modifie pas un PyTorch global.

## 2. Lancement quotidien

```powershell
cd C:\MLOmega
.\RUN_MLOMEGA_V18_8.ps1 -PersonId me
```

RUN démarre/valide Qdrant, Ollama, le Phone Bridge PC sur `8766`, puis BrainLive. Il attend le heartbeat avant d’afficher `RUN_READY`.

## 3. Réglages de capture Android importants

Dans `~/mlomega_android_config.env` :

```bash
AUDIO_SECONDS=3
IMAGE_SECONDS=25
GPS_SECONDS=30
REVERSE_GEOCODE=1
ADDRESS_SECONDS=300
ADDRESS_MIN_MOVE_METERS=80
POST_SESSION_STOP=1
DRAIN_UPLOADS_ON_STOP=1
DRAIN_UPLOADS_TIMEOUT_SECONDS=180
```

Les chunks audio de 3 s sont recommandés : ils capturent les mots et expressions fines. Le LLM live travaille ensuite sur une fenêtre de contexte, pas sur chaque chunk.

`REVERSE_GEOCODE=1` envoie les coordonnées à Nominatim/OpenStreetMap pour récupérer un lieu lisible. Il faut donc l’activer seulement si cette transmission est acceptable.

## 3bis. Capture Android

Après avoir copié le dossier `MLOmega_Phone_Bridge_V18_8/android` dans Termux :

```bash
cd ~/mlomega_phone_bridge/android
bash install_termux_android.sh
cp mlomega_android_config.env.example ~/mlomega_android_config.env
# édite API_BASE et TOKEN dans ~/mlomega_android_config.env
bash start_mlomega_v18_8_android_capture.sh
```

Pour arrêter proprement et demander le close-day :

```bash
bash stop_mlomega_v18_8_android_capture.sh
```

## 4. Logique live V18.8

- Audio/transcript : priorité immédiate.
- Image : copiée et persistée immédiatement ; le VLM live est évité sur les images identiques ou quasi identiques.
- Une image visuellement différente est analysée dès un silence, lorsque l’audio est vide, ou au plus tard après la fenêtre de partage équitable.
- Un jeu vidéo ou une vidéo peut changer chaque frame : la file live conserve seulement les changements visuels les plus récents. Toutes les images restent disponibles au deep vision post-stop.
- GPS : seul un changement de lieu/libellé/adresse ou un mouvement significatif peut déclencher une nouvelle analyse LLM.

Réglages V18.8 déjà écrits par INSTALL dans `.env` :

```dotenv
MLOMEGA_BRAINLIVE_LLM_MIN_INTERVAL_S=12
MLOMEGA_BRAINLIVE_LLM_AUDIO_WINDOW_S=45
MLOMEGA_BRAINLIVE_LLM_MAX_WINDOW_S=90
MLOMEGA_BRAINLIVE_IMAGE_DHASH_CHANGE_BITS=8
MLOMEGA_BRAINLIVE_IMAGE_LIVE_REFRESH_S=600
MLOMEGA_BRAINLIVE_IMAGE_MIN_VLM_INTERVAL_S=20
MLOMEGA_BRAINLIVE_IMAGE_FORCE_AFTER_S=90
MLOMEGA_BRAINLIVE_IMAGE_QUEUE_TARGET=4
MLOMEGA_BRAINLIVE_MAX_BUNDLE_MINUTES=25
```

Ne modifie ces valeurs qu’après une mesure sur ton PC. Pour une RTX 3070 8 Go, garde `moondream` en live et `qwen3-vl:8b` en deep/post-stop.

## 5. Arrêt / close-day

```powershell
.\STOP_MLOMEGA_V18_8.ps1 -PersonId me
```

Le flux est :

```text
arrêt capture téléphone
→ drain du spool Bridge
→ drain final inbox BrainLive
→ assembly de bundles d’activité (max. 25 min)
→ deep audio WhisperX + Pyannote + SpeechBrain
→ deep vision Qwen-VL sur jusqu’à 12 images représentatives/bundle
→ Brain2
→ longitudinal jour / coordination / Life Model / live-ready
→ manifest de rétention
→ purge seulement si zéro étape pending/retryable/blocked
```

Un arrêt Android avec `-AllowPostStopOnSessionStop` suit le même close-day et cible l’ID explicite de service de la session active.

## 6. Reprise après crash ou timeout

```powershell
.\RESUME_MLOMEGA_V18_8.ps1 -PersonId me
```

RESUME conserve les bundles deep audio, images deep vision et conversations Brain2 déjà terminés. Il reprend seulement les unités `retryable_error`. Une étape `blocked` conserve les raw et explique la cause ; elle ne doit jamais être purgée.

## 7. Interventions : voir et enregistrer le résultat

Afficher la file de notifications live :

```powershell
.\.venv\Scripts\mlomega-audio.exe brainlive-delivery-queue LIVE_SESSION_ID --status queued
```

Enregistrer manuellement que l’intervention a été vue, suivie ou rejetée :

```powershell
.\.venv\Scripts\mlomega-audio.exe brainlive-delivery-feedback DELIVERY_ID --type acted --source dashboard --note "Pause faite"
```

Valeurs possibles : `delivered`, `displayed`, `seen`, `acted`, `dismissed`, `ignored`, `failed`.

Depuis le téléphone/dashboard, envoyer au Bridge :

```powershell
$headers = @{ "X-MLOmega-Token" = $env:MLOMEGA_PHONE_TOKEN }
$body = @{ delivery_id="DELIVERY_ID"; feedback_type="acted"; note="Pause faite" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8766/interventions/feedback" -Headers $headers -ContentType "application/json" -Body $body
```

BrainLive enregistre ce retour, l’attache à `candidate_id`/`delivery_id`, puis Brain2 le reçoit avec les preuves observées après coup. Un retour ne prétend jamais prouver seul une causalité : Brain2 le réconcilie avec la suite des observations.

## 8. Commandes utiles encore disponibles

```powershell
# Santé et diagnostic
.\DOCTOR_MLOMEGA_V18_8.ps1 -Full -Bridge -Delivery
.\.venv\Scripts\mlomega-audio.exe doctor-elite --fail
.\.venv\Scripts\mlomega-audio.exe brainlive-status
.\.venv\Scripts\mlomega-audio.exe brainlive-inbox-status

# Voix
.\.venv\Scripts\mlomega-audio.exe setup-me C:\audios\ma_voix.wav --display-name "Moi" --person-id me
.\.venv\Scripts\mlomega-audio.exe voice-pending
.\.venv\Scripts\mlomega-audio.exe name-voice UNKNOWN_VOICE_001 max --display-name "Max"

# Audio enregistré long
.\.venv\Scripts\mlomega-audio.exe flow-once "C:\audios\conversation_1h.wav" --max-chunk-seconds 600

# Brain2 / mémoire
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-run --person-id me --period day
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-run --person-id me --period week
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-run --person-id me --period month
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-digest --person-id me
.\.venv\Scripts\mlomega-audio.exe v14-ask "Qu’est-ce que je suis en train de refaire comme boucle ?" --person-id me
```

## 9. Audit de purge et interventions V14

```powershell
# Confirmer qu'un close-day a une gate de purge réellement éligible
.\.venv\Scripts\mlomega-audio.exe v18-poststop-cleanup-check RUN_ID --person-id me

# Voir les interventions Brain2/V14 proposées
.\.venv\Scripts\mlomega-audio.exe v14-interventions --person-id me
```


## V18.8.1 — images, silence et bundles

- Chaque image reçue est conservée. Le VLM live peut être sauté pour une frame quasi identique ; cela ne retire jamais la frame du deep vision.
- `MLOMEGA_BRAINLIVE_BUNDLE_DHASH_SPLIT_BITS=14` et `MLOMEGA_BRAINLIVE_PIXEL_SPLIT_MIN_SEPARATION_S=90` sont des indices de changement visuel pour séparer des activités silencieuses au même endroit.
- `MLOMEGA_BRAINLIVE_MAX_BUNDLE_MINUTES=25` évite une scène de plusieurs heures ; Brain2 peut ensuite reconnaître une continuité entre deux bundles voisins.
- Si un bundle contient une image mais que son fichier brut est introuvable au post-stop, le run devient `blocked_visual_evidence_unavailable` : aucune purge n’a lieu.
