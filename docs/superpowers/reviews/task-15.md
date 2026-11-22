# Registre des findings — Task 15

Base: 02793da

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/discovery.py:_TEMPLATE (Erreurs) | wrong claim "MCP error text starts with REST code (RATE_LIMITED: …)" in _TEMPLATE; plan §MCP raises ToolError(json) so FastMCP yields "Error executing tool <nom>: {json}" | fixed | d15322c |
| P2 | perf | minor | app/discovery.py:_TEMPLATE (Vault E2E) | derivation spec incomplete in _TEMPLATE: HKDF has no salt/32-byte output, phrase normalization also collapses whitespace, path_enc AAD usage unstated | fixed | f4ce73d |
| P3 | perf | minor | app/discovery.py:help_text | hard-coded quotas/session TTL in _TEMPLATE duplicate configurable Settings values (drift) in help_text | fixed | f4ce73d |

P2 fixed in the same commit (E2E derivation block completed: by-id body, HKDF details, normalization, AAD usage).
