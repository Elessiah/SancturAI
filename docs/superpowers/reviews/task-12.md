# Registre des findings — Task 12

Base: 46f0fe3

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/forum/moderation.py:_delete | N+1 : un upsert ReporterStat par signalant dans la boucle in ModerationService._delete | fixed | e6612e3 |
| P2 | perf | minor | app/forum/moderation.py:vote | écriture upsert/delete + requêtes inutiles quand delta == 0 (vote inchangé) in ModerationService.vote | fixed | 11c044b |
| P3 | perf | minor | app/forum/moderation.py:_delete | re-verrouillage thread+post redondant (remove_post relit ce que _lock_post a déjà verrouillé) in ModerationService._delete | wontfix | |
| P4 | perf | minor | app/forum/moderation.py:_evaluate | deux requêtes agrégées séparées (somme des poids, count upvotes) in ModerationService._evaluate | fixed | 11c044b |
| P5 | perf | minor | alembic/versions/0005_moderation.py | pas d'index sur forum_votes.agent_id / forum_reports.agent_id (cascade FK de suppression d'agent en seq scan) in 0005 | fixed | 11c044b |
| P6 | perf | minor | app/forum/moderation.py:vote/report | limites 200 / 20 / 86_400 en dur au lieu de constantes in ModerationService | fixed | 11c044b |
| P7 | perf | minor | alembic/versions/0005_moderation.py | commentaires Alembic « auto generated – please adjust » résiduels in 0005 | fixed | 11c044b |
| S1 | secu | major | app/forum/service.py:remove_post | signalements emportés par une suppression hors modération (auteur qui supprime son post signalé, réponses d'un fil supprimé) comptés comme échecs : un auteur fait chuter la précision/poids de ses signalants in ForumService.remove_post / ModerationService._delete | fixed | |
| S2 | secu | major | app/forum/moderation.py:_evaluate | plan-mandated : bait-and-switch — les votes +1 obtenus avant une édition continuent de protéger le post édité en spam (spec §7.2 édition libre + §7.4 seuil > votes +1) in ModerationService._evaluate | open | |

P3 wontfix : chemin de suppression rare ; le re-verrouillage est idempotent (déjà détenu) et conserve l'ordre documenté fil→post ainsi que le contrat de `remove_post` pour ses autres appelants.
