# ════════════════════════════════════════════════════════════════════════════
# ReferralEasy — gateway (FastAPI, §9/§12)
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not this directory:
#   docker build -f services/gateway/Dockerfile -t referraleasy/gateway:dev .
# compose.yaml fixes that contract (context: ., dockerfile: services/gateway/
# Dockerfile) and it is the right one: this is a uv *workspace* member, so the
# install is driven by the root lockfile and the root pyproject's member list.
#
# The guardrail layers are wired: §9's three come from `packages/guardrails`,
# which this image installs as a dependency (see the manifest and source copies
# below). Whether a booking write is *authorised* is therefore a property of the
# environment, not of this image — with `MODEL_BACKEND=offline` layer 2 closes by
# design (zero network calls) and §9 refuses the write, while the static help card
# keeps working. The image ships both behaviours; the deployment picks one.
#
# PINNING, and a caveat that is stated rather than hidden:
#   * The base image is pinned to a patch release, ahead of CI's `3.12`, because
#     compose's rule is "no floating tags" and CI's version must always be
#     reachable from here.
#   * uv is pinned to the version CI pins (env.UV_VERSION in ci.yml), so the
#     image resolves with the same resolver that produced uv.lock.
#   * `uv sync --frozen` refuses to build unless `uv.lock` already contains
#     `referraleasy-gateway`. That entry lands when the workspace member is
#     locked (`uv lock`, a repository-level step) — this Dockerfile deliberately
#     does NOT relock, because a build that silently re-resolves dependencies is
#     a build that can ship something other than the reviewed set.
# ════════════════════════════════════════════════════════════════════════════

# ── stage 1: resolve and install the workspace into a venv ──────────────────
FROM python:3.12.15-slim-trixie AS build

# The uv binary from the pinned official image. Copying the binary rather than
# `pip install uv` keeps the resolver version identical to CI's.
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

# UV_PYTHON_DOWNLOADS=never: the base image IS the interpreter, so uv must not
# fetch another one. Compile bytecode at install time; copy rather than hardlink
# because the cache layer and the venv layer are on different filesystems.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/repo/.venv

WORKDIR /repo

# Manifests first, so the dependency layer survives every source edit. Every
# member's manifest is copied, not just the gateway's: uv resolves the workspace
# from the root manifest and refuses a member whose path does not exist, so a
# missing manifest would make the lockfile look stale.
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
COPY services/agents/pyproject.toml services/agents/
COPY services/gateway/pyproject.toml services/gateway/
COPY services/workers/pyproject.toml services/workers/

# --no-editable: the runtime stage carries the venv and nothing else, so every
# member must be installed as a real wheel rather than as a pointer into /repo.
#
# --no-install-workspace is load-bearing, not an optimisation. Without it this first sync — which
# runs before any member's sources are copied — installs an **empty wheel** for every member and
# records that as satisfied. The second sync below then finds nothing changed and never repairs
# them, so the runtime image carries a venv with nine `referraleasy_*-0.1.0.dist-info` directories
# and not one actual package, and the container dies on `No module named referraleasy_gateway`.
# The flag makes this layer install third-party dependencies only, which is what putting it before
# the sources is for.
#
# --package rather than --all-packages: this image runs the gateway and nothing else, so there is no
# reason to build and install the agent and worker wheels into it.
RUN uv sync --frozen --package referraleasy-gateway --no-dev --no-editable --no-install-workspace

