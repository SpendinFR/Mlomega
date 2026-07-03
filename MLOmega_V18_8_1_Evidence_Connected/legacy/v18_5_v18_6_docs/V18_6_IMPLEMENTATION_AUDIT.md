# Audit d'implémentation V18.7 — profil Core BrainLive

## Périmètre volontaire

Cette release ne prend en charge qu'un flux local Windows :

```text
Android → Phone Bridge 8766 → BrainLive live → drain inbox
→ deep audio WhisperX/Pyannote/SpeechBrain → deep VLM → Brain2
→ clôture/cleanup sous jalons durables
```

Graphiti, Neo4j et Mem0 sont exclus : pas installés, pas démarrés, pas exigés
par le doctor V18.7.

## Travaux intégrés

### Installation et configuration

- Environnement Python isolé et verrouillé : une installation globale existante,
  y compris PyTorch, n'est pas modifiée.
- Préflight explicite : Windows 64-bit, droits administrateur, disque, RAM,
  GPU NVIDIA/VRAM, FFmpeg, Docker/WSL, Python 3.11 et Ollama CLI.
- Création transactionnelle `.venv.new`, bascule atomique puis restauration de
  l'ancien `.venv` et `.env` si un contrôle postérieur échoue.
- Vérification Hugging Face avant l'installation lourde et confirmation des
  deux accès Pyannote gated.
- Téléchargement et contrôle des modèles Ollama requis : `qwen3.5:9b`,
  `moondream`, `qwen3-vl:8b`.
- `doctor-core-v18-6` réel, sans dépendance Graphiti/Mem0, incluant charge
  deep audio et bridge temporaire pendant l'installation complète.

### Lancement et raccords

- Phone Bridge V18.7 inclus dans l'archive, port `8766` isolé de l'API `8765`.
- Pare-feu Windows privé/domaine ouvert uniquement pour le bridge ; token de
  bridge généré automatiquement.
- `RUN` vérifie Qdrant, Ollama, le doctor core, le bridge et le manifeste de
  session avant d'annoncer la capture active.
- Un `RUN` refuse une nouvelle capture tant qu'un travail de reprise est
  présent. Un manifeste JSON obsolète après une coupure n'est plus confondu
  avec une session active : la décision vient de l'état durable SQLite/PID.

### Stabilité et reprise post-stop

- Délai Ollama long par phase, `keep_alive` explicitement piloté, retries
  bornés et événements de diagnostic persistés.
- Deep audio, deep vision et Brain2 conservent leurs résultats par unité ; les
  unités terminées ne sont pas rejouées lors d'un `RESUME`.
- Un échec retryable reste retryable. Un blocage de configuration/preuve reste
  conservé et reprend le même `run_id` après correction + `RESUME` explicite.
- Bail SQLite PID-aware : une coupure électrique rend le propriétaire mort et
  permet de récupérer immédiatement le même run ; un processus encore vivant
  reste protégé contre une double exécution.
- L'inbox est drainée avant post-stop et aucune purge des sources n'est autorisée
  avant les cleanup gates finalisés.

### Optimisation GPU / modèles

- Faster-Whisper/Silero live restent résident pendant la capture.
- À l'arrêt, les caches live et modèles Ollama live sont libérés avant
  WhisperX/Pyannote.
- WhisperX, alignement et Pyannote sont partagés entre les bundles qui restent
  réellement à traiter dans une clôture ; ils ne sont pas rechargés par bundle.
- Une reprise dont tous les artefacts deep audio sont déjà complets ne charge
  plus WhisperX/Pyannote uniquement pour vérifier les checkpoints.
- Les phases deep audio, VLM et Brain2 libèrent explicitement les ressources
  Python/CUDA/Ollama entre elles, afin d'éviter l'empilement inutile de VRAM.

## Commandes de référence

```powershell
.\INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -HfToken "hf_..."
.\RUN_MLOMEGA_V18_7.ps1 -PersonId me
.\STOP_MLOMEGA_V18_7.ps1 -PersonId me
.\RESUME_MLOMEGA_V18_7.ps1 -PersonId me
```

Le seul secret requis côté PC est le token Hugging Face. Les permissions
physiques du premier téléphone Android restent nécessairement à confirmer sur
l'appareil ; elles ne peuvent pas être accordées par un script Windows.
