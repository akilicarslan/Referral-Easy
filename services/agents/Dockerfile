# ════════════════════════════════════════════════════════════════════════════
# ReferralEasy — the seven specialist agents, ONE image (§3, §5, §18)
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not this directory:
#   docker build -f services/agents/Dockerfile -t referraleasy/agents:dev .
# compose.yaml fixes that contract (`context: .`, `dockerfile: services/agents/
# Dockerfile`) and it is the right one: this is a uv *workspace* member, so the
# install is driven by the root lockfile and the root pyproject's member list.
#
# ONE IMAGE, SEVEN SERVICES — §18: "Tek agent image AGENT_ROLE ile coordinator/
# intake/coverage/matching/readiness/scheduling/recovery servisleri açar." The role
# is chosen at RUN time by AGENT_ROLE, which is why there is no per-role Dockerfile
# and no per-role image: seven images would be seven places for the tool allowlist
# (§3), the budget caps (§8) and the pinned model (§8) to drift apart, and the
# runtime already treats the role as *deployment configuration* rather than as code
# (packages/agent-runtime/.../roles.py).
#
# THIS IMAGE DOES NOT SERVE `local-guardrail`, and it never could.
#   §18 lists local-guardrail beside the seven roles as an *addition*, not as an
#   eighth role, and §9's layer 3 — "the local small semantic model" — is a CPU/ONNX
#   sentence encoder (ADR-0005), not an A2A agent: it publishes no Agent Card, holds
#   no tool allowlist and answers no task envelope. compose.yaml previously built
#   that service from THIS file, which was the Phase 0 guess that the agents image
#   would host it; no agent role exists for it, so the guess was wrong rather than
#   merely unwritten. ADR-0005 also leaves the classifier's threshold unmeasured, so
#   even a correct image would be an inert stage announcing `threshold_unmeasured`.
#   See docs/adr/ADR-0036-*.md for what it needs instead.
#
# NO MODEL CALL AT STARTUP. `.env` may set MODEL_BACKEND=live with real credentials,
# so this is a property of the image and not of the profile: `AgentSettings.from_env`
# resolves the model reference purely (no SDK construction, no socket — see
# `resolve_model`), `build_service` builds an httpx client and a lazy psycopg seam
# without opening either, and `build_model`/the ADK `Runner` are reached only inside
# a turn. Starting this container therefore costs nothing at a provider, and a health
# probe (`GET /healthz`) cannot cause a call either.
# ════════════════════════════════════════════════════════════════════════════

# ── stage 1: resolve and install the workspace into a venv ──────────────────
FROM python:3.12.15-slim-trixie AS build

# The uv binary from the pinned official image. Copying the binary rather than
# `pip install uv` keeps the resolver version identical to CI's (env.UV_VERSION).
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

# UV_PYTHON_DOWNLOADS=never: the base image IS the interpreter, so uv must not fetch
# another one. Compile bytecode at install time; copy rather than hardlink because the
# cache layer and the venv layer are on different filesystems.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/repo/.venv

WORKDIR /repo

# Manifests first, so the dependency layer survives every source edit. EVERY member's
# manifest is copied, not just this one's: uv resolves the workspace from the root
# manifest and fails the whole build with "Distribution not found at:
# file:///repo/<member>" if one is missing — which is exactly how
# services/gateway/Dockerfile stopped building when services/agents and
# services/workers joined the workspace. A stale COPY list is a build that breaks
# later, in someone else's phase.
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

# --package, not --all-packages: this image serves the specialists, and the venv it
# carries should be their dependency closure (contracts, domain, agent-runtime,
# skills) rather than every wheel in the repository. The manifests above are still
# all nine, because the workspace has to *resolve* before a single member can be
# selected from it.
#
# --no-install-workspace is load-bearing, not tidiness. This layer runs BEFORE the
# sources are copied, so "install the workspace members" here would mean building a
# wheel of a package whose `src/` is not in the tree yet: the build succeeds, the
# member gets a dist-info directory, and what it installs is nothing. The next `uv
# sync` then sees those members as already satisfied and leaves them empty — which is
# how this image first came up with `referraleasy_agent_runtime` missing from a venv
# that had its metadata. `--no-install-workspace` installs the third-party
# dependencies only, which is the whole point of putting this layer before the
# sources, and the second sync below installs the real members.
#
# --no-editable: the runtime stage carries the venv and nothing else, so every member
# must be installed as a real wheel rather than as a pointer into /repo.
RUN uv sync --frozen --package referraleasy-agents --no-dev --no-editable --no-install-workspace