# The sources the wheels are built from, copied after the dependency layer so a
# source edit does not invalidate it, then the install that produces the wheels.
COPY packages/contracts/python/src packages/contracts/python/src
COPY packages/domain/src packages/domain/src
COPY packages/agent-runtime/src packages/agent-runtime/src
COPY packages/rag/src packages/rag/src
COPY packages/skills/src packages/skills/src
COPY packages/telemetry/src packages/telemetry/src
# §9's three guardrail layers, and the `data/` tree beside them: the threshold record, the labelled
# corpus and the reference utterances are package data the classifier layer loads at startup, so an
# image with the module but not its data would close layer 3 for a reason that looks like a bug.
COPY packages/guardrails/src packages/guardrails/src
# §16's alert rules are a gateway *dependency* — the admin page serves the thresholds from the
# package rather than from a copy of them — so `packages/alerts` needs its source here, not only its
# manifest. A manifest alone gets past workspace resolution and then fails the install with
# `Distribution not found`, which is how this list was found to have gone stale: `packages/eval`,
# `packages/alerts` and `packages/release-registry` had all been added to the workspace since the
# last line here was written, and the image had been building from a compile-time snapshot of a
# smaller repo. `eval` and `release-registry` are not dependencies of this service; their sources
# are copied for the same reason the agents' and workers' are below — so that a member added to the
# closure does not turn a dependency line into a build failure.
COPY packages/alerts/src packages/alerts/src
COPY packages/eval/src packages/eval/src
COPY packages/release-registry/src packages/release-registry/src
# `services/agents` is a flat layout — the package sits beside the seven role entry directories —
# so its source copy is the package itself, not a `src/` tree.
COPY services/agents/referraleasy_agents services/agents/referraleasy_agents
COPY services/gateway/src services/gateway/src
COPY services/workers/src services/workers/src

RUN uv sync --frozen --package referraleasy-gateway --no-dev --no-editable

# ── stage 2: runtime ──────────────────────────────────────────────────────
FROM python:3.12.15-slim-trixie AS runtime

# curl is not decoration: compose's probe is
# `curl -fsS http://127.0.0.1:8080/healthz`, and an image without a client
# cannot answer it. The probe deliberately needs no token — a health check that
# needs a session reports "unhealthy" whenever the session fixture is the broken
# thing, which is the opposite of useful.
RUN apt-get update \
 && apt-get install --no-install-recommends --yes curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system app \
 && useradd --system --gid app --home-dir /app --create-home app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8080 \
    APP_ENV=production

WORKDIR /app

COPY --from=build /repo/.venv /app/.venv

# ── the one demo-fixture dependency, and why it is here ─────────────────────
# `LabelledCorpus.load()` verifies the classifier's threshold record against the corpus it was
# measured on, and one corpus case takes its text from `fixtures/documents/documents.json` — the
# injected referral letter — rather than copying it, so the corpus cannot drift from the fixture.
# That means the *hash* the record carries depends on reading that file.
#
# The package resolves the repo root as `Path(__file__).resolve().parents[4]`, which is the checkout
# layout. Installed into a wheel in this venv that is `/app/.venv`, so the fixtures are copied there
# and the default resolution finds them. It is a coupling worth stating rather than hiding: a deploy
# that installs the package anywhere else must pass `repo_root` to `LabelledCorpus.load` instead.
#
# This is what closed layer 3 before it was found — the container died at startup with
# `FileNotFoundError: /app/.venv/fixtures/documents/documents.json`, which is a real dependency the
# packages-only phase could not see (ADR-0038 shipped the layers, not the composition).
COPY fixtures /app/.venv/fixtures

# ── the release pin (§13), and why the registry travels with the image ──────
# Every dispatched task carries `release_manifest_id`, resolved at startup from `RELEASE_MANIFEST_ID`
# or from `registry/releases/latest.json` (see `referraleasy_gateway.release`). The same
# `parents[4]` resolution that makes the fixtures path `/app/.venv/fixtures` makes the manifest
# `/app/.venv/registry/releases/latest.json`, so the tree is copied there. Without it a container
# with a coordinator configured would refuse to dispatch — correctly, since §13 forbids sending a
# task under a release nobody can name, but not usefully.
COPY registry /app/.venv/registry

# The offline profile label is part of what the service serves (§18), so it is
# baked in rather than left to the environment to forget.

RUN chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# The module entry point, not a shell string: exec form means the process is
# PID 1's child with a real SIGTERM path, and there is no shell to swallow the
# exit code.
CMD ["python", "-m", "referraleasy_gateway"]
