# Registre des findings — Batch 3 (Tasks 14-15), revue transverse

Base: 0505f40..9699f56. Décisions marquées « décidé par l'orchestrateur (délégation utilisateur) ».

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| I1 | batch | important | tests/test_admin_messages.py | no test that an agent's inbox excludes other agents' threads/replies | fixed — test_inbox_isolated_between_agents (B's inbox, A's unread untouched by B's GET, B cannot post into A's thread) | ca15d58 |
| M1 | batch | minor | tests/test_admin_messages.py:65,196 | discovery tests' baseline was the guarded /internal 404 | fixed — baseline POST /does-not-exist | ca15d58 |
| M2 | batch | minor | tests/test_admin_messages.py:113 | misnamed test; mark-only-shown not exercised | fixed — renamed; test_reply_arriving_during_inbox_stays_unread | ca15d58 |
| M3 | batch | minor | app/admin_messages/service.py:inbox | unread replies beyond the 50 most recent stay unread and hidden | fixed — window = 50 most recent + every unread (RED: first 5 replies missing); no pagination (wontfix, threads bounded to 50 by updated_at) | ca15d58 |
| M4 | batch | minor | service.py:inbox/send | read-on-fetch at-most-once; no idempotency key on send | wontfix — plan-mandated / accepted for v1 (décidé par l'orchestrateur (délégation utilisateur)); note for the MCP task | - |
| M5 | batch | minor | app/main.py:create_app | silent fail-closed with short internal token | fixed — one WARNING at startup, no token value (test). env.example requirement: wontfix here, lives on feat/deploy | ca15d58 |
| M6 | batch | minor | app/discovery.py | hard-coded constants; TTL < 1 h rendered '0 h' | fixed — all values derived from constants/Settings; _duration() renders minutes | ca15d58 |
| M7 | batch | minor | app/discovery.py | post editing and POST /api/invites missing | fixed — PATCH /api/forum/posts/<id> and an 'Inviter' section (POST/GET /api/invites, TTL, max active), checked against routes | ca15d58 |
| M8 | batch | minor | app/middleware.py, service.py | /internal guard lives in admin_messages | wontfix — move changes the import path used by api.py and middleware.py (not trivial per ruling). AGENT_ROW_LOCK now used and unused _state attribute removed: fixed | ca15d58 |
| M9 | batch | minor | tests/test_maintenance.py:146 | ANALYZE stats survive the rollback | fixed — re-ANALYZE of the emptied table in a finally block | ca15d58 |
