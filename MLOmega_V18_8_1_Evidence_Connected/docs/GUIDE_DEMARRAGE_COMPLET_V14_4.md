# Guide de démarrage complet — V14.4 Brain 2.0 Final

## Installation rapide Windows

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[test,llm]
mlomega-audio init-db
mlomega-audio doctor-elite
```

Installe aussi localement selon ton choix/config : FFmpeg, Ollama/Qwen, Qdrant local ou LanceDB, WhisperX, pyannote + token HuggingFace accepté.

## Première voix : toi

```powershell
mlomega-audio setup-me C:\audios\ma_voix.wav --display-name "Will / Moi"
```

## Lancement autonome

```powershell
mlomega-audio flow-watch --poll-seconds 60
```

Dépose ensuite les audios dans :

```text
.mlomega_audio_elite\inbox\audio
```

et les transcripts JSON dans :

```text
.mlomega_audio_elite\inbox\transcripts
```

## Ce que flow-watch déclenche

```text
ingest audio/transcript
→ sync vectoriel incrémental
→ V13 strict build
→ subtopics
→ latent outcomes
→ V14.4 auto verification bridge
→ V13.4 autonomous insights
→ V14 Pattern Mirror
→ V14.3 scheduler hour/day/week/month
→ export self-model Markdown/JSON
```

## Usage normal

```powershell
mlomega-audio v14-ask "Qu'est-ce que je suis en train de refaire comme boucle ?" --person-id me
mlomega-audio v14-ask "Qu'est-ce que j'avais dit à Max à propos de la TV ?" --person-id me
mlomega-audio v14-self-model --person-id me
mlomega-audio export-self-model --person-id me --format markdown
```

## Audits

```powershell
mlomega-audio v13-audit-plan
mlomega-audio v14-audit
mlomega-audio v14-1-audit
mlomega-audio v14-2-audit
mlomega-audio v14-3-audit
mlomega-audio v14-4-audit
```

## Commandes restées manuelles volontairement

- `setup-me` / `enroll-voice` : identité, donc jamais devinée automatiquement.
- `name-voice` : le système ne doit pas inventer le nom d'une voix inconnue.
- `memory-revise` : correction humaine explicite.
- `sync-vectors --full` : réparation/rebuild complet, pas flux normal.


# Addendum V14.5 — Identité relationnelle + désirs/questions/blocages

La V14.5 est intégrée automatiquement à `flow-watch`. Après chaque conversation, le système peut créer des hypothèses pending sur les personnes/voix inconnues et suivre les désirs, questions, incompréhensions, blocages, solutions candidates et prochaines actions.

Commandes utiles :

```powershell
mlomega-audio v14-5-audit
mlomega-audio v14-people-hypotheses
mlomega-audio v14-open-loops --person-id me
mlomega-audio v14-5-run <conversation_id> --person-id me
```

Important : la V14.5 ne confirme jamais seule qu'une voix inconnue est Max ou quelqu'un d'autre. Elle propose une hypothèse avec preuves/contre-preuves. La confirmation reste `name-voice` ou `enroll-voice`.


## V14.6 — Miroir interpersonnel complet

La V14.6 ajoute le modèle des autres et le couplage émotionnel : état probable d'une personne à l'instant T, effet de son humeur sur William, effet de William sur elle, micro-interactions, aftereffects sur l'heure/jour, boucles relationnelles et interventions.

Commandes utiles :

```powershell
mlomega-audio v14-6-audit
mlomega-audio v14-people-models --person-id me
mlomega-audio v14-social-aftereffects --person-id me
```

`flow-watch` appelle automatiquement V14.6 après V14.5. Le self-model exporté contient aussi la section `interpersonal_state_mirror`.
