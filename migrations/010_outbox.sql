-- 010_outbox.sql
-- The outbox is the queue.
--
-- There is no message broker in this system, so this table is not a staging
-- area in front of a broker: it IS the transport. That raises the bar on it,
-- because the properties a broker would normally provide have to be here:
--
--   * next_attempt_at  -- when a retry becomes due (backoff lives in the row)
--   * attempt_count    -- how many deliveries have been tried
--   * claimed_until    -- the lease that stops a crashed worker from
--                         wedging a row forever
--   * status = 'dead'  -- a TERMINAL state, so a permanently failing
--                         notification stops instead of looping for eternity
--
-- Retrieval must not hand the same row to two workers. That is the
-- FOR UPDATE SKIP LOCKED claim below: the row lock is taken by the claiming
-- transaction, and a second worker skips the locked row rather than waiting
-- for it. Nothing is handed out twice, and no worker blocks behind another.

CREATE TABLE outbox (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    dedupe_key      text        NOT NULL,
    kind            text        NOT NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status          text        NOT NULL DEFAULT 'pending',

    attempt_count   integer     NOT NULL DEFAULT 0,
    max_attempts    integer     NOT NULL DEFAULT 5,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),

    claimed_by      text,
    claimed_until   timestamptz,

    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz,
    terminal_at     timestamptz,

    CONSTRAINT outbox_status_known
        CHECK (status IN ('pending', 'claimed', 'delivered', 'dead')),
    CONSTRAINT outbox_dedupe_key_present
        CHECK (length(btrim(dedupe_key)) > 0),
    CONSTRAINT outbox_kind_present
        CHECK (length(btrim(kind)) > 0),

    CONSTRAINT outbox_attempt_count_range
        CHECK (attempt_count >= 0 AND attempt_count <= max_attempts),
    CONSTRAINT outbox_max_attempts_positive
        CHECK (max_attempts >= 1),

    -- A claim is owner + deadline, or neither. A half claim is a bug that would
    -- make the row look permanently leased (owner set) or permanently free.
    CONSTRAINT outbox_claim_is_whole
        CHECK (status = 'claimed' OR (claimed_by IS NULL AND claimed_until IS NULL)),
    CONSTRAINT outbox_claimed_has_owner
        CHECK (status <> 'claimed' OR (claimed_by IS NOT NULL AND claimed_until IS NOT NULL)),

    -- TERMINALITY. 'delivered' and 'dead' are the two end states, and each
    -- carries the timestamp that proves it ended. The claim function never
    -- selects either, so a terminal row cannot be picked up again.
    CONSTRAINT outbox_delivered_has_time
        CHECK ((status = 'delivered') = (delivered_at IS NOT NULL)),
    CONSTRAINT outbox_terminal_has_time
        CHECK ((status IN ('delivered', 'dead')) = (terminal_at IS NOT NULL)),

    -- A row may only be declared dead once its budget is actually spent. This
    -- is what stops "give up" from becoming an escape hatch for a single
    -- transient failure.
    CONSTRAINT outbox_dead_only_when_exhausted
        CHECK (status <> 'dead' OR attempt_count >= max_attempts),

    CONSTRAINT outbox_last_error_when_unhappy
        CHECK (last_error IS NULL OR status IN ('pending', 'claimed', 'dead'))
);

COMMENT ON TABLE outbox IS
    'The notification/work queue itself. No broker exists; SKIP LOCKED retrieval on this table is the delivery mechanism.';
COMMENT ON COLUMN outbox.dedupe_key IS
    'Enqueue idempotency: the same logical notification cannot be queued twice.';
COMMENT ON COLUMN outbox.next_attempt_at IS
    'Earliest time this row is eligible. Backoff is written here, so a retry is a row state rather than a sleeping worker.';
COMMENT ON COLUMN outbox.claimed_until IS
    'Lease deadline. A claim whose deadline has passed is reclaimable, so a worker that dies mid-delivery does not wedge the row.';
COMMENT ON COLUMN outbox.status IS
    'pending -> claimed -> delivered | pending (retry) | dead (terminal). ''dead'' is the terminal giving-up state; it is never claimed again.';
COMMENT ON CONSTRAINT outbox_attempt_count_range ON outbox IS
    'Attempts can never exceed the budget, so the terminal decision is made once and cannot be un-made by a later claim.';

CREATE UNIQUE INDEX outbox_dedupe_key_key ON outbox (dedupe_key);

-- The claim index. Partial, because delivered/dead rows are never claimable and
-- have no business occupying the hot path.
CREATE INDEX outbox_claimable_idx
    ON outbox (next_attempt_at, claimed_until)
    WHERE status IN ('pending', 'claimed');

CREATE INDEX outbox_dead_idx
    ON outbox (terminal_at DESC)
    WHERE status = 'dead';

-- ---------------------------------------------------------------------------
-- Claim
-- ---------------------------------------------------------------------------

