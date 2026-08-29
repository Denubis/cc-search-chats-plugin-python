ALTER TABLE cc_search_chats.refresh_run
    ADD COLUMN metadata_checked_source_count bigint NOT NULL DEFAULT 0
        CHECK (metadata_checked_source_count >= 0),
    ADD COLUMN attempted_source_count bigint NOT NULL DEFAULT 0
        CHECK (attempted_source_count >= 0),
    ADD COLUMN attempted_content_bytes bigint NOT NULL DEFAULT 0
        CHECK (attempted_content_bytes >= 0),
    ADD COLUMN blocked_source_count bigint NOT NULL DEFAULT 0
        CHECK (blocked_source_count >= 0),
    ADD COLUMN transient_failure_source_count bigint NOT NULL DEFAULT 0
        CHECK (transient_failure_source_count >= 0);

CREATE TABLE cc_search_chats.source_failure_current (
    source_root_id text NOT NULL
        REFERENCES cc_search_chats.source_root_current ON DELETE CASCADE,
    source_file_relative text NOT NULL,
    provider text NOT NULL CHECK (provider IN ('claude', 'codex')),
    file_device bigint NOT NULL,
    file_inode bigint NOT NULL,
    observed_size bigint NOT NULL CHECK (observed_size >= 0),
    observed_mtime_ns bigint NOT NULL,
    parser_state_version integer NOT NULL CHECK (parser_state_version > 0),
    failure_record_ordinal bigint CHECK (failure_record_ordinal >= 0),
    failure_source_line bigint CHECK (failure_source_line > 0),
    failure_source_byte_offset bigint CHECK (failure_source_byte_offset >= 0),
    failure_code text NOT NULL,
    failure_detail text NOT NULL,
    failure_class text NOT NULL
        CHECK (failure_class IN ('deterministic', 'transient')),
    attempted_content_bytes bigint NOT NULL
        CHECK (attempted_content_bytes >= 0),
    consecutive_failures integer NOT NULL CHECK (consecutive_failures > 0),
    first_failed_at timestamptz NOT NULL DEFAULT now(),
    last_failed_at timestamptz NOT NULL DEFAULT now(),
    retry_after timestamptz,
    last_run_id bigint
        REFERENCES cc_search_chats.refresh_run ON DELETE SET NULL,
    PRIMARY KEY (source_root_id, source_file_relative),
    CHECK (
        (failure_class = 'deterministic' AND retry_after IS NULL)
        OR (failure_class = 'transient' AND retry_after IS NOT NULL)
    )
);

CREATE TABLE cc_search_chats.auto_refresh_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    request_id bigint NOT NULL DEFAULT 0 CHECK (request_id >= 0),
    requested_at timestamptz,
    state text NOT NULL DEFAULT 'idle'
        CHECK (state IN ('idle', 'pending', 'launching', 'launched', 'running',
                         'complete', 'failed')),
    launch_attempt_count integer NOT NULL DEFAULT 0
        CHECK (launch_attempt_count >= 0),
    last_launch_attempt_at timestamptz,
    next_launch_retry_at timestamptz,
    launched_at timestamptz,
    completed_at timestamptz,
    refresh_run_id bigint
        REFERENCES cc_search_chats.refresh_run ON DELETE SET NULL,
    last_error text,
    CHECK (request_id > 0 OR state = 'idle')
);

INSERT INTO cc_search_chats.auto_refresh_state (singleton) VALUES (true);
