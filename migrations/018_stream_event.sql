-- 018_stream_event.sql
-- §12's event log, durable, so more than one gateway replica can serve the same stream.
--
-- Until now the log behind `GET /v1/workflows/{id}/events` lived in one gateway process: a second
-- replica had no history to replay and assigned its own `seq` from zero, so a client reconnecting to
-- it saw a different sequence under the same workflow. This table is that log.
--
--   * `seq` is per workflow, contiguous from 0, assigned by the appending gateway while it holds a
--     transaction-scoped advisory lock on the workflow. The primary key is the backstop: two
--     replicas that somehow raced to the same `seq` cannot both commit it.
--   * `event_id` is the content-addressed id the gateway computes (§12), unique across the table,
--     so a replay from any replica returns byte-identical frames for the same cursor.
--   * Rows are append-only. Nothing here is workflow state — `workflow` (003) is — so losing a row
--     loses a replay frame, never a booking; but nothing deletes them in normal operation either.

CREATE TABLE stream_event (
    workflow_id         uuid        NOT NULL REFERENCES workflow (id),
    seq                 integer     NOT NULL,
    event_id            text        NOT NULL UNIQUE,
    type                text        NOT NULL,
    payload             jsonb,
    progress_elapsed_ms integer,
    created_at          timestamptz NOT NULL,
    synthetic           boolean     NOT NULL,

    PRIMARY KEY (workflow_id, seq),
    CONSTRAINT stream_event_seq_nonnegative CHECK (seq >= 0),
    CONSTRAINT stream_event_id_shape CHECK (event_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT stream_event_type_known CHECK (
        type IN ('surface_update', 'workflow_state', 'terminal', 'partial_text', 'agent_progress',
                 'notice')
    )
);

COMMENT ON TABLE stream_event IS
    'The §12 SSE event log. Shared by every gateway replica; seq is contiguous per workflow.';
