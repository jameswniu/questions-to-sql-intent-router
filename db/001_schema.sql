CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA core;
CREATE SCHEMA rag;
CREATE SCHEMA sem;
CREATE SCHEMA agg;
CREATE SCHEMA app;
CREATE SCHEMA ops;

CREATE TABLE core.regions (
    region text PRIMARY KEY
);

/*
 The composite keys below make the region carried on a row provably the region of its state,
 claim or adjuster. Row-level security filters on those copies, so a mismatch would be a leak.
*/
CREATE TABLE core.states (
    state text PRIMARY KEY,
    name text NOT NULL,
    region text NOT NULL REFERENCES core.regions,
    UNIQUE (state, region)
);

CREATE TABLE core.policyholders (
    policyholder_id int PRIMARY KEY,
    first_name text NOT NULL,
    last_name text NOT NULL,
    email text NOT NULL,
    phone text NOT NULL,
    ssn text NOT NULL,
    dob date NOT NULL,
    state text NOT NULL REFERENCES core.states
);

CREATE TABLE core.policies (
    policy_id int PRIMARY KEY,
    policyholder_id int NOT NULL REFERENCES core.policyholders,
    product text NOT NULL,
    region text NOT NULL,
    state text NOT NULL,
    coverage_a numeric(12, 2) NOT NULL CHECK (coverage_a > 0),
    original_effective date NOT NULL,
    written_premium numeric(12, 2) NOT NULL CHECK (written_premium > 0),
    FOREIGN KEY (state, region) REFERENCES core.states (state, region)
);

CREATE TABLE core.adjusters (
    adjuster_id int PRIMARY KEY,
    name text NOT NULL,
    region text NOT NULL REFERENCES core.regions,
    UNIQUE (adjuster_id, region)
);

CREATE TABLE core.claims (
    claim_id int PRIMARY KEY,
    policy_id int NOT NULL REFERENCES core.policies,
    region text NOT NULL,
    state text NOT NULL,
    peril text NOT NULL CHECK (peril IN ('water', 'wind', 'hail', 'fire', 'theft', 'mold')),
    loss_date date NOT NULL,
    reported_date date NOT NULL,
    closed_date date,
    status text NOT NULL CHECK (status IN ('open', 'closed', 'denied')),
    adjuster_id int NOT NULL,
    edition text NOT NULL,
    deductible numeric(12, 2) NOT NULL,
    reserve numeric(12, 2) NOT NULL DEFAULT 0,
    damage_estimate numeric(12, 2) NOT NULL,
    denial_reason text CHECK (denial_reason IN ('below_deductible', 'excluded_peril', 'late_notice', 'mold_sublimit')),
    channel text NOT NULL,
    UNIQUE (claim_id, region),
    FOREIGN KEY (state, region) REFERENCES core.states (state, region),
    FOREIGN KEY (adjuster_id, region) REFERENCES core.adjusters (adjuster_id, region),
    CHECK (reported_date >= loss_date),
    CHECK ((status = 'open') = (closed_date IS NULL)),
    CHECK (closed_date IS NULL OR closed_date >= reported_date),
    CHECK (status <> 'denied' OR denial_reason IN ('below_deductible', 'excluded_peril', 'late_notice'))
);

CREATE TABLE core.payments (
    payment_id int PRIMARY KEY,
    claim_id int NOT NULL,
    region text NOT NULL,
    paid_date date NOT NULL,
    amount numeric(12, 2) NOT NULL CHECK (amount > 0),
    kind text NOT NULL CHECK (kind IN ('indemnity', 'expense')),
    status text NOT NULL CHECK (status IN ('issued', 'voided', 'reissue')),
    FOREIGN KEY (claim_id, region) REFERENCES core.claims (claim_id, region)
);

CREATE INDEX payments_claim_idx ON core.payments (claim_id);

CREATE TABLE core.earned_premium (
    region text NOT NULL REFERENCES core.regions,
    month date NOT NULL CHECK (extract(day FROM month) = 1),
    amount numeric(14, 2) NOT NULL,
    exposure int NOT NULL,
    PRIMARY KEY (region, month)
);

CREATE TABLE rag.documents (
    doc_id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('wording', 'guideline', 'memo', 'bulletin', 'note', 'scan')),
    title text NOT NULL,
    ref text,
    edition text,
    effective_from date,
    effective_to date,
    region text REFERENCES core.regions,
    claim_id int,
    sensitivity text NOT NULL CHECK (sensitivity IN ('general', 'claim')),
    uri text NOT NULL,
    sha256 text NOT NULL,
    embed_model text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doc_id, sensitivity),
    FOREIGN KEY (claim_id, region) REFERENCES core.claims (claim_id, region),
    CHECK ((sensitivity = 'claim') = (claim_id IS NOT NULL)),
    CHECK (sensitivity = 'general' OR region IS NOT NULL)
);

/*
 A chunk's sensitivity is pinned to its document's by the composite key, so it cannot drift to
 general. Its region and claim are only checked against core.claims; the triggers below hold them
 to the document's.
*/
CREATE TABLE rag.chunks (
    chunk_id text PRIMARY KEY,
    doc_id text NOT NULL,
    anchor text NOT NULL,
    section_path text NOT NULL,
    ordinal int NOT NULL,
    header text,
    body text NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(header, '') || ' ' || body)) STORED,
    embedding vector(384),
    region text,
    sensitivity text NOT NULL,
    claim_id int,
    edition text,
    effective_from date,
    effective_to date,
    quarantined boolean NOT NULL DEFAULT false,
    quarantine_reason text,
    FOREIGN KEY (doc_id, sensitivity) REFERENCES rag.documents (doc_id, sensitivity) ON DELETE CASCADE,
    FOREIGN KEY (claim_id, region) REFERENCES core.claims (claim_id, region),
    CHECK ((sensitivity = 'claim') = (claim_id IS NOT NULL)),
    CHECK (sensitivity = 'general' OR region IS NOT NULL)
);

