# PROD_BACKLOG — MLOmega V19, complétion produit (E31→E36)

Issu de l'audit d'alignement vision↔livré du 2026-07-04. Principe directeur : **brancher l'existant avant de construire du neuf** — le cœur V18.8 contient déjà la réactivité conversationnelle (hot capsule, relation packs, open loops, interventions proactives H1), les modèles relationnels et la correction mémoire ; plusieurs « gaps » sont des tuyaux manquants, pas des capacités manquantes. Même discipline que E1→E29 : une étape = une branche = une PR, push au fil de l'eau, tests réels exécutés ici quand c'est du PC, ADR dans DECISIONS.md.

## E31 — Conversation live → BrainLive V18.8 (LE branchement prioritaire)

**Constat** : audiort produit les transcripts (sous-titres) mais la boucle BrainLive du cœur ne les reçoit pas — le moteur d'interventions conversationnelles existe (v18_8_live_policy, hotloop, turn buffer) et n'entend pas la conversation V19.
**Faire** : injecter les segments finaux d'audiort dans le chemin d'entrée live du cœur (turn buffer / live session — lire `brainlive_realtime_v15_2`/`brainlive_hotloop_v15_6`/`v18_8_live_policy.plan_live_dispatch` pour le point d'entrée exact ; ADR) avec `live_session_id` V19 partagé ; laisser le debounce/policy existant produire les candidats H1 → queue → delivery_adapter → lunettes. Résultat attendu : parler d'un sujet Y avec X déclenche rappels/suggestions issus de la mémoire — la capacité V18.8, dans le monde XR.
**Test** : transcript simulé mentionnant un sujet présent dans la mémoire de test → intervention en queue avec evidence → viewer.

## E32 — Identité multi-indice (visage + voix + enrollment)

**Constat** : aucune reco faciale ; voice_identity existe mais nocturne seulement ; « personne connue » n'existe donc pas en live → scénarios 2/3 bloqués.
**Faire** : embeddings faciaux locaux ONNX (ArcFace/InsightFace-like, licence vérifiée, MODEL_MANIFEST) sur crops person de VisionRT ; brancher `voice_identity`/`voice_embeddings` du cœur au flux audiort ; **enrollment vocal** (« retiens : c'est Sarah » → capture visage+voix → entité nommée + graine de relation pack) ; fusion multi-indice (visage+voix+contexte) avec seuil §17.2 (pas de nom sous confiance) ; correction vocale (« non, ce n'est pas Paul » → `memory_correction` existant).
**Test** : enrollment simulé → re-reconnaissance sur nouvelle frame/voix → PersonTag nommé + ContextCard relation pack.

## E33 — IntentRouter vocal, actions device, mode payant

**Constat** : après wake word, seuls des cas codés (où-est/what_is/ocr) ; pas de multi-tour ; pas de lancement d'apps ; `llm: openai/gemini` = config sans client.
**Faire** : routeur d'intentions général (grammaire locale rapide + repli parsing LLM live léger pour le reste), **multi-tour** (contexte de la dernière commande/réponse/cible : « et ça ? », « zoom dessus », « traduis-le ») ; actions Android (Intents : Maps navigation, YouTube, app arbitraire, volume/luminosité lunettes via one-xr si utile) ; **toggles UI à la voix** (« cache tout », « mode Free Guy », « pause privée ») branchés au broker/density + câbler le geste balayage Kotlin→Unity déjà émis ; **mode payant** : clients OpenAI/Gemini/Anthropic derrière `LLMProvider`/`VisionModelProvider` (bascule vocale « mode payant » / retour local, indicateur StatusBar cloud actif, estimation de coût par requête affichée, politique de données du profil respectée).
**Test** : chaîne voix simulée → intent routé → action/toggle ; bascule cloud opt-in mockée + réelle si clé fournie.

## E34 — Proactivité réelle & hot context device

**Constat** : les prédictions nocturnes ne sont pas injectées dans le live ; `entities_hot` du téléphone ne reçoit que la vision ; 3 situations proactives seulement.
**Faire** : charger les prédictions/attentions du jour (life model store + outcomes) dans le HotSceneContext du scene_adapter → suggestions proactives contextuelles (« tu voulais racheter X », routine déviée, promesse due) ; **prefetch des relation packs** vers le SceneCache device à la reconnaissance d'une personne (latence zéro pour la ContextCard) ; **briefing du matin** (première session du jour → carte résumé : agenda déduit, prédictions, choses à ne pas oublier).
**Test** : prédiction du jour en base → scène correspondante simulée → suggestion proactive en queue ; briefing généré à l'ouverture de session.

## E35 — Sorties : voix, correction, replay

**Faire** : **TTS local** (sherpa-onnx TTS, même dépendance que l'ASR) pour les réponses courtes (« c'est quoi ça » en conduite/capture-only, confirmations) avec toggle voix/silence ; endpoint **replay** (clips/keyframes par plage horaire depuis les tables existantes) → `VirtualScreen` (composant déjà prêt) et companion-web ; correction vocale câblée bout en bout.
**Test** : requête « rejoue 14h30 » simulée → clip servi → VirtualScreen intent.

## E36 — Ops de prod

**Faire** : accès hors-maison (Tailscale/WireGuard documenté + testé : le live contextuel dehors passe par le VPN, latence mesurée, politique dégradée explicite sinon) ; **backup automatique chiffré** de la mémoire (SQLite + médias evidence → destination configurable, planifié, testé en restauration) ; quotas stockage surveillés par doctor ; profil temporaire d'inconnu via VLM (description apparence → entité provisoire non nommée, fusionnable à l'enrollment).

## Puis : les deux finals

- **E30-A (PC, sans matériel)** : close-day réel complet — Qdrant + Ollama allumés, vie synthétique injectée, 10 stages chronométrés < 6 h sur RTX 3070, journal gpu_phase, doctor -Memory, benchs publiés.
- **E30-B (matériel)** : gates G1→G8 réels (Unity + SDK XREAL + S25), bench LAN réel, session 3 h, capture-only sur second téléphone, compilation Kotlin/Unity de E22-E26.
