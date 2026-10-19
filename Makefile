# ReferralEasy — task entry points (§0.1, §18).
# Targets are stubbed in Phase 0 and filled in as their phase lands.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE      := docker compose
PROFILE      ?= core
ENV_FILE     ?= .env
PY           := uv run

# Every workspace member's source root, in the one place the list lives. `mypy .` is not usable here
# — see the long note under [tool.mypy] in pyproject.toml — so the target names them instead.
PYTHON_SOURCE_ROOTS := \
	packages/contracts/python/src \
	packages/domain/src \
	packages/rag/src \
	packages/skills/src \
	packages/agent-runtime/src \
	packages/telemetry/src \
	packages/guardrails/src \
	packages/release-registry/src \
	packages/eval/src \
	packages/alerts/src \
	services/gateway/src \
	services/workers/src \
	services/agents/referraleasy_agents

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ─── setup ──────────────────────────────────────────────────────────────────

.PHONY: env
env: ## Create .env from .env.example if it does not exist
	@test -f $(ENV_FILE) || { cp .env.example $(ENV_FILE); echo "created $(ENV_FILE) — review it"; }

.PHONY: bootstrap
bootstrap: env ## Install Python (uv) and Node (pnpm) dependencies
	uv sync --all-packages
	pnpm install

# ─── demo lifecycle (§18) ───────────────────────────────────────────────────

.PHONY: demo
demo: ## One command: validate env → compose up → migrate → seed → index → health → print URLs
	$(PY) python scripts/demo.py

.PHONY: up
up: env ## Start the compose stack (PROFILE=core|showcase|dashboards)
	$(COMPOSE) --profile $(PROFILE) up -d --wait

.PHONY: down
down: ## Stop the compose stack and remove orphans
	$(COMPOSE) --profile core --profile showcase --profile dashboards down --remove-orphans

.PHONY: migrate
migrate: ## Apply PostgreSQL migrations
	$(PY) python scripts/migrate.py

.PHONY: seed
seed: ## Verify the demo's datasets are loaded and match their sources (writes nothing)
	$(PY) python scripts/seed.py

.PHONY: index
index: ## Build OpenSearch indices and embeddings for the directory RAG
	$(PY) python scripts/index_directory.py

.PHONY: health
health: ## Probe every service healthcheck
	$(PY) python scripts/health.py

# ─── quality gates ──────────────────────────────────────────────────────────

.PHONY: test
test: ## Run unit + contract tests (spikes excluded)
	uv run pytest

.PHONY: test-all
test-all: ## Run every test including integration, e2e and spikes
	uv run pytest -m ""
	pnpm -r --if-present test

.PHONY: test-integration
test-integration: ## Database-backed integration tests. Needs the compose postgres up.
	@# Without this the same tests SKIP and the run still exits 0, which reads as a pass. The
	@# count is asserted so a green run means the tests actually ran. See .github/workflows/ci.yml.
	@test -n "$$TEST_DATABASE_URL" || { \
		echo "TEST_DATABASE_URL is not set. Start the stack and export it first:"; \
		echo "  make up && export TEST_DATABASE_URL=postgresql://referraleasy:referraleasy@localhost:\$${POSTGRES_HOST_PORT:-55433}/referraleasy"; \
		echo "  uv run python scripts/migrate.py --database-url \"\$$TEST_DATABASE_URL\""; \
		exit 1; }
	@# Setting TEST_DATABASE_URL is necessary but not sufficient: if the variable names a database
	@# that does not answer, every test below still skips and pytest still exits 0. So the run's
	@# output is checked for a skip rather than trusted from its exit code — a green run over
	@# nothing is the failure mode this whole target exists to prevent, and it was reachable.
	@#
	@# The guard cannot report a suite it never collected, and `migrations/tests` was exactly that:
	@# 124 schema-level tests outside `testpaths`, in a directory whose helpers were imported from a
	@# bare `conftest`. Both are fixed — the directory is in `testpaths` and its helpers are in
	@# `migration_fixtures.py` — so this target went from 240 collected integration tests to 344.
	@out="$$(uv run pytest -m integration -q -rs --ignore=tests/rag 2>&1)"; code=$$?; \
	printf '%s\n' "$$out"; \
	if printf '%s' "$$out" | grep -qE '^SKIPPED'; then \
		echo "FAIL: integration tests skipped. A skipped test is not a passing one - is PostgreSQL actually reachable at \$$TEST_DATABASE_URL?"; \
		exit 1; \
	fi; \
	exit $$code
	@echo "NOTE: tests/rag integration tests need OpenSearch and are excluded above."

