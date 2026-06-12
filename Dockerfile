# syntax=docker/dockerfile:1

# ---- build: resolve deps + install app into .venv ----
FROM ghcr.io/astral-sh/uv:python3.14-alpine AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# compile wheels that lack musllinux binaries (httptools, uvloop, etc.)
RUN apk add --no-cache build-base libffi-dev

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: only venv + app sources (templates) ----
FROM python:3.14-alpine AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apk add --no-cache ffmpeg curl \
    && mkdir -p /app/data /app/recordings \
    && chown -R zzk:zzk /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app

USER zzk

EXPOSE 8000

VOLUME ["/app/data", "/app/recordings"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