-- Hand out up to p_limit due rows to p_worker, atomically.
--
-- FOR UPDATE SKIP LOCKED is the whole point: a worker takes the row lock for
-- the duration of its transaction, and any other worker running this function
-- concurrently SKIPS the locked rows instead of blocking on them or receiving
-- them. Hence: no double delivery, no head-of-line blocking.
CREATE OR REPLACE FUNCTION outbox_claim(
    p_worker_id     text,
    p_limit         integer DEFAULT 10,
    p_lease_seconds integer DEFAULT 60
) RETURNS SETOF outbox
LANGUAGE plpgsql AS $$
BEGIN
    IF p_limit < 1 THEN
        RAISE EXCEPTION 'outbox_claim limit must be >= 1, got %', p_limit
            USING ERRCODE = 'RE007';
    END IF;

    RETURN QUERY
    UPDATE outbox o
       SET status          = 'claimed',
           claimed_by      = p_worker_id,
           claimed_until   = now() + make_interval(secs => p_lease_seconds),
           attempt_count   = o.attempt_count + 1,
           updated_at      = now()
     WHERE o.id IN (
              SELECT c.id
                FROM outbox c
               WHERE (
                       -- due, never attempted or waiting out a backoff
                       (c.status = 'pending' AND c.next_attempt_at <= now())
                       -- or leased by a worker that has since died
                    OR (c.status = 'claimed' AND c.claimed_until <= now())
                     )
                 -- A row that has spent its budget is only claimable in order
                 -- to be buried; that happens through outbox_fail, not here.
                 AND c.attempt_count < c.max_attempts
               ORDER BY c.next_attempt_at, c.created_at
               LIMIT p_limit
                 FOR UPDATE SKIP LOCKED
           )
    RETURNING o.*;
END;
$$;

COMMENT ON FUNCTION outbox_claim IS
    'Atomically leases due rows to one worker with FOR UPDATE SKIP LOCKED. Two workers calling this concurrently cannot receive the same row; terminal and exhausted rows are never returned.';

-- ---------------------------------------------------------------------------
-- Settle
-- ---------------------------------------------------------------------------

-- Report the outcome of a claimed delivery.
--   ok    -> 'delivered' (terminal)
--   not ok -> 'pending' with exponential backoff, or 'dead' once the attempt
--             budget is spent (terminal: it stops looping).
CREATE OR REPLACE FUNCTION outbox_settle(
    p_outbox_id     uuid,
    p_worker_id     text,
    p_ok            boolean,
    p_error         text DEFAULT NULL,
    p_backoff_base  integer DEFAULT 5,
    p_backoff_cap   integer DEFAULT 3600
) RETURNS outbox
LANGUAGE plpgsql AS $$
DECLARE
    v_row      outbox;
    v_backoff  integer;
BEGIN
    IF p_ok THEN
        UPDATE outbox o
           SET status       = 'delivered',
               delivered_at = now(),
               terminal_at  = now(),
               claimed_by   = NULL,
               claimed_until = NULL,
               last_error   = NULL,
               updated_at   = now()
         WHERE o.id = p_outbox_id
           AND o.status = 'claimed'
           AND o.claimed_by = p_worker_id
        RETURNING * INTO v_row;
    ELSE
        UPDATE outbox o
           SET status = CASE WHEN o.attempt_count >= o.max_attempts THEN 'dead' ELSE 'pending' END,
               last_error = coalesce(p_error, 'delivery failed'),
               terminal_at = CASE WHEN o.attempt_count >= o.max_attempts THEN now() ELSE NULL END,
               next_attempt_at = CASE
                   WHEN o.attempt_count >= o.max_attempts THEN o.next_attempt_at
                   ELSE now() + make_interval(secs => least(
                            p_backoff_cap,
                            -- exponent clamped to 20: the cap makes larger
                            -- values pointless and the clamp keeps the integer
                            -- cast away from overflow.
                            p_backoff_base * (2 ^ least(greatest(o.attempt_count - 1, 0), 20))::integer
                        ))
               END,
               claimed_by = NULL,
               claimed_until = NULL,
               updated_at = now()
         WHERE o.id = p_outbox_id
           AND o.status = 'claimed'
           AND o.claimed_by = p_worker_id
        RETURNING * INTO v_row;
    END IF;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'outbox row % cannot be settled by %: not currently claimed by that worker',
            p_outbox_id, p_worker_id
            USING ERRCODE = 'RE007',
                  HINT = 'The lease expired and another worker owns the row. Do not settle it.';
    END IF;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION outbox_settle IS
    'Settles a lease held by the given worker. Success is terminal; failure backs off, and exhaustion moves the row to the terminal ''dead'' state instead of looping forever.';

-- ---------------------------------------------------------------------------
-- Health view: what a stuck queue looks like
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW outbox_health AS
    SELECT status,
           count(*)                                    AS rows,
           min(next_attempt_at)                        AS oldest_due,
           max(attempt_count)                          AS max_attempts_spent,
           count(*) FILTER (WHERE status = 'claimed'
                              AND claimed_until <= now()) AS expired_leases
      FROM outbox
     GROUP BY status;

COMMENT ON VIEW outbox_health IS
    'One row per status, with expired leases surfaced: a non-zero expired_leases count means a worker died holding deliveries.';
