"""Tests for the indexing pipeline.

Acceptance criteria coverage:
- cc-search-v2.AC2.1: Session indexed into session, message, compact_event tables
- cc-search-v2.AC2.2: FTS5 index returns matches for keywords
- cc-search-v2.AC2.3: Re-index triggers only when mtime newer
- cc-search-v2.AC2.4: Materialised views reflect current data
- cc-search-v2.AC2.5: No compact_boundary -> all messages in epoch 0
- cc-search-v2.AC2.6: 3 compact_boundaries -> epochs 0-3
- cc-search-v2.AC7.1: DB created automatically (covered in test_index.py)
- cc-search-v2.AC7.4: Corrupted DB recovery (covered in test_index.py)
"""

import sqlite3
from pathlib import Path

from cc_search_chats.core.models import SessionMeta
from cc_search_chats.storage.index import (
    compute_epoch_keywords,
    index_session,
    needs_reindex,
    update_all_keywords,
)
from tests.conftest import SESSION_ID_A, _make_session_lines


def _write_session_file(
    tmp_path: Path,
    session_id: str,
    lines: list[str],
) -> SessionMeta:
    """Write JSONL lines to a temp file and return SessionMeta."""
    session_file = tmp_path / f"{session_id}.jsonl"
    session_file.write_text("\n".join(lines), encoding="utf-8")
    stat = session_file.stat()
    return SessionMeta(
        session_id=session_id,
        file_path=str(session_file),
        project_path="/home/brian/project",
        file_size=stat.st_size,
        modified_at=stat.st_mtime,
    )


