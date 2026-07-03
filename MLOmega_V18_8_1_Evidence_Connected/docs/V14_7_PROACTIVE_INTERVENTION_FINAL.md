# V14.7 — Proactive Intervention Layer Final

Cette couche transforme Brain2 de miroir passif en compagnon d’orientation temporelle.
Elle ne remplace aucune couche précédente. Elle lit les signaux créés par V13/V14/V14.2/V14.3/V14.4/V14.5/V14.6 et décide lesquels méritent une intervention au bon moment.

## Pourquoi cette couche existe

Le système savait déjà dire après coup :

- tu es dans une boucle ;
- Max te tend souvent ;
- une micro-interaction positive peut améliorer ta journée ;
- une action est ouverte ;
- un choix ressemble à un ancien choix ;
- une prédiction doit être vérifiée.

V14.7 ajoute la question décisive : **faut-il te le dire maintenant, plus tard, ou seulement le garder dans le rapport ?**

## Ce qu’elle crée

- opportunités d’intervention ;
- queue d’interventions ;
- priorités low / medium / high / critical ;
- timing now / soon / today / before_next_action / watch_only ;
- cooldown pour éviter les doublons ;
- export `intervention_inbox_<person_id>.md` ;
- feedback utilisateur : acted / dismissed / helpful / not_relevant / too_intrusive ;
- hooks pour mesurer plus tard si l’intervention a aidé.

## Exemples

- Conversation tendue avec Max → “pause avant décision, risque de rumination/report”.
- Micro-échange positif → “bonne fenêtre pour lancer une petite action”.
- Boucle surcharge → complexification → blocage → “réduis à 10 minutes”.
- Désir ouvert depuis plusieurs jours → “prochaine action minuscule à faire aujourd’hui”.
- Prédiction ou forecast actif → “surveiller sans notifier”.

## Limites

V14.7 ne pousse pas directement sur iPhone. Elle écrit une queue et un fichier export ; un pont iPhone/PC peut ensuite les utiliser pour notifier.

Elle ne force pas l’action : elle propose une hypothèse avec preuves, risque, timing et feedback.