.PHONY: test-integration-rag
test-integration-rag: ## OpenSearch-backed retrieval tests. Needs the compose opensearch up.
	@# Kept separate from test-integration because it needs a different service, not because it is
	@# less important. Unlike the database half this has NO CI job: a service container for
	@# OpenSearch is slow and fiddly enough that an unverifiable job would be worse than a
	@# documented gap. So these 13 tests, alone in the suite, are run only when someone runs this.
	@# The RAG tests fall back from the compose hostname to a host-reachable one on their own
	@# (packages/rag/.../config.py HOST_FALLBACK_OPENSEARCH_URLS), so no variable is needed here.
	@# Same guard as the database half, and it is not theoretical: these tests skip when OpenSearch
	@# does not answer within their probe timeout, and that skip prints as a green run. It was
	@# reproduced — a run under load reported "8 passed, 5 skipped" and exited 0, which is a
	@# retrieval suite reporting success while a third of it never executed. The probe timeout is
	@# short enough that a busy cluster trips it, so "skip" here means "could not measure", never
	@# "nothing to measure".
	@out="$$(uv run pytest tests/rag -m integration -q -rs 2>&1)"; code=$$?; \
	printf '%s\n' "$$out"; \
	if printf '%s' "$$out" | grep -qE '^SKIPPED'; then \
		echo "FAIL: the retrieval tests skipped, so nothing was verified. Is OpenSearch reachable - and is it answering fast enough for the probe?"; \
		exit 1; \
	fi; \
	exit $$code
	@echo "NOTE: these are the only integration tests with no CI job - see .github/workflows/ci.yml."

.PHONY: spike
spike: ## Phase 0 provider smoke tests — REQUIRES credentials. Writes docs/adr + tests/contracts
	uv run pytest -m spike -v

.PHONY: e2e
e2e: ## Scripted booking with an isolated mock API and temporary database (REPEAT=2 to loop)
	$(PY) python scripts/e2e.py --repeat $(or $(REPEAT),1)

.PHONY: eval
eval: ## Run the evaluation CLI against a release manifest + dataset (RUNS=<file> for a replay)
	@# §14 names three inputs and this target supplies two: the manifest and the dataset. The third
	@# is a replay's run records, which only a capture harness can produce — so `RUNS=` is how a
	@# caller supplies one, and without it every case reports as *unmeasured* rather than as passing.
	@# That is deliberate: a target that scored nothing and exited 0 would be the fabrication the
	@# offline profile's whole design exists to avoid.
	$(PY) python -m referraleasy_eval --release-manifest registry/releases/latest.json \
		$(if $(RUNS),--runs $(RUNS),)

.PHONY: lint
lint: ## Lint Python and TypeScript
	uv run ruff check .
	uv run ruff format --check .
	pnpm lint

.PHONY: typecheck
typecheck: ## Type-check Python and TypeScript
	$(MAKE) typecheck-python
	pnpm typecheck

.PHONY: typecheck-python
typecheck-python: ## Type-check the Python source roots (shared with CI)
	uv run mypy $(PYTHON_SOURCE_ROOTS)

.PHONY: fmt
fmt: ## Auto-format Python and TypeScript
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: ci
ci: lint typecheck test ## Exactly what CI runs

# ─── housekeeping ───────────────────────────────────────────────────────────

.PHONY: contracts
contracts: ## Regenerate Pydantic + Zod bindings from packages/contracts/schemas
	pnpm gen:contracts

.PHONY: clean
clean: ## Remove build artefacts and caches (never touches volumes/)
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name node_modules -prune -exec rm -rf {} +
	find . -type d -name dist -prune -exec rm -rf {} +
