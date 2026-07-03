# Guide MLOmega V18.7.1 - installation, lancement et reprise

> Profil production : `CORE_BRAINLIVE_V18_7_PHONE` = Qdrant + Ollama + BrainLive + Phone Bridge. Neo4j, Graphiti et Mem0 ne font pas partie de ce profil.

## 1. Installation PC - une commande

Dans PowerShell **administrateur**, dans `C:\MLOmega` :

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -HfToken "hf_xxx" -PersonId me
```

INSTALL installe ou démarre Python 3.11, FFmpeg, Docker Desktop/WSL2 si nécessaire, Qdrant, Ollama, puis les modèles et le `.venv` MLOmega. Les packages GPU utilisent les wheels PyTorch CUDA 12.1 dans `.venv`; le pilote NVIDIA reste un prérequis Windows.

L'installation valide Qdrant, Ollama, Silero VAD, faster-whisper, WhisperX, Pyannote, SpeechBrain, les embeddings/reranker, les trois modèles Ollama et un smoke test Bridge isolé. Elle doit terminer uniquement par `PRODUCTION_READY`.

Si Windows demande un redémarrage, laisse la reprise au logon s'exécuter. Sinon :

```powershell
.\INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -ResumeAfterReboot -PersonId me
```

## 2. Flux quotidien - les commandes recommandées

```powershell
# Démarre Qdrant, Ollama, Phone Bridge et BrainLive; attend un heartbeat.
.\RUN_MLOMEGA_V18_7.ps1 -PersonId me

# Arrêt complet sûr de la journée.
.\STOP_MLOMEGA_V18_7.ps1 -PersonId me

# Après crash, timeout ou coupure du PC.
.\RESUME_MLOMEGA_V18_7.ps1 -PersonId me

# Voir le digest une fois la clôture terminée.
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-digest --person-id me
```

`STOP` effectue exactement :

```text
drain Bridge -> drain final inbox BrainLive -> arrêt session
-> post-stop : assembly -> deep audio WhisperX/Pyannote/SpeechBrain
-> deep vision VLM -> Brain2 V13/V14
-> longitudinal jour -> coordination V15.12 -> Life Model V15.13
-> live-ready V15.9 -> manifests et cleanup gate -> purge phone_* seulement si tout est completed
```

L'arrêt Android garde le même parcours : le téléphone arrête les capteurs, vide son spool local, appelle `/session/stop`, puis le Bridge déclenche le même close-day. Le Bridge est désormais lié à l'ID exact de la session active : en cas de manifeste ambigu, il échoue fermé et ne purge rien.

## 3. Compatibilité avec tes anciennes commandes

Les commandes métier restent disponibles. Les wrappers V18.7 sont recommandés pour la capture quotidienne car ils conservent les identifiants de session et empêchent les services concurrents.

```powershell
# Toujours disponible, mais en premier plan : préfère RUN au quotidien.
.\.venv\Scripts\mlomega-audio.exe brainlive-start-service --person-id me

.\.venv\Scripts\mlomega-audio.exe brainlive-status
.\.venv\Scripts\mlomega-audio.exe brainlive-inbox-status

# Fonctionne encore, mais STOP est plus sûr car il transmet le service_run_id exact.
.\.venv\Scripts\mlomega-audio.exe brainlive-stop-service --close-day

# Longitudinal manuel (jour est déjà inclus dans STOP; week/month restent manuels).
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-run --person-id me --period day
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-run --person-id me --period week
.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-run --person-id me --period month

.\.venv\Scripts\mlomega-audio.exe brain2-longitudinal-digest --person-id me
.\.venv\Scripts\mlomega-audio.exe v14-ask "Qu'est-ce que je suis en train de refaire comme boucle ?" --person-id me
.\.venv\Scripts\mlomega-audio.exe v14-interventions --person-id me
.\.venv\Scripts\mlomega-audio.exe v14-clarifications --person-id me
.\.venv\Scripts\mlomega-audio.exe v14-answer Q_ID "Oui, c'est Max, mon frère" --person-id me
.\.venv\Scripts\mlomega-audio.exe voice-pending
.\.venv\Scripts\mlomega-audio.exe name-voice UNKNOWN_VOICE_001 max --display-name "Max"
.\.venv\Scripts\mlomega-audio.exe setup-me C:\audios\ma_voix.wav --display-name "Moi" --person-id me
.\.venv\Scripts\mlomega-audio.exe flow-once "C:\audios\conversation_1h.wav" --max-chunk-seconds 600
```

`setup-me` est toujours possible mais plus obligatoire avant la première journée. Le système démarre avec clusters inconnus persistants puis tu peux nommer/enrôler les voix volontairement.

`doctor-elite` est gardé comme alias de compatibilité : dans le profil V18.7 il appelle désormais le doctor core et ne réclame plus Neo4j/Graphiti/Mem0. Pour un contrôle complet :

```powershell
.\DOCTOR_MLOMEGA_V18_7.ps1 -Full -Bridge
.\DOCTOR_MLOMEGA_V18_7.ps1 -Full -Bridge -Delivery
.\.venv\Scripts\mlomega-audio.exe v18-release-audit --strict --fail
```

## 4. Après erreur ou crash

```powershell
.\RESUME_MLOMEGA_V18_7.ps1 -PersonId me
```

RESUME reprend le même run logique. Un bundle deep audio, une image VLM ou une conversation Brain2 déjà `completed` n'est pas rejoué. Seule l'unité interrompue repart depuis son propre début. `retryable_error` se reprend; `blocked` conserve toutes les sources jusqu'à correction de la cause.

## 5. Téléphone Android

Après `EXPORT_PHONE_CONFIG_V18_7.ps1`, copie le fichier généré dans Termux, accorde les permissions Micro/Fichiers/Réseau/Arrière-plan, puis utilise les scripts Android du Bridge. L'option historique `-AllowPostStopOnSessionStop` est toujours active quand le Bridge est lancé par RUN : l'arrêt Android enclenche le close-day complet, sans purge anticipée.
