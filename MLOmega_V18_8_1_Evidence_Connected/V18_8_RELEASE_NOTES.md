# MLOmega V18.8.1 — Evidence-Connected Live / Activity-Aware Bundles

## Correctifs de liaison V18.8.1

- **Image → bundle → Qwen-VL deep → Brain2 :** chaque frame capturée garde désormais `frame_id`, chemin brut, SHA et signature visuelle dans l’évidence de bundle. Le deep vision réhydrate aussi les anciens bundles depuis `vision_frames` si nécessaire.
- **Aucun faux succès visuel :** un bundle qui possède des images mais aucune image lisible devient `blocked_visual_evidence_unavailable`. Le post-stop, Brain2 et la purge s’arrêtent alors ; les sources restent pour correction et `RESUME`.
- **Silence live :** le debounce Qwen ne considère plus chaque chunk vide comme une nouvelle frontière. Seule la transition effective parole → silence peut fermer une fenêtre sémantique.
- **Découpage d’activité sans audio :** l’assembleur combine changement VLM, changement de lieu résolu, changement dHash perceptuel et plafond de 25 minutes. Le dHash est un indice de transition, jamais une interprétation : la compréhension détaillée reste au VLM deep/Brain2.
- **Même lieu, plusieurs activités :** une session à domicile ou un jeu prolongé est découpée sur changement visuel significatif ou au plafond, puis Brain2 peut relier des bundles voisins si l’activité est en fait continue.
- **Retours d’intervention :** aucune validation manuelle n’est imposée. Les outcomes observables restent des faits du système ; les tables de delivery/feedback existantes restent compatibles sans devenir un formulaire obligatoire.

## Ce qui reste volontairement inchangé

- Audio en chunks de 3 s, avec tous les mots/timestamps conservés.
- Réception Bridge parallèle audio/image/GPS ; priorité audio dans BrainLive, avec créneau image au silence et délai maximal anti-famine.
- VLM live léger (`moondream`) et Qwen-VL lourd uniquement au post-stop.
- Deep vision : au plus 12 keyframes représentatives par bundle, jamais toutes les images.
- Les raw ne sont jamais purgés tant qu’un stage, une image, un bundle, une conversation ou une reprise reste pending, retryable ou blocked.

## Validation sur PC cible

Les tests locaux valident la logique. INSTALL reste responsable des probes réelles Windows : pilote NVIDIA, CUDA, Docker/WSL2, Hugging Face/Pyannote, Ollama, Qdrant et Android/Bridge avant `PRODUCTION_READY`.
