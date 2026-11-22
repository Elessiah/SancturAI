# Registre des findings — Task 17 (branche feat/relay)

Base: f19446d

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | relay/relay.py:process_batch | row locks + open transaction held across up to 20 HTTP calls (200 s) in process_batch; all sends replayed if connection drops mid-batch | fixed | |
| P2 | perf | major | app/maintenance.py:run_maintenance_once | purge_old_rows seq-scans outbox (no sent_at/created_at index) every 60 s and its failure blocks the error/disk-alert flush | fixed | |
| P3 | perf | minor | relay/relay.py:main | sleep(interval) after a full batch caps backlog drain at 20 events / 5 s; per-call timeout set per request instead of on the client | fixed | |
| P4 | perf | minor | app/maintenance.py:purge_old_rows | unbatched DELETEs | wontfix | |

P4 wontfix: outbox is bounded to ~30 days of low-volume events; hourly purge now, batching would be over-engineering.
