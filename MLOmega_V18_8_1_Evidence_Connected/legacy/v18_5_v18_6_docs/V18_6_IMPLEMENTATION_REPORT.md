# MLOmega V18.7 — implémentation « installation sûre, lancement surveillé, reprise durable »

## Objectif livré

Cette release transforme le flux cible en un profil opérationnel unique :

```text
Téléphone Android → Phone Bridge V18.7 → BrainLive live
→ drain final de l’inbox → deep audio WhisperX/Pyannote/SpeechBrain
→ deep vision VLM → Brain2 → clôture journalière → purge seulement après validation
```

Graphiti, Neo4j et Mem0 sont exclus de ce profil : ils ne sont ni installés, ni démarrés, ni contrôlés, ni exécutés par le flux normal.

## Commandes canoniques

```powershell
# Installation PC — PowerShell administrateur
.\INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -HfToken "hf_..."

# Démarrage d’une capture
.\RUN_MLOMEGA_V18_7.ps1 -PersonId me

# Arrêt propre et clôture
.\STOP_MLOMEGA_V18_7.ps1 -PersonId me

# Après timeout, crash de processus ou extinction PC
.\RESUME_MLOMEGA_V18_7.ps1 -PersonId me
```

Les anciens points d’entrée d’installation V17 redirigent vers l’installateur V18.7 ; ils ne relancent plus l’ancienne stack Graphiti/Mem0.

## Installation : garde-fous mis en place

L’installateur V18.7 ne retourne un succès qu’après avoir vérifié localement :

- Windows 64 bits, PowerShell administrateur, disque libre, RAM, GPU NVIDIA et VRAM ;
- Python **3.11 64 bits** dans un `.venv` isolé : aucun Python/PyTorch global n’est modifié ;
- Docker Desktop/WSL2 réellement prêt, Qdrant sain ;
- Ollama installé, démarré et modèles réellement présents : `qwen3.5:9b`, `moondream`, `qwen3-vl:8b` ;
- token Hugging Face valide **et autorisé** pour les deux dépôts Pyannote requis ;
- chargements réels successifs : LLM, VLM, WhisperX `large-v3`, alignement, Pyannote, Silero, SpeechBrain, embedder et reranker ;
- initialisation/migration SQLite ;
- Phone Bridge temporaire avec port, projet et permission post-stop vérifiés.

L’environnement est préparé dans `.venv.new`, puis basculé seulement après l’installation des paquets et `pip check`. En cas d’échec après le basculement, l’ancien `.venv` et l’ancien `.env` sont restaurés. Une réinstallation détecte un Bridge identique déjà actif et le réutilise pour le smoke test ; un port 8766 occupé par un autre programme bloque clairement avant téléchargement long.

Le pare-feu n’ouvre le Bridge que sur les profils Windows Privé/Domaine. Un profil uniquement Public est signalé avant de prétendre que le téléphone pourra joindre le PC.

## Démarrage : aucun démarrage sur un état incomplet

`RUN_MLOMEGA_V18_7.ps1` :

1. charge la configuration du projet ;
2. démarre et contrôle Qdrant et Ollama ;
3. exécute le doctor core ;
4. récupère les services BrainLive morts ;
5. refuse de démarrer une nouvelle capture tant qu’une inbox, un post-stop ou une clôture inachevée exige `RESUME` ;
6. démarre/valide le Bridge exact du projet ;
7. démarre BrainLive et attend son manifeste durable.

`-Restart` ne peut pas couper une session en cours. Il faut toujours passer par `STOP`, puis `RESUME` si nécessaire.

## Post-stop, erreurs et coupure PC

Les conditions prévues sont :

- appels Ollama centralisés avec délais post-stop longs, `keep_alive` et retries bornés ;
- erreurs transitoires classées (`timeout`, transport local, HTTP 429/5xx, verrou SQLite, OOM GPU) ;
- erreurs de configuration/preuve/Hugging Face laissées bloquées, jamais déguisées en succès ;
- chaque étape a un checkpoint durable : assembly, deep audio, deep vision, silent life, Brain2 par conversation, puis clôture journalière ;
- deep audio et deep vision reprennent par unité terminée ; Brain2 ne rejoue pas les conversations déjà validées ;
- extinction PC : PID mort ⇒ service `orphaned` immédiatement, inbox drainée avant post-stop, même run logique réutilisé ;
- aucune purge téléphone/inbox tant que les gates de post-stop **et** close-day ne sont pas complètes.

`RESUME` réhydrate d’abord Qdrant et Ollama, exécute les contrôles, draine les fichiers retenus, puis reprend le même run logique. Il ne crée pas un second jour par collision `person_id + date` et ne recommence pas les checkpoints terminés.

## Optimisation des ressources

- live : faster-whisper, VAD et reconnaissance vocale restent résidents pendant la capture ;
- frontière live → deep : caches live et modèles Ollama légers sont explicitement libérés ;
- deep audio : WhisperX, modèle d’alignement et Pyannote sont chargés une seule fois pour l’ensemble d’un post-stop, pas une fois par bundle ;
- GPU : deep audio, VLM lourd et Brain2 sont exécutés séquentiellement avec libération mémoire entre phases ;
- SentenceTransformers/Qdrant sont mis en cache au niveau processus lorsque utilisés ;
- le paramètre Bridge `-PumpSeconds` agit maintenant réellement comme override global ; sans override, les cadences optimisées par type restent actives.

## Point volontairement non modifié (P2 exclu)

La limite deep audio reste à `1 800 s` par bundle. V18.7 n’ajoute pas de découpage automatique des conversations longues : c’était explicitement hors périmètre P2. Les tours et épisodes restent toutefois segmentés après transcription ; une bande qui dépasse la limite reste un blocage retenu, jamais une troncature silencieuse.

## Limite physique non automatisable

Le PC peut être installé et validé avec le token HF comme seule donnée secrète manuelle. En revanche, Windows ne peut pas accorder à distance les permissions Android (micro, fichiers, batterie, réseau) ni copier la configuration générée dans Termux. La release ne prétend donc pas qu’un téléphone est prêt sans premier paquet réellement reçu ; le Bridge PC, lui, est validé à l’installation et à chaque lancement.
