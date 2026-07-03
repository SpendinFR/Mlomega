# Guide de démarrage complet — MemoryLight Omega Audio Elite V13.3 Brain 2.0

Ce guide part de l'objectif réel du projet : audio/transcript 24/24 → mémoire prouvée → épisodes → modèle dynamique → prédictions → vérification → correction.

## 0. Ce que la V13.3 fait automatiquement

Quand un audio ou transcript arrive dans le flux direct, le système peut enchaîner :

```text
audio/transcript
→ ingestion brute
→ transcription / diarisation si audio
→ identification vocale me + autres voix connues/inconnues
→ stockage source_spans / turns / preuves
→ V13 strict Qwen
→ épisodes / situations / sous-sujets
→ états internes / pensées probables / speech acts
→ actions / choix / outcomes
→ similarités avec les anciens cas
→ patterns / contradictions / relations
→ prédictions / simulations / interventions
→ recherche d'indices dispersés qui vérifient d'anciennes prédictions ou intentions
```

Important : le brut est conservé. Les analyses sont des hypothèses ou des consolidations, jamais un remplacement du réel.

## 1. Prérequis Windows

### 1.1 Installer Python

Installe Python 3.11 ou 3.12 64-bit. Coche **Add Python to PATH**.

### 1.2 Installer FFmpeg

FFmpeg doit être dans le PATH. Il sert à préparer les audios longs, retirer les longs silences et créer des chunks plus faciles à transcrire.

Vérifie :

```powershell
ffmpeg -version
```

### 1.3 Installer Ollama + Qwen

Installe Ollama, puis :

```powershell
ollama pull qwen3:8b
```

Vérifie :

```powershell
ollama list
```

### 1.4 HuggingFace / pyannote

Pour la diarisation des speakers, il faut un token HuggingFace et accepter les modèles pyannote concernés sur HuggingFace.

Dans `.env` :

```powershell
MLOMEGA_HF_TOKEN=hf_xxxxxxxxxxxxxxxxx
```

### 1.5 GPU RTX 3070

Lance l'installation Windows fournie :

```powershell
scripts\windows_install_all.ps1
```

Puis :

```powershell
mlomega-audio doctor-elite
```

## 2. Configuration `.env`

Copie :

```powershell
copy .env.windows.example .env
```

Réglages recommandés :

```text
MLOMEGA_ENABLE_OLLAMA=true
MLOMEGA_OLLAMA_MODEL=qwen3:8b
MLOMEGA_ENABLE_WHISPERX=true
MLOMEGA_ENABLE_PYANNOTE=true
MLOMEGA_ENABLE_SPEECHBRAIN=true
MLOMEGA_REQUIRE_SELF_VOICE=true
MLOMEGA_VOICE_LEARNING_STRICT=true
MLOMEGA_WHISPERX_DEVICE=cuda
MLOMEGA_WHISPERX_COMPUTE_TYPE=float16
MLOMEGA_WHISPERX_BATCH_SIZE=8
MLOMEGA_VOICE_THRESHOLD=0.72
```

## 3. Initialiser la base

```powershell
mlomega-audio init-db
mlomega-audio v13-audit-plan
```

## 4. Setup obligatoire : ta voix = `me`

Avant les audios 24/24, enregistre un fichier de ta voix seul, propre, idéalement 30 à 90 secondes.

Puis :

```powershell
mlomega-audio setup-me C:\chemin\ma_voix.wav --display-name "Will / Moi"
```

Cela crée :

```text
person_id = me
display_name = Will / Moi
is_user = true
voice_embedding = empreinte de ta voix
```

Ensuite, à chaque audio :

```text
SPEAKER_00 → empreinte vocale → match me → person_id=me → is_user=true
```

Si ta voix n'est pas configurée et `MLOMEGA_REQUIRE_SELF_VOICE=true`, l'ingestion audio échoue au lieu de te laisser en UNKNOWN.

## 5. Apprentissage actif des autres voix

Pendant les conversations, le système compare les voix à celles connues.

Si la voix est connue :

```text
SPEAKER_01 → Max
```

Si elle est inconnue :

```text
SPEAKER_01 → UNKNOWN_VOICE_003
```

Le système regroupe cette voix dans le temps. Quand elle revient souvent, il ouvre une question :

```powershell
mlomega-audio voice-pending
```

