# Registre des findings — Task 9

Base: 03ab996

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/vault/service.py:put_raw | blobs (content_enc up to 5 MB) loaded for existing row and reloaded by refresh on every overwrite, in put_raw | fixed | 7fa1710 |
| P2 | perf | major | app/vault/service.py:delete | full row with content_enc loaded just to delete it, in delete | fixed | 7fa1710 |
| P3 | perf | major | app/vault/service.py:move | lock order row->agent inverse of put_raw agent->row: deadlock risk, in move | fixed | 7fa1710 |
| P4 | perf | minor | app/vault/service.py:read | duplicated decrypt/CryptoError->FORBIDDEN block in read and move | fixed | XXX |
| P5 | perf | minor | app/vault/service.py:list_files | unbounded listing (no file-count cap/pagination; zero-size files bypass quota), decrypts every row, in list_files | open | |
| P6 | perf | minor | app/vault/service.py:_put | 5 MB encrypt/decrypt runs on the event loop, in _put/read | wontfix | |

wontfix P6: libsodium XChaCha20 does ~5 MB in a few ms; offloading to a thread adds complexity for no measurable gain.
