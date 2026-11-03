-- 017_alerts.sql
-- §16's durable alerting: what each rule last concluded, the transitions it produced, and the mock
-- email each transition composed.
--
-- Three tables, because they are three different records written by one scheduler tick:
--
--   * alert_state   -- the CURRENT state per (rule, series): firing or not, since when, when someone
--                      was last told, and the last evaluation including 'unmeasured'. This is what
--                      "what is firing right now" reads, and what the dedup engine compares against.
--   * alert_event   -- one row per TRANSITION (opened / resolved). Append-only. A rule that stays
--                      firing writes nothing here; that is what dedup means.
--   * alert_message -- the mock outbox. One message per event, enforced by a UNIQUE event_id, so a
--                      retried tick cannot compose a second email for the same transition (§16: "a
--                      notification failure does not start a new infinite email loop"). Delivery is
--                      to the log only; nothing here is ever sent.
--   * alert_run     -- one row per tick, so the admin page can say WHEN rules were last evaluated
--                      and which ones could not be (a rule with no source is reported, not skipped).
--
-- The general `outbox` (010) is deliberately NOT reused: its worker delivers to the notification API,
-- and an alert email must never leave the process.

CREATE TABLE alert_state (
    key             text        PRIMARY KEY,
    rule            text        NOT NULL,
    series          text        NOT NULL DEFAULT '',
    firing          boolean     NOT NULL DEFAULT false,
    since           timestamptz,
    notified_at     timestamptz,
    last_state      text        NOT NULL,
    last_detail     text        NOT NULL DEFAULT '',
    last_evaluation jsonb       NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at    timestamptz NOT NULL,

    CONSTRAINT alert_state_rule_known
        CHECK (rule IN ('terminal_failure_rate', 'sustained_attempt_latency', 'circuit_open', 'quality_drop')),
    CONSTRAINT alert_state_last_state_known
        CHECK (last_state IN ('firing', 'ok', 'unmeasured')),
    -- 'unmeasured' never changes `firing`: a rule that cannot decide has not resolved anything.
    -- So the only forbidden pair is a row that says it is firing and has no start time.
    CONSTRAINT alert_state_firing_has_since
        CHECK (NOT firing OR since IS NOT NULL)
);

CREATE TABLE alert_event (
    id          bigserial   PRIMARY KEY,
    key         text        NOT NULL,
    rule        text        NOT NULL,
    series      text        NOT NULL DEFAULT '',
    severity    text        NOT NULL,
    kind        text        NOT NULL,
    at          timestamptz NOT NULL,
    detail      text        NOT NULL,
    evaluation  jsonb       NOT NULL,

    CONSTRAINT alert_event_severity_known CHECK (severity IN ('page', 'warn', 'info')),
    CONSTRAINT alert_event_kind_known CHECK (kind IN ('opened', 'resolved')),
    -- A retried tick that re-inserts the same transition is a no-op, not a second event.
    CONSTRAINT alert_event_once UNIQUE (key, kind, at)
);

CREATE INDEX alert_event_at_idx ON alert_event (at DESC);

CREATE TABLE alert_message (
    id            bigserial   PRIMARY KEY,
    event_id      bigint      NOT NULL UNIQUE REFERENCES alert_event (id),
    dedupe_key    text        NOT NULL,
    recipient     text        NOT NULL,
    subject       text        NOT NULL,
    body          text        NOT NULL,
    state         text        NOT NULL DEFAULT 'pending',
    attempt_count integer     NOT NULL DEFAULT 0,
    max_attempts  integer     NOT NULL DEFAULT 5,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz,

    CONSTRAINT alert_message_state_known
        CHECK (state IN ('pending', 'delivered', 'failed', 'dead')),
    CONSTRAINT alert_message_attempts_bounded
        CHECK (attempt_count >= 0 AND attempt_count <= max_attempts),
    CONSTRAINT alert_message_delivered_has_time
        CHECK ((state = 'delivered') = (delivered_at IS NOT NULL)),
    -- 'dead' only once the budget is spent: giving up is not an escape hatch for one failure.
    CONSTRAINT alert_message_dead_only_when_exhausted
        CHECK (state <> 'dead' OR attempt_count >= max_attempts)
);

CREATE TABLE alert_run (
    id           bigserial   PRIMARY KEY,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL,
    scheduler    text        NOT NULL,
    evaluations  jsonb       NOT NULL,
    skipped      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    events       integer     NOT NULL DEFAULT 0,
    error        text,

    CONSTRAINT alert_run_ordered CHECK (finished_at >= started_at)
);

CREATE INDEX alert_run_started_idx ON alert_run (started_at DESC);

COMMENT ON TABLE alert_message IS
    'The §16 mock email outbox. Composed and recorded, delivered to the log, never sent.';
