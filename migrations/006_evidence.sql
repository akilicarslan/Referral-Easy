-- 006_evidence.sql
-- Evidence store: the provenance record behind claims like "the patient
-- consented" or "coverage was verified".
--
-- Contracts hand around an `evidence_id` with a source, an `authoritative`
-- flag and a freshness age. The requirements are:
--   * it must still be retrievable later, unchanged -> rows are append-only;
--   * a stale record must be distinguishable from a fresh one -> freshness is
--     a stored, computed fact (`stale_after`), not something each caller
--     recomputes from memory.
--
-- Freshness is deliberately a STORED fact rather than a per-caller computation:
-- two readers looking at the same row cannot disagree about when it goes stale,
-- because neither of them does the arithmetic.
--
-- It would be a STORED GENERATED column if PostgreSQL allowed it. It does not:
-- `timestamptz + interval` is STABLE rather than IMMUTABLE, because the result
-- genuinely depends on the session TimeZone across a DST boundary. So the value
-- is computed by the BEFORE INSERT trigger below and stored in an ordinary
-- column. The trigger always overwrites whatever the caller supplied, so the
-- stored value is still the database's answer, not the writer's.

CREATE TABLE evidence (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_id   text        NOT NULL,
    kind          text        NOT NULL,
    source_system text        NOT NULL,
    source_id     text        NOT NULL,
    authoritative boolean     NOT NULL,
    observed_at   timestamptz NOT NULL,
    fresh_for     interval    NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    content_hash  text        NOT NULL,
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    provenance    jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Freshness derived at write time by evidence_stamp_freshness(). Readers
    -- compare this to now(); they never recompute it themselves.
    stale_after   timestamptz NOT NULL,

    CONSTRAINT evidence_evidence_id_present CHECK (length(btrim(evidence_id)) > 0),
    CONSTRAINT evidence_kind_present        CHECK (length(btrim(kind)) > 0),
    CONSTRAINT evidence_source_present      CHECK (length(btrim(source_system)) > 0
                                                   AND length(btrim(source_id)) > 0),
    CONSTRAINT evidence_fresh_for_positive  CHECK (fresh_for > interval '0'),
    CONSTRAINT evidence_content_hash_hex    CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    -- Evidence cannot be observed in the future. A tiny tolerance absorbs clock
    -- skew between the source system and this database.
    CONSTRAINT evidence_observed_not_future
        CHECK (observed_at <= now() + interval '5 minutes'),
    -- An age of zero or less would mean the record was never fresh at all.
    CONSTRAINT evidence_stale_after_observed
        CHECK (stale_after > observed_at)
);

COMMENT ON TABLE evidence IS
    'Append-only provenance store for evidence_id references carried by contracts. Telemetry is NOT the record of evidence; this table is.';
COMMENT ON COLUMN evidence.evidence_id IS
    'The identifier contracts carry. Unique per source, so the same external id from two systems cannot collide.';
COMMENT ON COLUMN evidence.authoritative IS
    'True when the source is entitled to assert this fact. A non-authoritative record is still stored, but cannot be used as the basis of an approval.';
COMMENT ON COLUMN evidence.stale_after IS
    'observed_at + fresh_for, computed and stored at write time. Distinct from now() means stale: one comparison, no per-caller arithmetic.';
COMMENT ON COLUMN evidence.provenance IS
    'How the record was obtained (endpoint, document reference, capture method). Free-form by design; the indexed shape is in the columns above.';

-- One row per (source, evidence id). Two systems may legitimately use the same
-- opaque id, so the source is part of the identity.
CREATE UNIQUE INDEX evidence_identity_key
    ON evidence (source_system, evidence_id);

CREATE INDEX evidence_source_lookup_idx ON evidence (source_system, source_id);
CREATE INDEX evidence_kind_idx          ON evidence (kind, observed_at DESC);
CREATE INDEX evidence_stale_after_idx   ON evidence (stale_after);

-- The database computes freshness, and it computes it every time. A writer
-- cannot hand this table a stale_after of its own choosing.
CREATE OR REPLACE FUNCTION evidence_stamp_freshness() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.stale_after := NEW.observed_at + NEW.fresh_for;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION evidence_stamp_freshness IS
    'Derives stale_after from observed_at + fresh_for on insert. Overwrites any value the caller supplied, so freshness is never caller-asserted.';

CREATE TRIGGER evidence_stamp_freshness_trigger
    BEFORE INSERT ON evidence
    FOR EACH ROW EXECUTE FUNCTION evidence_stamp_freshness();

-- Evidence is a statement about the past: rewriting one rewrites history.
CREATE TRIGGER evidence_append_only_trigger
    BEFORE UPDATE OR DELETE ON evidence
    FOR EACH ROW EXECUTE FUNCTION refuse_row_mutation();

CREATE TRIGGER evidence_no_truncate_trigger
    BEFORE TRUNCATE ON evidence
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_row_mutation();

-- ---------------------------------------------------------------------------
-- Freshness accessors
-- ---------------------------------------------------------------------------

-- Stale/fresh is a boolean comparison against a stored fact.
CREATE OR REPLACE FUNCTION evidence_is_fresh(p_evidence_pk uuid)
RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce((
        SELECT e.stale_after > now()
          FROM evidence e
         WHERE e.id = p_evidence_pk
    ), false)
$$;

COMMENT ON FUNCTION evidence_is_fresh IS
    'True when the evidence row exists and its stored stale_after is still in the future. Missing evidence is NOT fresh (fails closed).';

-- Exactly the shape a contract needs when it says "here is an evidence_id":
-- present, and provably fresh or provably stale.
CREATE OR REPLACE VIEW evidence_freshness AS
    SELECT e.id,
           e.evidence_id,
           e.kind,
           e.source_system,
           e.source_id,
           e.authoritative,
           e.observed_at,
           e.fresh_for,
           e.stale_after,
           (e.stale_after > now())  AS is_fresh,
           (now() - e.observed_at)  AS age
      FROM evidence e;

COMMENT ON VIEW evidence_freshness IS
    'Evidence with its freshness resolved at read time. `is_fresh` false and a stale_after in the past identify a stale record unambiguously.';
