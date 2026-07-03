# V3.2.2 — Découpage précis + contexte local

La mémoire ne doit pas analyser un bloc WhisperX comme si c'était toujours une phrase humaine parfaite. Cette version ajoute une couche de segmentation avant l'ingestion mémoire.

## Ce qui se passe sur un audio long

1. WhisperX transcrit l'audio avec timestamps mots si disponibles.
2. Pyannote assigne les locuteurs.
3. `segmentation.normalize_transcript_turns()` transforme les segments en utterances atomiques.
4. Le découpage se fait par ponctuation, pauses, durée maximale et nombre maximal de mots.
5. Chaque utterance garde son lien vers le segment d'origine, ses mots, ses timestamps et son texte source.
6. L'ingestion crée des `source_spans` pour l'utterance et pour chaque mot horodaté.
7. Le microscope LLM analyse l'utterance actuelle avec une fenêtre locale: 3 utterances avant, 2 après.
8. Les souvenirs créés pointent vers une preuve exacte et vers le run d'extraction.

## Pourquoi c'est important

Un audio de 15 minutes ne doit pas devenir un seul prompt global ni des gros paragraphes flous. La mémoire doit produire des unités exploitables:

- phrase / micro-phrase;
- timestamps précis;
- speaker/personne;
- preuve textuelle;
- contexte avant/après;
- analyse uniquement attribuée à l'utterance courante;
- frames/facettes indexables.

Cette couche ne rend pas WhisperX infaillible. Elle garantit que le projet ne traite plus un segment brut comme une vérité parfaite: il le découpe, le prouve, le contextualise, puis l'analyse.
