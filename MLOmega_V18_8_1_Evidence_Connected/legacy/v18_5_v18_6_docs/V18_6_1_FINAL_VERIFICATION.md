# MLOmega V18.7.0 — vérification finale de release

## Contrat de cette release

Le profil livré est exclusivement **CORE_BRAINLIVE_V18_7_PHONE** : capture téléphone, Phone Bridge, BrainLive live, deep audio WhisperX/Pyannote/SpeechBrain, deep vision Ollama, Brain2 V13–V17 et Qdrant. **Graphiti, Neo4j et Mem0 sont exclus** : ils ne sont ni dans le verrou de dépendances, ni dans le profil validé.

L’objectif n’est pas de prétendre qu’un PC quelconque ne peut jamais avoir un problème externe. L’objectif réel est :

- l’installation ne déclare jamais une réussite avant les vérifications concrètes ;
- tout prérequis externe manquant bloque avec un diagnostic précis ;
- une erreur transitoire ne détruit aucune source ni aucun checkpoint ;
- une reprise n’exécute à nouveau que le premier travail non validé.

## Installation : ce qui est installé et testé

`INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -HfToken "hf_..."` :

1. vérifie administrateur, Windows x64, Python 3.11 64 bits, GPU NVIDIA/CUDA, RAM, VRAM, espace disque, FFmpeg, Docker/WSL2, réseau privé, port du Bridge et token Hugging Face/gated Pyannote ;
2. crée une `.venv.new` isolée : aucun PyTorch global existant n’est modifié ;
3. installe le lock de production : Torch/Torchaudio CUDA, FastAPI/Uvicorn/Pillow, faster-whisper, WhisperX, Pyannote, SpeechBrain, Silero VAD, SentenceTransformers, Qdrant et client Ollama ;
4. force le profil sans Graphiti/Mem0, initialise SQLite et démarre Qdrant/Ollama ;
5. vérifie Ollama >= 0.12.7, puis tire `qwen3.5:9b`, `moondream` et `qwen3-vl:8b` ;
6. lance un doctor réel : génération LLM, génération VLM, chargement WhisperX large-v3, alignement, Pyannote, Silero VAD, SpeechBrain, modèles vectoriels, Qdrant lecture/écriture et Phone Bridge authentifié ;
7. active la nouvelle `.venv` uniquement après ces validations. En cas d’échec, l’ancienne `.venv` et le `.env` précédent sont restaurés.

L’option `-SkipHeavyModelSmoke` existe uniquement pour diagnostic technique ; `RUN` et `RESUME` la refusent ensuite.

## Démarrage

`RUN_MLOMEGA_V18_7.ps1 -PersonId me` vérifie :

- le rapport d’installation et la somme de `.env` ;
- le manifest complet de release : tout fichier modifié/manquant bloque le RUN ;
- Qdrant et Ollama ;
- le doctor core, puis le Phone Bridge authentifié et lié au bon dossier projet ;
- l’absence de clôture/inbox à reprendre avant d’ouvrir une nouvelle session ;
- la publication effective du manifeste BrainLive avant de déclarer le flux actif.

La première autorisation Android et la mise en place Termux restent matériellement manuelles : Android ne permet pas à Windows d’accorder des permissions micro/caméra ou d’écrire arbitrairement la configuration sur le téléphone. Le PC génère la configuration Android et le Bridge PC est validé avant toute capture.

## Timeout, retry et optimisation

- Ollama : keep-alive explicite, timeouts post-stop longs (`LLM=900 s`, `VLM=300 s`), deux retries bornés avec backoff 15 s puis 60 s pour timeout/connexion/429/5xx/SQLite busy/GPU OOM.
- Live : reste volontairement court et non bloquant ; un cadre VLM live en erreur est conservé pour la passe deep, il ne casse pas la capture.
- Deep audio : runtime WhisperX + alignement + Pyannote partagé sur toute la clôture ; pas de rechargement par bundle ; batch de départ 4 avec repli 4 → 2 → 1 sur OOM.
- VRAM : caches live libérés avant deep audio ; deep audio libéré avant VLM ; VLM libéré avant Brain2.
- Deep vision : checkpoint par image ; une image réussie n’est pas rejouée.

## Reprise exacte après crash / extinction

`RESUME_MLOMEGA_V18_7.ps1 -PersonId me` démarre les dépendances, vérifie l’intégrité de la release, récupère une session BrainLive orpheline, réingère l’inbox durablement, puis reprend le **même run** de clôture.

Granularité :

- deep audio : chaque bundle déjà raffiné est conservé ; seul le bundle non terminé repart ;
- deep vision : chaque image VLM `ok` est conservée ; seule l’image non terminée repart ;
- Brain2 : chaque conversation déjà complète est ignorée ; à l’intérieur de la conversation en cours, V13, V13 subtopics, latent outcomes, V14 auto-verify, autonome, mirror, people/open loops, interpersonal, interventions, clarifications, V17 cases et V17 similarity ont chacun un checkpoint durable ;
- exemple : crash pendant V14 interventions → reprise à V14 interventions, pas à V13 ni au début du bundle ;
- V15 : les étapes daily V17 longitudinal, V15.12, V15.13, V15.9 et export périodique sont également checkpointées ; un crash dans V15.13 rejoue V15.13 seulement depuis son entrée ;
- aucune purge de sources tant que l’inbox, le post-stop et le close-day n’ont pas tous été validés.

Un arrêt électrique au milieu d’un moteur ne peut pas reprendre à une instruction interne arbitraire : il recommence l’entrée du moteur inachevé. C’est la frontière sûre et déterministe ; toutes les étapes antérieures validées restent intactes.

## Validation effectuée avant livraison

Sans rejouer la suite de tests du projet :

- compilation/parse de tous les modules Python source et Phone Bridge ;
- test personnalisé du mécanisme de checkpoints : une étape validée a été vérifiée comme `skipped_checkpoint` à la reprise, tandis qu’une étape qui simulait un timeout repartait seule ;
- vérification de signature effective du hook V18 Brain2 `checkpoint_run_id` ;
- contrôle du lock core : absence de Graphiti/Mem0/Neo4j ;
- validation de l’archive et du manifest SHA-256 depuis une extraction neuve.

La validation matérielle finale dépend nécessairement du PC cible : pilotes NVIDIA, accès Internet/Hugging Face, Docker Desktop/WSL, Ollama et autorisations Android. L’installateur est conçu pour s’arrêter avant utilisation si l’un de ces éléments n’est pas réellement utilisable.