Exemple de réponse :

```text
UNKNOWN_VOICE_003 — 5 observations — 7 minutes
```

Tu peux la nommer :

```powershell
mlomega-audio name-voice UNKNOWN_VOICE_003 max --display-name "Max"
```

Le système :

```text
UNKNOWN_VOICE_003 → Max
met à jour speaker_profiles
ajoute une empreinte vocale connue pour Max
corrige les turns/source_spans/lifestream/memory_cards/retrieval_chunks concernés
conserve les speaker_label bruts
crée une révision de modèle
```

Si c'était toi :

```powershell
mlomega-audio name-voice UNKNOWN_VOICE_003 me --display-name "Will / Moi" --is-user
```

## 6. Audios longs, silences et chunks

Pour un audio d'une heure, le flux direct peut :

```text
garder le brut
créer une version de travail sans longs silences
couper en chunks de 15 minutes par défaut
transcrire chaque chunk
```

Test manuel :

```powershell
mlomega-audio preprocess-audio C:\audios\long.wav --max-chunk-seconds 900
```

## 7. Flux direct : fichier arrive → moteurs lancés

Dossiers par défaut :

```text
.mlomega_audio_elite\inbox\audio
.mlomega_audio_elite\inbox\transcripts
.mlomega_audio_elite\inbox\processed
.mlomega_audio_elite\inbox\failed
```

Lancer une seule passe :

```powershell
mlomega-audio flow-watch --once
```

Lancer en continu :

```powershell
mlomega-audio flow-watch --poll-seconds 60
```

Traiter un fichier directement :

```powershell
mlomega-audio flow-once C:\audios\conversation.wav
```

Ou transcript :

```powershell
mlomega-audio flow-once C:\exports\whatsapp.json
```

## 8. Sous-sujets dans une longue conversation

Une conversation d'une heure peut contenir plusieurs sujets/situations. V13.3 ajoute une segmentation stricte Qwen :

```powershell
mlomega-audio v13-subtopics <conversation_id>
```

Cela crée :

```text
conversation_subtopic_segments
```

Chaque sous-sujet garde :

```text
start_turn_id
end_turn_id
situation_type
summary
evidence_turn_ids
confidence
```

Donc le système peut analyser indépendamment plusieurs micro-situations dans le même audio.

## 9. Outcomes dispersés dans le 24/24

Tu n'es pas obligé de dire explicitement : « j'ai finalement choisi X ».

La V13.3 ajoute un resolver d'outcomes latents : quand une nouvelle conversation arrive, Qwen cherche si elle contient des indices qui résolvent une ancienne intention, prédiction, décision ou engagement.

Commande :

```powershell
mlomega-audio v13-discover-outcomes <conversation_id>
```

Le flux direct le lance automatiquement après `v13-build`.

Exemple :

```text
ancienne intention : installer le système ce soir
conversation suivante : "hier j'ai lancé l'installation mais j'ai bloqué sur CUDA"
→ outcome latent : action commencée, obstacle CUDA, statut changed/partial
```

## 10. Parler au cerveau 2.0

Exemples :

```powershell
mlomega-audio v13-predict next_action "Je viens de recevoir une réponse floue sur l'installation" --person-id me
```

```powershell
mlomega-audio v13-predict next_emotion "Quelqu'un remet en doute mon idée alors que je pense avoir raison" --person-id me
```

```powershell
mlomega-audio v13-predict next_phrase "Je veux savoir ce que je vais probablement répondre à cette situation" --person-id me
```

Réponse attendue :

```json
{
  "prediction_target": "next_action",
  "prediction": "...",
  "probability": 0.78,
  "confidence": 0.71,
  "why": [...],
  "similar_cases": [...],
  "counter_evidence": [...],
  "intervention": "..."
}
```

## 11. Vérifier une prédiction

Quand tu sais ce qui s'est passé :

```powershell
mlomega-audio v13-verify <prediction_id> "Finalement j'ai fait X, pas Y"
```

Cela met à jour :

```text
prediction_results
calibration_scores
model_revisions
v13_replay_events
```

## 12. Commandes utiles

```powershell
mlomega-audio doctor-elite
mlomega-audio v13-audit-plan
mlomega-audio speakers
mlomega-audio voice-pending
mlomega-audio v13-overview
mlomega-audio memory-overview
```