# The sources the wheels are built from, copied after the dependency layer so a source
# edit does not invalidate it, then the install that produces the wheels.
COPY packages/contracts/python/src packages/contracts/python/src
COPY packages/domain/src packages/domain/src
COPY packages/agent-runtime/src packages/agent-runtime/src
COPY packages/skills/src packages/skills/src
# §15's telemetry. `agent-runtime` depends on it for the server-span middleware, so this sync
# installs it as a real wheel — an image with the manifest copied but not the sources installs an
# *empty* wheel and dies on `No module named referraleasy_telemetry` at the first import.
COPY packages/telemetry/src packages/telemetry/src
COPY services/agents services/agents

RUN uv sync --frozen --package referraleasy-agents --no-dev --no-editable

# ── stage 2: runtime ──────────────────────────────────────────────────────
FROM python:3.12.15-slim-trixie AS runtime

# curl is not decoration: compose's probe is `curl -fsS http://127.0.0.1:8080/healthz`
# and an image without a client cannot answer it. The probe deliberately needs no
# token — `GET /healthz` is the one unauthenticated route on the A2A app
# (packages/agent-runtime/.../app.py), because a health check that needs a service
# token reports "unhealthy" whenever the token fixture is the broken thing.
RUN apt-get update \
 && apt-get install --no-install-recommends --yes curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system app \
 && useradd --system --gid app --home-dir /app --create-home app

# HOST/PORT are what `resolve_port` reads (AGENT_PORT, then compose's PORT, then 8080),
# so setting PORT here makes the image runnable with no environment at all.
#
# REFERRALEASY_SKILLS_ROOT points at the skill packages copied below. Without it the
# loader falls back to walking up from the installed package looking for a `skills/`
# directory — which finds the same place — but naming it is what keeps the image's
# skill content a deliberate deployment artefact rather than an accident of the
# filesystem layout (packages/skills/src/referraleasy_skills/loader.py).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8080 \
    APP_ENV=production \
    REFERRALEASY_SKILLS_ROOT=/app/skills

WORKDIR /app

COPY --from=build /repo/.venv /app/.venv

# The skill packages (§6). The loader is in the wheel; the skill *content* is not —
# eight packages of SKILL.md + manifest.json + read-only references per §22's layout —
# so it is shipped as data. A deployment that shipped none would still serve turns and
# record why (AgentService.skill_source_or_empty), which is precisely why this COPY has
# to be deliberate: the failure mode is quiet.
COPY packages/skills /app/skills

# The seven entry points, each renamed to the role VALUE that AGENT_ROLE carries.
# The directory names in the repository are service names (`intake`, `coverage`) and
# the role codes are agent names (`referral_intake`, `coverage_network`); they are not
# the same strings, and only the role code goes on the wire. Naming the image's
# directories after the code means the CMD below needs no lookup table: AGENT_ROLE is
# the path, and `docker compose exec <service> ls /app/roles` prints the seven.
COPY services/agents/coordinator /app/roles/care_coordinator
COPY services/agents/intake /app/roles/referral_intake
COPY services/agents/coverage /app/roles/coverage_network
COPY services/agents/matching /app/roles/provider_matching
COPY services/agents/readiness /app/roles/referral_readiness
COPY services/agents/scheduling /app/roles/scheduling
COPY services/agents/recovery /app/roles/recovery_handoff

RUN chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# The role's own entry point, not `referraleasy_agents.service:asgi_app`. The two
# serve the same ASGI app, but they fail differently and this is the difference that
# matters: `asgi_app` catches a wiring failure and returns a *handler-less* app that
# boots, reports healthy and refuses every task, while `service.main(role=...)` lets
# the failure out and the container exits non-zero. A container that says why it
# cannot serve is worth more than one that is up and useless, and compose would
# otherwise wait happily for the second one.
#
# `exec` keeps the process as PID 1's own — no shell left in the signal path — so a
# `docker compose stop` is delivered to uvicorn as SIGTERM rather than swallowed.
#
# Refusing an unset AGENT_ROLE here costs one line and replaces the alternative, which
# is an import error from a path that reads `/app/roles//__main__.py`.
CMD ["sh", "-c", "test -n \"$AGENT_ROLE\" || { echo 'AGENT_ROLE is not set. It selects which of the seven agents this image serves: care_coordinator, referral_intake, coverage_network, provider_matching, referral_readiness, scheduling, recovery_handoff.' >&2; exit 64; }; exec python \"/app/roles/$AGENT_ROLE/__main__.py\""]
