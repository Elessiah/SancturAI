# Registre des findings — Task 14

Base: 0505f40

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/admin_messages/service.py:232 | check-then-insert short_id (1 SELECT per try, race between concurrent sends gives IntegrityError 500) in AdminMessageService._new_short_id | fixed | b6f1a9b |
| P2 | perf | major | app/admin_messages/service.py:271 | unbounded message history reloaded and scanned on every call in AdminMessageService.inbox | fixed | b6f1a9b |
| P3 | perf | minor | app/admin_messages/api.py:93 | MAX_TEXT duplicated with outbox.service.MAX_TEXT["agent_message"], magic `+ 100` Field slack in SendIn/ReplyIn | fixed | f28a0df |
| P4 | perf | minor | app/admin_messages/service.py:222 | DB count fetches timestamps and duplicates limiter state (DB needed across restarts, limiter for races) in AdminMessageService._check_daily_limit | wontfix | |
| P5 | perf | major | app/admin_messages/service.py:80 | retry loop catches every IntegrityError (FK on deleted agent retried 20x then misleading SERVER_BUSY) in AdminMessageService._create_thread | fixed | f28a0df |
| S1 | secu | major | app/admin_messages/api.py:37 | /internal existence oracle without token (GET → "Method Not Allowed", malformed JSON → 422 before the dependency, "not found" vs "Not Found"), guard per-route so future /internal routes unguarded (spec §9.3) in internal_guard/verify_internal | fixed | |
| S2 | secu | major | app/admin_messages/service.py:63 | plan-mandated: 4-hex short_id never recycled, 65 536 threads total then permanent SERVER_BUSY for every new thread (f^20 failure: 1 % at 80 % fill, 12 % at 90 %; 100 agents × 3/day fill it in ~7 months, 1 000 in ~3 weeks) in AdminMessageService._create_thread | open | |
| S3 | secu | minor | app/admin_messages/service.py:73 | daily limit TOCTOU after a restart (DB count read without lock, in-memory limiter empty: concurrent sends reach up to 5/24h) in AdminMessageService.send | open | |
| S4 | secu | minor | app/admin_messages/service.py:33 | compare_digest on raw bytes may leak the token length, and only an empty configured token fails closed (no minimum length) in verify_internal | open | |

P4 wontfix: la table est bornée à 3 lignes/jour/agent et le compteur DB doit survivre aux redémarrages, le limiteur mémoire ne sert qu'à la course ; fusionner coûterait plus qu'il ne rapporte.
