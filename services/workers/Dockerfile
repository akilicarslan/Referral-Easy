# ════════════════════════════════════════════════════════════════════════════
# ReferralEasy — worker and scheduler, ONE image (§7, §11, §18)
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not this directory:
#   docker build -f services/workers/Dockerfile -t referraleasy/worker:dev .
# compose.yaml fixes that contract (`context: .`, `dockerfile: services/workers/
# Dockerfile`) and it is right for the same reason as the other Python services:
# this is a uv *workspace* member, so the install is driven by the root lockfile.
#
# ONE IMAGE, TWO SERVICES, chosen by SERVICE_ROLE. compose.yaml already names the
# role that way (`SERVICE_ROLE: worker`, `SERVICE_ROLE: scheduler`) and it is §18's
# shape applied to the other half of the platform: the role is deployment
# configuration, so the image carries both and neither is a second Dockerfile.
#
# WHAT THE ROLES ACTUALLY ARE, measured against the package rather than assumed:
#
#   worker     `python -m referraleasy_workers outbox --watch` — the outbox drain
#              loop (§7). It claims rows with SELECT ... FOR UPDATE SKIP LOCKED,
#              delivers them over HTTP, and sleeps between passes. This is a real,
#              long-running process; it is what `services/workers` was written for.
#
#   scheduler  NOT IMPLEMENTED, and this image refuses it rather than running
#              something that looks like it. There is no evaluation-sampling or
#              retention job in this repository: nothing reads EVAL_CRON,
#              EVAL_TIMEZONE or EVAL_SAMPLE_RATE (grep finds them only in
#              compose.yaml and .env.example), `referraleasy_eval` does not exist as
#              a package, and the one loop that does exist — the outbox drain — must
#              NOT be run twice: `referraleasy_workers/__main__.py` is explicit that a
#              second loop would be a second owner for the same rows. See ADR-0036.
#
# NO MODEL CALL AT STARTUP: this image has no model dependency at all. It carries
# contracts, domain and the agent runtime's budget/HTTP pieces, and `referraleasy_
# workers` reaches the world only through BOOKING_API_URL and NOTIFICATION_URL.
#
# NO LISTENER. Nothing here opens a port, so the image deliberately ships no curl and
# no EXPOSE: a probe for this container has to observe a process, and compose.yaml
# says which one and how (the Phase 0 entries claimed ports 8081/8082, which were a
# guess about a server that this package never had).
# ════════════════════════════════════════════════════════════════════════════

# ── stage 1: resolve and install the workspace into a venv ──────────────────
FROM python:3.12.15-slim-trixie AS build

# The uv binary from the pinned official image, the same version CI pins, so the image
# resolves with the resolver that produced uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/repo/.venv

WORKDIR /repo

# Manifests first, so the dependency layer survives every source edit. All nine, not
# just this one's: uv resolves the workspace from the root manifest and fails with
# "Distribution not found at: file:///repo/<member>" when one is missing, which is how
# services/gateway/Dockerfile stopped building when this member joined the workspace.
COPY pyproject.toml uv.lock ./
COPY packages/contracts/python/pyproject.toml packages/contracts/python/
COPY packages/domain/pyproject.toml packages/domain/
COPY packages/agent-runtime/pyproject.toml packages/agent-runtime/
COPY packages/rag/pyproject.toml packages/rag/
COPY packages/skills/pyproject.toml packages/skills/
COPY packages/telemetry/pyproject.toml packages/telemetry/
COPY packages/guardrails/pyproject.toml packages/guardrails/
COPY packages/eval/pyproject.toml packages/eval/
COPY packages/alerts/pyproject.toml packages/alerts/
COPY packages/release-registry/pyproject.toml packages/release-registry/
COPY services/gateway/pyproject.toml services/gateway/
COPY services/workers/pyproject.toml services/workers/
COPY services/agents/pyproject.toml services/agents/

