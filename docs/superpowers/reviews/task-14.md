# Registre des findings — Task 14

Base: 0505f40

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/admin_messages/service.py:232 | check-then-insert short_id (1 SELECT per try, race between concurrent sends gives IntegrityError 500) in AdminMessageService._new_short_id | fixed | b6f1a9b |
| P2 | perf | major | app/admin_messages/service.py:271 | unbounded message history reloaded and scanned on every call in AdminMessageService.inbox | fixed | b6f1a9b |
| P3 | perf | minor | app/admin_messages/api.py:93 | MAX_TEXT duplicated with outbox.service.MAX_TEXT["agent_message"], magic `+ 100` Field slack in SendIn/ReplyIn | open | |
| P4 | perf | minor | app/admin_messages/service.py:222 | DB count fetches timestamps and duplicates limiter state (DB needed across restarts, limiter for races) in AdminMessageService._check_daily_limit | open | |
