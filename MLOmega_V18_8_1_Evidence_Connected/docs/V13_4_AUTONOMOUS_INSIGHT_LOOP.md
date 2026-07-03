# V13.4 — Boucle autonome d'hypothèses, prédictions et interventions

La commande `v13-predict next_*` n'est pas le cœur autonome. Elle sert à poser une question ciblée.
Le cœur autonome est lancé après chaque conversation via `flow-watch` / `v13-build` :

```text
conversation ingérée
→ V13 strict engines
→ sous-sujets
→ outcomes latents
→ V13.4 autonomous insights
→ inbox de prédictions / hypothèses / alertes / interventions
```

## Commandes

```powershell
mlomega-audio v13-insights --limit 20
mlomega-audio v13-autonomous <conversation_id>
mlomega-audio v13-ask "À ton avis, qu'est-ce que je vais faire dans cette situation ?" --person-id me
```

## Différence

- `v13-insights` : ce que le système a trouvé tout seul.
- `v13-autonomous` : force l'analyse autonome sur une conversation.
- `v13-ask` : tu parles naturellement, le système infère lui-même si c'est une question mémoire, prédiction, simulation ou modèle.
- `v13-predict next_action ...` : mode expert ciblé, utile mais pas obligatoire pour le cerveau autonome.

## Exemple d'insight autonome

```json
{
  "insight_type": "loop_risk",
  "priority": "high",
  "title": "Risque de refaire une boucle de validation",
  "summary": "William demande une preuve de complétude avant d'agir, comme dans plusieurs échanges précédents.",
  "prediction_target": "next_action",
  "predicted_value": "Il va probablement demander une confirmation supplémentaire ou un test concret.",
  "probability": 0.76,
  "confidence": 0.68,
  "why": ["cas similaires", "pattern de besoin de preuve", "contexte technique flou"],
  "counter_evidence": ["si la procédure est très claire, il peut agir directement"],
  "intervention": "Donner une action minimale vérifiable maintenant."
}
```

Le système ne doit pas attendre que tu demandes `next_action`. Il doit remplir cette file d'attente seul.
