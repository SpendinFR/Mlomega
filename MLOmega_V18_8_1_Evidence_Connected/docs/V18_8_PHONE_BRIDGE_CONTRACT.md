# Contrat Phone Bridge V18.8

Le Bridge écoute par défaut sur `8766`, séparé d’un éventuel Dashboard/API legacy.

- Uploads audio/image/transcript/GPS : files séparées, audio prioritaire au transport.
- `/session/stop` avec `-AllowPostStopOnSessionStop` : drain Bridge → stop explicite du `service_run_id` actif → close-day. Aucun fallback vers le dernier service global.
- `/interventions/feedback` : accepte un JSON authentifié contenant `delivery_id` et `feedback_type` (`delivered`, `displayed`, `seen`, `acted`, `dismissed`, `ignored`, `failed`). Il dépose une preuve durable dans `brainlive_inbox/feedback`.
- Le Bridge ne purge `phone_*` qu’après un close-day terminé et une cleanup gate positive.

Test santé :

```powershell
Invoke-RestMethod http://127.0.0.1:8766/health
```

La purge ne peut avoir lieu que lorsque le close-day retourne `eligible=true` pour la cleanup gate.
