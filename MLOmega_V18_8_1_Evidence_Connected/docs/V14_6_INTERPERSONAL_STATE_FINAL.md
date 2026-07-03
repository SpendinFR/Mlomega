# V14.6 — Other Person Model + Interpersonal Emotional Coupling

V14.6 ajoute le miroir interpersonnel complet au-dessus de V14.5.

Objectif : ne plus seulement modéliser William, mais aussi l'effet des autres sur William et l'effet de William sur les autres, sans prétendre lire les pensées. Tout reste hypothèse avec preuves, contre-preuves, confiance et statut.

## Ce que V14.6 observe

- état probable d'une autre personne à l'instant T ;
- émotions probables, tension, ouverture, énergie, intention sociale ;
- pensées/besoins/évitements probables de l'autre, avec incertitude ;
- contagion émotionnelle : joie, tension, fatigue, motivation, menace, détente ;
- micro-interactions : caissier joyeux, inconnu tendu, appel court, message sec ;
- aftereffects : effet probable sur l'heure, la journée, les prochaines actions ;
- modèles relationnels long terme : Max, famille, amis, clients, collègues ;
- boucles interpersonnelles : si l'autre est vague, William pousse ; si William pousse, l'autre esquive ;
- interventions relationnelles : quoi faire pour désamorcer, réparer, utiliser un levier positif.

## Tables ajoutées

- `v14_6_other_person_state_snapshots`
- `v14_6_interpersonal_emotional_couplings`
- `v14_6_micro_interaction_impacts`
- `v14_6_social_aftereffects`
- `v14_6_relationship_state_models`
- `v14_6_interpersonal_loop_cards`
- `v14_6_intervention_suggestions`
- `v14_6_person_model_summaries`
- `v14_6_interpersonal_runs`
- `v14_6_contract_checks`

## Commandes

```powershell
mlomega-audio v14-6-audit
mlomega-audio v14-6-run <conversation_id> --person-id me
mlomega-audio v14-people-models --person-id me
mlomega-audio v14-people-models --person-id me --person-hint Max
mlomega-audio v14-social-aftereffects --person-id me
```

## Flow automatique

`flow-watch` appelle maintenant V14.6 après V14.5 et avant le scheduler/export :

```text
audio/transcript
→ ingestion
→ V13 strict
→ latent outcomes
→ V14.4 auto verification
→ V13.4 autonomous insights
→ V14 Pattern Mirror
→ V14.5 people/openloops
→ V14.6 interpersonal state mirror
→ V14.3 scheduler/export self-model
```

## Limites et sécurité

- Pas de confirmation automatique d'identité.
- Pas de diagnostic psychologique.
- Pas de lecture d'esprit : seulement des hypothèses à partir des traces.
- Pas de regex/keyword rules dans cette couche.
- Si Qwen/Ollama n'est pas disponible, le run est enregistré en erreur et aucune hypothèse fausse n'est créée.
