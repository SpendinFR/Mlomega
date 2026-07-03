> **Historique V17 :** ne pas utiliser comme procédure d’installation ou d’arrêt V18.8. Utilise `GUIDE_INSTALL_MLOMEGA_V18_8_RUNTIME.md` et les scripts `RUN_MLOMEGA_V18_8.ps1` / `STOP_MLOMEGA_V18_8.ps1`.

# MLOmega BrainLive Phone Bridge V17.5 — Quality Capture Final

Objectif : envoyer depuis Android des chunks audio/images/GPS vers le PC MLOmega V17.4.2+ avec priorité à la qualité exploitable par Whisper, SpeechBrain, Moondream live et le gros VLM Brain2.

## Flux

```text
Android Termux
→ audio chunks 3-5s qualité voix
→ images scène compressées intelligemment
→ GPS/current
→ upload prioritaire audio
→ receiver PC
→ .mlomega_audio_elite/brainlive_inbox/audio|images|gps
→ BrainLive live
→ Brain2 post-stop
→ purge audio/images après Brain2 OK
```

## Audio

Profil par défaut :

```env
AUDIO_FORMAT=wav
AUDIO_SECONDS=4
AUDIO_SAMPLE_RATE=16000
AUDIO_CHANNELS=1
```

Le script tente un vrai WAV/PCM et vérifie l'en-tête `RIFF/WAVE`. Si le Termux:API du téléphone ne produit pas de vrai WAV, il peut fallback en AAC haute qualité 256 kbps pour ne pas perdre la capture. Pour refuser tout fallback, mets :

```env
AUDIO_REQUIRE_WAV=1
```

Côté PC, l'audio est livré dans :

```text
.mlomega_audio_elite/brainlive_inbox/audio/phone_....wav
.mlomega_audio_elite/brainlive_inbox/audio/phone_....wav.json
```

Le sidecar contient `timestamp_start`, `timestamp_end`, `sample_rate_hz`, `channels`, `sha256`, source Android et GPS courant si disponible.

## Image

Profil par défaut :

```env
IMAGE_SECONDS=25
IMAGE_OPTIMIZE=1
IMAGE_MAX_DIM=1600
IMAGE_JPEG_QUALITY=88
```

C'est fait pour garder assez de détails pour Moondream en live et Qwen/Qwen-VL en offline, sans envoyer les pleins 12/48 MP du téléphone toutes les 25 secondes.

Les images sont livrées avec sidecar :

```text
.mlomega_audio_elite/brainlive_inbox/images/phone_....jpg
.mlomega_audio_elite/brainlive_inbox/images/phone_....jpg.json
```

Le sidecar contient `captured_at`, `timestamp_start`, `gps`, `camera_id`, `max_dim_px`, `jpeg_quality`, etc.

## GPS

Le GPS est envoyé séparément et écrit :

```text
.mlomega_audio_elite/brainlive_inbox/gps/current.json
```

Le receiver attache aussi le GPS courant aux sidecars audio/image au moment de la livraison. Cela permet de relier audio + image + lieu même si les uploads arrivent en parallèle.

## Stop / post-stop / purge

Le script Android stoppe d'abord les captures, laisse les uploaders vider le spool local, puis appelle `/session/stop`.

Côté PC, si le receiver est lancé avec `-AllowPostStopOnSessionStop`, il fait :

```text
drain queue PC
→ brainlive-post-stop-flow --person-id me --force
→ brain2-longitudinal-run --person-id me --period day
→ purge des fichiers phone_* audio/images si post-stop OK
```

La purge ne touche pas `gps/current.json`, les DB, transcripts, observed cases, patterns, ni les logs. Elle supprime les raw audio/images et leurs petits sidecars du dossier `brainlive_inbox` après consolidation réussie.

Pour désactiver la purge :

```powershell
.\pc\run_brainlive_phone_receiver.ps1 -Token "..." -NoCleanupAfterPostStop
```

Pour tester sans supprimer :

```powershell
.\pc\run_brainlive_phone_receiver.ps1 -Token "..." -CleanupDryRun
```

Endpoint manuel :

```powershell
curl -X POST -H "X-MLomega-Token: TON_SECRET" http://PC:8765/cleanup-media
```

## Installation Android

```bash
cd android
bash install_termux_android.sh
nano ~/mlomega_android_config.env
bash start_mlomega_v17_android_capture.sh
```

Stop propre :

```bash
bash stop_mlomega_v17_android_capture.sh
```

## Lancement PC

```powershell
cd pc
.\install_brainlive_phone_receiver.ps1 -ProjectRoot C:\MLOmega
.\run_brainlive_phone_receiver.ps1 -Token "TON_GROS_SECRET" -ProjectRoot C:\MLOmega -AllowPostStopOnSessionStop
```

## Vérifications

Android :

```bash
bash status_mlomega_v17_android_capture.sh
```

PC :

```powershell
curl -H "X-MLomega-Token: TON_GROS_SECRET" http://127.0.0.1:8765/status
```
