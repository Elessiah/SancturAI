# Registre des findings — Task 9

Base: 03ab996

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/vault/service.py:put_raw | blobs (content_enc up to 5 MB) loaded for existing row and reloaded by refresh on every overwrite, in put_raw | fixed | 7fa1710 |
| P2 | perf | major | app/vault/service.py:delete | full row with content_enc loaded just to delete it, in delete | fixed | 7fa1710 |
| P3 | perf | major | app/vault/service.py:move | lock order row->agent inverse of put_raw agent->row: deadlock risk, in move | fixed | 7fa1710 |
| P4 | perf | minor | app/vault/service.py:read | duplicated decrypt/CryptoError->FORBIDDEN block in read and move | fixed | 3d8c213 |
| P5 | perf | minor | app/vault/service.py:list_files | unbounded listing (no file-count cap/pagination; zero-size files bypass quota), decrypts every row, in list_files | fixed (via S2) | 38d7d54 |
| P6 | perf | minor | app/vault/service.py:_put | 5 MB encrypt/decrypt runs on the event loop, in _put/read | wontfix | |
| S1 | secu | major | app/vault/api.py:routes | plaintext vault paths in request URL (PUT/GET/DELETE /api/vault/{path}, ?prefix=) written to uvicorn/nginx access logs (plan Dockerfile CMD lacks --no-access-log), plan-mandated, in vault router | open | |
| S2 | secu | major | app/vault/service.py:put_raw | no file-count cap: zero-size files and uncounted path_enc bytes bypass the 50 MB quota, unbounded rows/listing (re-files P5), in put_raw | fixed | 38d7d54 |
| S3 | secu | minor | app/vault/service.py:_put | path+content encrypted before the per-file size check (allocation before rejection), in _put | fixed | b679d2c |
| S4 | secu | minor | app/vault/paths.py:normalize_path | control characters other than NUL (LF, CR, TAB, DEL, C1) accepted in paths, in normalize_path | fixed | b679d2c |

wontfix P6: libsodium XChaCha20 does ~5 MB in a few ms; offloading to a thread adds complexity for no measurable gain.
wontfix S1: corrigé côté déploiement (plan Task 19, commit c1fd436) : pas d'URI dans les logs.
