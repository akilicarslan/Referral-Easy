-- 012_release_registry.sql
-- What is running, and what was running before that.
--
-- IMMUTABILITY IS THE FEATURE. A release registry that can be edited is not a
-- record of what was deployed; it is a record of what someone most recently
-- wanted to believe was deployed. So the manifest table is append-only:
--
--   * a BEFORE UPDATE OR DELETE trigger raises (RE008) for every writer,
--     including the table owner -- RLS and GRANTs are no help against a
--     superuser, a trigger is. This is the "no UPDATE path" requirement;
--   * TRUNCATE is refused by a statement-level trigger;
--   * UPDATE and DELETE are additionally revoked, as defence in depth for
--     non-owner roles.
--
-- Status changes therefore cannot be an UPDATE. They are new rows in
-- `release_event`, themselves append-only, and the current status is a VIEW
-- over the latest event. History is the only source of truth.

CREATE TABLE release_registry (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id    text        NOT NULL,
    component     text        NOT NULL,
    version       text        NOT NULL,
    manifest      jsonb       NOT NULL,
    manifest_hash text        NOT NULL,
    git_sha       text,
    built_at      timestamptz NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT now(),
    registered_by text        NOT NULL,

    CONSTRAINT release_registry_release_id_present
        CHECK (length(btrim(release_id)) > 0),
    CONSTRAINT release_registry_component_present
        CHECK (length(btrim(component)) > 0),
    CONSTRAINT release_registry_version_present
        CHECK (length(btrim(version)) > 0),
    CONSTRAINT release_registry_manifest_hash_hex
        CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT release_registry_registered_by_present
        CHECK (length(btrim(registered_by)) > 0),
    -- A manifest cannot claim to have been built after it was registered.
    CONSTRAINT release_registry_built_before_registration
        CHECK (built_at <= registered_at),

    -- A given component version is registered once. Re-registering the same
    -- version with different content would mean the artifact moved, which is
    -- exactly the event a registry must make impossible.
    CONSTRAINT release_registry_component_version_key UNIQUE (component, version),
    CONSTRAINT release_registry_release_id_key        UNIQUE (release_id)
);

COMMENT ON TABLE release_registry IS
    'Append-only release manifests. There is no UPDATE path: the trigger refuses one for every role, and the status of a release lives in release_event.';
COMMENT ON COLUMN release_registry.manifest_hash IS
    'sha256 hex of the manifest as deployed. If it changes, the artifact changed, and that is a new release, not an edit.';
COMMENT ON COLUMN release_registry.manifest IS
    'The full manifest as deployed, stored verbatim so what ran can be reconstructed, not paraphrased.';

CREATE INDEX release_registry_component_idx
    ON release_registry (component, built_at DESC);

-- The hard immutability guard. Statement-level triggers cover TRUNCATE, which
-- a row-level trigger would never see.
CREATE TRIGGER release_registry_no_update_trigger
    BEFORE UPDATE OR DELETE ON release_registry
    FOR EACH ROW EXECUTE FUNCTION refuse_row_mutation();

CREATE TRIGGER release_registry_no_truncate_trigger
    BEFORE TRUNCATE ON release_registry
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_row_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON release_registry FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Status, as a sequence of facts rather than a mutable column
-- ---------------------------------------------------------------------------
CREATE TABLE release_event (
    id            bigserial   PRIMARY KEY,
    release_id    uuid        NOT NULL REFERENCES release_registry (id) ON DELETE RESTRICT,
    event         text        NOT NULL,
    environment   text        NOT NULL,
    actor         text        NOT NULL,
    note          text,
    occurred_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT release_event_event_known
        CHECK (event IN ('staged', 'activated', 'rolled_back', 'revoked')),
    CONSTRAINT release_event_environment_present
        CHECK (length(btrim(environment)) > 0),
    CONSTRAINT release_event_actor_present
        CHECK (length(btrim(actor)) > 0)
);

COMMENT ON TABLE release_event IS
    'Append-only deployment events. Status is derived from these rows, never stored in a column that someone could update.';

CREATE INDEX release_event_release_idx ON release_event (release_id, occurred_at DESC);
CREATE INDEX release_event_environment_idx ON release_event (environment, occurred_at DESC);

CREATE TRIGGER release_event_append_only_trigger
    BEFORE UPDATE OR DELETE ON release_event
    FOR EACH ROW EXECUTE FUNCTION refuse_row_mutation();

CREATE TRIGGER release_event_no_truncate_trigger
    BEFORE TRUNCATE ON release_event
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_row_mutation();

-- Current status per release and environment, read from the event log.
CREATE OR REPLACE VIEW release_status AS
    SELECT DISTINCT ON (e.release_id, e.environment)
           r.release_id    AS release_code,
           r.id            AS release_pk,
           r.component,
           r.version,
           r.manifest_hash,
           e.environment,
           e.event         AS status,
           e.actor         AS status_actor,
           e.occurred_at   AS status_at
      FROM release_event e
      JOIN release_registry r ON r.id = e.release_id
     ORDER BY e.release_id, e.environment, e.occurred_at DESC, e.id DESC;

COMMENT ON VIEW release_status IS
    'Latest release event per (release, environment). Derived, therefore impossible to drift from the recorded history.';
