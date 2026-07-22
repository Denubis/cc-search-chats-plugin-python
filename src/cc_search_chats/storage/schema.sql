-- CC Search Chats v2 — Database Schema
-- SQLite with FTS5, WAL mode, and materialised views.
--
-- Pragmas (WAL, synchronous=NORMAL, foreign_keys=ON) are applied at
-- connection time in index.py, not here.

-- ============================================================
-- Core tables
-- ============================================================

CREATE TABLE IF NOT EXISTS session (
    session_id  TEXT PRIMARY KEY,
    project_path TEXT NOT NULL,        -- lossy decoded dir name (filter/group key)
    real_project_path TEXT,            -- true fs path from session cwd (display only)
    file_path   TEXT NOT NULL,
    file_size   INTEGER NOT NULL,
    modified_at TEXT NOT NULL,   -- file mtime at last index (ISO 8601)
    indexed_at  TEXT NOT NULL,   -- when we last indexed this file (ISO 8601)
    summary     TEXT             -- from most recent type=summary record
);

CREATE TABLE IF NOT EXISTS message (
    uuid         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES session(session_id) ON DELETE CASCADE,
    parent_uuid  TEXT,
    epoch        INTEGER NOT NULL DEFAULT 0,
    timestamp    TEXT NOT NULL,
    role         TEXT NOT NULL,       -- 'user' or 'assistant'
    text_content TEXT NOT NULL DEFAULT '',
    is_summary   INTEGER NOT NULL DEFAULT 0  -- 1 if this is a summary record
    -- TODO(post-v2.0.0a1): Add is_sidechain for branch detection
);

CREATE INDEX IF NOT EXISTS idx_message_session_epoch
    ON message(session_id, epoch);

CREATE INDEX IF NOT EXISTS idx_message_session_timestamp
    ON message(session_id, timestamp);

CREATE TABLE IF NOT EXISTS compact_event (
    uuid         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES session(session_id) ON DELETE CASCADE,
    epoch        INTEGER NOT NULL,    -- this event starts this epoch number (first = 1)
    timestamp    TEXT NOT NULL,
    trigger      TEXT NOT NULL,       -- 'auto' or 'manual'
    pre_tokens   INTEGER NOT NULL,
    summary_text TEXT                 -- extracted from the user message following the boundary
);

CREATE INDEX IF NOT EXISTS idx_compact_event_session_epoch
    ON compact_event(session_id, epoch);

-- ============================================================
-- FTS5 full-text search index (external content table)
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    text_content,
    content='message',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 0'
);

-- FTS5 sync triggers: keep message_fts in sync with message table.

CREATE TRIGGER IF NOT EXISTS message_fts_insert
AFTER INSERT ON message
BEGIN
    INSERT INTO message_fts(rowid, text_content)
        VALUES (new.rowid, new.text_content);
END;

CREATE TRIGGER IF NOT EXISTS message_fts_delete
AFTER DELETE ON message
BEGIN
    INSERT INTO message_fts(message_fts, rowid, text_content)
        VALUES ('delete', old.rowid, old.text_content);
END;

CREATE TRIGGER IF NOT EXISTS message_fts_update
AFTER UPDATE ON message
BEGIN
    INSERT INTO message_fts(message_fts, rowid, text_content)
        VALUES ('delete', old.rowid, old.text_content);
    INSERT INTO message_fts(rowid, text_content)
        VALUES (new.rowid, new.text_content);
END;

-- ============================================================
-- Materialised views (regular tables + maintenance triggers)
-- ============================================================

-- Project-level aggregates.
CREATE TABLE IF NOT EXISTS project_summary (
    project_path    TEXT PRIMARY KEY,
    session_count   INTEGER NOT NULL DEFAULT 0,
    latest_activity TEXT    -- most recent session modified_at (ISO 8601)
);

-- Maintain project_summary on session changes.

CREATE TRIGGER IF NOT EXISTS project_summary_after_insert_session
AFTER INSERT ON session
BEGIN
    INSERT INTO project_summary(project_path, session_count, latest_activity)
        VALUES (new.project_path, 1, new.modified_at)
    ON CONFLICT(project_path) DO UPDATE SET
        session_count   = session_count + 1,
        latest_activity = MAX(latest_activity, new.modified_at);
END;

CREATE TRIGGER IF NOT EXISTS project_summary_after_delete_session
AFTER DELETE ON session
BEGIN
    UPDATE project_summary SET
        session_count   = session_count - 1,
        latest_activity = (
            SELECT MAX(modified_at) FROM session
            WHERE project_path = old.project_path
        )
    WHERE project_path = old.project_path;
    -- Remove row if no sessions remain.
    DELETE FROM project_summary
    WHERE project_path = old.project_path AND session_count <= 0;
END;

CREATE TRIGGER IF NOT EXISTS project_summary_after_update_session
AFTER UPDATE OF modified_at ON session
BEGIN
    UPDATE project_summary SET
        latest_activity = (
            SELECT MAX(modified_at) FROM session
            WHERE project_path = new.project_path
        )
    WHERE project_path = new.project_path;
END;

-- Per-epoch aggregates.
CREATE TABLE IF NOT EXISTS epoch_summary (
    session_id      TEXT NOT NULL,
    epoch           INTEGER NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    first_timestamp TEXT,
    last_timestamp  TEXT,
    PRIMARY KEY (session_id, epoch)
);

-- Maintain epoch_summary on message changes.

CREATE TRIGGER IF NOT EXISTS epoch_summary_after_insert_message
AFTER INSERT ON message
BEGIN
    INSERT INTO epoch_summary(session_id, epoch, message_count, first_timestamp, last_timestamp)
        VALUES (new.session_id, new.epoch, 1, new.timestamp, new.timestamp)
    ON CONFLICT(session_id, epoch) DO UPDATE SET
        message_count   = message_count + 1,
        first_timestamp = MIN(first_timestamp, new.timestamp),
        last_timestamp  = MAX(last_timestamp, new.timestamp);
END;

CREATE TRIGGER IF NOT EXISTS epoch_summary_after_delete_message
AFTER DELETE ON message
BEGIN
    UPDATE epoch_summary SET
        message_count   = message_count - 1,
        first_timestamp = (
            SELECT MIN(timestamp) FROM message
            WHERE session_id = old.session_id AND epoch = old.epoch
        ),
        last_timestamp = (
            SELECT MAX(timestamp) FROM message
            WHERE session_id = old.session_id AND epoch = old.epoch
        )
    WHERE session_id = old.session_id AND epoch = old.epoch;
    -- Remove row if no messages remain.
    DELETE FROM epoch_summary
    WHERE session_id = old.session_id AND epoch = old.epoch AND message_count <= 0;
END;
