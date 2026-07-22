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

import json
import sqlite3
from pathlib import Path

from cc_search_chats.core.models import SessionMeta
from cc_search_chats.storage.index import (
    index_all_projects,
    index_session,
    needs_reindex,
    search,
)
from tests.conftest import (
    SESSION_ID_A,
    SESSION_ID_B,
    _make_session_lines,
    _write_session_file,
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


class TestIndexAllProjects:
    """index --all: incrementally index every project under the projects dir."""

    def _make_project(self, projects_dir: Path, encoded: str, session_id: str) -> None:
        proj = projects_dir / encoded
        proj.mkdir(parents=True)
        lines = _make_session_lines(session_id, compact_boundaries=0)
        (proj / f"{session_id}.jsonl").write_text("\n".join(lines), encoding="utf-8")

    def test_indexes_every_project_dir(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Every project directory is discovered and its sessions indexed."""
        projects = tmp_path / "projects"
        self._make_project(projects, "-home-brian-alpha", SESSION_ID_A)
        self._make_project(projects, "-home-brian-beta", SESSION_ID_B)

        counts = index_all_projects(db_conn, projects_dir=projects)

        assert counts["projects"] == 2
        assert counts["indexed"] == 2
        assert counts["skipped"] == 0
        proj_paths = {
            r["project_path"]
            for r in db_conn.execute(
                "SELECT DISTINCT project_path FROM session"
            ).fetchall()
        }
        assert proj_paths == {"/home/brian/alpha", "/home/brian/beta"}

    def test_content_searchable_across_projects(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """After index --all, a project=None search spans every project."""
        projects = tmp_path / "projects"
        self._make_project(projects, "-home-brian-alpha", SESSION_ID_A)
        self._make_project(projects, "-home-brian-beta", SESSION_ID_B)
        index_all_projects(db_conn, projects_dir=projects)

        results = search(db_conn, "database")  # project=None -> all projects
        session_ids = {r["session_id"] for r in results}
        assert SESSION_ID_A in session_ids
        assert SESSION_ID_B in session_ids

    def test_reindex_is_incremental(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A second pass skips unchanged sessions."""
        projects = tmp_path / "projects"
        self._make_project(projects, "-home-brian-alpha", SESSION_ID_A)

        first = index_all_projects(db_conn, projects_dir=projects)
        assert first["indexed"] == 1
        assert first["skipped"] == 0

        second = index_all_projects(db_conn, projects_dir=projects)
        assert second["indexed"] == 0
        assert second["skipped"] == 1

    def test_empty_projects_dir(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """An empty projects dir indexes nothing."""
        projects = tmp_path / "projects"
        projects.mkdir()
        assert index_all_projects(db_conn, projects_dir=projects) == {
            "projects": 0,
            "indexed": 0,
            "skipped": 0,
        }

    def test_missing_projects_dir(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A nonexistent projects dir is handled gracefully."""
        assert index_all_projects(db_conn, projects_dir=tmp_path / "nope") == {
            "projects": 0,
            "indexed": 0,
            "skipped": 0,
        }


# ============================================================
# Duplicate UUID handling (regression for intra-file double-logging
# observed in real Claude Code session files)
# ============================================================


def _user_message_record(
    session_id: str,
    uuid: str,
    timestamp: str,
    content: str,
    parent_uuid: str | None = None,
) -> str:
    """Build a JSONL line for a type=user message."""
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "parentUuid": parent_uuid,
            "timestamp": timestamp,
            "sessionId": session_id,
            "message": {"role": "user", "content": content},
        }
    )


class TestDuplicateUuidHandling:
    """Regression: real Claude Code session files contain duplicate message
    UUIDs (the same logical message rewritten into the same JSONL on a later
    run, differing only in writer-version fields). The indexer must not crash
    on this input.
    """

    def test_intra_file_duplicate_message_uuids_does_not_crash(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Two identical message records in the same JSONL must not raise."""
        dup_uuid = "dup-msg-0001"
        lines = [
            _user_message_record(
                SESSION_ID_A, dup_uuid, "2026-02-07T10:00:00.000Z", "hello world"
            ),
            _user_message_record(
                SESSION_ID_A, dup_uuid, "2026-02-07T10:00:00.000Z", "hello world"
            ),
        ]
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)

        index_session(db_conn, meta)

        rows = db_conn.execute(
            "SELECT COUNT(*) FROM message WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()
        assert rows[0] == 1, "duplicate UUID should be absorbed, leaving one row"

    def test_cross_session_duplicate_message_uuids_does_not_crash(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A UUID shared by two different session JSONLs must not raise."""
        shared_uuid = "shared-msg-0001"
        lines_a = [
            _user_message_record(
                SESSION_ID_A, shared_uuid, "2026-02-07T10:00:00.000Z", "first"
            ),
        ]
        lines_b = [
            _user_message_record(
                SESSION_ID_B, shared_uuid, "2026-02-07T11:00:00.000Z", "second"
            ),
        ]
        meta_a = _write_session_file(tmp_path, SESSION_ID_A, lines_a)
        meta_b = _write_session_file(tmp_path, SESSION_ID_B, lines_b)

        index_session(db_conn, meta_a)
        index_session(db_conn, meta_b)

        sessions = db_conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        assert sessions == 2, "both session rows should be present"

        msg_rows = db_conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        assert msg_rows == 1, "first-seen UUID wins; the second is ignored"

    def test_intra_file_duplicate_compact_boundary_does_not_crash(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Two identical compact_boundary records in the same JSONL must not raise."""
        dup_uuid = "dup-compact-0001"
        boundary = json.dumps(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": dup_uuid,
                "timestamp": "2026-02-07T10:00:00.000Z",
                "sessionId": SESSION_ID_A,
                "compactMetadata": {"trigger": "auto", "preTokens": 1000},
            }
        )
        lines = [boundary, boundary]
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)

        index_session(db_conn, meta)

        rows = db_conn.execute(
            "SELECT COUNT(*) FROM compact_event WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()
        assert rows[0] == 1, "duplicate compact_boundary should be absorbed"


class TestRealProjectPath:
    """The session's true filesystem path (from cwd) is stored for display."""

    def _index_with_cwd(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        session_id: str,
        cwd: str,
    ) -> None:
        line = json.dumps(
            {
                "type": "user",
                "uuid": f"{session_id}-u1",
                "timestamp": "2026-02-07T10:00:00Z",
                "sessionId": session_id,
                "cwd": cwd,
                "message": {"role": "user", "content": "hello world"},
            }
        )
        session_file = tmp_path / f"{session_id}.jsonl"
        session_file.write_text(line, encoding="utf-8")
        meta = SessionMeta(
            session_id=session_id,
            file_path=str(session_file),
            project_path="/home/brian/lossy/decoded",  # the lossy stand-in
            file_size=session_file.stat().st_size,
            modified_at=session_file.stat().st_mtime,
        )
        index_session(db_conn, meta)

    def test_real_project_path_captured_from_cwd(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        cwd = "/home/brian/people/Brian/cc-search-chats-plugin-python"
        self._index_with_cwd(db_conn, tmp_path, SESSION_ID_A, cwd)
        row = db_conn.execute(
            "SELECT real_project_path FROM session WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()
        assert row["real_project_path"] == cwd

    def test_real_project_path_null_without_cwd(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        # The sample session fixture carries no cwd field.
        row = indexed_db.execute(
            "SELECT real_project_path FROM session WHERE session_id = ?",
            (SESSION_ID_A,),
        ).fetchone()
        assert row["real_project_path"] is None

    def test_migration_adds_column_to_old_db(self, tmp_path: Path) -> None:
        # Simulate a pre-existing DB whose session table predates the column.
        from cc_search_chats.storage.index import close_db, open_db

        db_path = tmp_path / "old.db"
        raw = sqlite3.connect(str(db_path))
        raw.execute(
            "CREATE TABLE session (session_id TEXT PRIMARY KEY, project_path TEXT, "
            "file_path TEXT, file_size INTEGER, modified_at TEXT, indexed_at TEXT, "
            "summary TEXT)"
        )
        raw.commit()
        raw.close()

        conn = open_db(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(session)").fetchall()]
        close_db(conn)
        assert "real_project_path" in cols
