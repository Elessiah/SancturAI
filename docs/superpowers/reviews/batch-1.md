# Registre des findings — Batch 1 (Tasks 1-10), revue transverse

Base: bb54565

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| I1 | batch | important | app/identity/api.py:RegisterStartIn/RegisterCompleteIn | NUL in invite_code/challenge_id gives unauthenticated, unlimited 500 that feeds the Discord 5xx alert; register/complete has no IP limit | fixed | f053c6a |
| I2 | batch | important | app/vault/service.py:aad_path/aad_content | vault AAD strings part of the immutable v1 contract but not in spec, not in crypto.py, not frozen by vectors; phrase normalization not in spec | fixed | f053c6a, 288cfbc |
| I3 | batch | important | plan T16:ip(ctx) | planned MCP ip(ctx) skips normalize_ip: per-IP limits bypassable over MCP (/64) and split from REST | fixed (plan) | 288cfbc |
| I4 | batch | important | plan T17 purge/backoff, plan T18 formatNotify | T6 S5 (abandoned outbox rows, 1.4 h backoff) and T6 S4 (Discord mentions/markdown/fake prefix) wontfix notes not carried in plan text | fixed (plan) | 288cfbc |
| M1 | batch | minor | app/vault/api.py GET routes | vault GET responses lack Cache-Control: no-store; two conventions (per-route lines) | fixed | f053c6a |
| M2 | batch | minor | app/identity/api.py:logout | logout counts toward agent: 60/min; a limited agent cannot erase its keys | fixed | f053c6a |
| M3 | batch | minor | app/identity/service.py:derive | SERVER_BUSY without retry_after after a passed challenge; retry after 30 s burns a challenge | fixed | f053c6a |
| M4 | batch | minor | various (HINTS, INSTRUCTIONS, LoginIn, HANDLE_RE, plan T16 [:64]) | constants drift (3/18/30 s, handle regex x4, phrase 300 vs 1024, invite 32 vs 64) | fixed | f053c6a, 288cfbc |
| M5 | batch | minor | app/vault/api.py:list_files | E2E switch inconsistent with spec §6.3 (header vs ?raw=1) | fixed | f053c6a, 288cfbc |
| M6 | batch | minor | app/vault/service.py:_lock_agent, app/identity/service.py:create_invite | FOR UPDATE on agents blocks FOR KEY SHARE of every FK insert | fixed | f053c6a |
| M7 | batch | minor | app/vault/service.py:list_files | list prefix not NFC-normalized (NFD input misses NFC paths) | fixed | f053c6a |
| M8 | batch | minor | alembic 0002/0003, tests/test_migrations.py | no CHECK on vault size / outbox attempts / outbox kind; downgrade untested | fixed | f053c6a |
| M9 | batch | minor | app/vault/service.py:_lock_agent | vanished agent with live session: FK IntegrityError → 500 | fixed | f053c6a |
| M10 | batch | minor | tests/identity/test_suspension.py | §7.4 vault access for suspended agent untested | fixed | f053c6a |
| M11 | batch | minor | tests/conftest.py:9 | testcontainers.postgres DeprecationWarning | fixed | f053c6a |
| M12 | batch | minor | app/vault/service.py:_decrypt_content | every AEAD failure reported as "encrypted client-side" (misleading on corruption) | fixed | f053c6a |
| M13 | batch | minor | plan T19 nginx | client_max_body_size 8m < worst-case JSON of 5 MB non-ASCII text; HTML 413 not §9.2 | fixed (plan) | 288cfbc |
| M14 | batch | minor | tests/vault/test_vault_at_rest.py | §9.3 dump test bypasses create_agent/README/outbox and ignores the session token | fixed | f053c6a |
| M15 | batch | minor | tests/helpers.py:make_agent | pool index = count(agents): deletion then make_agent reuses a live identity | fixed | f053c6a |

Notes:
- I1: pattern `^[A-Za-z0-9_-]{1,32}$` (REST Pydantic + `IdentityService`, so MCP is covered); `register-complete:{ip}` 10/h; DBAPIError SQLSTATE class 22 / `DataError` → `VALIDATION_ERROR` (fixed message, no value); shared sanitizer `app.text.check_text(value, field)` (reject) / `strip_control(text)` (strip), wired into plan T11/T12/T14.
- M8: CHECKs added by editing migrations 0002/0003 in place (nothing deployed yet; 0004+ revision ids are reserved by plan Tasks 11-14).
- M9: `VaultService` has no access to the session store, so the session is not revoked (it expires within 24 h); every vault write answers `UNAUTHENTICATED`.
- M1: the `INTERNAL` 500 rendered by the outermost `_CatchAll`/`ServerErrorMiddleware` lies outside the middleware and carries no `no-store` (body is a fixed, secret-free message).
