CREATE SCHEMA IF NOT EXISTS cc_search_chats;

CREATE TABLE IF NOT EXISTS cc_search_chats.corpus_revision (
    revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cc_search_chats.corpus_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    current_revision_id bigint REFERENCES cc_search_chats.corpus_revision
);

INSERT INTO cc_search_chats.corpus_state (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS cc_search_chats.message (
    revision_id bigint NOT NULL REFERENCES cc_search_chats.corpus_revision ON DELETE CASCADE,
    provider text NOT NULL CHECK (provider IN ('claude', 'codex')),
    source_session_id text NOT NULL,
    logical_message_id text NOT NULL,
    canonical_locator text NOT NULL,
    timestamp_text text NOT NULL,
    role text NOT NULL,
    session_kind text NOT NULL CHECK (session_kind IN ('primary', 'agent', 'unknown')),
    conversation_epoch integer NOT NULL CHECK (conversation_epoch >= 0),
    content_class text NOT NULL CHECK (content_class IN ('prose', 'tool_name', 'tool_input', 'tool_output')),
    prose_content text NOT NULL,
    repository text,
    cwd text,
    submitted_by text NOT NULL CHECK (submitted_by IN ('human', 'identified_harness', 'unknown')),
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', left(prose_content, 250000))
    ) STORED,
    PRIMARY KEY (revision_id, provider, source_session_id, logical_message_id, content_class)
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_attribute AS a
        JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = 'cc_search_chats.message'::regclass
          AND a.attname = 'search_vector'
          AND pg_get_expr(d.adbin, d.adrelid) NOT LIKE '%250000%'
    ) THEN
        DROP INDEX IF EXISTS cc_search_chats.message_search_vector_idx;
        ALTER TABLE cc_search_chats.message DROP COLUMN search_vector;
        ALTER TABLE cc_search_chats.message ADD COLUMN search_vector tsvector
          GENERATED ALWAYS AS (
              to_tsvector('simple', left(prose_content, 250000))
          ) STORED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS message_search_vector_idx
ON cc_search_chats.message USING gin (search_vector);

CREATE TABLE IF NOT EXISTS cc_search_chats.physical_alias (
    revision_id bigint NOT NULL,
    provider text NOT NULL,
    source_session_id text NOT NULL,
    logical_message_id text NOT NULL,
    content_class text NOT NULL,
    locator text NOT NULL,
    source_file_relative text NOT NULL,
    record_ordinal bigint NOT NULL CHECK (record_ordinal >= 0),
    source_line bigint NOT NULL CHECK (source_line = record_ordinal + 1),
    source_byte_offset bigint NOT NULL CHECK (source_byte_offset >= 0),
    raw_byte_length bigint NOT NULL CHECK (raw_byte_length >= 0),
    source_digest text NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (
        revision_id, provider, source_session_id, logical_message_id,
        content_class, source_file_relative, record_ordinal
    ),
    FOREIGN KEY (revision_id, provider, source_session_id, logical_message_id, content_class)
        REFERENCES cc_search_chats.message
        ON DELETE CASCADE
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cc_search_chats.physical_alias'::regclass
          AND conname = 'physical_alias_pkey'
          AND pg_get_constraintdef(oid) NOT LIKE '%source_file_relative%'
    ) THEN
        ALTER TABLE cc_search_chats.physical_alias
          DROP CONSTRAINT physical_alias_pkey;
        ALTER TABLE cc_search_chats.physical_alias
          ADD PRIMARY KEY (
              revision_id, provider, source_session_id, logical_message_id,
              content_class, source_file_relative, record_ordinal
          );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS cc_search_chats.semantic_revision (
    semantic_revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    corpus_revision_id bigint NOT NULL REFERENCES cc_search_chats.corpus_revision,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cc_search_chats.semantic_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    current_semantic_revision_id bigint REFERENCES cc_search_chats.semantic_revision
);

INSERT INTO cc_search_chats.semantic_state (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS cc_search_chats.message_embedding (
    semantic_revision_id bigint NOT NULL REFERENCES cc_search_chats.semantic_revision ON DELETE CASCADE,
    revision_id bigint NOT NULL,
    provider text NOT NULL,
    source_session_id text NOT NULL,
    logical_message_id text NOT NULL,
    content_class text NOT NULL,
    embedding vector(1024) NOT NULL,
    PRIMARY KEY (semantic_revision_id, revision_id, provider, source_session_id, logical_message_id, content_class),
    FOREIGN KEY (revision_id, provider, source_session_id, logical_message_id, content_class)
        REFERENCES cc_search_chats.message
        ON DELETE CASCADE
);
