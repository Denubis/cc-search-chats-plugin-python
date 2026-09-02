"""Tests for the indexing pipeline.

Acceptance criteria coverage:
- cc-search-v2.AC2.1: Session indexed into session, message, compact_event tables
- cc-search-v2.AC2.2: FTS5 index returns matches for keywords
- cc-search-v2.AC2.3: Re-index triggers only when mtime newer
- cc-search-v2.AC2.4: Materialised views reflect current data
- cc-search-v2.AC2.5: No compact_boundary -> all messages in epoch 0
- cc-search-v2.AC2.6: 3 compact_boundaries -> epochs 0-3
- cc-search-v2.AC7.1: DB created automatically (covered in test_index.py)
- Index damage preservation and reporting (covered in test_index.py)
"""

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
from tests.conftest import (
    SESSION_ID_A,
    SESSION_ID_B,
    _make_session_lines,
    _write_session_file,
)

from cc_search_chats.core.discovery import decode_project_path, encode_project_path
from cc_search_chats.core.models import SessionMeta, SessionRecord
from cc_search_chats.storage import index as index_module
from cc_search_chats.storage.index import (
    index_all_projects,
    index_session,
    jit_reindex,
    needs_reindex,
    reindex_project,
    search,
)

if TYPE_CHECKING:
    from pathlib import Path


