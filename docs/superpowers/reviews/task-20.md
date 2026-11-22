# Registre des findings — Task 20

Base: 1a9170f

| ID | Cycle | Sév. | Fichier:ligne | Signature | Statut | Commit |
|----|-------|------|---------------|-----------|--------|--------|
| P1 | perf | minor | deploy/nginx/sancturai.conf:97 | proxy/limiter/header directives duplicated between `location ~ ^/mcp(/|$)` and `location /` | wontfix | - |
| P2 | perf | minor | .github/workflows/ci.yml:image | arm64 image built under QEMU emulation instead of a native arm runner | wontfix | - |
| P3 | perf | minor | .github/workflows/ci.yml:test | relay install/tests run serially after the app tests in the same job | wontfix | - |
| S1 | secu | minor | .github/workflows/ci.yml:test,image | checkout persists GITHUB_TOKEN in .git/config (persist-credentials default) in test and image jobs | fixed | e4a99a4 |
| S2 | secu | minor | .github/workflows/ci.yml:image | third-party actions pinned by major tag, not commit SHA, in the job holding packages: write | fixed | 0394573 |

- P1 wontfix: nginx cannot share a location body; an `include` snippet would add a second file to install (setup.md §4 copies one file). Drift is already guarded by `test_nginx_mcp_body_cap_matches_app` (directive-set equality with `location /`).
- P2 wontfix: deps are pure wheels (no compile), the gha cache (`mode=max`) is configured, and the job runs only on push to main; a native arm runner cannot be validated from here and the brief mandates a plain `linux/arm64` build.
- P3 wontfix: relay suite is 41 fast tests; a separate job would repeat checkout and uv setup and save little wall time. Lint and types already run before the tests (fail-fast order), and uv cache covers both lockfiles (`**/uv.lock`).
- S2 fixed: every `uses:` in ci.yml is pinned to a full commit SHA with a `# vX.Y.Z` comment, and .github/dependabot.yml (weekly, github-actions) keeps the pins current.
- Reviewed, no defect: `pull_request` (not `_target`), top-level `contents: read` with `packages: write` only on `image`, no secret in PR builds, push gated on main, GHCR owner lowercased in `run` via env (no injection). nginx `^/mcp(/|$)` regex wins over prefix `/` and `/internal/` (no overlap); same zones/limits/X-Real-IP/headers (inherited server-level include), `/internal/` still 404; app BodyLimitMiddleware remains the authority where nginx is looser.
