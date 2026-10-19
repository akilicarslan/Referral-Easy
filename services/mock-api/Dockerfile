# ════════════════════════════════════════════════════════════════════════════
# ReferralEasy — mock hospital network API
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not this directory.
#   docker build -f services/mock-api/Dockerfile -t referraleasy/mock-api:dev .
# compose.yaml fixes that contract (context: ., dockerfile: services/mock-api/
# Dockerfile) and it is the right one: the service SERVES `fixtures/`, so the
# image has to carry the shared fixture set, and the workspace install is driven
# by the root lockfile.
#
# Offline by construction: no model provider, no credentials, no outbound call
# at build or run time. Every response is flagged `synthetic: true`.
# ════════════════════════════════════════════════════════════════════════════

# ── stage 1: compile TypeScript against the workspace lockfile ───────────────
FROM node:22.22.3-alpine3.24 AS build

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
WORKDIR /repo

# Same package manager the repository pins, so the image cannot drift onto a
# different resolver than CI.
RUN corepack enable && corepack prepare pnpm@12.10.1 --activate

# Manifests first: the dependency layer survives every source edit.
# `packages/contracts/ts` is copied as a workspace member even though this
# service does not depend on it — pnpm resolves the workspace from the root
# manifest files, and a missing member would make the lockfile look stale.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# The contracts package, not just its manifest. `@referraleasy/contracts` resolves to `dist/`
# (`main`/`types` both point there, and `files` is `["dist"]`), and `dist/` is gitignored — so on a
# clean checkout the manifest alone resolves to nothing and `services/mock-api/src/contracts/enums.ts`
# fails with "Cannot find module '@referraleasy/contracts'". A local `pnpm typecheck` passes only
# because a developer's working tree already has a `dist/` from an earlier build, which is why this
# never showed up outside a container.
COPY packages/contracts/ts/package.json packages/contracts/ts/
COPY packages/contracts/ts/tsconfig.json packages/contracts/ts/
COPY packages/contracts/ts/src packages/contracts/ts/src
COPY services/mock-api/package.json services/mock-api/
# --ignore-scripts: a container has no use for dependency build scripts, and
# the one in this tree (esbuild's) is deliberately not approved.
RUN pnpm install --frozen-lockfile --ignore-scripts --filter @referraleasy/mock-api...

COPY services/mock-api/tsconfig.json services/mock-api/tsconfig.build.json services/mock-api/
COPY services/mock-api/src services/mock-api/src
# Contracts first: the service's TypeScript imports its declarations, so building the service before
# its dependency produces the same "Cannot find module" error the copy above exists to prevent.
RUN pnpm --filter @referraleasy/contracts build \
 && pnpm --filter @referraleasy/mock-api build

# `deploy --prod` produces a self-contained tree with the runtime dependencies
# only — resolved from the same lockfile, not from a second, unverified install.
RUN pnpm --filter @referraleasy/mock-api deploy --prod /deployed

# ── stage 2: runtime ────────────────────────────────────────────────────────
FROM node:22.22.3-alpine3.24 AS runtime

# curl is not decoration: the compose health probe is
# `curl -fsS http://127.0.0.1:4010/healthz` (ADR-0023) and an image without a
# client cannot answer it.
RUN apk add --no-cache curl \
 && addgroup -S app && adduser -S app -G app

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=4010 \
    MOCK_API_DB=/app/.data/mock-api.sqlite \
    MOCK_API_FIXTURES_DIR=/app/fixtures \
    MOCK_FAULT_SEED=42

WORKDIR /app

COPY --from=build /deployed/node_modules ./node_modules
COPY --from=build /deployed/package.json ./package.json
COPY --from=build /repo/services/mock-api/dist ./dist
# The dataset the service serves: directory, coverage, documents, evidence AND
# fixtures/fault-scenarios (§17 fault injection reads the same directory).
COPY fixtures ./fixtures

# One writable path for the SQLite store: mounting a volume there is how the
# demo keeps bookings across a `docker compose restart`.
RUN mkdir -p /app/.data && chown -R app:app /app
USER app

EXPOSE 4010
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:4010/healthz || exit 1

CMD ["node", "dist/server.js"]
