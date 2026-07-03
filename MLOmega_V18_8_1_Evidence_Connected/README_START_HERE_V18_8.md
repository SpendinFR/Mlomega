# MLOmega V18.8.1 — capture fine, live maîtrisé, fermeture reprenable

## Ce que V18.8.1 finalise

- Les chunks audio de 3 secondes restent tous capturés, transcrits et conservés.
- Qwen live ne reçoit plus automatiquement chaque micro-chunk : il reçoit une fenêtre de contexte après silence, changement réel de GPS/activité visuelle, ou après une fenêtre audio bornée.
- Le GPS `current.json` inchangé ne peut plus relancer une boucle LLM indéfiniment.
- Chaque image est conservée immédiatement. Le VLM live ne réanalyse pas les images identiques/quasi identiques ; il est planifié pendant un silence, à vide, ou après une attente maximale afin de ne jamais affamer l’audio ni oublier la vision.
- Même au même endroit et sans parole, les activités visuelles différentes créent des bundles séparés. Tout bundle est plafonné à 25 minutes pour rester sous la limite deep audio de 30 minutes.
- Brain2/deep vision ne relit pas toutes les images : il sélectionne jusqu’à 12 keyframes représentatives par bundle, parmi toutes les images brutes conservées.
- Une intervention live conserve sa chaîne `candidate_id → delivery_id → outcome observé → Brain2`. Les observations ultérieures restent la source principale ; un retour explicite reste facultatif, jamais obligatoire.

## Commandes quotidiennes

```powershell
# Démarrer PC + Qdrant + Ollama + Phone Bridge + BrainLive
.\RUN_MLOMEGA_V18_8.ps1 -PersonId me

# Arrêt propre : drain Bridge/inbox → deep audio/vision → Brain2 → close-day → purge seulement si toutes les gates sont vertes
.\STOP_MLOMEGA_V18_8.ps1 -PersonId me

# Après coupure, timeout ou arrêt PC : reprend le même run logique
.\RESUME_MLOMEGA_V18_8.ps1 -PersonId me
```

Les commandes métier historiques (`flow-once`, `setup-me`, `voice-pending`, `v14-ask`, longitudinal day/week/month) restent disponibles dans `mlomega-audio.exe`.
