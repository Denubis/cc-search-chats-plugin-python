ALTER TABLE cc_search_chats.refresh_run
    ADD COLUMN owner_pid integer,
    ADD COLUMN phase text NOT NULL DEFAULT 'done'
        CHECK (phase IN ('scan', 'parse', 'fts_commit', 'done')),
    ADD COLUMN heartbeat_at timestamptz,
    ADD COLUMN completed_units bigint NOT NULL DEFAULT 0
        CHECK (completed_units >= 0),
    ADD COLUMN total_units bigint NOT NULL DEFAULT 0
        CHECK (total_units >= 0),
    ADD CHECK (completed_units <= total_units);

ALTER TABLE cc_search_chats.semantic_revision
    ADD COLUMN owner_pid integer,
    ADD COLUMN phase text NOT NULL DEFAULT 'done'
        CHECK (
            phase IN (
                'model_preflight', 'model_load', 'semantic_embed',
                'semantic_commit', 'done'
            )
        ),
    ADD COLUMN heartbeat_at timestamptz,
    ADD COLUMN completed_units bigint NOT NULL DEFAULT 0
        CHECK (completed_units >= 0),
    ADD COLUMN total_units bigint NOT NULL DEFAULT 0
        CHECK (total_units >= 0),
    ADD CHECK (completed_units <= total_units);

UPDATE cc_search_chats.semantic_revision
SET completed_units = embedded_count,
    total_units = embedded_count
WHERE status = 'complete';