# --package referraleasy-workers: the venv carries this member's dependency closure
# (contracts, domain, agent-runtime, psycopg, httpx) and not the specialists' ADK
# stack, which a queue drain has no use for. The manifests above are still all nine
# because the workspace must resolve before any member can be selected from it.
#
# --no-install-workspace is load-bearing: this layer runs before the sources are
# copied, and a workspace member installed from a tree that has no `src/` yet gets a
# dist-info directory and no code. The second sync below installs the real members.
#
# --no-editable: the runtime stage carries the venv and nothing else, so the member
# must be installed as a real wheel rather than as a pointer into /repo.
RUN uv sync --frozen --package referraleasy-workers --no-dev --no-editable --no-install-workspace

# The sources the wheels are built from, copied after the dependency layer so a source
# edit does not invalidate it, then the install that produces the wheels.
COPY packages/contracts/python/src packages/contracts/python/src
COPY packages/domain/src packages/domain/src
COPY packages/agent-runtime/src packages/agent-runtime/src
# §15's emitter, and a declared dependency of this service: the workers own the two HITL
# measurements because they own the human queue (ADR-0035). Its manifest was copied above and its
# source was not, so this install would have failed with
# `Distribution not found at: file:///repo/packages/telemetry`. Found by
# `tests/test_docker_member_lists.py`, which is why that guard exists.
COPY packages/telemetry/src packages/telemetry/src
COPY services/workers/src services/workers/src

RUN uv sync --frozen --package referraleasy-workers --no-dev --no-editable

# ── stage 2: runtime ──────────────────────────────────────────────────────
FROM python:3.12.15-slim-trixie AS runtime

# No extra packages: this image has no HTTP surface, so its probe needs no curl, and it
# reads /proc instead of the process table, so it needs no procps either (the
# HEALTHCHECK below records why `pgrep -f` is the wrong tool here — it was measured).
RUN groupadd --system app \
 && useradd --system --gid app --home-dir /app --create-home app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_ENV=production

WORKDIR /app

COPY --from=build /repo/.venv /app/.venv

RUN chown -R app:app /app
USER app

# PID 1 is the loop itself — the CMD below `exec`s it — so a loop that dies is an exited
# container (restart policy: unless-stopped), not an unhealthy one. What this probe adds
# is the case liveness cannot see: that PID 1 is still the drain loop and not a
# substituted command. compose.yaml overrides it with the same arm plus a psycopg
# connect to DATABASE_URL, and says what neither arm establishes.
#
# /proc/1/cmdline, NOT `pgrep -f "referraleasy_workers outbox"`, which was measured to be
# a vacuous arm: pgrep matches any process whose command line contains the pattern, and
# the shell running this test carries the pattern as one of its arguments — so it
# matched even in a container with no worker in it at all. PID 1's command line cannot
# contain the probe's own text.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD tr '\0' ' ' < /proc/1/cmdline | grep -q "referraleasy_workers outbox" || exit 1

# SERVICE_ROLE picks the command, exactly as AGENT_ROLE picks the agents' entry point
# in services/agents/Dockerfile. `exec` keeps the worker as PID 1's own process, so
# SIGTERM reaches it directly and a stop is a stop rather than a kill after the grace
# period.
#
# The scheduler arm refuses on purpose, with the reason on stderr and exit 64. The
# alternative — falling through to the outbox loop — would run a second owner of the
# same queue rows while claiming to be a scheduler, which is worse than not starting:
# a fabricated service is exactly what this repository's health and evaluation rules
# exist to prevent.
#
# One-shot commands (the package's other verb) are run as:
#   docker compose run --rm worker python -m referraleasy_workers reconcile <key>
# which replaces this CMD rather than competing with it.
CMD ["sh", "-c", "case \"$SERVICE_ROLE\" in \
worker) exec python -m referraleasy_workers outbox --watch ;; \
scheduler) echo 'SERVICE_ROLE=scheduler has no implementation in this repository: nothing reads EVAL_CRON/EVAL_TIMEZONE, and the outbox loop must not be run twice. See docs/adr/ADR-0036-*.md' >&2; exit 64 ;; \
*) echo \"SERVICE_ROLE is '$SERVICE_ROLE'; it must be 'worker' or 'scheduler'.\" >&2; exit 64 ;; \
esac"]
