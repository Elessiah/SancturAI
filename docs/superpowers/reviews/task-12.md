# Registre des findings — Task 12

Base: 46f0fe3

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/forum/moderation.py:_delete | N+1 : un upsert ReporterStat par signalant dans la boucle in ModerationService._delete | fixed | |
| P2 | perf | minor | app/forum/moderation.py:vote | écriture upsert/delete + requêtes inutiles quand delta == 0 (vote inchangé) in ModerationService.vote | open | |
| P3 | perf | minor | app/forum/moderation.py:_delete | re-verrouillage thread+post redondant (remove_post relit ce que _lock_post a déjà verrouillé) in ModerationService._delete | open | |
| P4 | perf | minor | app/forum/moderation.py:_evaluate | deux requêtes agrégées séparées (somme des poids, count upvotes) in ModerationService._evaluate | open | |
| P5 | perf | minor | alembic/versions/0005_moderation.py | pas d'index sur forum_votes.agent_id / forum_reports.agent_id (cascade FK de suppression d'agent en seq scan) in 0005 | open | |
| P6 | perf | minor | app/forum/moderation.py:vote/report | limites 200 / 20 / 86_400 en dur au lieu de constantes in ModerationService | open | |
| P7 | perf | minor | alembic/versions/0005_moderation.py | commentaires Alembic « auto generated – please adjust » résiduels in 0005 | open | |
