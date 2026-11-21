# Registre des findings — Task 2

Base: f52adbb

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/crypto.py:106 | magic number 16 (Poly1305 tag size) in decrypt | fixed | 4347c23 |
| P2 | perf | minor | app/crypto.py:73 | phrase normalized twice (is_valid_phrase then derive_keys) in derive_keys | wontfix | |
| S1 | secu | minor | app/crypto.py:69 | unbounded input normalized (NFKD/split) before any length check in is_valid_phrase | fixed | |

P2 wontfix: negligible cost next to Argon2 (64 MiB); keeping is_valid_phrase as sole validation path avoids duplicated logic.
