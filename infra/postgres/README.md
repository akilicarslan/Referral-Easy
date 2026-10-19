# PostgreSQL

The system of record for control state. OpenSearch is a search and telemetry
store and is never a transaction source; nothing in this directory assumes
otherwise.

## What is where

| Path | Purpose |
|---|---|
| `init/00-bootstrap.sql` | Server-level settings, applied once on first initialisation of the data directory. Not a migration. |
| `conf/postgresql.conf.snippet` | Settings for the demo workload. Include, don't replace. |
| `../../migrations/` | The schema itself, plus the runner and its tests. |

`compose.yaml` is owned elsewhere in the repo. This directory only supplies the
files it mounts; the service definition is not edited from here.

## What compose needs to mount

For the settings and the bootstrap script to take effect, the postgres service
has to mount these two paths. The fragment belongs in `compose.yaml` (owned
elsewhere) — it is written out here so the contract is visible from both sides:

```yaml
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: referraleasy
      POSTGRES_USER: referraleasy
      POSTGRES_PASSWORD: referraleasy   # from .env in any real environment
    command:
      - postgres
      - -c
      - config_file=/etc/postgresql/postgresql.conf
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./infra/postgres/conf/postgresql.conf.snippet:/etc/postgresql/postgresql.conf:ro
      - ./infra/postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      # The runner must not start before the server accepts connections, and
      # "up" is not the same thing as "ready to serve queries".
      test: ["CMD-SHELL", "pg_isready -U referraleasy -d referraleasy"]
      interval: 5s
      timeout: 5s
      retries: 10
    ports:
      - "5432:5432"
```

Two details worth keeping:

* **`docker-entrypoint-initdb.d` runs only when the data directory is empty.** On
  an existing volume, changes to `init/` take effect only after the volume is
  recreated. Anything that must apply to an existing database belongs in
  `migrations/`, not in `init/`.
* **The healthcheck gates migrations.** `docker compose up` returning is not the
  same as PostgreSQL accepting queries; `pg_isready` is what distinguishes them.

## Applying the schema

```bash
DATABASE_URL=postgresql://referraleasy:referraleasy@localhost:5432/referraleasy \
  uv run python scripts/migrate.py --status

DATABASE_URL=postgresql://referraleasy:referraleasy@localhost:5432/referraleasy \
  uv run python scripts/migrate.py
```

Migrations are forward-only and idempotent in effect (an applied file is never
re-run). Running the command twice is safe; the second run reports that the
database is up to date. See `../../migrations/README.md` for the full rules and
for the constraint-to-test map.

## Connecting by hand

```bash
docker compose exec postgres psql -U referraleasy -d referraleasy
```

Useful first queries:

```sql
-- What is applied.
SELECT version, filename, checksum, applied_at FROM schema_migrations ORDER BY version;

-- What is stuck.
SELECT * FROM outbox_health;
SELECT * FROM handoff_queue;

-- Is the audit log internally consistent? (empty result = yes)
SELECT * FROM audit_verify_chain();
```

## Sizing for the demo

The defaults are tuned for a single-node demo, not for production: a small
shared buffer pool and aggressive autovacuum. `conf/postgresql.conf.snippet`
documents each change and why it is safe at this scale, and each one carries a
comment saying what a real deployment would use instead.

The tables carry their own indexes for the two access patterns that matter at
any scale:

* **lease expiry** — `workflow_lease_idx` (partial, only leased rows) so a
  sweeper can find abandoned work without scanning the table;
* **queue retrieval** — `outbox_claimable_idx` (partial, only `pending` and
  `claimed`) so `FOR UPDATE SKIP LOCKED` never walks delivered or dead rows.
