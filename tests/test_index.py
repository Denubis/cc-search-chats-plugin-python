"""Tests for database connection, schema init, and integrity checking.

Verifies:
    cc-search-v2.AC7.1: Index DB created automatically on first use.
    Index damage is reported, discarded, and rebuilt only by an explicit command.
    Environmental SQLite failures are not misclassified as damage.
"""

import errno
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cc_search_chats.storage import index as index_module
from cc_search_chats.storage.index import (
    close_db,
    discard_damaged_database,
    ensure_fts5,
    format_index_error,
    get_db_path,
    open_db,
)

# All expected tables that the schema should create.
EXPECTED_TABLES = {
    "session",
    "message",
    "compact_event",
    "message_fts",
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


def _named_operational_error(message: str, error_name: str) -> sqlite3.OperationalError:
    """Build a deterministic SQLite-origin-style error for boundary tests."""
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorname = error_name
    return exc


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

    def test_relative_env_override_resolves_to_actual_absolute_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Diagnostics and sandbox configuration receive an actual directory."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CC_SEARCH_DB_PATH", "relative/index.db")

        path = get_db_path()

        assert path == tmp_path / "relative" / "index.db"
        assert path.is_absolute()

    def test_creates_parent_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Parent directory is created if it does not exist."""
        nested = tmp_path / "deep" / "nested" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(nested))
        path = get_db_path()
        assert path.parent.exists()

    def test_parent_creation_failure_is_actionable_not_corruption(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A filesystem permission error has no safe timestamp connection."""
        db_path = tmp_path / "forbidden" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))

        def deny_mkdir(
            _path: Path, *, parents: bool = False, exist_ok: bool = False
        ) -> None:
            del parents, exist_ok
            raise PermissionError("parent denied")

        monkeypatch.setattr(Path, "mkdir", deny_mkdir)

        with pytest.raises(RuntimeError) as caught:
            get_db_path()

        message = str(caught.value)
        assert "PermissionError: parent denied" in message
        assert str(db_path.parent) in message
        assert "timestamp is unavailable" in message
        assert "no safe SQLite connection was available" in message
        assert "sandbox_workspace_write.writable_roots" in message
        assert "corrupt" not in message.lower()


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

    def test_notadb_deletes_file_and_reports_rebuild_command(
        self, tmp_path: Path
    ) -> None:
        """A non-database cache is deleted without rebuilding in this invocation."""
        db_path = tmp_path / "corrupt.db"
        original = b"THIS IS NOT A SQLITE DATABASE" * 100
        db_path.write_bytes(original)

        with pytest.raises(RuntimeError) as exc_info:
            open_db(db_path)

        message = str(exc_info.value)
        assert "SQLITE_NOTADB" in message
        assert (
            f"The damaged index at {db_path} is no longer present. "
            "Run `cc-search-chats index` to rebuild it."
        ) in message
        assert "was deleted" not in message
        assert "SQLite stopped the operation with an explicit damage result" in message
        assert "cannot safely open" not in message
        assert "Rebuilding from chat history" not in message
        assert not db_path.exists()

    @pytest.mark.parametrize(
        ("error_name", "expect_deleted", "expected_remedy"),
        [
            ("SQLITE_CORRUPT_INDEX", True, None),
            (
                "SQLITE_READONLY_DIRECTORY",
                False,
                "Grant read/write access to the index directory",
            ),
            (
                "SQLITE_BUSY_TIMEOUT",
                False,
                "Wait for the concurrent indexing or database operation to finish",
            ),
        ],
    )
    def test_integrity_exception_deletes_only_explicit_damage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        error_name: str,
        expect_deleted: bool,
        expected_remedy: str | None,
    ) -> None:
        """CORRUPT deletes while READONLY and BUSY preserve on the same path."""
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        close_db(conn)
        original = db_path.read_bytes()
        error = _named_operational_error("forced integrity failure", error_name)

        def fail_integrity(_conn: sqlite3.Connection) -> str | None:
            raise error

        monkeypatch.setattr(index_module, "_check_integrity", fail_integrity)

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert error_name in message
        assert "OperationalError: forced integrity failure" in message
        if expect_deleted:
            assert (
                f"The damaged index at {db_path} is no longer present. "
                "Run `cc-search-chats index` to rebuild it."
            ) in message
            assert "was deleted" not in message
            assert not db_path.exists()
        else:
            assert "The database file was deleted" not in message
            assert db_path.read_bytes() == original
            assert expected_remedy is not None
            assert expected_remedy in message

    def test_damage_and_failed_unlink_report_both_causes_and_file_remains(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A denied unlink reports damage and permissions without claiming success."""
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        close_db(conn)
        original = db_path.read_bytes()
        damage_error = _named_operational_error(
            "database disk image is malformed",
            "SQLITE_CORRUPT_INDEX",
        )

        def fail_integrity(_conn: sqlite3.Connection) -> str | None:
            raise damage_error

        attempted_paths: list[Path] = []

        def deny_unlink(path: Path, missing_ok: bool = False) -> None:
            assert missing_ok is True
            attempted_paths.append(path)
            raise PermissionError(errno.EACCES, "unlink denied", str(path))

        monkeypatch.setattr(index_module, "_check_integrity", fail_integrity)
        monkeypatch.setattr(Path, "unlink", deny_unlink)

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_CORRUPT_INDEX" in message
        assert "OperationalError: database disk image is malformed" in message
        assert "PermissionError" in message
        assert "unlink denied" in message
        assert "The damaged database file remains" in message
        assert str(db_path) in message
        assert "Grant read/write access to the index directory" in message
        assert "sandbox_workspace_write.writable_roots" in message
        assert "~/.codex/config.toml" in message
        assert "The database file was deleted" not in message
        assert "`cc-search-chats index`" not in message
        assert db_path.read_bytes() == original
        assert attempted_paths == [db_path.with_name(f"{db_path.name}-wal")]

    def test_damage_and_eio_unlink_omit_permission_remedy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Generic cleanup I/O failure reports its cause without permission advice."""
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        close_db(conn)
        original = db_path.read_bytes()
        damage_error = _named_operational_error(
            "database disk image is malformed",
            "SQLITE_CORRUPT_INDEX",
        )

        def fail_integrity(_conn: sqlite3.Connection) -> str | None:
            raise damage_error

        def fail_unlink_eio(path: Path, missing_ok: bool = False) -> None:
            assert missing_ok is True
            raise OSError(errno.EIO, "cleanup I/O failure", str(path))

        monkeypatch.setattr(index_module, "_check_integrity", fail_integrity)
        monkeypatch.setattr(Path, "unlink", fail_unlink_eio)

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_CORRUPT_INDEX" in message
        assert "OperationalError: database disk image is malformed" in message
        assert "OSError: [Errno 5] cleanup I/O failure" in message
        assert "The damaged database file remains" in message
        assert str(db_path) in message
        assert "Grant read/write access" not in message
        assert "Codex" not in message
        assert "sandbox_workspace_write.writable_roots" not in message
        assert "~/.codex/config.toml" not in message
        assert db_path.read_bytes() == original

    def test_damage_closes_connection_then_removes_sidecars_before_main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Damage cleanup closes SQLite and removes paired WAL state first."""
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        close_db(conn)
        wal_path = db_path.with_name(f"{db_path.name}-wal")
        shm_path = db_path.with_name(f"{db_path.name}-shm")
        damage_error = _named_operational_error(
            "database disk image is malformed",
            "SQLITE_CORRUPT",
        )
        damaged_connection: sqlite3.Connection | None = None

        def fail_integrity(conn: sqlite3.Connection) -> str | None:
            nonlocal damaged_connection
            damaged_connection = conn
            wal_path.write_bytes(b"paired WAL state")
            shm_path.write_bytes(b"transient SHM state")
            raise damage_error

        original_unlink = Path.unlink
        attempted_paths: list[Path] = []

        def record_unlink(path: Path, missing_ok: bool = False) -> None:
            assert missing_ok is True
            assert damaged_connection is not None
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                damaged_connection.execute("SELECT 1")
            attempted_paths.append(path)
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(index_module, "_check_integrity", fail_integrity)
        monkeypatch.setattr(Path, "unlink", record_unlink)

        with pytest.raises(RuntimeError):
            open_db(db_path)

        assert attempted_paths[:2] == [wal_path, shm_path]
        assert attempted_paths[-1] == db_path
        assert not wal_path.exists()
        assert not shm_path.exists()
        assert not db_path.exists()

    def test_already_absent_damage_cleanup_reports_end_state(
        self, tmp_path: Path
    ) -> None:
        """An absent path is successful cleanup without claiming deletion agency."""
        db_path = tmp_path / "already-absent.db"
        conn = MagicMock(spec=sqlite3.Connection)

        message = discard_damaged_database(
            conn,
            db_path,
            "SQLITE_CORRUPT_INDEX (DatabaseError: malformed image)",
            "SQLite stopped the operation with an explicit damage result.",
        )

        assert (
            f"The damaged index at {db_path} is no longer present. "
            "Run `cc-search-chats index` to rebuild it."
        ) in message
        assert "was deleted" not in message
        assert not db_path.exists()
        conn.close.assert_called_once_with()

    def test_damage_close_failure_reports_both_causes_without_unlink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A failed close blocks cleanup and reports only its supported remedy."""
        db_path = tmp_path / "index.db"
        original = b"damaged index bytes"
        db_path.write_bytes(original)
        damage_error = _named_operational_error(
            "database disk image is malformed",
            "SQLITE_CORRUPT_INDEX",
        )
        close_error = _named_operational_error(
            "database connection is busy",
            "SQLITE_BUSY_TIMEOUT",
        )
        conn = MagicMock(spec=sqlite3.Connection)
        conn.close.side_effect = close_error
        attempted_paths: list[Path] = []

        def fail_integrity(_conn: sqlite3.Connection) -> str | None:
            raise damage_error

        def record_unlink(path: Path, missing_ok: bool = False) -> None:
            del missing_ok
            attempted_paths.append(path)

        monkeypatch.setattr(index_module.sqlite3, "connect", lambda _path: conn)
        monkeypatch.setattr(index_module, "_check_integrity", fail_integrity)
        monkeypatch.setattr(Path, "unlink", record_unlink)

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_CORRUPT_INDEX" in message
        assert "OperationalError: database disk image is malformed" in message
        assert "SQLITE_BUSY_TIMEOUT" in message
        assert "OperationalError: database connection is busy" in message
        assert "The damaged database file remains" in message
        assert str(db_path) in message
        assert "Wait for the concurrent indexing" in message
        assert "Grant read/write access" not in message
        assert "sandbox_workspace_write.writable_roots" not in message
        assert "The database file was deleted" not in message
        assert "`cc-search-chats index`" not in message
        assert attempted_paths == []
        assert db_path.read_bytes() == original

    def test_damage_and_corrupt_close_name_both_damage_causes_without_stale_policy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A corrupt close result stays in the sole damage-policy handler."""
        db_path = tmp_path / "index.db"
        original = b"damaged index bytes"
        db_path.write_bytes(original)
        operation_error = _named_operational_error(
            "query found malformed image",
            "SQLITE_CORRUPT_INDEX",
        )
        close_error = _named_operational_error(
            "close found malformed virtual table",
            "SQLITE_CORRUPT_VTAB",
        )
        conn = MagicMock(spec=sqlite3.Connection)
        conn.close.side_effect = close_error
        attempted_paths: list[Path] = []

        def fail_integrity(_conn: sqlite3.Connection) -> str | None:
            raise operation_error

        def record_unlink(path: Path, missing_ok: bool = False) -> None:
            del missing_ok
            attempted_paths.append(path)

        monkeypatch.setattr(index_module.sqlite3, "connect", lambda _path: conn)
        monkeypatch.setattr(index_module, "_check_integrity", fail_integrity)
        monkeypatch.setattr(Path, "unlink", record_unlink)

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_CORRUPT_INDEX" in message
        assert "OperationalError: query found malformed image" in message
        assert "SQLITE_CORRUPT_VTAB" in message
        assert "OperationalError: close found malformed virtual table" in message
        assert "The damaged database file remains" in message
        assert str(db_path) in message
        assert "not deleted" not in message
        assert "will not modify" not in message
        assert attempted_paths == []
        assert db_path.read_bytes() == original

    def test_unwritable_wal_reports_actionable_environment_error(
        self, tmp_path: Path
    ) -> None:
        """A sidecar-free WAL index under mode 500 is not called corrupt."""
        db_dir = tmp_path / "index-dir"
        db_dir.mkdir()
        db_path = db_dir / "index.db"
        conn = open_db(db_path)
        conn.execute(
            "INSERT INTO session "
            "(session_id, project_path, file_path, file_size, modified_at, "
            "indexed_at, summary) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                "s1",
                "/project",
                "/session.jsonl",
                1,
                "2026-07-31T01:00:00+00:00",
                "2026-07-31T02:00:00+00:00",
            ),
        )
        conn.commit()
        close_db(conn)
        original = db_path.read_bytes()

        db_dir.chmod(0o500)
        try:
            with pytest.raises(RuntimeError) as exc_info:
                open_db(db_path)
        finally:
            db_dir.chmod(0o700)

        message = str(exc_info.value)
        assert "SQLITE_READONLY_DIRECTORY" in message
        assert "attempt to write a readonly database" in message
        assert str(db_dir) in message
        assert "read/write access" in message
        assert "sandbox_workspace_write.writable_roots" in message
        assert "~/.codex/config.toml" in message
        assert "timestamp is unavailable" in message
        cause = (
            "SQLITE_READONLY_DIRECTORY "
            "(OperationalError: attempt to write a readonly database)"
        )
        assert message.count(cause) == 1
        assert (
            "Stored session-indexing timestamp is unavailable for the same reason."
            in message
        )
        assert "corrupt" not in message.lower()
        assert db_path.read_bytes() == original

    def test_connect_readonly_failure_is_actionable_not_corruption(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A connection failure is classified before any connection exists."""
        db_path = tmp_path / "index.db"
        error = _named_operational_error("connect denied", "SQLITE_READONLY_DIRECTORY")
        monkeypatch.setattr(
            index_module.sqlite3,
            "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
        )

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_READONLY_DIRECTORY" in message
        assert "OperationalError: connect denied" in message
        assert "timestamp is unavailable" in message
        assert "no safe SQLite connection was available" in message
        assert "corrupt" not in message.lower()

    def test_connect_corruption_deletes_without_connection(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Explicit connect-time damage can clean up without a connection."""
        db_path = tmp_path / "index.db"
        db_path.write_bytes(b"damaged index bytes")
        error = _named_operational_error(
            "file is not a database",
            "SQLITE_NOTADB",
        )
        monkeypatch.setattr(
            index_module.sqlite3,
            "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
        )

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_NOTADB" in message
        assert "OperationalError: file is not a database" in message
        assert (
            f"The damaged index at {db_path} is no longer present. "
            "Run `cc-search-chats index` to rebuild it."
        ) in message
        assert "was deleted" not in message
        assert not db_path.exists()

    def test_in_memory_damage_never_uses_filesystem_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The explicit in-memory identity is closed without Path operations."""
        error = _named_operational_error(
            "transient database disk image is malformed",
            "SQLITE_CORRUPT_INDEX",
        )

        def fail_pragmas(_conn: sqlite3.Connection) -> None:
            raise error

        filesystem_calls: list[tuple[str, Path]] = []
        original_mkdir = Path.mkdir
        original_exists = Path.exists
        original_stat = Path.stat
        original_unlink = Path.unlink

        def record_mkdir(
            path: Path,
            mode: int = 0o777,
            parents: bool = False,
            exist_ok: bool = False,
        ) -> None:
            filesystem_calls.append(("mkdir", path))
            original_mkdir(
                path,
                mode=mode,
                parents=parents,
                exist_ok=exist_ok,
            )

        def record_exists(path: Path) -> bool:
            filesystem_calls.append(("exists", path))
            return original_exists(path)

        def record_stat(path: Path, *, follow_symlinks: bool = True) -> object:
            filesystem_calls.append(("stat", path))
            return original_stat(path, follow_symlinks=follow_symlinks)

        def record_unlink(path: Path, missing_ok: bool = False) -> None:
            filesystem_calls.append(("unlink", path))
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(index_module, "_apply_pragmas", fail_pragmas)
        monkeypatch.setattr(Path, "mkdir", record_mkdir)
        monkeypatch.setattr(Path, "exists", record_exists)
        monkeypatch.setattr(Path, "stat", record_stat)
        monkeypatch.setattr(Path, "unlink", record_unlink)

        with pytest.raises(RuntimeError) as caught:
            open_db(":memory:")

        message = str(caught.value)
        assert "SQLITE_CORRUPT_INDEX" in message
        assert "OperationalError: transient database disk image is malformed" in message
        assert "in-memory" in message.lower()
        assert "The persistent index was not modified." in message
        assert "The database file was deleted" not in message
        assert "`cc-search-chats index`" not in message
        assert filesystem_calls == []

    def test_pragma_readonly_failure_is_actionable_not_corruption(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A write failure after quick_check is not relabelled as damage."""
        db_path = tmp_path / "index.db"
        error = _named_operational_error("pragma denied", "SQLITE_READONLY_DIRECTORY")

        def fail_pragmas(_conn: sqlite3.Connection) -> None:
            raise error

        monkeypatch.setattr(index_module, "_apply_pragmas", fail_pragmas)

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_READONLY_DIRECTORY" in message
        assert "OperationalError: pragma denied" in message
        assert "timestamp is unavailable" in message
        assert "SQLITE_ERROR (OperationalError: no such table: session)" in message
        assert "unavailable for the same reason" not in message
        assert "SQLite denied read access" not in message
        assert "corrupt" not in message.lower()

    def test_migration_readonly_failure_reports_latest_recorded_indexing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A migration write failure may report state read on the same connection."""
        db_path = tmp_path / "old.db"
        raw = sqlite3.connect(db_path)
        raw.execute(
            "CREATE TABLE session (session_id TEXT PRIMARY KEY, project_path TEXT, "
            "file_path TEXT, file_size INTEGER, modified_at TEXT, indexed_at TEXT, "
            "summary TEXT)"
        )
        raw.executemany(
            "INSERT INTO session VALUES (?, '/p', '/f', 1, ?, ?, NULL)",
            [
                ("older", "2026-07-30T00:00:00Z", "2026-07-30T01:00:00Z"),
                ("newer", "2026-07-31T00:00:00Z", "2026-07-31T02:00:00Z"),
            ],
        )
        raw.commit()
        raw.close()
        error = _named_operational_error("migration denied", "SQLITE_READONLY")

        def fail_migration(_conn: sqlite3.Connection) -> None:
            raise error

        monkeypatch.setattr(index_module, "_migrate_schema", fail_migration)

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        message = str(caught.value)
        assert "SQLITE_READONLY" in message
        assert "OperationalError: migration denied" in message
        assert "Most recent recorded session indexing: 2026-07-31T02:00:00Z." in message
        assert "valid as of" not in message.lower()
        assert "corrupt" not in message.lower()

    @pytest.mark.parametrize(
        ("quick_check_result", "expected_detail"),
        [
            ("page 2 is malformed", "PRAGMA quick_check returned: page 2 is malformed"),
            (None, "PRAGMA quick_check returned no result"),
        ],
    )
    def test_completed_non_ok_quick_check_deletes_damage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        quick_check_result: str | None,
        expected_detail: str,
    ) -> None:
        """Only a completed bad/missing quick_check result takes the damage path."""
        db_path = tmp_path / "index.db"
        db_path.write_bytes(b"pre-existing index bytes")
        monkeypatch.setattr(
            index_module,
            "_check_integrity",
            lambda _conn: quick_check_result,
        )

        with pytest.raises(RuntimeError) as caught:
            open_db(db_path)

        assert expected_detail in str(caught.value)
        assert "SQLite opened the database and completed PRAGMA quick_check" in str(
            caught.value
        )
        assert "cannot safely open" not in str(caught.value)
        assert (
            f"The damaged index at {db_path} is no longer present. "
            "Run `cc-search-chats index` to rebuild it."
        ) in str(caught.value)
        assert "was deleted" not in str(caught.value)
        assert "Rebuilding from chat history" not in str(caught.value)
        assert not db_path.exists()

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


class TestIntegrityClassification:
    """Only explicit SQLite damage results may be labelled corruption."""

    @pytest.mark.parametrize(
        ("error_name", "expected"),
        [
            ("SQLITE_CORRUPT", True),
            ("SQLITE_CORRUPT_INDEX", True),
            ("SQLITE_NOTADB", True),
            ("SQLITE_READONLY", False),
            ("SQLITE_READONLY_DIRECTORY", False),
            ("SQLITE_BUSY", False),
            ("SQLITE_BUSY_TIMEOUT", False),
            ("SQLITE_IOERR", False),
            ("SQLITE_FUTURE_ERROR", False),
            (None, False),
        ],
    )
    def test_only_explicit_damage_names_are_corruption(
        self, error_name: str | None, expected: bool
    ) -> None:
        classifier = getattr(index_module, "_is_corruption_error_name", None)
        assert classifier is not None
        assert classifier(error_name) is expected

    def test_environment_formatter_refuses_classified_damage(
        self, tmp_path: Path
    ) -> None:
        """Damage policy cannot fall back to the environmental formatter."""
        db_path = tmp_path / "index.db"
        error = _named_operational_error(
            "database disk image is malformed",
            "SQLITE_CORRUPT_INDEX",
        )

        with pytest.raises(AssertionError, match="discard_damaged_database"):
            format_index_error(db_path, error)

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (("ok",), "ok"),
            (("database disk image is malformed",), "database disk image is malformed"),
            (None, None),
        ],
    )
    def test_integrity_check_preserves_sqlite_result(
        self, result: tuple[str] | None, expected: str | None
    ) -> None:
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.return_value.fetchone.return_value = result

        assert index_module._check_integrity(conn) == expected


