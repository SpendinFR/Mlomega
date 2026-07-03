# Guide MLOmega V18.7 — installation, lancement et reprise

> Utilise uniquement les scripts V18.7 à la racine. Le profil de production est `CORE_BRAINLIVE_V18_7_PHONE` : Qdrant + Ollama + BrainLive + Phone Bridge, sans Neo4j, Graphiti ni Mem0.

## Installation

Dans PowerShell **administrateur**, depuis le dossier dézippé :

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -HfToken "hf_xxx" -PersonId "me"
```

Le token HF doit avoir accepté les modèles Pyannote gated. L’installateur construit `.venv.new`, installe les versions verrouillées (dont Silero VAD), puis bascule atomiquement seulement après les probes réelles. Seul `PRODUCTION_READY` est un succès.

Si Windows demande un reboot, laisse la tâche de reprise au logon s’exécuter. À défaut :

```powershell
.\INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -ResumeAfterReboot -PersonId "me"
```

## Vérification PC

```powershell
.\DOCTOR_MLOMEGA_V18_7.ps1 -Full -Bridge
```

Le test de livraison Bridge réel est optionnel et dépose un fixture dans l’inbox :

```powershell
.\DOCTOR_MLOMEGA_V18_7.ps1 -Full -Bridge -Delivery
```

## Téléphone Android — première configuration

```powershell
.\EXPORT_PHONE_CONFIG_V18_7.ps1
```

Copie la configuration générée dans le Bridge/Termux, puis accorde les permissions Android micro, fichiers/Termux, réseau et arrière-plan. Ces permissions ne peuvent pas être automatisées depuis Windows.

## Commandes quotidiennes

```powershell
# Démarrer les services, le Bridge et BrainLive.
.\RUN_MLOMEGA_V18_7.ps1 -PersonId me

# Arrêter proprement, drainer, faire le post-stop et fermer la journée.
.\STOP_MLOMEGA_V18_7.ps1 -PersonId me

# Reprendre après crash, timeout, arrêt PC ou service temporairement indisponible.
.\RESUME_MLOMEGA_V18_7.ps1 -PersonId me
```

`RUN_READY` n’est affiché qu’après un heartbeat BrainLive frais. Ne lance pas un second RUN si un état de reprise existe : utilise RESUME.

## Après un crash

`RESUME` garde le même run logique jour/session. Il saute les bundles deep audio, images VLM et conversations Brain2 déjà terminés et ne relance que la première unité incomplète. Les raw restent conservés tant qu’un stage est `pending`, `retryable_error` ou `blocked`.

Si le statut est `blocked`, corrige la cause déclarée (token HF, raw absent, disque, modèle, contrat), puis lance RESUME. Ne supprime pas les raw, la base SQLite ou les manifests manuellement.

## Diagnostics utiles

```powershell
.\.venv\Scripts\python.exe -m mlomega_audio_elite.cli brainlive-runtime-status
.\.venv\Scripts\python.exe -m mlomega_audio_elite.cli brainlive-recovery-status --person-id me
.\.venv\Scripts\python.exe -m mlomega_audio_elite.cli brainlive-post-stop-flow-audit --person-id me
.\.venv\Scripts\python.exe -m mlomega_audio_elite.cli voice-pending
.\.venv\Scripts\python.exe -m mlomega_audio_elite.cli v14-clarifications --person-id me
Get-Content .\.mlomega_audio_elite\runtime\logs\brainlive.err.log -Tail 120
Get-Content .\.mlomega_audio_elite\runtime\logs\phone-bridge.err.log -Tail 120
```
