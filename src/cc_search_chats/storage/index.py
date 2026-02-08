"""Database connection, schema initialisation, and integrity checking.

Imperative Shell module — owns the SQLite connection and all SQL execution.
Pure query-building logic lives in core/search.py.
"""

import os
import sqlite3
import sys
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_db_path() -> Path:
    """Return the path to the index database.

    Uses CC_SEARCH_DB_PATH environment variable if set (for test isolation),
    otherwise defaults to ~/.cc-search-chats/index.db.

    Creates the parent directory if it does not exist.
    """
    env_path = os.environ.get("CC_SEARCH_DB_PATH")
    if env_path:
        path = Path(env_path)
    else:
        path = Path.home() / ".cc-search-chats" / "index.db"

    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply connection-time pragmas."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


def _has_schema(conn: sqlite3.Connection) -> bool:
    """Check if the database already has our tables."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='session'"
    ).fetchone()
    return row[0] > 0


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Read and execute schema.sql to create all tables and triggers."""
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)


def _check_integrity(conn: sqlite3.Connection) -> bool:
    """Run quick_check and return True if database is healthy."""
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()
        return result is not None and result[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def open_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the database, applying pragmas and checking integrity.

    If the database is new (no tables), the schema is applied automatically.
    If the database is corrupted, it is deleted and recreated from scratch
    with a warning to stderr.

    Args:
        db_path: Path to the database file. If None, uses get_db_path().

    Returns:
        A configured sqlite3.Connection with row_factory = sqlite3.Row.
    """
    if db_path is None:
        db_path = get_db_path()

    # Ensure parent directory exists (caller may pass an arbitrary path).
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Check integrity first — before pragmas, since a corrupt DB may fail on them.
    if db_path.exists() and db_path.stat().st_size > 0:
        if not _check_integrity(conn):
            print(
                "Index database corrupted \u2014 this can happen with network "
                "filesystems or sync tools (Syncthing, NFS). Rebuilding from "
                "chat history...",
                file=sys.stderr,
            )
            conn.close()
            db_path.unlink(missing_ok=True)
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

    _apply_pragmas(conn)

    if not _has_schema(conn):
        _apply_schema(conn)

    return conn


def close_db(conn: sqlite3.Connection) -> None:
    """Close the database connection."""
    conn.close()


def ensure_fts5() -> bool:
    """Runtime check that FTS5 is available in this SQLite build.

    Returns:
        True if FTS5 is available.

    Raises:
        RuntimeError: If FTS5 is not available.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_check USING fts5(c)")
        conn.execute("DROP TABLE _fts5_check")
        return True
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "SQLite FTS5 extension not available \u2014 required for search indexing"
        ) from exc
    finally:
        conn.close()
