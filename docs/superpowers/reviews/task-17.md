# Registre des findings — Task 17 (branche feat/relay)

Base: f19446d

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | relay/relay.py:process_batch | row locks + open transaction held across up to 20 HTTP calls (200 s) in process_batch; all sends replayed if connection drops mid-batch | fixed | b41791f |
| P2 | perf | major | app/maintenance.py:run_maintenance_once | purge_old_rows seq-scans outbox (no sent_at/created_at index) every 60 s and its failure blocks the error/disk-alert flush | fixed | b41791f |
| P3 | perf | minor | relay/relay.py:main | sleep(interval) after a full batch caps backlog drain at 20 events / 5 s; per-call timeout set per request instead of on the client | fixed | b41791f |
| P4 | perf | minor | app/maintenance.py:purge_old_rows | unbatched DELETEs | wontfix | |
| S1 | secu | major | relay/relay.py:format_payload | agent/short_id forwarded as raw attacker-controlled JSONB, text/kind neither typed nor bounded (no strict allow-list) in format_payload | fixed | b30b704 |
| S2 | secu | major | relay/relay.py:process_batch | unbounded payload (up to ~255 MB jsonb x BATCH) loaded into host memory by CLAIM_PENDING in process_batch; a malformed row raised out of the loop | fixed | b30b704 |
| S3 | secu | minor | relay/relay.py:main | empty/missing SANCTURAI_BOT_TOKEN not refused at startup in main (crash loop or 401 retries burning attempts) | fixed | |
| S4 | secu | minor | relay/relay.py:main | httpx.Client trust_env=True in main honors HTTP(S)_PROXY/ALL_PROXY/.netrc (token + agent text could leave via a proxy) | fixed | |

P4 wontfix: outbox is bounded to ~30 days of low-volume events; hourly purge now, batching would be over-engineering.

Secu round 2: no new finding (bounds match app MAX_TEXT/HANDLE_PATTERN and the bot contract; bot 400 and out-of-contract rows set attempts=MAX_ATTEMPTS, never re-claimed). Final round: S3, S4 fixed.
