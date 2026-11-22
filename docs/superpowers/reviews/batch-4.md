# Registre des findings — Batch 4 (Task 16, serveur MCP), revue transverse

Base: 8235633..d58afd5. Décisions marquées « décidé par l'orchestrateur (délégation utilisateur) ».

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| C1 | batch (hors périmètre) | critical | app/main.py (REST) | REST bodies not size-limited in the app: FastAPI parses the body before auth/limiter/pydantic, 3 × 16 MiB dense JSON to /api/login OOM the 896m container | fixed — BodyLimitMiddleware (app/middleware.py), outermost pure ASGI before routing/parsing: per-route caps 16 MiB (PUT /api/vault/…, by-id, /mcp), 128 KiB (/api/forum/), 64 KiB elsewhere incl. unknown routes; Content-Length rejected upfront, chunked bodies counted while read (stops at cap, never handed to the app); VALIDATION_ERROR without echo. nginx per-location caps: wontfix here, live on feat/deploy | 967759d |
| I1 | batch | important | app/mcp_server.py:SessionOpenLimit._dispatch | MCP large-body lock covered only bodies > 1 MiB; aggregate parse memory unbounded (40 × 1 MiB dense ≈ 960 MiB) | fixed — same middleware holds an 18 MiB in-flight byte budget shared by REST and MCP (declared length, chunked = route cap then shrunk to real size), bounded wait 10 s then SERVER_BUSY + Retry-After 5, bounded hold 60 s (JSON SERVER_BUSY if no response yet, S9/S11 kept); arithmetic in app/middleware.py (≈400 MiB baseline + 25 × 18 MiB ≈ 450 MiB < 896). MCP lock, its 1 MiB threshold and its two timers removed | 967759d |
| I2 | batch | important | app/mcp_server.py:build_mcp | SDK max_sessions left at 10 000 (≈600 MiB+), above the OOM line; per-IP cap does not hold on IPv6 | fixed — max_sessions=1000 (≈60 MiB heap, ≈120 MiB RSS, in the budget), session_idle_timeout=1800 explicit (app/mcp_limits.py); prune threshold on DEFAULT_MAX_SESSIONS replaced by a 60 s sweep. 600 s idle: wontfix (30 min documented in the guide) — décidé par l'orchestrateur (délégation utilisateur) | 967759d |
| I3 | batch | important | app/main.py:mount /mcp | exact `/mcp` answered 307 to http://…/mcp/ (clients refuse or replay in clear) | fixed — Route("/mcp") replays the request on the router as /mcp/ (same guard, same middlewares); live test with and without slash | 967759d |
| M1 | batch | minor | app/identity/sessions.py | ending an MCP session left the bound token/keys until the 24 h TTL | fixed — SessionStore.drop_dead_mcp(is_live), run right after a DELETE and by a sweep at most every 60 s on MCP traffic (idle-reaped sessions). Residual: with no MCP traffic at all, keys of idle-reaped sessions wait for the next request or the TTL — accepted | 967759d |
| M2 | batch | minor | app/mcp_server.py:forum_threads/read/search | MCP clamped pages where REST rejects | fixed — Field(ge=1, le=PAGE_MAX / SEARCH_PAGE_MAX), VALIDATION_ERROR like REST (same ruling as S5) — décidé par l'orchestrateur (délégation utilisateur) | 967759d |
| M3 | batch | minor | app/mcp_server.py:register_start/complete | limiter hit before disk and pattern checks (burns register: budget) | fixed — invite_code/challenge_id typed max_length=32 + URLSAFE_ID_PATTERN, ensure_writable() before the limiter | 967759d |
| M4 | batch | minor | app/mcp_server.py (all dict tools) | FastMCP returned text + structuredContent (two copies, 2 × 6.7 M chars for a 5 MB vault_read) | fixed — every tool registered with structured_output=False (text JSON only, the guide's contract) | 967759d |
| M5 | batch | minor | pyproject.toml / app/mcp_server.py | private SDK internals | wontfix (accepted with the 1.30.x pin + live tests); pin comment now lists all three: session_manager._server_instances, FastMCP._tool_manager, ToolError.__cause__ = ValidationError (DEFAULT_MAX_SESSIONS no longer used) | 967759d |
| M6 | batch | minor | app/discovery.py | guide silent on 30 min MCP idle expiry, per-IP MCP limits, non-JSON transport errors | fixed — all three added (values from app/mcp_limits.py: 404 then initialize + login; 20 open sessions and 60 openings/h per IP or /64, close with DELETE; JSON-RPC 404/503 and plain-text 421/403), plus body sizes and SERVER_BUSY; test_help_text_documents_mcp_transport_limits | 967759d |
| M7 | batch | minor | tests/test_mcp.py, tests/conftest.py | MCP-only disk guards, SUSPENDED, shared agent: bucket, /mcp untested; live_url can spin forever | fixed — test_mcp_disk_guards_and_suspension, test_mcp_shares_agent_bucket_with_rest, test_mcp_without_trailing_slash, test_mcp_register_checks_before_spending_ip_budget; live_url fails after 30 s or when the server task ends | 967759d |

## Notes de conception (I1 + C1)
- Hold bound now applies to every request carrying a body (REST included): a REST request still running after 60 s
  is cancelled with SERVER_BUSY. Body-less requests (GET, the MCP SSE stream) are neither counted nor bounded.
- No per-IP share of the budget: global saturation gives SERVER_BUSY, never OOM (same trade-off as nginx
  `limit_conn sancturai_total`) — décidé par l'orchestrateur (délégation utilisateur).
- SDK `max_request_body_size` stays at 16 MiB (= MAX_UPLOAD_BYTES) as defence in depth.

## Preuves TDD
RED (avant implémentation) : `tests/test_body_limit.py` ImportError (BodyLimitMiddleware absent) ;
`tests/test_mcp.py tests/identity/test_sessions.py` : 7 failed, 22 passed — session cap (ImportError),
`/mcp` 307, keys kept after DELETE, pages clamped, register budget spent, structuredContent duplicated,
drop_dead_mcp missing. GREEN : 272 passed ; ruff et mypy propres.
