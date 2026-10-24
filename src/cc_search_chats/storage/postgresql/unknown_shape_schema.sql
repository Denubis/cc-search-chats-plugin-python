-- Unknown record shapes: per-file counts, the shape that blocked a source,
-- and the number of records an index run omitted under ignore mode.
-- Shapes are key-name fingerprints only (see core/record_shape.py).
ALTER TABLE cc_search_chats.source_file_current
    ADD COLUMN unknown_shapes jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(unknown_shapes) = 'object');

ALTER TABLE cc_search_chats.source_failure_current
    ADD COLUMN failure_shape jsonb
        CHECK (failure_shape IS NULL OR jsonb_typeof(failure_shape) = 'object');

ALTER TABLE cc_search_chats.refresh_run
    ADD COLUMN ignored_unknown_record_count bigint NOT NULL DEFAULT 0
        CHECK (ignored_unknown_record_count >= 0);
