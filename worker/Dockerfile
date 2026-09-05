# Iris Realtime Worker Dockerfile.
#
# Every image is pinned to a full version (Dependabot bumps them, see
# .github/dependabot.yml): a floating tag meant the next `docker compose build`
# on a fresh box could pull a different Python or uv than the one that was
# tested, and nobody would know until a call failed.

# uv gets its own FROM line rather than an inline `COPY --from=<image>`:
# Dependabot's docker ecosystem only reads FROM, so an inline pin is one it
# would never bump.
FROM ghcr.io/astral-sh/uv:0.12.8 AS uv

# ---- Stage 1: build the venv ----
FROM python:3.14.7-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ---- Stage 2: runtime ----
FROM python:3.14.7-slim AS runner
WORKDIR /app

# The worker runs as an unprivileged uid, not root. A fixed uid (10001) rather
# than a name so compose can pin `user:` and the tmpfs /tmp is owned by the
# same identity whichever base image is underneath.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin worker

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 . .

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# The root filesystem is read-only in compose (deploy/docker-compose.yml) and
# /tmp is a tmpfs. Bytecode is never written (it would fail silently anyway),
# and HOME points at the one writable place so any library ~/.cache lands there
# rather than raising EROFS from a directory that does not exist.
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/tmp
USER 10001:10001
# /ready, not /health: /health stays 200 while a dependency is merely degraded
# so orchestrators do not restart a live process; /ready is the one that gates
# (503 on misconfiguration: missing config, db credentials or key). A required-
# but-missing pepper never gets this far — agent.py dies at boot before the
# server exists, and the restart policy loops it. The probe reads
# RT_DOCKER_HEALTH_HOST/PORT from the container env with rt_health.py's
# defaults, so moving the port does not turn every check into a 'down'.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://%s:%s/ready' % (os.getenv('RT_DOCKER_HEALTH_HOST') or '127.0.0.1', os.getenv('RT_DOCKER_HEALTH_PORT') or '8080'), timeout=2)" || exit 1
CMD ["python", "agent.py", "start"]
