CREATE TABLE cc_search_chats.source_root_current (
    source_root_id text PRIMARY KEY CHECK (source_root_id ~ '^[0-9a-f]{64}$'),
    provider text NOT NULL CHECK (provider IN ('claude', 'codex')),
    resolved_path text NOT NULL,
    configured_order integer NOT NULL CHECK (configured_order >= 0),
    UNIQUE (provider, resolved_path)
);

CREATE TABLE cc_search_chats.source_file_current (
    source_root_id text NOT NULL
        REFERENCES cc_search_chats.source_root_current ON DELETE CASCADE,
    source_file_relative text NOT NULL,
    file_device bigint NOT NULL,
    file_inode bigint NOT NULL,
    observed_size bigint NOT NULL CHECK (observed_size >= 0),
    observed_mtime_ns bigint NOT NULL,
    complete_byte_offset bigint NOT NULL CHECK (complete_byte_offset >= 0),
    next_record_ordinal bigint NOT NULL CHECK (next_record_ordinal >= 0),
    next_source_line bigint NOT NULL
        CHECK (next_source_line = next_record_ordinal + 1),
    parser_state_version integer NOT NULL CHECK (parser_state_version > 0),
    parser_state jsonb NOT NULL,
    source_status text NOT NULL CHECK (source_status IN ('indexed', 'excluded')),
    pending_bytes bigint NOT NULL CHECK (pending_bytes >= 0),
    updated_revision_id bigint NOT NULL
        REFERENCES cc_search_chats.corpus_revision,
    PRIMARY KEY (source_root_id, source_file_relative)
);

CREATE TABLE cc_search_chats.refresh_run (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL
        CHECK (status IN ('building', 'complete', 'partial', 'failed')),
    corpus_revision_id bigint REFERENCES cc_search_chats.corpus_revision,
    source_count bigint NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    changed_source_count bigint NOT NULL DEFAULT 0
        CHECK (changed_source_count >= 0),
    failed_source_count bigint NOT NULL DEFAULT 0
        CHECK (failed_source_count >= 0),
    advanced_source_count bigint NOT NULL DEFAULT 0
        CHECK (advanced_source_count >= 0),
    diagnostics jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE UNIQUE INDEX physical_alias_current_occurrence_idx
ON cc_search_chats.physical_alias_current (
    source_root_id, source_file_relative, record_ordinal, content_class
);
