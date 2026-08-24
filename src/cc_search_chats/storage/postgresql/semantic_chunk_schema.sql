ALTER TABLE cc_search_chats.embedding_profile
    ADD COLUMN model_revision text NOT NULL DEFAULT 'unknown',
    ADD COLUMN pooling text NOT NULL DEFAULT 'attention-mask-mean',
    ADD COLUMN attention_implementation text NOT NULL DEFAULT 'sdpa',
    ADD COLUMN chunker_id text NOT NULL DEFAULT 'whole-message:v1',
    ADD COLUMN target_content_tokens integer NOT NULL DEFAULT 768
        CHECK (target_content_tokens > 0),
    ADD COLUMN max_tokens integer NOT NULL DEFAULT 1024
        CHECK (max_tokens > 0),
    ADD COLUMN overlap_tokens integer NOT NULL DEFAULT 96
        CHECK (overlap_tokens >= 0),
    ADD CHECK (overlap_tokens < target_content_tokens),
    ADD CHECK (target_content_tokens <= max_tokens);

INSERT INTO cc_search_chats.embedding_profile (
    profile_id, model_id, model_revision, dimensions,
    passage_prefix, query_prefix, pooling, normalized,
    attention_implementation, chunker_id,
    target_content_tokens, max_tokens, overlap_tokens
) VALUES (
    'nemotron-3-embed-8b-bf16:chunks-v1',
    'nvidia/Nemotron-3-Embed-8B-BF16',
    'c44c20ab3f6b430336706847a6372de4b2eb3dbd',
    1024,
    'passage: ',
    'query: ',
    'attention-mask-mean',
    true,
    'sdpa',
    'nemotron-token-chunks-768-1024-96:v1',
    768,
    1024,
    96
);

CREATE TABLE cc_search_chats.semantic_chunk_current (
    provider text NOT NULL,
    source_session_id text NOT NULL,
    logical_message_id text NOT NULL,
    content_class text NOT NULL,
    profile_id text NOT NULL
        REFERENCES cc_search_chats.embedding_profile,
    chunk_ordinal integer NOT NULL CHECK (chunk_ordinal >= 0),
    chunker_id text NOT NULL,
    token_start integer NOT NULL CHECK (token_start >= 0),
    token_end integer NOT NULL CHECK (token_end > token_start),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    source_text_digest text NOT NULL
        CHECK (source_text_digest ~ '^[0-9a-f]{64}$'),
    passage_text text NOT NULL CHECK (passage_text ~ '[^[:space:]]'),
    input_digest text NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (
        provider, source_session_id, logical_message_id, content_class,
        profile_id, chunk_ordinal
    ),
    FOREIGN KEY (provider, source_session_id, logical_message_id, content_class)
        REFERENCES cc_search_chats.message_current
        ON DELETE CASCADE
);

CREATE INDEX semantic_chunk_current_input_idx
ON cc_search_chats.semantic_chunk_current (profile_id, input_digest);

DROP TABLE cc_search_chats.message_embedding_current;
