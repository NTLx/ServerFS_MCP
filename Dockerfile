# syntax=docker/dockerfile:1

# ---------- build stage: install locked deps into /app ----------
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY README.md ./

# locked, no dev deps, into a plain venv we can copy
RUN uv sync --frozen --no-dev --no-editable && \
    uv pip install --python /app/.venv/bin/python .

# ---------- runtime stage ----------
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ripgrep && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get clean

# dedicated non-root account (no login shell, no home contents)
RUN groupadd -g 10001 serverfs && \
    useradd -u 10001 -g 10001 -s /usr/sbin/nologin -M serverfs

WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# smoke-test that the serverfs_mcp package and rg are importable/runnable
RUN .venv/bin/python -c "import serverfs_mcp; import shutil; assert shutil.which('rg')" && \
    rg --version

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=5s \
    CMD .venv/bin/python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=2).close()"

CMD ["python", "-m", "serverfs_mcp"]
