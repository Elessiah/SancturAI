# Registre des findings — Task 15

Base: 02793da

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/discovery.py:_TEMPLATE (Erreurs) | wrong claim "MCP error text starts with REST code (RATE_LIMITED: …)" in _TEMPLATE; plan §MCP raises ToolError(json) so FastMCP yields "Error executing tool <nom>: {json}" | fixed | d15322c |
| P2 | perf | minor | app/discovery.py:_TEMPLATE (Vault E2E) | derivation spec incomplete in _TEMPLATE: HKDF has no salt/32-byte output, phrase normalization also collapses whitespace, path_enc AAD usage unstated | open | |