## 13. Premier test complet recommandé

```powershell
mlomega-audio init-db
mlomega-audio setup-me C:\audios\ma_voix.wav --display-name "Will / Moi"
mlomega-audio doctor-elite
mlomega-audio flow-once C:\audios\conversation_test.wav
mlomega-audio v13-overview
mlomega-audio voice-pending
mlomega-audio v13-predict next_action "Je veux savoir ce que je vais faire après cette discussion" --person-id me
```

## 14. Limites honnêtes

Le système est prêt à être exploité, mais il n'est pas magique.

Fort :

```text
brut / preuves / temps
speaker me prioritaire
unknown voices regroupées
sous-sujets
épisodes
similarités
patterns
prédictions vérifiables
révisions
```

Dépend encore de :

```text
qualité Qwen
qualité WhisperX
qualité pyannote
qualité SpeechBrain
quantité de données
vérification des prédictions
```

La règle :

```text
le brut est réel
l'analyse est probabiliste
le pattern devient fort par répétition
la prédiction devient forte par vérification
```

# Addendum V14.2 final

La commande `v14-ask` utilise maintenant la couche V14.2 : SQL structuré + recherche vectorielle + fusion/ranking anti-bruit.

## Recherche vectorielle

Après ingestion, la sync vectorielle est incrémentale automatiquement. Pour forcer une conversation :

```powershell
mlomega-audio sync-vectors --conversation-id <conversation_id>
```

Pour reconstruire volontairement tout l'index vectoriel :

```powershell
mlomega-audio sync-vectors --full
```

## Audio long

Le flow automatique ne supprime plus les silences par défaut. Il découpe en chunks sans casser les timestamps originaux :

```powershell
mlomega-audio flow-watch --poll-seconds 60
```

Le mode manuel avec suppression de silence existe, mais il est marqué dangereux pour les preuves temporelles :

```powershell
mlomega-audio preprocess-audio audio.wav --remove-silence
```

À éviter pour la mémoire 24/24 si tu veux des timestamps fiables.

## Questions naturelles solides

```powershell
mlomega-audio v14-ask "Quand est-ce que j'ai parlé de mon ancienne peur de perdre le contrôle ?" --person-id me
mlomega-audio v14-ask "Que va faire Max si je lui dis ça ?" --person-id me
mlomega-audio v14-ask "Qu'est-ce que je répète sans le voir ?" --person-id me
```

V14.2 sépare : faits bruts, hypothèses, prédictions, contre-preuves, éléments manquants.

---

# Addendum V14.3 — Consolidation automatique + exports Self Model

Après `setup-me`, le mode recommandé reste :

```powershell
mlomega-audio flow-watch --poll-seconds 60
```

Dès qu'un audio/transcript arrive, le système lance le flux complet puis V14.3 vérifie automatiquement si les consolidations sont dues : heure, jour, semaine, mois. Tu n'as donc pas besoin de lancer `v14-consolidate` à la main au quotidien.

Pour vérifier l'état du scheduler :

```powershell
mlomega-audio v14-scheduler-status --person-id me
```

Pour forcer une consolidation complète maintenant :

```powershell
mlomega-audio v14-auto-consolidate --person-id me --force
```

Pour exporter ce que le système pense savoir de toi :

```powershell
mlomega-audio export-self-model --person-id me --format markdown
mlomega-audio export-self-model --person-id me --format json
```

Les exports sont créés dans :

```text
.mlomega_audio_elite\exports
```

Ils contiennent : mots, expressions, pensées probables, émotions, choix, actions, outcomes, relations, personnes déclencheuses, boucles, contradictions, prédictions, angles morts, questions ouvertes, preuves et niveaux de confiance.


## V14.6 — Miroir interpersonnel complet

La V14.6 ajoute le modèle des autres et le couplage émotionnel : état probable d'une personne à l'instant T, effet de son humeur sur William, effet de William sur elle, micro-interactions, aftereffects sur l'heure/jour, boucles relationnelles et interventions.

Commandes utiles :

```powershell
mlomega-audio v14-6-audit
mlomega-audio v14-people-models --person-id me
mlomega-audio v14-social-aftereffects --person-id me
```

`flow-watch` appelle automatiquement V14.6 après V14.5. Le self-model exporté contient aussi la section `interpersonal_state_mirror`.
