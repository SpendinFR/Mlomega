# V13.2 Brain 2.0 Strict Final

Cette version applique le plan Brain 2.0 en mode strict.

## Décisions clés

- Les commandes publiques `v13-*` n’appellent plus la V12 comme baseline.
- Le mode `--allow-evidence-only` a été supprimé des commandes V13.
- Les moteurs cognitifs V13 exigent Qwen/Ollama. Si Qwen échoue ou produit un JSON invalide, la V13 échoue proprement au lieu de remplir les tables avec des règles faibles.
- Les liens temporels et liens d’objets sont créés comme bookkeeping structurel, pas comme inférence psychologique.
- La prosodie/audio émotionnelle est traitée comme signal requis : si elle manque, le système note le manque dans `v13_readiness_checks` et `v13_prosody_requirements`; il ne remplace pas par une hypothèse texte déguisée.

## Couverture du plan

La V13.2 conserve les 6 couches :

1. Evidence Layer
2. Interpretation Layer
3. Episode Layer
4. Model Layer
5. Prediction Layer
6. Intervention Layer

Elle ajoute en plus :

- `brain2_temporal_links`
- `brain2_object_links`
- `v13_llm_contracts`
- `v13_engine_dependencies`
- `v13_readiness_checks`
- `v13_prosody_requirements`

Ces tables garantissent que les objets produits par Qwen sont reliés entre eux, au temps, aux épisodes, aux preuves et aux moteurs qui les ont produits.

## Commandes

```powershell
mlomega-audio v13-audit-plan
mlomega-audio v13-build <conversation_id>
mlomega-audio v13-overview
mlomega-audio v13-predict next_action "contexte actuel" --person-id me
mlomega-audio v13-verify <prediction_id> "résultat observé"
```

## Ce que ça veut dire honnêtement

La V13.2 est prête comme système logiciel strict : tables, moteurs, contrats Qwen, liens, audit, build, prédiction, vérification, correction.

Elle n’est pas encore une intelligence prouvée sans historique : la qualité dépendra de Qwen, de la quantité de données, et des prédictions vérifiées.
