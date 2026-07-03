# Contrat d’intégration Phone Bridge V18.1

Le code Phone Bridge n’est pas inclus dans cette archive. Ce document est donc un contrat de déploiement, pas une preuve d’exécution Android/iOS.

## Entrées autorisées

Chaque capture envoyée au service doit conserver son sidecar/provenance et son owner explicite. Le Bridge ne substitue jamais un owner local par défaut.

## Stop et conservation

Le receiver doit exécuter cette séquence, dans cet ordre :

```text
1. stop de session (ne plus accepter de nouveau média pour cette session)
2. drain / stabilisation des fichiers et événements déjà déposés
3. post-stop V18 session-scopé
4. coordinator day-scopé pour les runs globaux V17/Life Model si nécessaire
5. v18-poststop-cleanup-check RUN_ID --person-id PERSON_ID
6. suppression raw uniquement si la réponse structurée indique eligible=true
```

Toute erreur réseau, sortie invalide, timeout, owner différent, manifeste incomplet ou `eligible=false` signifie **conserver le raw media**. Le Bridge doit journaliser la décision et reporter la purge, jamais la forcer.

## Interventions live

Le Bridge ne crée pas une livraison par horizon. Il lit la queue de livraison H1, ou laisse le service BrainLive la gérer :

```text
signal → analyse fusionnée → décision H1 → queue dédupliquée → livraison Bridge
```

H0 et H2 peuvent rester visibles comme observations, mais ne déclenchent pas chacun une notification indépendante.

## Tests d’intégration requis avant activation

- un seul signal transcript avec une intervention candidate produit une seule livraison ;
- owner explicitement transmis de la capture au post-stop ;
- toute purge est refusée sans manifest retenu et cleanup gate positive ;
- le Bridge tolère les retries idempotents et un retour de queue déjà connu ;
- la mauvaise valeur de `person_id`, un sidecar invalide ou une réponse de gate non parseable bloquent la suppression.
