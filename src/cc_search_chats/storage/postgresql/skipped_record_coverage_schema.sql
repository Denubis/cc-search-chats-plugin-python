ALTER TABLE cc_search_chats.source_file_current
    ADD COLUMN skipped_record_count bigint NOT NULL DEFAULT 0
        CHECK (skipped_record_count >= 0);
