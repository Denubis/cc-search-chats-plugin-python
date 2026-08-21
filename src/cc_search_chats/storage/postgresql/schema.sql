CREATE TABLE IF NOT EXISTS cc_search_chats.corpus_revision (
    revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE cc_search_chats.corpus_revision
    ADD COLUMN IF NOT EXISTS completed_at timestamptz,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'complete',
    ADD COLUMN IF NOT EXISTS message_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS alias_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS source_watermarks jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS failure jsonb;

CREATE TABLE IF NOT EXISTS cc_search_chats.corpus_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    current_revision_id bigint REFERENCES cc_search_chats.corpus_revision
);

INSERT INTO cc_search_chats.corpus_state (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS cc_search_chats.message_current (
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
    embedding_input_digest text NOT NULL CHECK (embedding_input_digest ~ '^[0-9a-f]{64}$'),
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', left(prose_content, 250000))
    ) STORED,
    PRIMARY KEY (provider, source_session_id, logical_message_id, content_class)
);

CREATE INDEX IF NOT EXISTS message_current_search_vector_idx
ON cc_search_chats.message_current USING gin (search_vector);

CREATE INDEX IF NOT EXISTS message_current_canonical_locator_idx
ON cc_search_chats.message_current (canonical_locator);

CREATE TABLE IF NOT EXISTS cc_search_chats.physical_alias_current (
    provider text NOT NULL,
    source_session_id text NOT NULL,
    logical_message_id text NOT NULL,
    content_class text NOT NULL,
    source_root_id text NOT NULL,
    locator text NOT NULL,
    source_file_relative text NOT NULL,
    record_ordinal bigint NOT NULL CHECK (record_ordinal >= 0),
    source_line bigint NOT NULL CHECK (source_line = record_ordinal + 1),
    source_byte_offset bigint NOT NULL CHECK (source_byte_offset >= 0),
    raw_byte_length bigint NOT NULL CHECK (raw_byte_length >= 0),
    source_digest text NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (
        provider, source_session_id, logical_message_id, content_class,
        source_root_id, source_file_relative, record_ordinal
    ),
    FOREIGN KEY (provider, source_session_id, logical_message_id, content_class)
        REFERENCES cc_search_chats.message_current
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS physical_alias_current_locator_idx
ON cc_search_chats.physical_alias_current (locator);

CREATE TABLE IF NOT EXISTS cc_search_chats.embedding_profile (
    profile_id text PRIMARY KEY,
    model_id text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    passage_prefix text NOT NULL,
    query_prefix text NOT NULL,
    normalized boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO cc_search_chats.embedding_profile (
    profile_id, model_id, dimensions, passage_prefix, query_prefix, normalized
) VALUES (
    'nemotron-3-embed-8b-bf16:v1',
    'nvidia/Nemotron-3-Embed-8B-BF16',
    1024,
    'passage: ',
    'query: ',
    true
)
ON CONFLICT (profile_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS cc_search_chats.semantic_revision (
    semantic_revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    corpus_revision_id bigint NOT NULL REFERENCES cc_search_chats.corpus_revision,
    profile_id text NOT NULL REFERENCES cc_search_chats.embedding_profile,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL DEFAULT 'building',
    embedded_count bigint NOT NULL DEFAULT 0,
    failure jsonb
);

ALTER TABLE cc_search_chats.semantic_revision
    ADD COLUMN IF NOT EXISTS profile_id text NOT NULL
        DEFAULT 'nemotron-3-embed-8b-bf16:v1',
    ADD COLUMN IF NOT EXISTS completed_at timestamptz,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'complete',
    ADD COLUMN IF NOT EXISTS embedded_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS failure jsonb;

CREATE TABLE IF NOT EXISTS cc_search_chats.semantic_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    current_semantic_revision_id bigint REFERENCES cc_search_chats.semantic_revision
);

INSERT INTO cc_search_chats.semantic_state (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS cc_search_chats.legacy_snapshot_inventory (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    corpus_revision_id bigint,
    semantic_revision_id bigint,
    captured_at timestamptz NOT NULL DEFAULT now(),
    embedding_pool_imported_at timestamptz,
    imported_embedding_rows bigint
);

ALTER TABLE cc_search_chats.legacy_snapshot_inventory
    ADD COLUMN IF NOT EXISTS embedding_pool_imported_at timestamptz,
    ADD COLUMN IF NOT EXISTS imported_embedding_rows bigint;

INSERT INTO cc_search_chats.legacy_snapshot_inventory (
    singleton, corpus_revision_id, semantic_revision_id
)
SELECT true, corpus.current_revision_id, semantic.current_semantic_revision_id
FROM cc_search_chats.corpus_state AS corpus
LEFT JOIN cc_search_chats.semantic_state AS semantic ON semantic.singleton
WHERE corpus.singleton
  AND EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema = 'cc_search_chats'
        AND table_name = 'message'
        AND column_name = 'revision_id'
  )
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS cc_search_chats.cutover_validation (
    validation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    corpus_revision_id bigint NOT NULL REFERENCES cc_search_chats.corpus_revision,
    installed_commit text NOT NULL CHECK (installed_commit ~ '^[0-9a-f]{40,64}$'),
    uat_evidence jsonb NOT NULL,
    validated_at timestamptz NOT NULL DEFAULT now(),
    accepted_at timestamptz
);

CREATE TABLE IF NOT EXISTS cc_search_chats.embedding_value (
    profile_id text NOT NULL REFERENCES cc_search_chats.embedding_profile,
    input_digest text NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    embedding vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, input_digest)
);

CREATE TABLE IF NOT EXISTS cc_search_chats.message_embedding_current (
    provider text NOT NULL,
    source_session_id text NOT NULL,
    logical_message_id text NOT NULL,
    content_class text NOT NULL,
    profile_id text NOT NULL,
    input_digest text NOT NULL,
    PRIMARY KEY (
        provider, source_session_id, logical_message_id, content_class,
        profile_id
    ),
    FOREIGN KEY (provider, source_session_id, logical_message_id, content_class)
        REFERENCES cc_search_chats.message_current
        ON DELETE CASCADE,
    FOREIGN KEY (profile_id, input_digest)
        REFERENCES cc_search_chats.embedding_value
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS message_embedding_current_value_idx
ON cc_search_chats.message_embedding_current (profile_id, input_digest);