/*
 Chat roles read chunks under row-level security, and the planner will not use this index for
 them because the tsvector match operator is not leakproof. It still serves superuser queries.
*/
CREATE INDEX chunks_tsv_idx ON rag.chunks USING gin (tsv);

CREATE TABLE rag.scan_fields (
    doc_id text NOT NULL,
    field text NOT NULL,
    value text,
    confidence real,
    bbox int[],
    flagged boolean NOT NULL DEFAULT false,
    flag_reason text,
    region text,
    sensitivity text NOT NULL,
    claim_id int,
    PRIMARY KEY (doc_id, field),
    FOREIGN KEY (doc_id, sensitivity) REFERENCES rag.documents (doc_id, sensitivity) ON DELETE CASCADE,
    FOREIGN KEY (claim_id, region) REFERENCES core.claims (claim_id, region),
    CHECK ((sensitivity = 'claim') = (claim_id IS NOT NULL)),
    CHECK (sensitivity = 'general' OR region IS NOT NULL)
);

/*
 Row security on chunks and scan fields reads the region and claim copied onto each row, so a row
 whose copy disagrees with its document would be shown under the wrong region's policy. A foreign
 key cannot hold the copy, since region and claim_id are NULL on general documents and a composite
 key skips its check when any column is NULL. A mismatch is refused, never corrected, because it
 means the writer has a bug. The error is a foreign-key violation, which is what it amounts to.
 A document that row security hides from the writer is refused as missing.
*/
CREATE FUNCTION rag.row_matches_document() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS
$$
DECLARE
    doc record;
BEGIN
    SELECT d.sensitivity, d.region, d.claim_id INTO doc FROM rag.documents d WHERE d.doc_id = NEW.doc_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION '% row names document %, which does not exist or is not visible', TG_TABLE_NAME, NEW.doc_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF (NEW.sensitivity, NEW.region, NEW.claim_id) IS DISTINCT FROM (doc.sensitivity, doc.region, doc.claim_id) THEN
        RAISE EXCEPTION '% row (sensitivity %, region %, claim %) disagrees with its document % (sensitivity %, region %, claim %)',
            TG_TABLE_NAME, NEW.sensitivity, NEW.region, NEW.claim_id, NEW.doc_id, doc.sensitivity, doc.region, doc.claim_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER chunks_match_document BEFORE INSERT OR UPDATE ON rag.chunks
    FOR EACH ROW EXECUTE FUNCTION rag.row_matches_document();
CREATE TRIGGER scan_fields_match_document BEFORE INSERT OR UPDATE ON rag.scan_fields
    FOR EACH ROW EXECUTE FUNCTION rag.row_matches_document();

/* The same agreement seen from the document: moving it to another region or claim leaves its rows behind. */
CREATE FUNCTION rag.document_matches_rows() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS
$$
BEGIN
    IF EXISTS (
        SELECT 1 FROM rag.chunks c
        WHERE c.doc_id = NEW.doc_id AND (c.region, c.claim_id) IS DISTINCT FROM (NEW.region, NEW.claim_id)
    ) OR EXISTS (
        SELECT 1 FROM rag.scan_fields f
        WHERE f.doc_id = NEW.doc_id AND (f.region, f.claim_id) IS DISTINCT FROM (NEW.region, NEW.claim_id)
    ) THEN
        RAISE EXCEPTION 'document % (region %, claim %) disagrees with its chunks or scan fields',
            NEW.doc_id, NEW.region, NEW.claim_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER documents_match_rows AFTER UPDATE OF region, claim_id ON rag.documents
    FOR EACH ROW EXECUTE FUNCTION rag.document_matches_rows();

CREATE TABLE app.principals (
    role_name text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('adjuster', 'supervisor', 'analyst')),
    regions text[] NOT NULL
);

CREATE TABLE app.settings (
    key text PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO app.settings (key, value) VALUES ('min_cell_count', '10'), ('dominance_share', '0.5');

CREATE TABLE app.bootstrap_state (
    step text PRIMARY KEY,
    done_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ops.request_log (
    request_id uuid PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    user_id text NOT NULL,
    role_name text NOT NULL,
    route text,
    outcome text,
    refusal_reason text,
    stage_ms jsonb,
    total_ms int,
    first_event_ms int,
    tokens_in int,
    tokens_out int,
    cache_read_tokens int,
    cost_usd numeric(10, 6),
    verifier jsonb,
    mode text,
    source text,
    question_redacted text,
    -- Live mode only: why it fell back to the no-key answer, what it wrote to the prompt cache, the models that
    -- answered as each reply named itself, and each prompt template called with the hash of its fixed parts.
    fallback text,
    cache_write_tokens int,
    models text[],
    prompt_hashes jsonb
);

CREATE TABLE ops.audit_log (
    request_id uuid NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    user_id text NOT NULL,
    role_name text NOT NULL,
    route text,
    claim_ids int[],
    doc_ids text[]
);

CREATE TABLE ops.feedback (
    request_id uuid NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    user_id text NOT NULL,
    rating smallint NOT NULL CHECK (rating IN (-1, 1)),
    note text
);

CREATE TABLE ops.eval_runs (
    run_id uuid PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    split text NOT NULL,
    mode text NOT NULL,
    git_commit text,
    metrics jsonb NOT NULL
);
