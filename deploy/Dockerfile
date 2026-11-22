FROM python:3.13-slim AS build
# Même version que `uv --version` en local et que `version:` de setup-uv dans ci.yml (mettre les trois à jour ensemble)
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
RUN useradd --uid 10001 --gid 33 --no-create-home --shell /usr/sbin/nologin sancturai
WORKDIR /app
COPY --from=build --chown=root:root /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:33
CMD ["sh", "-c", "umask 0007 && exec uvicorn app.main:create_app --factory --uds /run/sancturai/app.sock --workers 1 --no-server-header --no-proxy-headers --no-access-log"]
