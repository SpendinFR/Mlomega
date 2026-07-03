# MLOmega V18.5 — projet cœur

## Ce que V18.5 ajoute

Le flux BrainLive garde le temps réel rapide pendant la journée. À la clôture, le post-stop ajoute désormais une étape **deep audio** avant Brain2 :

```text
audio téléphone → VAD/ASR/SpeechBrain live → turns rapides
→ V15.14 assemble les scènes/bundles
→ V18.5 réunit les chunks appartenant à chaque bundle
→ WhisperX large-v3 + alignement mot à mot + Pyannote offline
→ conversation Brain2 raffinée et versionnée
→ deep vision / silent life / V13-V14-V17
→ longitudinal → coordination → Life Model → live-ready → cleanup gate
```

Ce n'est pas `flow-once` : aucun second import parallèle n'est créé. La conversation live issue du bundle reste traçable mais devient `superseded`; Brain2 consomme une nouvelle conversation raffinée, liée au même bundle et aux mêmes fichiers bruts.

## Lancer / fermer

```powershell
# Lance BrainLive
.\.venv\Scripts\mlomega-audio.exe brainlive-start-service --person-id me

# Ferme la journée complète. Le deep audio post-stop est inclus par défaut.
.\.venv\Scripts\mlomega-audio.exe brainlive-stop-service --close-day
```

Le deep audio requiert : `ffmpeg`, `MLOMEGA_ENABLE_WHISPERX=true`, `MLOMEGA_ENABLE_PYANNOTE=true`, un token Hugging Face valide et l'acceptation des modèles Pyannote. Si une scène contient de l'audio mais que son chunk brut manque ou que WhisperX/Pyannote échoue, la clôture échoue explicitement : aucune purge ne doit partir.

## Vérifier le résultat

```powershell
.\.venv\Scripts\mlomega-audio.exe brainlive-deep-audio-audit --person-id me --package-date 2026-06-21
.\.venv\Scripts\mlomega-audio.exe brainlive-close-day-status --person-id me --package-date 2026-06-21
.\.venv\Scripts\mlomega-audio.exe v18-release-audit --strict --fail
```

## Diagnostic seulement

Pour isoler un problème sans lancer l'étape audio profonde :

```powershell
.\.venv\Scripts\mlomega-audio.exe brainlive-post-stop-flow --person-id me --no-deep-audio
```

Cette option ne doit pas être utilisée pour une clôture de production si tu veux la réanalyse audio. Ne donne jamais les mêmes chunks de BrainLive à `flow-once`; garde `flow-once` pour les imports audio externes/anciens qui ne sont pas passés dans BrainLive.
