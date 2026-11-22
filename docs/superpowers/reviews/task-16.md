# Registre des findings — Task 16

Base: 8235633

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/mcp_server.py:vault_* | extra `db.get(Agent)` on every vault call (REST vault uses session only) in authed/vault_* | fixed | b5094c7 |
| P2 | perf | minor | app/mcp_server.py:db_only/authed | duplicated error-mapping try/except in db_only and authed | fixed | b5094c7 |
| P3 | perf | minor | app/mcp_server.py:ip | reimplements REST `client_ip` instead of calling it on the request | open | |
| P4 | perf | minor | app/mcp_server.py | `# type: ignore[type-arg]` repeated on every tool's Context parameter (no alias) | open | |
| P5 | perf | minor | app/mcp_server.py:forum_threads/forum_read/forum_search | page clamps use magic numbers duplicating REST Query bounds | open | |
