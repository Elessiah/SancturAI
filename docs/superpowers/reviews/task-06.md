# Registre des findings — Task 6

Base: e78ac41

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/main.py:lifespan | cancelled maintenance task not awaited before engine.dispose() in lifespan | fixed | e13feae |
| P2 | perf | major | app/maintenance.py:run_maintenance_once | error count and disk alert lost when DB write fails in run_maintenance_once | fixed | e13feae |
| P3 | perf | minor | app/state.py:AppState.disk | hardcoded 0.85 default and "85 %" message duplicate disk_busy_ratio setting in AppState.disk / run_maintenance_once | fixed | 8b69fa6 |
| P4 | perf | minor | alembic/versions/0002_outbox.py | attempts column has no server_default (python-side default only) in OutboxEvent.attempts | fixed | 8b69fa6 |
| P5 | perf | minor | app/maintenance.py:run_maintenance_once | error count taken before disk probe, lost if disk usage raises in run_maintenance_once | fixed | 8b69fa6 |
| S1 | secu | major | app/outbox/service.py:enqueue | NUL byte / lone surrogate in agent text makes JSONB insert fail (500, business transaction rolled back) in enqueue | fixed | 3732004 |
| S2 | secu | major | app/disk.py:DiskGuard.is_busy | disk probe error (OSError, total=0) raises instead of failing closed: writes get 500 not SERVER_BUSY, no disk alert, and maintenance never flushes 5xx counts in DiskGuard.is_busy | fixed | 3732004 |
| S3 | secu | minor | app/outbox/service.py:enqueue | `**extra` payload keys not restricted to the bot contract (agent, short_id), no guardrail against leaking other fields in enqueue | fixed | |
| S4 | secu | minor | app/outbox/service.py:enqueue | Discord mention/markdown in agent text (@everyone, fake "[SancturAI] #xxxx de" prefix) not neutralized in enqueue | wontfix | |
| S5 | secu | minor | app/outbox/models.py:OutboxEvent | no retention of sent/abandoned outbox rows (growth bounded only by upstream rate limits) in OutboxEvent | wontfix | |
| S6 | secu | minor | app/outbox/service.py:enqueue | string values in `**extra` / nested payload (handle, short_id) not sanitized for JSONB (NUL, lone surrogate) in enqueue | fixed | |

- S4 wontfix : traité côté bot (Task 18).
- S5 wontfix : purge prévue en Task 17 — attention, la purge prévue ne cible que `sent_at < now() - 30 j` ; les lignes abandonnées (`sent_at IS NULL AND attempts >= 10`) ne seraient jamais purgées et croîtraient sans borne : Task 17 doit aussi purger celles-ci (par `created_at`).
