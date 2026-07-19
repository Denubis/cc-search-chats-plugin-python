"""Tests for database connection, schema init, and integrity checking.

Verifies:
    cc-search-v2.AC7.1: Index DB created automatically on first use.
    cc-search-v2.AC7.4: Corrupted DB detected and auto-regenerated.
"""

import sqlite3
from pathlib import Path

import pytest

from cc_search_chats.storage.index import (
    close_db,
    ensure_fts5,
    get_db_path,
    open_db,
)

# All expected tables that the schema should create.
EXPECTED_TABLES = {
    "session",
    "message",
    "compact_event",
    "message_fts",
    "message_fts_vocab",
    "project_summary",
    "epoch_summary",
}


def _get_table_names(conn: sqlite3.Connection) -> set[str]:
    """Return set of user-defined table names (excluding FTS5 internals)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    # FTS5 creates internal tables like message_fts_config, message_fts_data, etc.
    # We just check for the main tables we care about.
    return {r[0] for r in rows}


class TestGetDbPath:
    """Tests for get_db_path()."""

    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default path is ~/.cc-search-chats/index.db."""
        monkeypatch.delenv("CC_SEARCH_DB_PATH", raising=False)
        path = get_db_path()
        assert path.name == "index.db"
        assert path.parent.name == ".cc-search-chats"

    def test_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CC_SEARCH_DB_PATH overrides the default path."""
        custom = tmp_path / "custom.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(custom))
        path = get_db_path()
        assert path == custom

    def test_creates_parent_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Parent directory is created if it does not exist."""
        nested = tmp_path / "deep" / "nested" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(nested))
        path = get_db_path()
        assert path.parent.exists()


class TestOpenDb:
    """Tests for open_db()."""

    def test_creates_new_db_with_schema(self, tmp_path: Path) -> None:
        """AC7.1: New DB file is created and schema applied automatically."""
        db_path = tmp_path / "new.db"
        assert not db_path.exists()

        conn = open_db(db_path)
        try:
            assert db_path.exists()
            tables = _get_table_names(conn)
            for expected in EXPECTED_TABLES:
                assert expected in tables, f"Missing table: {expected}"
        finally:
            close_db(conn)

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        """Database uses WAL journal mode."""
        conn = open_db(tmp_path / "wal.db")
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"
        finally:
            close_db(conn)

    def test_foreign_keys_enabled(self, tmp_path: Path) -> None:
        """Foreign key enforcement is active."""
        conn = open_db(tmp_path / "fk.db")
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert fk == 1
        finally:
            close_db(conn)

    def test_row_factory_is_row(self, tmp_path: Path) -> None:
        """Connection row_factory is sqlite3.Row for dict-like access."""
        conn = open_db(tmp_path / "factory.db")
        try:
            assert conn.row_factory is sqlite3.Row
        finally:
            close_db(conn)

    def test_reopening_existing_db(self, tmp_path: Path) -> None:
        """Opening an existing valid DB does not re-apply schema or lose data."""
        db_path = tmp_path / "existing.db"

        # Create and populate
        conn1 = open_db(db_path)
        conn1.execute(
            "INSERT INTO session "
            "(session_id, project_path, file_path, file_size, modified_at, "
            "indexed_at, summary) VALUES "
            "('s1', '/proj', '/f.jsonl', 100, '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z', NULL)"
        )
        conn1.commit()
        close_db(conn1)

        # Reopen — data should still be there
        conn2 = open_db(db_path)
        try:
            row = conn2.execute(
                "SELECT session_id FROM session WHERE session_id = 's1'"
            ).fetchone()
            assert row is not None
            assert row["session_id"] == "s1"
        finally:
            close_db(conn2)

    def test_corrupt_db_is_recovered(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC7.4: Corrupted DB is detected, warning printed, and DB rebuilt."""
        db_path = tmp_path / "corrupt.db"

        # Write garbage bytes to simulate corruption
        db_path.write_bytes(b"THIS IS NOT A SQLITE DATABASE" * 100)

        conn = open_db(db_path)
        try:
            # (a) No exception raised — we got here
            # (b) Stderr contains corruption warning
            captured = capsys.readouterr()
            assert "corrupted" in captured.err.lower()
            assert "sync" in captured.err.lower() or "syncthing" in captured.err.lower()

            # (c) Schema is valid — all expected tables present
            tables = _get_table_names(conn)
            for expected in EXPECTED_TABLES:
                assert expected in tables, f"Missing table after recovery: {expected}"
        finally:
            close_db(conn)

    def test_db_path_defaults_to_get_db_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """open_db() with no argument uses get_db_path()."""
        db_file = tmp_path / "default.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_file))
        conn = open_db()
        try:
            assert db_file.exists()
        finally:
            close_db(conn)


class TestEnsureFts5:
    """Tests for ensure_fts5()."""

    def test_fts5_available(self) -> None:
        """FTS5 should be available in this Python build."""
        # Should not raise
        result = ensure_fts5()
        assert result is True

    def test_fts5_unavailable_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If FTS5 is not available, RuntimeError is raised."""
        from unittest.mock import MagicMock

        # Create a mock connection whose execute always raises for FTS5 DDL.
        mock_conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such module: fts5")
        mock_conn.close = MagicMock()

        monkeypatch.setattr(
            "cc_search_chats.storage.index.sqlite3.connect",
            lambda *a, **kw: mock_conn,
        )
        with pytest.raises(RuntimeError, match="FTS5"):
            ensure_fts5()


class TestCloseDb:
    """Tests for close_db()."""

    def test_closes_connection(self, tmp_path: Path) -> None:
        """Connection is closed after close_db()."""
        conn = open_db(tmp_path / "close.db")
        close_db(conn)
        # Attempting to use a closed connection should raise
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
