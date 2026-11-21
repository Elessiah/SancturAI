# Registre des findings — Task 10

Base: 4fcf303

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | app/vault/service.py:delete_raw | DELETE...RETURNING duplicated in delete_raw / delete | fixed | |
| P2 | perf | minor | app/vault/service.py:list_raw | listing SELECT duplicated in list_raw / list_files | fixed | |
| P3 | perf | minor | app/vault/api.py:write_raw | path_id validated only after base64-decoding up to 5 MB | fixed | |