class TestDiagnosticTimestamp:
    """State wording is exact about what the stored timestamps establish."""

    def test_uses_maximum_recorded_session_indexing_time(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        conn.executemany(
            "INSERT INTO session "
            "(session_id, project_path, file_path, file_size, modified_at, "
            "indexed_at, summary) VALUES (?, '/p', '/f', 1, ?, ?, NULL)",
            [
                ("older", "2026-07-30T00:00:00Z", "2026-07-30T01:00:00Z"),
                ("newer", "2026-07-31T00:00:00Z", "2026-07-31T02:00:00Z"),
            ],
        )
        conn.commit()

        message = format_index_error(
            db_path,
            PermissionError("forced write denial"),
            conn,
        )
        close_db(conn)

        assert "Most recent recorded session indexing: 2026-07-31T02:00:00Z." in message
        assert "valid as of" not in message.lower()

    def test_empty_index_has_its_own_state_sentence(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)

        message = format_index_error(
            db_path,
            PermissionError("forced write denial"),
            conn,
        )
        close_db(conn)

        assert "The index contains no recorded session indexing time." in message
        assert "timestamp is unavailable" not in message

    def test_failed_safe_read_names_why_timestamp_is_unavailable(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "index.db"
        conn = MagicMock(spec=sqlite3.Connection)
        timestamp_error = _named_operational_error(
            "safe read denied", "SQLITE_READONLY_DIRECTORY"
        )
        conn.execute.side_effect = timestamp_error

        message = format_index_error(
            db_path,
            PermissionError("forced write denial"),
            conn,
        )

        assert "timestamp is unavailable" in message
        assert "SQLITE_READONLY_DIRECTORY" in message
        assert "OperationalError: safe read denied" in message
        assert "unavailable for the same reason" not in message
        assert "no recorded session indexing time" not in message


class TestDiagnosticRemedy:
    """A diagnostic recommends only remedies supported by its cause."""

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("/tmp/chats/vanished.jsonl"),
            _named_operational_error("database or disk is full", "SQLITE_FULL"),
        ],
    )
    def test_non_permission_failure_does_not_recommend_database_permissions(
        self,
        tmp_path: Path,
        exc: OSError | sqlite3.OperationalError,
    ) -> None:
        db_path = tmp_path / "index" / "index.db"
        conn = open_db(db_path)

        message = format_index_error(db_path, exc, conn)
        close_db(conn)

        assert f"{exc.__class__.__name__}: {exc}" in message
        assert "Grant read/write access" not in message
        assert "sandbox_workspace_write.writable_roots" not in message
        assert "~/.codex/config.toml" not in message

    def test_permission_error_recommends_actual_database_directory(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "index" / "index.db"
        conn = open_db(db_path)

        message = format_index_error(db_path, PermissionError("write denied"), conn)
        close_db(conn)

        assert (
            f"Grant read/write access to the index directory {db_path.parent}"
            in message
        )
        assert f'"{db_path.parent}"' in message
        assert "sandbox_workspace_write.writable_roots" in message


class TestCloseDb:
    """Tests for close_db()."""

    def test_closes_connection(self, tmp_path: Path) -> None:
        """Connection is closed after close_db()."""
        conn = open_db(tmp_path / "close.db")
        close_db(conn)
        # Attempting to use a closed connection should raise
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
