> **Historique V17 :** ne pas utiliser comme procédure d’installation ou d’arrêt V18.8. Utilise `GUIDE_INSTALL_MLOMEGA_V18_8_RUNTIME.md` et les scripts `RUN_MLOMEGA_V18_8.ps1` / `STOP_MLOMEGA_V18_8.ps1`.

# MLOmega BrainLive Phone Bridge V17 — audio priorité

Addon séparé pour envoyer depuis Android vers le PC sans modifier le cœur MLOmega.

## Logique

- Audio capturé toutes les 3–5 secondes, 4s par défaut.
- Images capturées toutes les 30 secondes.
- GPS capturé toutes les 15 secondes.
- Capture et upload sont séparés.
- Uploaders séparés : audio, image, GPS.
- Une image lente ne bloque jamais l'uploader audio.
- Les fichiers Android sont supprimés après upload HTTP réussi, sauf si `KEEP_SENT_FILES=1`.
- Si le PC, Tailscale ou le réseau tombe, les fichiers restent dans le spool Android et repartent plus tard.

## Dossiers PC V17

Le receiver écrit dans :

```txt
.mlomega_audio_elite\brainlive_inbox\audio
.mlomega_audio_elite\brainlive_inbox\images
.mlomega_audio_elite\brainlive_inbox\transcripts
.mlomega_audio_elite\brainlive_inbox\gps\current.json
```

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

Option post-stop :

```powershell
.\run_brainlive_phone_receiver.ps1 -ProjectRoot "C:\MLOmega" -Token "TON_GROS_SECRET" -Port 8765 -AllowPostStopOnSessionStop
```

## Android

Installer Tailscale, Termux, Termux:API.

Dans Termux :

```bash
chmod +x *.sh
./install_termux_android.sh
nano ~/mlomega_android_config.env
```

Modifier :

```bash
API_BASE="http://IP_TAILSCALE_DU_PC:8765"
TOKEN="TON_GROS_SECRET"
```

Cadence par défaut :

```bash
AUDIO_SECONDS=4
IMAGE_SECONDS=30
GPS_SECONDS=15
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

## Adresse type Maps / lieu

Par défaut : latitude, longitude, précision, `maps_url`.

Pour ajouter une adresse lisible façon Scriptable :

```bash
REVERSE_GEOCODE=1
ADDRESS_SECONDS=300
ADDRESS_MIN_MOVE_METERS=80
```

L'adresse n'est pas demandée toutes les 15 secondes : elle est recalculée toutes les 5 minutes max, ou si déplacement significatif.

## Performance

La priorité est l'audio :

- uploader audio dédié ;
- timeout audio court ;
- pump PC priorise `audio` avant `gps`, `transcript`, `image` ;
- image uploader séparé avec timeout plus long ;
- GPS uploader séparé.

Donc une image lourde peut mettre du temps, mais elle ne bloque pas l'envoi des audios suivants.

## À vérifier avant journée complète

1. `brainlive-start-service` tourne.
2. Receiver PC `/health` répond.
3. Android `status` montre les 6 boucles : audio, image, gps, upload_audio, upload_gps, upload_image.
4. Les dossiers `brainlive_inbox` reçoivent bien audio/images/current.json.
5. Désactiver l'optimisation batterie pour Termux, Termux:API et Tailscale.
