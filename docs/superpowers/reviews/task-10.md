# Registre des findings — Task 10

Base: 4fcf303

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/vault/service.py:delete_raw | DELETE...RETURNING duplicated in delete_raw / delete | fixed | 6ff5aa7 |
| P2 | perf | minor | app/vault/service.py:list_raw | listing SELECT duplicated in list_raw / list_files | fixed | 6ff5aa7 |
| P3 | perf | minor | app/vault/api.py:write_raw | path_id validated only after base64-decoding up to 5 MB | fixed | 6ff5aa7 |
| S1 | secu | major | app/vault/service.py:MAX_PATH_ENC_BYTES | path_enc cap (1024 B, RawWriteIn max_length=2000) below server-mode max (512 chars NFC up to 4 B each + 40 = 2088 B): E2E client cannot write a valid non-ASCII path, nor re-PUT a blob returned by read_raw, breaking §6.3 compatibility (plan-mandated) | fixed | a82980f |
| S2 | secu | minor | app/vault/api.py:write_raw | /by-id/{pid} declared before /{path:path} shadows server-mode paths under /by-id/ (REST read/write of e.g. /by-id/notes.md impossible) (plan-mandated) | fixed | a82980f |
| S3 | secu | minor | tests/vault/test_vault_e2e.py | no test that another agent's path_id is NOT_FOUND on by-id GET/PUT/DELETE and raw listing | fixed | a82980f |

S1/S2 plan-mandated, fixed per user ruling: S1 `MAX_PATH_ENC_BYTES = 4 * MAX_PATH + AEAD_OVERHEAD` (2088), `RawWriteIn.path_enc` max_length 2784; S2 `/by-id` prefix reserved in `normalize_path` (spec §6.1 updated).
Secu round 2: no new finding (final round, S3 fixed).
