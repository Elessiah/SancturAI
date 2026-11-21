# Registre des findings — Task 1

Base: d8de511

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | major | app/errors.py:129 | unmapped 4xx HTTPException (400/405/408/415...) falls back to INTERNAL (500, no hint match, not counted) and 405 mapped to NOT_FOUND in _HTTP_TO_CODE (user-approved deviation: unmapped 4xx -> VALIDATION_ERROR, 5xx -> INTERNAL; 405 kept NOT_FOUND) | fixed | c419afe |
| P2 | perf | minor | app/state.py:235 | hard-coded Semaphore(2) default duplicates Settings.argon2_concurrency in AppState.argon_sem (AppState.argon_sem now required, built only via build()) | fixed | c419afe |
| S1 | secu | major | app/errors.py:178 | plan-mandated: catch-all Exception handler in install_error_handlers does not stop propagation — Starlette's ServerErrorMiddleware re-raises after sending the 500, so uvicorn logs the full traceback incl. exception message (e.g. SQLAlchemy DBAPIError with bound parameters) to docker logs, defeating the "never log the message" intent. Suggested fix: pure-ASGI catch-all middleware (log type only, record, send INTERNAL if response not started, do not re-raise) | open | |
| S2 | secu | minor | app/db.py:69 | plan-mandated: SQLAlchemy errors embed bound parameters (vault ciphertext, path_id, token hashes) in their str() in make_engine; suggested defense-in-depth: create_async_engine(..., hide_parameters=True) | open | |