def _snapshot_index(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Capture every user-visible index projection needed for rollback checks."""
    queries = {
        "session": "SELECT * FROM session ORDER BY session_id",
        "message": "SELECT * FROM message ORDER BY uuid",
        "compact_event": "SELECT * FROM compact_event ORDER BY uuid",
        "fts": (
            "SELECT message.uuid, message_fts.text_content "
            "FROM message_fts JOIN message ON message.rowid = message_fts.rowid "
            "ORDER BY message.uuid"
        ),
        "project_summary": "SELECT * FROM project_summary ORDER BY project_path",
        "epoch_summary": ("SELECT * FROM epoch_summary ORDER BY session_id, epoch"),
    }
    return {
        name: [tuple(row) for row in conn.execute(query).fetchall()]
        for name, query in queries.items()
    }


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


class TestTransactionalRebuild:
    """A rebuild either replaces the complete project index or changes nothing."""

    def test_rebuild_rejects_active_transaction_without_rolling_it_back(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A caller-owned transaction remains open and unchanged."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )
        db_conn.execute(
            "INSERT INTO session "
            "(session_id, project_path, file_path, file_size, modified_at, "
            "indexed_at, summary) VALUES "
            "('caller-pending', '/caller', '/caller.jsonl', 1, "
            "'2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z', NULL)"
        )
        assert db_conn.in_transaction

        with pytest.raises(
            sqlite3.ProgrammingError,
            match="reindex_project requires a connection with no active transaction",
        ):
            reindex_project(db_conn, "/target")

        assert db_conn.in_transaction
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM session WHERE session_id = 'caller-pending'"
            ).fetchone()[0]
            == 1
        )
        db_conn.rollback()

    def test_single_session_rejects_active_transaction_without_committing_it(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """Incremental indexing cannot commit a caller-owned transaction."""
        replacement_meta = _write_session_file(
            tmp_path,
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=0),
        )
        db_conn.execute(
            "INSERT INTO session "
            "(session_id, project_path, file_path, file_size, modified_at, "
            "indexed_at, summary) VALUES "
            "('caller-pending', '/caller', '/caller.jsonl', 1, "
            "'2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z', NULL)"
        )
        assert db_conn.in_transaction

        with pytest.raises(
            sqlite3.ProgrammingError,
            match="index_session requires a connection with no active transaction",
        ):
            index_session(db_conn, replacement_meta)

        assert db_conn.in_transaction
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM session WHERE session_id = 'caller-pending'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM session WHERE session_id = ?",
                (SESSION_ID_A,),
            ).fetchone()[0]
            == 0
        )
        db_conn.rollback()

    def test_single_session_autocommit_open_failure_is_reported_and_skipped(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A source-open skip preserves prior state, including in autocommit mode."""
        old_meta = _write_session_file(
            tmp_path,
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=1),
        )
        index_session(db_conn, old_meta)
        before = _snapshot_index(db_conn)
        db_conn.isolation_level = None
        missing_path = tmp_path / "missing-replacement.jsonl"
        missing_meta = SessionMeta(
            session_id=SESSION_ID_A,
            file_path=str(missing_path),
            project_path=old_meta.project_path,
            file_size=1,
            modified_at=old_meta.modified_at + 1,
        )

        indexed = index_session(db_conn, missing_meta)

        captured = capsys.readouterr()
        assert indexed is False
        assert f"Warning: Could not open session file {missing_path}:" in captured.err
        assert "Skipping." in captured.err
        assert not db_conn.in_transaction
        assert _snapshot_index(db_conn) == before

    def test_success_replaces_project_and_preserves_unrelated_sessions(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Project deletion uses the same lossy key stored by discovery."""
        real_project = "/home/brian/my-project/.worktree"
        encoded = encode_project_path(real_project)
        stored_project = decode_project_path(encoded)

        (tmp_path / "stale").mkdir()
        stale_meta = _write_session_file(
            tmp_path / "stale",
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=1),
            stored_project,
        )
        index_session(db_conn, stale_meta)

        unrelated_id = "cccccccc-1111-2222-3333-cccccccccccc"
        (tmp_path / "unrelated").mkdir()
        unrelated_meta = _write_session_file(
            tmp_path / "unrelated",
            unrelated_id,
            _make_session_lines(unrelated_id, compact_boundaries=0),
            "/unrelated/project",
        )
        index_session(db_conn, unrelated_meta)

        projects_dir = tmp_path / "projects"
        current_dir = projects_dir / encoded
        current_dir.mkdir(parents=True)
        current_file = current_dir / f"{SESSION_ID_B}.jsonl"
        current_file.write_text(
            "\n".join(_make_session_lines(SESSION_ID_B, compact_boundaries=0)),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )

        counts = reindex_project(db_conn, real_project)

        assert counts == {"indexed": 1, "skipped": 0}
        project_sessions = db_conn.execute(
            "SELECT session_id, modified_at FROM session "
            "WHERE project_path = ? ORDER BY session_id",
            (stored_project,),
        ).fetchall()
        assert [row["session_id"] for row in project_sessions] == [SESSION_ID_B]
        replacement_modified_at = project_sessions[0]["modified_at"]
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?",
                (SESSION_ID_B,),
            ).fetchone()[0]
            == 4
        )
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM message_fts "
                "JOIN message ON message.rowid = message_fts.rowid "
                "WHERE message_fts MATCH 'database' AND message.session_id = ?",
                (SESSION_ID_B,),
            ).fetchone()[0]
            > 0
        )
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?", (SESSION_ID_A,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM compact_event WHERE session_id = ?",
                (SESSION_ID_A,),
            ).fetchone()[0]
            == 0
        )
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM message_fts "
                "JOIN message ON message.rowid = message_fts.rowid "
                "WHERE message_fts MATCH 'database' AND message.session_id = ?",
                (SESSION_ID_A,),
            ).fetchone()[0]
            == 0
        )
        summary = db_conn.execute(
            "SELECT session_count, latest_activity FROM project_summary "
            "WHERE project_path = ?",
            (stored_project,),
        ).fetchone()
        assert summary["session_count"] == 1
        assert summary["latest_activity"] == replacement_modified_at
        replacement_epoch = db_conn.execute(
            "SELECT epoch, message_count FROM epoch_summary WHERE session_id = ?",
            (SESSION_ID_B,),
        ).fetchall()
        assert [(row["epoch"], row["message_count"]) for row in replacement_epoch] == [
            (0, 4)
        ]
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM epoch_summary WHERE session_id = ?",
                (SESSION_ID_A,),
            ).fetchone()[0]
            == 0
        )
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM session WHERE session_id = ?",
                (unrelated_id,),
            ).fetchone()[0]
            == 1
        )

    def test_missing_file_is_reported_and_skipped_during_atomic_rebuild(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A skipped source is absent while readable replacements commit."""
        project_path = "/home/brian/project"
        (tmp_path / "stale").mkdir()
        stale_meta = _write_session_file(
            tmp_path / "stale",
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=1),
            project_path,
        )
        index_session(db_conn, stale_meta)

        (tmp_path / "replacement").mkdir()
        valid_meta = _write_session_file(
            tmp_path / "replacement",
            SESSION_ID_B,
            _make_session_lines(SESSION_ID_B, compact_boundaries=0),
            project_path,
        )
        valid_meta = SessionMeta(
            session_id=valid_meta.session_id,
            file_path=valid_meta.file_path,
            project_path=valid_meta.project_path,
            file_size=valid_meta.file_size,
            modified_at=2.0,
        )
        missing_path = tmp_path / "vanished.jsonl"
        missing_meta = SessionMeta(
            session_id="dddddddd-1111-2222-3333-dddddddddddd",
            file_path=str(missing_path),
            project_path=project_path,
            file_size=1,
            modified_at=1.0,
        )
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.list_session_files",
            lambda *_args: [missing_meta, valid_meta],
        )

        counts = reindex_project(db_conn, project_path)

        captured = capsys.readouterr()
        assert counts == {"indexed": 1, "skipped": 1}
        assert f"Warning: Could not open session file {missing_path}:" in captured.err
        assert "Skipping." in captured.err
        assert not db_conn.in_transaction
        assert [
            row["session_id"]
            for row in db_conn.execute(
                "SELECT session_id FROM session WHERE project_path = ?",
                (project_path,),
            ).fetchall()
        ] == [SESSION_ID_B]
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?",
                (SESSION_ID_B,),
            ).fetchone()[0]
            == 4
        )

    def test_parser_failure_rolls_back_complete_project_state(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-SQLite failure names its cause and retains the prior index."""
        project_path = "/home/brian/project"
        (tmp_path / "stale").mkdir()
        stale_meta = _write_session_file(
            tmp_path / "stale",
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=1),
            project_path,
        )
        index_session(db_conn, stale_meta)
        before = _snapshot_index(db_conn)

        (tmp_path / "replacement").mkdir()
        replacement_meta = _write_session_file(
            tmp_path / "replacement",
            SESSION_ID_B,
            _make_session_lines(SESSION_ID_B, compact_boundaries=0),
            project_path,
        )
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.list_session_files",
            lambda *_args: [replacement_meta],
        )

        def failing_parse(
            _lines: object, session_id: str, *, full_content: bool = False
        ) -> object:
            del full_content
            yield SessionRecord(
                record_type="user",
                uuid="partial-replacement-message",
                parent_uuid=None,
                timestamp="2026-07-31T00:00:00Z",
                session_id=session_id,
                role="user",
                text_content="partial replacement",
                leaf_uuid=None,
            )
            raise ValueError("parser exploded")

        monkeypatch.setattr(index_module, "parse_session", failing_parse)

        with pytest.raises(RuntimeError) as caught:
            reindex_project(db_conn, project_path)

        assert "ValueError: parser exploded" in str(caught.value)
        assert "prior index contents remain intact" in str(caught.value).lower()
        assert _snapshot_index(db_conn) == before
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'partial'"
            ).fetchone()[0]
            == 0
        )

    def test_single_session_parser_failure_is_atomic(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The public incremental index operation also rolls back on exceptions."""
        (tmp_path / "old").mkdir()
        old_meta = _write_session_file(
            tmp_path / "old",
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=1),
        )
        index_session(db_conn, old_meta)
        before = _snapshot_index(db_conn)

        (tmp_path / "replacement").mkdir()
        replacement_meta = _write_session_file(
            tmp_path / "replacement",
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=0),
        )

        def failing_parse(
            _lines: object, session_id: str, *, full_content: bool = False
        ) -> object:
            del full_content
            yield SessionRecord(
                record_type="user",
                uuid="partial-single-session-message",
                parent_uuid=None,
                timestamp="2026-07-31T00:00:00Z",
                session_id=session_id,
                role="user",
                text_content="partial single-session replacement",
                leaf_uuid=None,
            )
            raise ValueError("single parser exploded")

        monkeypatch.setattr(index_module, "parse_session", failing_parse)

        with pytest.raises(ValueError, match="single parser exploded"):
            index_session(db_conn, replacement_meta)

        assert _snapshot_index(db_conn) == before
        assert (
            db_conn.execute(
                "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'partial'"
            ).fetchone()[0]
            == 0
        )


