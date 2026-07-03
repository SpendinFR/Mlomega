# MLOmega V18.7 — installation opérable Windows

## Contrat de la release

- Profil supporté : Windows 11 64-bit, Python 3.11, GPU NVIDIA CUDA, Docker Desktop/WSL2, Internet pendant l'installation.
- Installation : `INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -HfToken "hf_..."`
- Lancement : `RUN_MLOMEGA_V18_7.ps1 -PersonId me`
- Arrêt depuis le téléphone : le bridge vide sa file, BrainLive fait un drain final de l'inbox, puis le post-stop devient reprenable par étapes.
- Reprise après crash/arrêt PC : `RESUME_MLOMEGA_V18_7.ps1 -PersonId me`.

Le script ne déclare jamais un succès si une dépendance, un modèle, Qdrant, Ollama, le bridge ou un modèle deep ne répond pas réellement.

## Seule entrée sensible obligatoire

`-HfToken` doit avoir accepté les modèles Pyannote gated du même compte Hugging Face. Le token téléphone est généré par l'installateur. L'enrôlement de voix est volontairement optionnel : les voix inconnues restent des clusters `voice_pending` jusqu'à validation.

## Limites physiques explicites

Windows ne peut pas accorder automatiquement les autorisations Android (micro/fichiers/réseau) sur le téléphone. Le doctor vérifie le bridge PC ; le premier test téléphone doit confirmer un paquet réel. Aucune release honnête ne doit afficher "téléphone prêt" sans ce paquet.

## Contrat de reprise V18.7

- `STOP_MLOMEGA_V18_7.ps1` ne purge jamais les sources tant que tous les jalons requis ne sont pas terminés.
- Après un timeout, une coupure de courant ou un arrêt Windows, lancez `RESUME_MLOMEGA_V18_7.ps1`. Il draine d’abord l’inbox conservée, reprend le même `run_id`, conserve les étapes déjà validées et ne rejoue que l’étape incomplète.
- Si le défaut était une configuration corrigée ensuite (token HF, modèle local, espace disque), `RESUME` utilise un redémarrage explicite et contrôlé du même run ; il ne crée pas un second jour ni une seconde analyse des bundles terminés.
- Une exécution encore vivante n’est jamais prise de force : son PID/host possède un bail SQLite. Après extinction, le PID mort permet au premier `RESUME` de récupérer immédiatement ce même bail.

## Limite physique à connaître

L’installation Windows peut préparer et vérifier tout le côté PC avec le seul token Hugging Face. La première connexion d’un téléphone Android nécessite tout de même les permissions Android (micro, stockage/Termux, réseau) et la copie de la configuration générée vers Termux : Windows ne peut pas accorder ces permissions à distance. Une fois cette étape appareil faite, `RUN_MLOMEGA_V18_7.ps1` démarre le bridge et vérifie son contrat de santé avant BrainLive.


## Réinstallation et garde-fous réseau

Une réinstallation reconstruit un environnement isolé avant bascule ; elle ne réinstalle pas Python ou PyTorch dans l’environnement global. Le port Bridge `8766` doit être libre ou déjà occupé par le Bridge de **ce même projet**. Le launcher `RUN` refuse aussi de démarrer une capture si une reprise est obligatoire : exécutez `RESUME` plutôt que de superposer une nouvelle journée sur des médias conservés.

## Compatibilité V18.7.1

Les commandes CLI métier historiques sont conservées. Pour les captures réelles, utiliser les wrappers `RUN_MLOMEGA_V18_7.ps1`, `STOP_MLOMEGA_V18_7.ps1` et `RESUME_MLOMEGA_V18_7.ps1` : ils préservent l'identité explicite de la session et les checkpoints de reprise. `doctor-elite` reste un alias de compatibilité vers le doctor core du profil sans Graphiti/Mem0.
