-- 004_session_task.sql
-- Sessions (one run of one agent role against one workflow) and the tasks
-- inside them. Kept deliberately small: this is bookkeeping around the
-- workflow, not a second state machine.
--
-- Note the layering: a session/task NEVER carries the workflow's control
-- state. Where those disagree, `workflow` wins, because that is the table
-- whose transitions are guarded.

CREATE TABLE session (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id   uuid        NOT NULL REFERENCES workflow (id) ON DELETE RESTRICT,
    agent_role    text        NOT NULL,
    actor_kind    text        NOT NULL DEFAULT 'automation',
    started_at    timestamptz NOT NULL DEFAULT now(),
    ended_at      timestamptz,
    model_ref     text,
    metadata      jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT session_actor_kind_known
        CHECK (actor_kind IN ('automation', 'human')),
    CONSTRAINT session_agent_role_present
        CHECK (length(btrim(agent_role)) > 0),
    CONSTRAINT session_end_after_start
        CHECK (ended_at IS NULL OR ended_at >= started_at)
);

COMMENT ON TABLE session IS
    'One agent (or operator) run against one workflow. A session is not a transaction scope: it may span many short transitions.';
COMMENT ON COLUMN session.model_ref IS
    'Opaque reference to the model or runtime used. Recorded for provenance; never a credential.';

CREATE INDEX session_workflow_idx ON session (workflow_id, started_at DESC);

CREATE TABLE task (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      uuid        NOT NULL REFERENCES session (id) ON DELETE RESTRICT,
    workflow_id     uuid        NOT NULL REFERENCES workflow (id) ON DELETE RESTRICT,
    kind            text        NOT NULL,
    state           text        NOT NULL DEFAULT 'queued',
    attempt         integer     NOT NULL DEFAULT 1,
    idempotency_key text        NOT NULL,
    input           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    output          jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,

    CONSTRAINT task_state_known
        CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    CONSTRAINT task_kind_present
        CHECK (length(btrim(kind)) > 0),
    CONSTRAINT task_idempotency_key_present
        CHECK (length(btrim(idempotency_key)) > 0),
    CONSTRAINT task_attempt_positive CHECK (attempt >= 1),

    -- A terminal task must say when it finished, and a non-terminal one must
    -- not pretend to have finished.
    CONSTRAINT task_terminal_has_finish_time
        CHECK ((state IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)),
    CONSTRAINT task_error_only_when_failed
        CHECK (error IS NULL OR state = 'failed'),
    CONSTRAINT task_finish_after_start
        CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),

    -- Re-running the same logical task in the same session is a retry, not a
    -- new task: the caller must reuse the row and bump `attempt`.
    CONSTRAINT task_session_idempotency_key UNIQUE (session_id, idempotency_key)
);

COMMENT ON TABLE task IS
    'One unit of work inside a session. Replay of the same idempotency key updates the existing row (attempt += 1) rather than inserting a second one.';

CREATE INDEX task_workflow_idx ON task (workflow_id, created_at DESC);
CREATE INDEX task_open_idx ON task (state) WHERE state IN ('queued', 'running');
