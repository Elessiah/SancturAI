# Registre des findings — Task 1

Base: d8de511

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/errors.py:129 | unmapped 4xx HTTPException (400/405/408/415...) falls back to INTERNAL (500, no hint match, not counted) and 405 mapped to NOT_FOUND in _HTTP_TO_CODE (user-approved deviation: unmapped 4xx -> VALIDATION_ERROR, 5xx -> INTERNAL; 405 kept NOT_FOUND) | fixed | FIXSHA |
| P2 | perf | minor | app/state.py:235 | hard-coded Semaphore(2) default duplicates Settings.argon2_concurrency in AppState.argon_sem (AppState.argon_sem now required, built only via build()) | fixed | FIXSHA |
