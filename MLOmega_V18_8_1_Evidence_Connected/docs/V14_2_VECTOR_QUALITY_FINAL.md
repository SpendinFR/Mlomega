# V14.2 Brain 2.0 Vector Quality Final

Cette version garde V13/V14 et ajoute la couche qui manquait pour l'usage réel long terme :

```text
question naturelle
→ route Qwen stricte
→ SQL par couche mémoire
→ recherche vectorielle sémantique
→ fusion / ranking anti-bruit
→ réponse séparant fait, hypothèse, prédiction, manque de contexte
```

## 1. Pourquoi la recherche vectorielle est nécessaire

Une question comme :

```text
Quand est-ce que j'ai parlé de mon ancienne peur de perdre le contrôle ?
```

n'a pas forcément de date. Une sélection uniquement SQL/récence peut rater une ancienne conversation sémantiquement très proche. V14.2 lance donc aussi le moteur vectoriel quand la question peut viser une ancienne trace non datée.

Tables ajoutées :

```text
v14_2_vector_search_runs
v14_2_vector_candidates
v14_2_fusion_runs
v14_2_fused_candidates
v14_2_answer_packets
```

## 2. Anti-bruit 24/24

Beaucoup d'audio ne veut pas dire beaucoup de vérité. V14.2 ajoute des guardrails :

```text
répétition courte ≠ pattern profond
vocabulaire fréquent ≠ boucle de vie
humeur du jour ≠ trait stable
projet omniprésent ≠ preuve psychologique
```

Les candidats sont scorés avec :

```text
confiance
preuve
outcome
contre-preuve
open loop
longueur utile
source brute vs consolidation
pénalité des répétitions faibles
```

Tables :

```text
v14_2_noise_guardrail_reports
v14_2_selection_signal_scores
```

## 3. Timestamps audio longs

Le brut audio est sacré. Le flux automatique ne supprime plus les silences par défaut. Il découpe l'audio original en chunks et garde une carte :

```text
audio original → chunk → offset original → turns/source_spans recalés
```

Tables :

```text
audio_chunk_groups
audio_timestamp_maps
audio_chunk_conversation_links
```

Le mode `--remove-silence` existe uniquement en manuel et est marqué unsafe tant qu'un remap exact des silences n'est pas fourni.

## 4. Sync vectorielle incrémentale

Avant, `sync_vectors()` pouvait resynchroniser toute la mémoire. V14.2 ajoute :

```text
vector_sync_manifest
```

Chaque point vectoriel garde son hash texte. Si le texte n'a pas changé, il est sauté. L'ingestion appelle maintenant :

```text
sync_vectors(conversation_id=<nouvelle_conversation>)
```

Donc une nouvelle conversation ne force pas à ré-embedder toute la vie.

## 5. Commandes utiles

```powershell
mlomega-audio v14-ask "Quand est-ce que j'ai parlé de mon ancienne peur de perdre le contrôle ?" --person-id me
mlomega-audio v14-select "Que prédis-tu de mon avenir proche ?" --person-id me
mlomega-audio v14-2-audit
mlomega-audio sync-vectors --conversation-id <conversation_id>
mlomega-audio sync-vectors --full   # seulement pour rebuild volontaire
```

## 6. Principe final

```text
brut = preuve
vectoriel = vieux souvenirs sémantiques
V13 = prédiction/simulation
V14 = patterns longs
V14.2 = route + fusion + ranking + anti-bruit
```
