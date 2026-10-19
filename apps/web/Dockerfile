# ════════════════════════════════════════════════════════════════════════════
# ReferralEasy — web client
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not this directory.
#   docker build -f apps/web/Dockerfile -t referraleasy/web:dev .
# compose.yaml fixes that contract (context: ., dockerfile: apps/web/Dockerfile)
# and it is the right one: the workspace install is driven by the root lockfile,
# and the client bundles `@referraleasy/contracts`, whose manifest is a second
# workspace member.
#
# Two stages: Vite compiles the bundle against the frozen lockfile, then a
# static nginx image serves it. No Node process ships in the runtime image.
#
# The container listens on 3000, not 80 — compose publishes `3000:3000` and
# probes `http://127.0.0.1:3000/`, and `apps/web/nginx.conf` follows suit.
# ════════════════════════════════════════════════════════════════════════════

# ── stage 1: compile the bundle against the workspace lockfile ───────────────
FROM node:22.22.3-alpine3.24 AS build

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
WORKDIR /repo

# The package manager the repository pins, so the image cannot drift onto a
# different resolver than CI.
RUN corepack enable && corepack prepare pnpm@12.10.1 --activate

# Manifests first: the dependency layer survives every source edit.
# Every workspace member's manifest is copied even when it is not needed here —
# pnpm resolves the workspace from the root manifests, and a missing member
# makes the lockfile look stale.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/contracts/ts/package.json packages/contracts/ts/
COPY services/mock-api/package.json services/mock-api/
COPY apps/web/package.json apps/web/
# `--filter @referraleasy/contracts...` is not redundant: the contracts package
# is built below and its compiler (typescript) is one of ITS devDependencies,
# which are only installed for a package that the filter selects by name.
# --ignore-scripts: a container has no use for dependency build scripts, and
# the one in this tree (esbuild's) is deliberately not approved.
RUN pnpm install --frozen-lockfile --ignore-scripts \
      --filter @referraleasy/web... \
      --filter @referraleasy/contracts...

# `@referraleasy/contracts` resolves through its `exports` map to `dist/`, so it
# has to be compiled here — the local `dist/` never enters the image. Its
# devDependencies are deliberately not copied: only the generator output and the
# compiler config are needed, and `pnpm gen` would reach for the JSON schemas
# outside this context.
COPY packages/contracts/ts/tsconfig.json packages/contracts/ts/
COPY packages/contracts/ts/src packages/contracts/ts/src
RUN pnpm --filter @referraleasy/contracts build

# Every entry `vite.config.ts` declares, not just the first two: `admin.html` was added to the
# build's `input` map and not to this line, so the image failed with
# `Could not resolve entry module "admin.html"` three stages later — after pnpm had installed and
# the contracts had built. `tests/test_docker_member_lists.py` now reads the `input` map and
# checks this line against it, because the failure mode is always the same one: a list that
# something new has to be added to, and nothing that says so.
COPY apps/web/index.html apps/web/demo.html apps/web/admin.html \
     apps/web/vite.config.ts apps/web/tsconfig.json apps/web/
COPY apps/web/src apps/web/src

# Vite inlines `VITE_GATEWAY_URL` at build time. Empty is the default on
# purpose: the bundle then talks to its own origin, and `nginx.conf` proxies
# `/v1/*` to the gateway service on the compose network. Bake in a different
# value only when the API lives somewhere else.
ARG VITE_GATEWAY_URL=""
ENV VITE_GATEWAY_URL=$VITE_GATEWAY_URL
RUN pnpm --filter @referraleasy/web build

# ── stage 2: serve the static bundle ────────────────────────────────────────
FROM nginx:1.30-alpine AS runtime

# curl is not decoration: compose probes
# `curl -fsS http://127.0.0.1:3000/` for this service, and a container without a
# client cannot answer it.
RUN apk add --no-cache curl

COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
  CMD curl -fsS http://127.0.0.1:3000/ > /dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
