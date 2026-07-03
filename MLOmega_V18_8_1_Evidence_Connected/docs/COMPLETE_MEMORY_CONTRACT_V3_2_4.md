# V3.2.4 — Complete Memory Contract

Cette version verrouille les éléments du contrat mémoire audio complet : transcript exact, voix, contexte global, analyse au millimètre, mémoire utile, et premiers patterns exploitables.

## Ajouts principaux

- `speaker_uncertainty_segments` : segments où le locuteur ou la personne résolue est incertain.
- `conversation_turning_points` : moments de bascule de la conversation : pivot de sujet, tension émotionnelle, clarification, décision, contradiction, engagement.
- `activation_signals` : déclencheur + émotion + réaction probable par utterance.
- `person_reaction_patterns` : consolidation des réactions typiques par personne, autre personne, sujet et déclencheur.

## Effet attendu

Le moteur futur peut maintenant demander directement :

- Qui parle, quand, avec quelle confiance ?
- Quels segments sont incertains ?
- Quel est le ton général et les moments de bascule ?
- Qu’est-ce qui active ou tend l’utilisateur ?
- Comment l’utilisateur réagit avec cette personne ou sur ce sujet ?
- Quels premiers indices de boucle apparaissent ?

Ces couches sont reliées aux `memory_cards`, aux `memory_facets`, aux `memory_links`, aux `source_spans`, à la vector DB, à Mem0 et à Graphiti via la synchronisation externe existante.
