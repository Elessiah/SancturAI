# Registre des findings — Task 16

Base: 8235633

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/mcp_server.py:vault_* | extra `db.get(Agent)` on every vault call (REST vault uses session only) in authed/vault_* | fixed | b5094c7 |
| P2 | perf | minor | app/mcp_server.py:db_only/authed | duplicated error-mapping try/except in db_only and authed | fixed | b5094c7 |
| P3 | perf | minor | app/mcp_server.py:ip | reimplements REST `client_ip` instead of calling it on the request | fixed | 4f39132 |
| P4 | perf | minor | app/mcp_server.py | `# type: ignore[type-arg]` repeated on every tool's Context parameter (no alias) | fixed | 4f39132 |
| P5 | perf | minor | app/mcp_server.py:forum_threads/forum_read/forum_search | page clamps use magic numbers duplicating REST Query bounds | fixed | 4f39132 |
| P6 | perf | minor | app/mcp_server.py:authed | redundant `ensure_writable` in authed(writable=True): forum/moderation/invite services already enforce it | fixed | 4f39132 |
| P7 | perf | minor | app/mcp_server.py:Call | unused `session` field on Call dataclass | fixed | 4f39132 |
| S1 | secu | major | app/mcp_server.py:vault_*/forum_*/admin_message_send/login | string args lack the REST `max_length` caps (vault content/path/src/dst/prefix, forum category/title/body/reason/query, admin text, phrase): oversized input processed, unbounded `category` echoed in NOT_FOUND, in build_mcp tools | fixed | eb67e5d |
| S2 | secu | major | app/main.py:mount /mcp | unauthenticated MCP session opening unlimited per IP: one client fills the SDK's 10 000 sessions (30 min idle) and locks every agent out (503), in /mcp mount | fixed | eb67e5d |
| S3 | secu | major | app/mcp_server.py:build_mcp | SDK default 4 MiB body cap makes MCP `vault_write` reject 5 MB files that REST and spec §6 accept (non-JSON 413), in build_mcp | fixed | eb67e5d |
| S4 | secu | minor | app/mcp_server.py:with_session | expired/revoked MCP session reports UNAUTHENTICATED (Bearer hint) instead of SESSION_EXPIRED as REST, in with_session/token_for_mcp | open | |
| S5 | secu | minor | app/mcp_server.py:register_start/register_complete | plan-mandated: `handle[:32]`, `answers[:40]`, `a[:200]` silently truncate where REST rejects with VALIDATION_ERROR, in register_start/register_complete — décidé par l'utilisateur : refus comme REST (HANDLE_MAX/ANSWERS_MAX/ANSWER_MAX partagés) | fixed | 91e464c |
| S6 | secu | major | app/mcp_server.py:SessionOpenLimit | per-IP open *rate* only: 60 sessions/h kept alive (each ping resets the 30 min idle) accumulate without bound, so one IP fills the SDK's 10 000 sessions in ~7 days (167 IPs/64s in 1 h), in SessionOpenLimit | fixed | 91e464c |
| S7 | secu | major | app/mcp_server.py:SessionOpenLimit/MCP_MAX_BODY_BYTES | 16 MiB body cap (eb67e5d): SDK `json.loads` of an object-dense body (`[{},{},…]`) allocates ~400 MiB (measured, 4 MiB → ~100), so 2 concurrent unauthenticated POSTs OOM the 896m single-worker container, in SessionOpenLimit | fixed | 91e464c |
