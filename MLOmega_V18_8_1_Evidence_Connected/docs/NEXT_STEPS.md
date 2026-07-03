# Prochaines étapes recommandées

1. Lancer `mlomega-audio doctor-elite --fail` jusqu'à obtenir `OUI — stack élite détectée`.
2. Ingérer 5 à 10 conversations réelles avec `ingest-audio`.
3. Inspecter `mlomega-audio memory-overview` pour vérifier que les cartes, frames, facettes, preuves et liens se remplissent.
4. Inspecter quelques cartes avec `mlomega-audio memory-card <id>`.
5. Vérifier que Qdrant contient les couches `memory_card`, `memory_frame`, `turn`, `analysis`, `atomic_memory`, `reflection_state`, `pattern`, `self_model`.
6. Vérifier dans Neo4j/Graphiti que les épisodes sont créés.
7. Vérifier dans Mem0 que les souvenirs atomiques sont ajoutés par personne.
8. Ajouter ensuite seulement une UI : timeline, memory cards, frames, facets, proofs, states/edges/patterns.
9. Étape future : corrections utilisateur et contradictions manuelles.
10. Étape après mémoire stable : moteur Life Pattern / prédiction prudente.


## V3.2.1 external sync

Graphiti reçoit maintenant les tours bruts, les `memory_cards` canoniques et les `memory_frames` typées comme épisodes séparés. Mem0 reçoit les `atomic_memories` historiques plus les couches V3.2 `memory_card` et `memory_frame` avec leurs métadonnées/facettes.