class TestIndexSessionBasic:
    """cc-search-v2.AC2.1: Session data indexed correctly with epoch assignments."""

    def test_session_table_populated(self, indexed_db: sqlite3.Connection) -> None:
        """Session row exists with correct fields."""
        row = indexed_db.execute(
            "SELECT * FROM session WHERE session_id = ?", (SESSION_ID_A,)
        ).fetchone()
        assert row is not None
        assert row["project_path"] == "/home/brian/project"
        assert row["file_size"] > 0

    def test_message_table_populated(self, indexed_db: sqlite3.Connection) -> None:
        """Messages are inserted with correct data."""
        rows = indexed_db.execute(
            "SELECT * FROM message WHERE session_id = ? ORDER BY timestamp",
            (SESSION_ID_A,),
        ).fetchall()
        # 1 compact_boundary -> 2 epochs.
        # Epoch 0: 2 user + 2 assistant = 4 messages
        # Epoch 1: 1 summary user + 2 user + 2 assistant = 5 messages
        assert len(rows) >= 4
        # Check roles
        roles = {r["role"] for r in rows}
        assert roles == {"user", "assistant"}

    def test_compact_event_table_populated(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        """Compact event is recorded."""
        rows = indexed_db.execute(
            "SELECT * FROM compact_event WHERE session_id = ?", (SESSION_ID_A,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["epoch"] == 1
        assert rows[0]["trigger"] == "auto"

    def test_epoch_0_messages(self, indexed_db: sqlite3.Connection) -> None:
        """Messages before compact_boundary have epoch=0."""
        rows = indexed_db.execute(
            "SELECT * FROM message WHERE session_id = ? AND epoch = 0",
            (SESSION_ID_A,),
        ).fetchall()
        # Epoch 0: 2 user + 2 assistant = 4 messages
        assert len(rows) == 4

    def test_epoch_1_messages(self, indexed_db: sqlite3.Connection) -> None:
        """Messages after compact_boundary have epoch=1."""
        rows = indexed_db.execute(
            "SELECT * FROM message WHERE session_id = ? AND epoch = 1",
            (SESSION_ID_A,),
        ).fetchall()
        # Epoch 1: 1 summary user + 2 user + 2 assistant = 5 messages
        assert len(rows) == 5

    def test_compact_event_summary_text(self, indexed_db: sqlite3.Connection) -> None:
        """The user message after compact_boundary is linked as summary."""
        row = indexed_db.execute(
            "SELECT summary_text FROM compact_event WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()
        assert row["summary_text"] is not None
        assert "continued" in row["summary_text"].lower()


class TestFts5Index:
    """cc-search-v2.AC2.2: FTS5 index returns matches for keywords."""

    def test_fts_match_keyword(self, indexed_db: sqlite3.Connection) -> None:
        """FTS5 search for a keyword in message content returns results."""
        rows = indexed_db.execute(
            "SELECT * FROM message_fts WHERE message_fts MATCH 'database'"
        ).fetchall()
        assert len(rows) > 0

    def test_fts_match_specific_term(self, indexed_db: sqlite3.Connection) -> None:
        """FTS5 search for 'SQLite' returns results."""
        rows = indexed_db.execute(
            "SELECT * FROM message_fts WHERE message_fts MATCH 'sqlite'"
        ).fetchall()
        assert len(rows) > 0

    def test_fts_no_match(self, indexed_db: sqlite3.Connection) -> None:
        """FTS5 search for nonexistent term returns empty."""
        rows = indexed_db.execute(
            "SELECT * FROM message_fts WHERE message_fts MATCH 'xyznonexistent'"
        ).fetchall()
        assert len(rows) == 0


class TestNeedsReindex:
    """cc-search-v2.AC2.3: Re-index triggers only when mtime newer."""

    def test_returns_false_when_current(
        self, indexed_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """After indexing, needs_reindex returns False for same mtime."""
        session_file = tmp_path / f"{SESSION_ID_A}.jsonl"
        stat = session_file.stat()
        meta = SessionMeta(
            session_id=SESSION_ID_A,
            file_path=str(session_file),
            project_path="/home/brian/project",
            file_size=stat.st_size,
            modified_at=stat.st_mtime,
        )
        assert not needs_reindex(indexed_db, meta)

    def test_returns_true_when_newer_mtime(
        self, indexed_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """needs_reindex returns True when file mtime is newer."""
        session_file = tmp_path / f"{SESSION_ID_A}.jsonl"
        meta = SessionMeta(
            session_id=SESSION_ID_A,
            file_path=str(session_file),
            project_path="/home/brian/project",
            file_size=100,
            modified_at=9999999999.0,  # Far in the future
        )
        assert needs_reindex(indexed_db, meta)

    def test_returns_true_when_missing(self, indexed_db: sqlite3.Connection) -> None:
        """needs_reindex returns True for a session not in the DB."""
        meta = SessionMeta(
            session_id="nonexistent-session-id",
            file_path="/fake/path.jsonl",
            project_path="/fake",
            file_size=100,
            modified_at=1000.0,
        )
        assert needs_reindex(indexed_db, meta)


class TestMaterialisedViews:
    """cc-search-v2.AC2.4: Materialised views reflect current data."""

    def test_project_summary_populated(self, indexed_db: sqlite3.Connection) -> None:
        """project_summary has correct aggregates after indexing."""
        row = indexed_db.execute(
            "SELECT * FROM project_summary WHERE project_path = '/home/brian/project'"
        ).fetchone()
        assert row is not None
        assert row["session_count"] == 1
        assert row["latest_activity"] is not None

    def test_epoch_summary_populated(self, indexed_db: sqlite3.Connection) -> None:
        """epoch_summary has rows for each epoch."""
        rows = indexed_db.execute(
            "SELECT * FROM epoch_summary WHERE session_id = ? ORDER BY epoch",
            (SESSION_ID_A,),
        ).fetchall()
        assert len(rows) == 2  # epochs 0 and 1
        assert rows[0]["epoch"] == 0
        assert rows[1]["epoch"] == 1
        assert rows[0]["message_count"] == 4  # 2 user + 2 assistant
        assert rows[1]["message_count"] == 5  # 1 summary + 2 user + 2 assistant

    def test_epoch_summary_timestamps(self, indexed_db: sqlite3.Connection) -> None:
        """epoch_summary has correct first/last timestamps."""
        rows = indexed_db.execute(
            "SELECT * FROM epoch_summary WHERE session_id = ? ORDER BY epoch",
            (SESSION_ID_A,),
        ).fetchall()
        for row in rows:
            assert row["first_timestamp"] is not None
            assert row["last_timestamp"] is not None
            assert row["first_timestamp"] <= row["last_timestamp"]


class TestNoCompactBoundary:
    """cc-search-v2.AC2.5: Session with no compact_boundary -> all epoch 0."""

    def test_all_messages_epoch_0(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """All messages have epoch=0 when there are no compact_boundaries."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        rows = db_conn.execute(
            "SELECT DISTINCT epoch FROM message WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchall()
        epochs = [r["epoch"] for r in rows]
        assert epochs == [0]

    def test_no_compact_events(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """No compact_event rows when there are no compact_boundaries."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        count = db_conn.execute(
            "SELECT COUNT(*) FROM compact_event WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()[0]
        assert count == 0


class TestThreeCompactBoundaries:
    """cc-search-v2.AC2.6: 3 compact_boundaries -> epochs 0, 1, 2, 3."""

    def test_four_epochs(self, db_conn: sqlite3.Connection, tmp_path: Path) -> None:
        """Session with 3 compact_boundaries creates epochs 0-3."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=3)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        rows = db_conn.execute(
            "SELECT DISTINCT epoch FROM message WHERE session_id = ? ORDER BY epoch",
            (SESSION_ID_A,),
        ).fetchall()
        epochs = [r["epoch"] for r in rows]
        assert epochs == [0, 1, 2, 3]

    def test_three_compact_events(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Three compact_event rows with epochs 1, 2, 3."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=3)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        rows = db_conn.execute(
            "SELECT epoch FROM compact_event WHERE session_id = ? ORDER BY epoch",
            (SESSION_ID_A,),
        ).fetchall()
        epochs = [r["epoch"] for r in rows]
        assert epochs == [1, 2, 3]

    def test_epoch_message_counts(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Each epoch has the expected message count."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=3)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        rows = db_conn.execute(
            "SELECT epoch, COUNT(*) AS cnt FROM message "
            "WHERE session_id = ? GROUP BY epoch ORDER BY epoch",
            (SESSION_ID_A,),
        ).fetchall()
        # Epoch 0: 4 messages (no summary)
        # Epochs 1, 2, 3: 5 messages each (1 summary user + 2 user + 2 asst)
        assert rows[0]["cnt"] == 4
        for row in rows[1:]:
            assert row["cnt"] == 5


class TestReindexIdempotent:
    """Verify that re-indexing a session replaces old data cleanly."""

    def test_reindex_replaces_data(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Re-indexing the same session replaces previous data."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=1)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)

        # Index twice
        index_session(db_conn, meta)
        index_session(db_conn, meta)

        # Should have exactly 1 session row
        count = db_conn.execute(
            "SELECT COUNT(*) FROM session WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()[0]
        assert count == 1

        # Messages should not be duplicated
        msg_count = db_conn.execute(
            "SELECT COUNT(*) FROM message WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()[0]
        # With 1 compact_boundary: 4 + 5 = 9 messages
        assert msg_count == 9


# ============================================================
# Epoch keyword extraction tests (cc-search-v2.AC2.4)
# ============================================================


class TestKeywordExtraction:
    """cc-search-v2.AC2.4: TF-IDF keyword extraction populates epoch_summary.keywords."""

    def test_keywords_null_before_extraction(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        """epoch_summary.keywords is NULL before compute_epoch_keywords is called."""
        rows = indexed_db.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchall()
        assert len(rows) > 0
        for row in rows:
            assert row["keywords"] is None

    def test_keywords_populated_after_extraction(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        """epoch_summary.keywords is non-NULL after compute_epoch_keywords."""
        compute_epoch_keywords(indexed_db, SESSION_ID_A)

        rows = indexed_db.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchall()
        assert len(rows) > 0
        for row in rows:
            assert row["keywords"] is not None
            assert len(row["keywords"]) > 0

    def test_epoch_0_keywords_match_topic(self, indexed_db: sqlite3.Connection) -> None:
        """Epoch 0 (database/schema topic) produces relevant keywords."""
        compute_epoch_keywords(indexed_db, SESSION_ID_A)

        row = indexed_db.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ? AND epoch = 0",
            (SESSION_ID_A,),
        ).fetchone()
        keywords = row["keywords"].lower()
        # Epoch 0 messages discuss database schema and FTS5
        # At least one of these topic words should appear
        topic_words = {"database", "schema", "sqlite", "fts5", "search"}
        found = {w for w in topic_words if w in keywords}
        assert len(found) >= 1, f"Expected topic words in '{keywords}', found none"

    def test_epoch_1_keywords_match_topic(self, indexed_db: sqlite3.Connection) -> None:
        """Epoch 1 (authentication/OAuth topic) produces relevant keywords."""
        compute_epoch_keywords(indexed_db, SESSION_ID_A)

        row = indexed_db.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ? AND epoch = 1",
            (SESSION_ID_A,),
        ).fetchone()
        keywords = row["keywords"].lower()
        # Epoch 1 messages discuss authentication and OAuth
        topic_words = {"authentication", "oauth", "token", "tokens", "jwt", "refresh"}
        found = {w for w in topic_words if w in keywords}
        assert len(found) >= 1, f"Expected topic words in '{keywords}', found none"

    def test_keywords_are_comma_separated(self, indexed_db: sqlite3.Connection) -> None:
        """Keywords are stored as comma-separated values."""
        compute_epoch_keywords(indexed_db, SESSION_ID_A)

        rows = indexed_db.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchall()
        for row in rows:
            kw = row["keywords"]
            # Should contain at least one comma (multiple keywords)
            parts = [p.strip() for p in kw.split(",")]
            assert len(parts) >= 2, f"Expected multiple keywords, got: {kw}"

    def test_keywords_limited_to_top_n(self, indexed_db: sqlite3.Connection) -> None:
        """Keywords are limited to top_n terms."""
        top_n = 5
        compute_epoch_keywords(indexed_db, SESSION_ID_A, top_n=top_n)

        rows = indexed_db.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchall()
        for row in rows:
            parts = [p.strip() for p in row["keywords"].split(",")]
            assert len(parts) <= top_n


class TestKeywordExtractionGenericContent:
    """Keywords are still produced even for generic content."""

    def test_generic_content_produces_keywords(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Session with common words still produces keywords."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)
        compute_epoch_keywords(db_conn, SESSION_ID_A)

        row = db_conn.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ? AND epoch = 0",
            (SESSION_ID_A,),
        ).fetchone()
        assert row is not None
        assert row["keywords"] is not None
        assert len(row["keywords"]) > 0


class TestUpdateAllKeywords:
    """update_all_keywords batch keyword computation."""

    def test_update_single_session(self, indexed_db: sqlite3.Connection) -> None:
        """update_all_keywords with session_id populates that session's keywords."""
        update_all_keywords(indexed_db, session_id=SESSION_ID_A)

        rows = indexed_db.execute(
            "SELECT keywords FROM epoch_summary WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchall()
        for row in rows:
            assert row["keywords"] is not None

    def test_update_all_sessions(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """update_all_keywords with no session_id computes for all sessions."""
        # Index two sessions
        lines_a = _make_session_lines(SESSION_ID_A, compact_boundaries=1)
        meta_a = _write_session_file(tmp_path, SESSION_ID_A, lines_a)
        index_session(db_conn, meta_a)

        sid_b = "bbbbbbbb-1111-2222-3333-bbbbbbbbbbbb"
        lines_b = _make_session_lines(sid_b, compact_boundaries=0)
        meta_b = _write_session_file(tmp_path, sid_b, lines_b)
        index_session(db_conn, meta_b)

        # All keywords should be NULL
        null_count = db_conn.execute(
            "SELECT COUNT(*) FROM epoch_summary WHERE keywords IS NULL"
        ).fetchone()[0]
        assert null_count > 0

        # Update all
        update_all_keywords(db_conn)

        # No NULL keywords remaining
        null_count = db_conn.execute(
            "SELECT COUNT(*) FROM epoch_summary WHERE keywords IS NULL"
        ).fetchone()[0]
        assert null_count == 0
