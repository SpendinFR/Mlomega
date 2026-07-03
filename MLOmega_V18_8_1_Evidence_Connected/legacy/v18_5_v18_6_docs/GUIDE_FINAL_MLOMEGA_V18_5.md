# Guide final MLOmega V18.5 — flux live et révision audio post-stop

## But

V18.5 garde la faible latence de BrainLive mais améliore la source donnée à Brain2 après le stop. Il ne faut pas envoyer les chunks déjà ingérés par BrainLive dans `flow-once` : ce serait un second import parallèle. La passe V18.5 les réanalyse directement **dans leur bundle de scène**.

## Flux canonique

```text
Inbox / Phone Bridge
→ BrainLive : VAD, ASR rapide, première identité vocale, VLM live, fusion
→ stop/close-day
→ V15.14 : bundle multimodal d’une scène
→ V18.5 deep audio : sources brutes appartenant au bundle uniquement
   → préservation des écarts temporels utiles
   → WhisperX + alignement mot à mot + Pyannote
   → SpeechBrain offline et réconciliation avec les hypothèses live
   → conversation Brain2 raffinée, révision active
→ V16.1 deep VLM Qwen : observations visuelles en preuves séparées
→ V13/V14/V17 : épisodes, sous-sujets, causalité, cases/patterns
→ longitudinal / coordination / Life Model / live-ready
→ gate de rétention → purge conditionnelle
```

## Règles qui protègent Brain2

1. **Une seule conversation active par bundle.** Le transcript live demeure consultable pour audit, mais est `superseded` ; le transcript offline devient l’unique texte de dialogue actif.
2. **La vision profonde n’est pas de la parole.** Elle est transmise sous `context_addenda` avec source, date, rôle d’évidence et budget séparé.
3. **Aucune coupe silencieuse.** Les capsules et addenda trop grands deviennent des références omises explicites, jamais des sous-chaînes JSON.
4. **Pas de fallback trompeur.** Un échec WhisperX/Pyannote/SpeechBrain ou un chunk manquant bloque la clôture au lieu de prétendre que le live est une révision profonde.
5. **Les retries restent idempotents.** La révision est identifiée par bundle + manifeste de sources + profil de traitement ; la même source ne crée pas deux conversations actives.

## Installation / contrôle initial

```powershell
.\.venv\Scripts\mlomega-audio.exe init-db
.\.venv\Scripts\mlomega-audio.exe doctor-elite
.\.venv\Scripts\mlomega-audio.exe v18-release-audit --strict --fail
```

Vérifier que `ffmpeg`, WhisperX, Pyannote, SpeechBrain, Ollama et le modèle VLM offline sont réellement disponibles avant une journée réelle.

## Usage quotidien

```powershell
# Démarrer le live
.\.venv\Scripts\mlomega-audio.exe brainlive-start-service --person-id me

# Fin de journée complète
.\.venv\Scripts\mlomega-audio.exe brainlive-stop-service --close-day

# Vérifier les révisions de la journée
.\.venv\Scripts\mlomega-audio.exe brainlive-deep-audio-audit --person-id me --package-date YYYY-MM-DD
.\.venv\Scripts\mlomega-audio.exe brainlive-close-day-status --person-id me --package-date YYYY-MM-DD
```

## Diagnostic ciblé

```powershell
# Rejouer seulement la passe audio sur les bundles d’un jour (pas flow-once)
.\.venv\Scripts\mlomega-audio.exe brainlive-deep-audio-run --person-id me --package-date YYYY-MM-DD

# Audit de production SQLite/configuration
.\.venv\Scripts\mlomega-audio.exe v18-release-audit --strict --fail
```

`--no-deep-audio` existe seulement pour diagnostiquer un problème. Ne l’utilise pas pour la clôture de production : une scène audio doit être raffinée ou explicitement bloquer la purge.
