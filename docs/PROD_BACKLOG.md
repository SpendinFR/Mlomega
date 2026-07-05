# PROD_BACKLOG â€” MLOmega V19, complÃ©tion produit (E31â†’E36)

Issu de l'audit d'alignement visionâ†”livrÃ© du 2026-07-04. Principe directeur : **brancher l'existant avant de construire du neuf** â€” le cÅ“ur V18.8 contient dÃ©jÃ  la rÃ©activitÃ© conversationnelle (hot capsule, relation packs, open loops, interventions proactives H1), les modÃ¨les relationnels et la correction mÃ©moire ; plusieurs Â« gaps Â» sont des tuyaux manquants, pas des capacitÃ©s manquantes. MÃªme discipline que E1â†’E29 : une Ã©tape = une branche = une PR, push au fil de l'eau, tests rÃ©els exÃ©cutÃ©s ici quand c'est du PC, ADR dans DECISIONS.md.

## E31 â€” Conversation live â†’ BrainLive V18.8 (LE branchement prioritaire)

**Constat** : audiort produit les transcripts (sous-titres) mais la boucle BrainLive du cÅ“ur ne les reÃ§oit pas â€” le moteur d'interventions conversationnelles existe (v18_8_live_policy, hotloop, turn buffer) et n'entend pas la conversation V19.
**Faire** : injecter les segments finaux d'audiort dans le chemin d'entrÃ©e live du cÅ“ur (turn buffer / live session â€” lire `brainlive_realtime_v15_2`/`brainlive_hotloop_v15_6`/`v18_8_live_policy.plan_live_dispatch` pour le point d'entrÃ©e exact ; ADR) avec `live_session_id` V19 partagÃ© ; laisser le debounce/policy existant produire les candidats H1 â†’ queue â†’ delivery_adapter â†’ lunettes. RÃ©sultat attendu : parler d'un sujet Y avec X dÃ©clenche rappels/suggestions issus de la mÃ©moire â€” la capacitÃ© V18.8, dans le monde XR.
**Test** : transcript simulÃ© mentionnant un sujet prÃ©sent dans la mÃ©moire de test â†’ intervention en queue avec evidence â†’ viewer.

## E32 â€” IdentitÃ© multi-indice (visage + voix + enrollment)

**Constat** : aucune reco faciale ; voice_identity existe mais nocturne seulement ; Â« personne connue Â» n'existe donc pas en live â†’ scÃ©narios 2/3 bloquÃ©s.
**Faire** : embeddings faciaux locaux ONNX (ArcFace/InsightFace-like, licence vÃ©rifiÃ©e, MODEL_MANIFEST) sur crops person de VisionRT ; brancher `voice_identity`/`voice_embeddings` du cÅ“ur au flux audiort ; **enrollment vocal** (Â« retiens : c'est Sarah Â» â†’ capture visage+voix â†’ entitÃ© nommÃ©e + graine de relation pack) ; fusion multi-indice (visage+voix+contexte) avec seuil Â§17.2 (pas de nom sous confiance) ; correction vocale (Â« non, ce n'est pas Paul Â» â†’ `memory_correction` existant).
**Test** : enrollment simulÃ© â†’ re-reconnaissance sur nouvelle frame/voix â†’ PersonTag nommÃ© + ContextCard relation pack.

## E33 â€” IntentRouter vocal, actions device, mode payant

**Constat** : aprÃ¨s wake word, seuls des cas codÃ©s (oÃ¹-est/what_is/ocr) ; pas de multi-tour ; pas de lancement d'apps ; `llm: openai/gemini` = config sans client.
**Faire** : routeur d'intentions gÃ©nÃ©ral (grammaire locale rapide + repli parsing LLM live lÃ©ger pour le reste), **multi-tour** (contexte de la derniÃ¨re commande/rÃ©ponse/cible : Â« et Ã§a ? Â», Â« zoom dessus Â», Â« traduis-le Â») ; actions Android (Intents : Maps navigation, YouTube, app arbitraire, volume/luminositÃ© lunettes via one-xr si utile) ; **toggles UI Ã  la voix** (Â« cache tout Â», Â« mode Free Guy Â», Â« pause privÃ©e Â») branchÃ©s au broker/density + cÃ¢bler le geste balayage Kotlinâ†’Unity dÃ©jÃ  Ã©mis ; **mode payant** : clients OpenAI/Gemini/Anthropic derriÃ¨re `LLMProvider`/`VisionModelProvider` (bascule vocale Â« mode payant Â» / retour local, indicateur StatusBar cloud actif, estimation de coÃ»t par requÃªte affichÃ©e, politique de donnÃ©es du profil respectÃ©e).
**Test** : chaÃ®ne voix simulÃ©e â†’ intent routÃ© â†’ action/toggle ; bascule cloud opt-in mockÃ©e + rÃ©elle si clÃ© fournie.
**Ajout inventaire cÅ“ur (2026-07-04)** : intent Â« interroge ma mÃ©moire Â» â†’ brancher le routeur Brain2 riche (`brain2_router_v14_2.ask_brain2`, aujourd'hui CLI-only) plutÃ´t que le `/query` simple d'api.py â€” poser une question Ã  sa mÃ©moire depuis les lunettes.

## E34 â€” ProactivitÃ© rÃ©elle & hot context device

**Constat** : les prÃ©dictions nocturnes ne sont pas injectÃ©es dans le live ; `entities_hot` du tÃ©lÃ©phone ne reÃ§oit que la vision ; 3 situations proactives seulement.
**Faire** : charger les prÃ©dictions/attentions du jour (life model store + outcomes) dans le HotSceneContext du scene_adapter â†’ suggestions proactives contextuelles (Â« tu voulais racheter X Â», routine dÃ©viÃ©e, promesse due) ; **prefetch des relation packs** vers le SceneCache device Ã  la reconnaissance d'une personne (latence zÃ©ro pour la ContextCard) ; **briefing du matin** (premiÃ¨re session du jour â†’ carte rÃ©sumÃ© : agenda dÃ©duit, prÃ©dictions, choses Ã  ne pas oublier).
**Test** : prÃ©diction du jour en base â†’ scÃ¨ne correspondante simulÃ©e â†’ suggestion proactive en queue ; briefing gÃ©nÃ©rÃ© Ã  l'ouverture de session.
**Ajouts inventaire cÅ“ur (2026-07-04) â€” moteurs nocturnes Ã  consommer EN LIVE (P1)** : (a) `proactive_interventions_v14_7` â€” gÃ©nÃ©rÃ© la nuit seulement aujourd'hui, Ã  consulter/dÃ©clencher en session ; (b) `v18_predictive_retrieval` â€” la rÃ©cupÃ©ration prÃ©dictive dense doit enrichir le contexte live, pas seulement la calibration nocturne ; (c) `microscope`/`discourse_context` â€” l'analyse fine du discours (fils de sujets, actes de parole) doit tourner sur les tours live d'E31, pas seulement sur l'import batch ; (d) P2 : exposer en session les questions de `clarification_inbox_v14_8` gÃ©nÃ©rÃ©es la nuit (le systÃ¨me pose SA question au bon moment).

## E35 â€” Sorties : voix, correction, replay

**Faire** : **TTS local** (sherpa-onnx TTS, mÃªme dÃ©pendance que l'ASR) pour les rÃ©ponses courtes (Â« c'est quoi Ã§a Â» en conduite/capture-only, confirmations) avec toggle voix/silence ; endpoint **replay** (clips/keyframes par plage horaire depuis les tables existantes) â†’ `VirtualScreen` (composant dÃ©jÃ  prÃªt) et companion-web ; correction vocale cÃ¢blÃ©e bout en bout.
**Test** : requÃªte Â« rejoue 14h30 Â» simulÃ©e â†’ clip servi â†’ VirtualScreen intent.

## E36 â€” Ops de prod

**Faire** : accÃ¨s hors-maison (Tailscale/WireGuard documentÃ© + testÃ© : le live contextuel dehors passe par le VPN, latence mesurÃ©e, politique dÃ©gradÃ©e explicite sinon) ; ~~**backup automatique chiffrÃ©** de la mÃ©moire (SQLite + mÃ©dias evidence â†’ destination configurable, planifiÃ©, testÃ© en restauration)~~ **DIFFÃ‰RÃ‰ (dÃ©cision utilisateur 2026-07-05 â€” usage perso, gÃ©rÃ© manuellement)** ; quotas stockage surveillÃ©s par doctor ; profil temporaire d'inconnu via VLM (description apparence â†’ entitÃ© provisoire non nommÃ©e, fusionnable Ã  l'enrollment).

**PrioritÃ© utilisateur (2026-07-05)** : l'usage principal est DEHORS (tÃ©lÃ©phone en 4G/5G, PC Ã  la maison derriÃ¨re NAT) â†’ l'accÃ¨s hors-maison est **LE** livrable. Le backup chiffrÃ© est reportÃ© (gÃ©rÃ© Ã  la main).

## Puis : les deux finals

- **E30-A (PC, sans matÃ©riel)** : close-day rÃ©el complet â€” Qdrant + Ollama allumÃ©s, vie synthÃ©tique injectÃ©e, 10 stages chronomÃ©trÃ©s < 6 h sur RTX 3070, journal gpu_phase, doctor -Memory, benchs publiÃ©s.
- **E30-B (matÃ©riel)** : gates G1â†’G8 rÃ©els (Unity + SDK XREAL + S25), bench LAN rÃ©el, session 3 h, capture-only sur second tÃ©lÃ©phone, compilation Kotlin/Unity de E22-E26.


## E37 â€” Nuit complÃ¨te + owner (FAIT â€” audit de clÃ´ture 2026-07-05)

Faille critique rÃ©parÃ©e : l'audio brut V19 n'Ã©tait pas archivÃ© â†’ le nocturne (WhisperX+pyannote+attribution voixâ†’personID) n'avait rien Ã  traiter. Segments VAD archivÃ©s + events `speech_segment` au format Phone Bridge exact ; bundle bi-modal audio+vision prouvÃ© ; owner enrÃ´lable en V19 (Â« configure ma voix Â») ; `memory_owner_id` garanti sur la chaÃ®ne vision ; garde-fou pose placeholder.

## E38 â€” Intelligence fine (FAIT â€” 2026-07-05, branche feat/v19-e38-fine-intel)

(a) **Auto-confirmation d'hypothÃ¨ses d'identitÃ©** : prÃ©nom entendu dans la conversation â†’ associÃ© Ã  la personne prÃ©sente ; observations rÃ©pÃ©tÃ©es multi-sessions renforcent l'hypothÃ¨se Â« ? boulanger Â» ; promotion automatique probableâ†’observed Ã  seuil avec preuves ; rÃ©solution automatique des hypothÃ¨ses `clarification_inbox_v14_8`/`UNKNOWN_VOICE` quand les signaux convergent (sans enrollment manuel â€” l'enrollment reste le raccourci).
(b) **Changements d'attributs bi-modaux** : apparence des personnes connues inter-sessions (coiffure/vÃªtements â€” Â« PersonX a changÃ© de coupe Â») ; texte/prix par lieu : OCR mÃ©morisÃ© comparÃ© Ã  la re-visite ET croisÃ© avec les faits ENTENDUS en conversation au mÃªme endroit (Â« hier c'Ã©tait pas ce prix Â» â€” vu ou entendu). Nouveau type ChangeEvent `attribute_changed`.
(c) **Routineâ†’objet** : l'approche d'une entitÃ© liÃ©e Ã  une routine fait remonter proactivement le last-seen de l'objet associÃ© (TV â†’ tÃ©lÃ©commande), depuis `brain2_spatial_routine_models` + co-occurrences.

## ADR Â§E37 (complÃ©ment â€” session coupÃ©e avant la doc)

- Format `speech_segment` : dÃ©couvert dans le cÅ“ur (writer du Phone Bridge dans la chaÃ®ne sensor_fusion/service V15) et reproduit Ã  l'identique par `audio_archive.py` â€” colonnes/payload conformes Ã  ce que lisent `bundles_require_deep_audio` et `collect_live_raw_timeline`.
- SubtilitÃ© assembleur : `audio_timeline_json` se remplit via les tours texte (`brainlive_turn_buffer`, modality audio_text) tandis que le dÃ©clencheur deep audio lit les events `speech_segment` â€” les deux coexistent dans le mÃªme bundle (bridge conversationnel E31 + archive E37), c'est le fonctionnement nominal V18.8 reproduit.
- Conflit routeur corrigÃ© : patterns `owner_enroll` enregistrÃ©s AVANT `set_tts` (le `\bparle\b` du toggle TTS avalait Â« c'est moi qui parle Â»).
- Quota audio : ligne ajoutÃ©e au doctor `-Quota` ; purge par le close-day comme le tampon-jour.
