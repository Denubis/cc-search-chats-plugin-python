ALTER TABLE cc_search_chats.refresh_run
    ADD COLUMN read_source_count bigint NOT NULL DEFAULT 0
        CHECK (read_source_count >= 0),
    ADD COLUMN removed_source_count bigint NOT NULL DEFAULT 0
        CHECK (removed_source_count >= 0);
