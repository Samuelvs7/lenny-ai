-- =============================================================================
-- 001_init — core schema
--
-- Runs on stock PostgreSQL 14+. Deliberately requires NO extensions:
-- no pgvector, no pg_trgm. That means the same schema applies unchanged to a
-- Docker Postgres, a Supabase project, a Railway instance, or a bare local
-- server — which is what makes "a fresh evaluator can run this" achievable.
--
-- Vector search is served by `chunks.embedding` (packed float32 in BYTEA)
-- loaded into an in-process index at startup. Lexical search is served by
-- Postgres' built-in full-text search. See architecture.md → Retrieval.
-- =============================================================================

-- gen_random_uuid() is built in from PG 13 via pgcrypto being in core.
-- Guarded so the migration is safe on managed instances where it already exists.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- --------------------------------------------------------------------------
-- Conversation state
-- --------------------------------------------------------------------------

-- A chat session. Each row is one independent conversation context: messages
-- are scoped to a session and never read across sessions, which is what
-- guarantees the "each session maintains independent context" requirement.
CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL DEFAULT 'New chat',

    -- User metadata. This build has no authentication (documented assumption in
    -- PRD.md → Non-goals), so user_id defaults to a single local principal.
    -- The column exists so adding real auth is a write to this field, not a
    -- schema migration.
    user_id         TEXT NOT NULL DEFAULT 'local-user',
    user_metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Provider in use when the session was created. Recorded per-session AND
    -- per-message because the evaluator is expected to switch providers
    -- mid-demo, and we want the transcript to show which model said what.
    model_provider  TEXT,
    model_name      TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at     TIMESTAMPTZ
);

-- Sidebar query: most recent conversations first, excluding archived.
CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
    ON sessions (user_id, updated_at DESC)
    WHERE archived_at IS NULL;


-- One turn in a conversation.
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,

    -- Which skill produced this turn ('grounded_qa' | 'ship30_essay' |
    -- 'artifact_gen' | NULL for user turns). Makes routing behaviour auditable
    -- after the fact rather than only observable in logs.
    skill           TEXT,
    router_reason   TEXT,

    model_provider  TEXT,
    model_name      TEXT,
    latency_ms      INTEGER,

    -- Validated citations backing this answer: a JSON array of
    -- {chunk_id, episode_slug, guest, title, youtube_url, start_seconds, score}.
    -- Only citations that survived the citation validator are stored, so the
    -- persisted record cannot contain a fabricated source.
    citations       JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Free-form diagnostics: retrieval scores, refusal reason, token estimates.
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Loading a conversation in order — the hot path for every chat open.
CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at ASC);


-- A generated Markdown or HTML/CSS artifact shown in the Artifact Viewer.
-- Stored separately from messages because artifacts are versioned and
-- regenerated independently of the chat turn that requested them.
CREATE TABLE IF NOT EXISTS artifacts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id      UUID REFERENCES messages(id) ON DELETE SET NULL,

    kind            TEXT NOT NULL CHECK (kind IN ('markdown', 'html')),
    title           TEXT NOT NULL DEFAULT 'Untitled artifact',
    content         TEXT NOT NULL,

    -- Monotonic per session+title, so "Regenerate" produces v2 rather than
    -- destroying the version the user was looking at.
    version         INTEGER NOT NULL DEFAULT 1,

    -- What the sanitiser removed and why: {blocked: [...], stripped_tags: n,
    -- sanitised: bool}. Surfaced in the UI so the security posture is visible
    -- to the user, not just asserted in a document.
    safety_report   JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session_created
    ON artifacts (session_id, created_at DESC);


-- --------------------------------------------------------------------------
-- Knowledge base
-- --------------------------------------------------------------------------

-- One podcast episode, mirroring the YAML frontmatter of the source repo
-- (github.com/ChatPRD/lennys-podcast-transcripts).
CREATE TABLE IF NOT EXISTS episodes (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Directory name in the source repo, e.g. 'brian-chesky'. Natural key used
    -- to make re-ingestion idempotent.
    slug              TEXT NOT NULL UNIQUE,

    guest             TEXT,
    title             TEXT NOT NULL,
    youtube_url       TEXT,
    video_id          TEXT,
    publish_date      DATE,
    description       TEXT,
    duration_seconds  INTEGER,
    view_count        BIGINT,
    channel           TEXT,
    keywords          TEXT[] NOT NULL DEFAULT '{}',

    -- Provenance + refresh control. content_hash lets re-ingestion skip
    -- episodes whose transcript is byte-identical to what is already indexed.
    source_url        TEXT,
    content_hash      TEXT NOT NULL,

    -- Data-quality record: how much sponsor/ad-read text was removed. Kept
    -- because it is a claim we make in the docs and should be checkable in SQL.
    sponsor_segments_removed  INTEGER NOT NULL DEFAULT 0,
    sponsor_words_removed     INTEGER NOT NULL DEFAULT 0,

    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_episodes_publish_date ON episodes (publish_date DESC);


-- A retrievable passage. One row per chunk, carrying enough metadata to build
-- a citation that a human can verify without leaving the app.
CREATE TABLE IF NOT EXISTS chunks (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id      UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,

    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,

    -- Who is speaking, and where in the episode this passage starts. The start
    -- offset is what makes a citation deep-linkable:
    --   {youtube_url}&t={start_seconds}s  ->  jumps to the exact moment.
    speaker         TEXT,
    start_seconds   INTEGER,
    end_seconds     INTEGER,
    word_count      INTEGER NOT NULL DEFAULT 0,

    -- Lexical arm of retrieval. Generated column: always consistent with
    -- content, no application code can forget to refresh it.
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    -- Dense arm of retrieval: float32 vector packed little-endian into BYTEA.
    -- BYTEA rather than REAL[] because it is ~4x smaller on the wire and loads
    -- into a NumPy matrix with a single frombuffer() call at startup.
    embedding       BYTEA,
    embedding_dim   INTEGER,
    embedding_model TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (episode_id, chunk_index)
);

-- Full-text search index — the lexical half of hybrid retrieval.
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (tsv);

-- Startup index load pulls only embedded rows.
CREATE INDEX IF NOT EXISTS idx_chunks_embedded
    ON chunks (id)
    WHERE embedding IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chunks_episode ON chunks (episode_id);


-- Audit trail for ingestion runs: when the knowledge base was last refreshed,
-- with what model, and what it produced. Answers "is the index stale?" without
-- reading application logs.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    status               TEXT NOT NULL DEFAULT 'running'
                         CHECK (status IN ('running', 'succeeded', 'failed')),

    source_ref           TEXT,
    embedding_model      TEXT,

    episodes_seen        INTEGER NOT NULL DEFAULT 0,
    episodes_ingested    INTEGER NOT NULL DEFAULT 0,
    episodes_skipped     INTEGER NOT NULL DEFAULT 0,
    chunks_written       INTEGER NOT NULL DEFAULT 0,
    sponsor_segments_removed INTEGER NOT NULL DEFAULT 0,

    error               TEXT
);


-- --------------------------------------------------------------------------
-- updated_at maintenance
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sessions_updated_at ON sessions;
CREATE TRIGGER trg_sessions_updated_at
    BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
