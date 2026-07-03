# Vérification d’implémentation V18.7

Cette release a été vérifiée indépendamment de la suite de tests déjà présente dans le projet.

## Vérifications statiques exécutées

- `py_compile` : **91 modules Python** de `src/mlomega_audio_elite` et du Phone Bridge compilés sans erreur.
- CLI chargée : expose `doctor-core-v18-6`, `brainlive-resume-inbox-drain`, `brainlive-resume-close-day`, `brainlive-recover-stale-services` et `brainlive-recovery-status`.
- Contrôle structurel PowerShell : **11 scripts** V18.7 / Bridge avec délimiteurs et chaînes équilibrés. L’exécution PowerShell réelle reste à faire sur Windows.
- Les requirements core Windows et les scripts canoniques n’installent ni ne démarrent Graphiti, Neo4j ou Mem0.

## Vérifications ciblées de l’état durable

Dans des bases SQLite isolées :

1. un run `retryable_error` reprend le **même run_id** ;
2. un retry de transport réussit après trois tentatives bornées ;
3. une seconde exécution ne prend pas le bail d’un run encore possédé ;
4. un PID BrainLive mort avec heartbeat encore frais devient immédiatement `orphaned` ;
5. `brainlive-recovery-status` renvoie `resume_required` lorsqu’une inbox orpheline existe ;
6. les cadences Bridge par défaut et l’override global `MLOMEGA_PUMP_SECONDS` sont réellement appliqués.

## Ce que cela ne prétend pas prouver

Une archive ne peut pas démontrer qu’un PC particulier possède un pilote NVIDIA fonctionnel, Docker/WSL prêt, Internet, espace disque, VRAM, entitlement Hugging Face ou permissions Android. L’installateur et le doctor traitent ces éléments comme des gates réelles : pas de succès annoncé tant que les probes locales ne passent pas.
