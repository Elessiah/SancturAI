# Registre des findings — Task 20

Base: 1a9170f

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | deploy/nginx/sancturai.conf:97 | proxy/limiter/header directives duplicated between `location ~ ^/mcp(/|$)` and `location /` | wontfix | - |
| P2 | perf | minor | .github/workflows/ci.yml:image | arm64 image built under QEMU emulation instead of a native arm runner | wontfix | - |
| P3 | perf | minor | .github/workflows/ci.yml:test | relay install/tests run serially after the app tests in the same job | wontfix | - |

- P1 wontfix: nginx cannot share a location body; an `include` snippet would add a second file to install (setup.md §4 copies one file). Drift is already guarded by `test_nginx_mcp_body_cap_matches_app` (directive-set equality with `location /`).
- P2 wontfix: deps are pure wheels (no compile), the gha cache (`mode=max`) is configured, and the job runs only on push to main; a native arm runner cannot be validated from here and the brief mandates a plain `linux/arm64` build.
- P3 wontfix: relay suite is 41 fast tests; a separate job would repeat checkout and uv setup and save little wall time. Lint and types already run before the tests (fail-fast order), and uv cache covers both lockfiles (`**/uv.lock`).
