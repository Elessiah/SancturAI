# Registre des findings — Task 4

Base: ba28a77

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/ratelimit.py:_gc | stale buckets kept up to 1 day regardless of window (unbounded memory) in RateLimiter._gc | fixed | |
| P2 | perf | major | app/ratelimit.py:hit | IndexError (500) when limit<=0 on empty bucket in RateLimiter.hit | fixed | |
| P3 | perf | major | app/identity/sessions.py:revoke | full _mcp scan per revoke makes purge/revoke_agent O(sessions x mcp) in SessionStore.revoke | fixed | |
| P4 | perf | minor | app/identity/sessions.py:bind_mcp | orphan mcp entries for unknown token never purged in SessionStore.bind_mcp | fixed | |
| P5 | perf | minor | app/ratelimit.py:hit | deque allocated on every call via setdefault in RateLimiter.hit | fixed | |
| P6 | perf | minor | app/state.py:AppState | duplicated 86_400 default TTL in AppState.sessions | wontfix | |

wontfix P6: default factory is needed for directly-built AppState in tests; build() overrides it with the setting.
