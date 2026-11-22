# Registre des findings — Visibilité Task 7

Base: 8a97dd1

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/tribunal/proceedings.py:58 | exclusive standing lock of the accused (plus accused_id lookup) taken on every vote, needed only by a ban-closing vote, in TrialService._lock_for_closure | wontfix | |
| P2 | perf | minor | app/tribunal/proceedings.py:117 | trial thread reply insertion (lock, Post, reply_count, last_activity) duplicated from system_reply in TrialService.defend | fixed | c2f10cf |
| P3 | perf | minor | app/tribunal/proceedings.py:85 | four sequential queries (row, dossier, defense, your_vote) folded into one select with scalar subqueries in TrialService.read_trial | fixed | c2f10cf |
| P4 | perf | minor | app/cli.py:25 | chained add_parser/add_argument split by a run of spaces mid-line for unfreeze in _parser | fixed | c2f10cf |
| S1 | secu | major | app/tribunal/proceedings.py:143 | vote accepted after the deadline until the periodic task runs, can flip the deadline verdict (tie/zero = reactivate) in TrialService.vote | fixed | SHA |
| S2 | secu | minor | app/tribunal/proceedings.py:125 | defense accepted after the deadline in TrialService.defend | fixed | SHA |
| S3 | secu | minor | app/tribunal/proceedings.py:66 | votes of a voter frozen during the trial (no longer eligible) still counted in TrialService._tally | fixed | SHA |
| S4 | secu | minor | app/forum/service.py:207 | trial defense editable by the accused after reactivation (spec: non modifiable) in ForumService.edit | fixed | SHA |

P1 wontfix : la seule alternative correcte (verrou du procès d'abord, puis pg_try_advisory_xact_lock exclusif et repli par ROLLBACK TO SAVEPOINT, qui libère le verrou du procès sans relâcher le verrou partagé du votant) ajoute un chemin de reprise pour un gain négligeable ; prendre le verrou après le procès sans repli rouvre l'interblocage avec la défense, et un rollback complet relâcherait le verrou de statut du votant (S1 Task 6). Les votes sont bornés par eligible_count par procès, déjà sérialisés par la ligne trials, et l'accusé gelé n'a en vol que me/trial_read/trial_defense.
Revu sans finding : décompte agrégé GROUP BY sur la clé primaire (trial_id, voter_id) ; échéances par une requête sur ix_trials_status_deadline puis une transaction par procès dû ; trial_list paginée (count + page jointe, sans N+1) ; outils MCP délégués à TrialService (pas de doublon avec REST) ; aucune I/O bloquante (disque mis en cache par DiskGuard).
Perf round 1: no major (final perf round), all minors fixed or wontfix.
Revu sans finding (secu round 1) : une voix par agent (clé primaire, upsert), probation/gelé/banni/accusé/système exclus, banni : votes des procès ouverts purgés ; majorité stricte sur eligible_count figé ; clôture unique sous concurrence (ligne trials FOR UPDATE + relecture du statut ; unfreeze et admin_ban réévaluent status = 'open') ; décompte agrégé sans votants, your_vote du seul appelant, dossier sans signaleurs ; gelé : son procès seulement (FROZEN pour tout autre id) ; protection 30 j exclue pour closed_by = admin, règlement une seule fois ; outbox nettoyée ; MCP = REST (mêmes gardes dans le service).
Secu round 1: 1 major (S1) fixed, minors fixed.
