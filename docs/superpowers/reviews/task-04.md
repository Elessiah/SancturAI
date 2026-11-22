# Registre des findings — Task 4

Base: ba28a77

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/ratelimit.py:_gc | stale buckets kept up to 1 day regardless of window (unbounded memory) in RateLimiter._gc | fixed | 9d27a14 |
| P2 | perf | major | app/ratelimit.py:hit | IndexError (500) when limit<=0 on empty bucket in RateLimiter.hit | fixed | 9d27a14 |
| P3 | perf | major | app/identity/sessions.py:revoke | full _mcp scan per revoke makes purge/revoke_agent O(sessions x mcp) in SessionStore.revoke | fixed | 9d27a14 |
| P4 | perf | minor | app/identity/sessions.py:bind_mcp | orphan mcp entries for unknown token never purged in SessionStore.bind_mcp | fixed | 9d27a14 |
| P5 | perf | minor | app/ratelimit.py:hit | deque allocated on every call via setdefault in RateLimiter.hit | fixed | 9d27a14 |
| P7 | perf | minor | app/ratelimit.py:hit | tuple re-stored on every call although window unchanged in RateLimiter.hit | fixed | cd0a311 |
| P8 | perf | minor | app/identity/sessions.py:bind_mcp | empty set left in _mcp_by_token after rebind in SessionStore.bind_mcp | fixed | cd0a311 |
| P6 | perf | minor | app/state.py:AppState | duplicated 86_400 default TTL in AppState.sessions | wontfix | |
| S1 | secu | major | app/identity/sessions.py:create | unbounded live sessions per agent via repeated logins (memory DoS, O(n) purge) in SessionStore.create | fixed | d55d8ba |
| S2 | secu | major | app/ratelimit.py:hit | unbounded bucket count from rotating attacker keys e.g. IPv6 (memory DoS on shared host) in RateLimiter.hit | fixed | d55d8ba |
| S3 | secu | minor | app/ratelimit.py:hit | same key used with two windows shares one deque, short window evicts long-window hits (limit bypass) in RateLimiter.hit | fixed | 45f613b |
| S4 | secu | minor | app/identity/sessions.py:bind_mcp | live MCP binding silently rebound to another token/agent, previous token left alive with keys in memory in SessionStore.bind_mcp | fixed | 45f613b |
| S5 | secu | minor | app/identity/sessions.py:Session | keys held as immutable bytes cannot be zeroed on logout (plan-mandated) in Session | wontfix | |
| S6 | secu | major | app/ratelimit.py:hit | global MAX_KEYS shared by all key kinds: cheap rotating-IP register:{ip} flood (1 h window, ~28 req/s) fills cap and fails closed every untracked agent:/forum key (lockout of authenticated agents) in RateLimiter.hit | fixed | 0bfccd4 |

wontfix P6: default factory is needed for directly-built AppState in tests; build() overrides it with the setting.
wontfix S5: décision utilisateur : en Python, l'effacement mémoire des secrets n'offre pas de garantie réelle (copies PyNaCl/hashlib/interpréteur)
