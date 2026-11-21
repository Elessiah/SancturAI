# Registre des findings — Task 6

Base: e78ac41

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/main.py:lifespan | cancelled maintenance task not awaited before engine.dispose() in lifespan | fixed | e13feae |
| P2 | perf | major | app/maintenance.py:run_maintenance_once | error count and disk alert lost when DB write fails in run_maintenance_once | fixed | e13feae |
| P3 | perf | minor | app/state.py:AppState.disk | hardcoded 0.85 default and "85 %" message duplicate disk_busy_ratio setting in AppState.disk / run_maintenance_once | fixed | FIX |
| P4 | perf | minor | alembic/versions/0002_outbox.py | attempts column has no server_default (python-side default only) in OutboxEvent.attempts | fixed | FIX |
| P5 | perf | minor | app/maintenance.py:run_maintenance_once | error count taken before disk probe, lost if disk usage raises in run_maintenance_once | fixed | FIX |