class TestJitReindex:
    """JIT indexing tolerates source-open races without lying about counts."""

    def test_reports_and_skips_missing_file_while_indexing_readable_session(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        project_path = "/home/brian/project"
        valid_meta = _write_session_file(
            tmp_path,
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=0),
            project_path,
        )
        missing_path = tmp_path / "jit-vanished.jsonl"
        missing_meta = SessionMeta(
            session_id=SESSION_ID_B,
            file_path=str(missing_path),
            project_path=project_path,
            file_size=1,
            modified_at=valid_meta.modified_at + 1,
        )
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.list_session_files",
            lambda *_args: [missing_meta, valid_meta],
        )

        counts = jit_reindex(db_conn, project_path)

        captured = capsys.readouterr()
        assert counts == {"indexed": 1, "skipped": 1}
        assert f"Warning: Could not open session file {missing_path}:" in captured.err
        assert "Skipping." in captured.err
        assert not db_conn.in_transaction
        assert [
            row["session_id"]
            for row in db_conn.execute(
                "SELECT session_id FROM session ORDER BY session_id"
            ).fetchall()
        ] == [SESSION_ID_A]


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

    def test_reports_and_counts_missing_file_as_skipped(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        projects = tmp_path / "projects"
        (projects / "-home-brian-project").mkdir(parents=True)
        valid_meta = _write_session_file(
            tmp_path,
            SESSION_ID_A,
            _make_session_lines(SESSION_ID_A, compact_boundaries=0),
        )
        missing_path = tmp_path / "all-vanished.jsonl"
        missing_meta = SessionMeta(
            session_id=SESSION_ID_B,
            file_path=str(missing_path),
            project_path="/home/brian/project",
            file_size=1,
            modified_at=valid_meta.modified_at + 1,
        )
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.list_session_files",
            lambda *_args: [missing_meta, valid_meta],
        )

        counts = index_all_projects(db_conn, projects_dir=projects)

        captured = capsys.readouterr()
        assert counts == {"projects": 1, "indexed": 1, "skipped": 1}
        assert f"Warning: Could not open session file {missing_path}:" in captured.err
        assert "Skipping." in captured.err
        assert not db_conn.in_transaction
        assert [
            row["session_id"]
            for row in db_conn.execute(
                "SELECT session_id FROM session ORDER BY session_id"
            ).fetchall()
        ] == [SESSION_ID_A]

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
