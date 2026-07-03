> **Historique V17 :** ne pas utiliser comme procédure d’installation ou d’arrêt V18.8. Utilise `GUIDE_INSTALL_MLOMEGA_V18_8_RUNTIME.md` et les scripts `RUN_MLOMEGA_V18_8.ps1` / `STOP_MLOMEGA_V18_8.ps1`.

# MLOmega BrainLive Phone Bridge V17 — parallel pool

Addon séparé pour envoyer depuis Android vers BrainLive V17 sans modifier le cœur MLOmega.

## Logique importante

Le bridge est construit pour éviter le problème principal : une image lourde ou une requête GPS ne doit jamais bloquer l'audio.

Android lance des boucles séparées :

- `capture_audio_loop.sh` : capture audio toutes les 4 secondes par défaut.
- `capture_image_loop.sh` : capture image toutes les 30 secondes par défaut.
- `capture_gps_loop.sh` : GPS toutes les 60 secondes par défaut.
- `upload_audio_loop.sh` : upload audio uniquement.
- `upload_image_loop.sh` : upload images uniquement.
- `upload_gps_loop.sh` : upload GPS uniquement.

Donc le flux n'est pas :

```txt
capture -> upload -> attendre -> recapture
```

mais :

```txt
capture audio en continu  -> spool/audio_pending  -> uploader audio dédié
capture image en continu  -> spool/image_pending  -> uploader image dédié
capture GPS en continu    -> spool/gps_pending    -> uploader GPS dédié
```

Côté PC, le receiver utilise aussi un pool séparé :

- worker audio dédié, 2 workers par défaut ;
- worker image dédié ;
- worker GPS dédié ;
- worker transcript dédié ;
- worker session dédié.

Donc une image qui met du temps à être copiée n'empêche pas les fichiers audio d'arriver dans `brainlive_inbox\audio`. En même temps, les images ne sont pas trop décalées, car elles ont leur propre worker au lieu d'attendre que toute la file audio soit vidée.

## Dossiers V17

Le PC écrit dans :

```txt
.mlomega_audio_elite\brainlive_inbox\audio
.mlomega_audio_elite\brainlive_inbox\images
.mlomega_audio_elite\brainlive_inbox\transcripts
.mlomega_audio_elite\brainlive_inbox\gps\current.json
```

## Cadence par défaut Android

Dans `~/mlomega_android_config.env` :

```bash
AUDIO_SECONDS=4
IMAGE_SECONDS=30
GPS_SECONDS=60
```

Tu peux mettre `AUDIO_SECONDS=3` ou `5`, mais 4 est le meilleur compromis.

## Adresse / Maps

GPS brut toutes les 60 secondes : `lat`, `lon`, `accuracy`, `maps_url`.

Adresse lisible type Scriptable/iPhone facultative :

```bash
REVERSE_GEOCODE=1
ADDRESS_SECONDS=300
ADDRESS_MIN_MOVE_METERS=80
```

Même avec `REVERSE_GEOCODE=1`, l'adresse n'est pas demandée à chaque GPS. Elle est recalculée toutes les 5 minutes max ou si déplacement significatif.

## Stockage Android

Par défaut :

```bash
KEEP_SENT_FILES=0
```

Donc les audios/images/GPS sont supprimés d'Android après succès HTTP réel. Si le PC/Tailscale/réseau tombe, les fichiers restent dans le spool et repartent plus tard.

## PC

Dans `C:\MLOmega` :

```powershell
.\.venv\Scripts\mlomega-audio.exe brainlive-start-service --person-id me
```

Dans une autre fenêtre :

```powershell
cd C:\MLOmega\phone_bridge\pc
.\install_brainlive_phone_receiver.ps1 -ProjectRoot "C:\MLOmega"
.\run_brainlive_phone_receiver.ps1 -ProjectRoot "C:\MLOmega" -Token "TON_GROS_SECRET" -Port 8765
```

Options workers PC :

```powershell
.\run_brainlive_phone_receiver.ps1 `
  -ProjectRoot "C:\MLOmega" `
  -Token "TON_GROS_SECRET" `
  -Port 8765 `
  -AudioWorkers 2 `
  -ImageWorkers 1 `
  -GpsWorkers 1
```

Ne mets pas 10 workers partout : SQLite + disque + antivirus Windows peuvent ralentir. Le réglage par défaut est volontairement simple.

Pour autoriser le post-stop quand Android s'arrête :

```powershell
.\run_brainlive_phone_receiver.ps1 -ProjectRoot "C:\MLOmega" -Token "TON_GROS_SECRET" -Port 8765 -AllowPostStopOnSessionStop
```

## Android

Installer :

- Tailscale
- Termux
- Termux:API

Dans Termux :

```bash
chmod +x *.sh
./install_termux_android.sh
nano ~/mlomega_android_config.env
```

Modifie seulement au début :

```bash
API_BASE="http://IP_TAILSCALE_DU_PC:8765"
TOKEN="TON_GROS_SECRET"
```

Lancer :

```bash
./start_mlomega_v17_android_capture.sh
```

Arrêter :

```bash
./stop_mlomega_v17_android_capture.sh
```

Statut :

```bash
./status_mlomega_v17_android_capture.sh
```

## Vérification prod courte

1. Lance BrainLive.
2. Lance le receiver PC.
3. Lance Android 5 minutes.
4. Vérifie `brainlive_inbox\audio`, `brainlive_inbox\images`, `brainlive_inbox\gps\current.json`.
5. Vérifie `curl http://IP_TAILSCALE_DU_PC:8765/health` depuis Android ou PC.
6. Puis seulement lance 1h ou journée.
