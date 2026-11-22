# Registre des findings — Visibilité Task 4

Base: 5c504b1

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/forum/service.py:180 | probation post quota debited before thread lookup and tribunal rule in ForumService.reply | fixed | 2a43c44 |
| P2 | perf | minor | app/tribunal/reports.py:43 | weekly quota debited before target checks in AgentReportService.report | wontfix | |
| P3 | perf | minor | app/maintenance.py:34 | expiry window hard-coded (30 days literal) instead of REPORT_ACTIVE in purge_old_rows | fixed | 2a43c44 |
| P4 | perf | minor | app/tribunal/reports.py:75 | active/expired agent-report predicate duplicated across report, open_trial and purge in AgentReportService.report | fixed | 2a43c44 |
| P5 | perf | minor | app/maintenance.py:34 | expired agent-report purge without matching index in purge_old_rows | wontfix | |
| P6 | perf | minor | app/maintenance.py:37 | stale log label omitting agent reports in purge_old_rows | fixed | 2a43c44 |

- P2 wontfix : même ordre que `report_post` (REPORT_LIMIT avant `_lock_post`) ; le quota borne le coût (verrou de la cible, 4 requêtes) des tentatives refusées et le sondage de pseudos ; refus déterministes et expliqués. Comportement voulu, pas un défaut.
- P5 wontfix : purge horaire (PURGE_EVERY_SECONDS), table bornée (3 signalements / 7 j / agent, purgés à 30 j), même choix que l'outbox sans index ; un index dédié imposerait de toucher la migration 0008.
- P1 : classé minor (même ordre préexistant pour un fil inexistant) ; corrigé : le quota de probation (3/jour) n'est débité qu'après le contrôle du fil et de la règle tribunal ; test de non-débit ajouté.
- Perf tour 1 (final) : aucun major. Somme des poids et consommation : index partiel `ix_agent_reports_target_pending (target_id, created_at) WHERE trial_id IS NULL` ; doublon : index unique partiel ; procès d'un fil : `ix_trials_thread` ; protection : `ix_trials_accused_closed`. `eligible_voters` : un COUNT sur `agents` (petite table) une fois par ouverture. Pas de N+1 (pseudos des signaleurs en une requête `IN`), pas d'I/O bloquante. `reporter_weight` réutilisé, pas dupliqué. Découpage rules/system/opening/reports/api conforme à la structure du plan ; `system_reply` et les constantes DEFENSE_MAX/DELETIONS_* sont consommés par les Tasks 5-7.
