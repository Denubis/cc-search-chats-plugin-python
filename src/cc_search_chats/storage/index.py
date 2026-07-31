"""Database connection, schema initialisation, and integrity checking.

Imperative Shell module — owns the SQLite connection and all SQL execution.
Pure query-building logic lives in core/search.py.
"""

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cc_search_chats.core.models import CompactEvent, SessionMeta, SessionRecord
from cc_search_chats.core.parser import parse_session

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

IN_MEMORY_DATABASE: Literal[":memory:"] = ":memory:"
type DatabaseIdentity = Path | Literal[":memory:"]


class ProjectRebuildError(RuntimeError):
    """Report a rolled-back rebuild while retaining its structured cause."""

    def __init__(self, project_path: str, cause: Exception) -> None:
        self.project_path = project_path
        self.cause = cause
        super().__init__(
            f"Project index rebuild failed for {project_path}: "
            f"{cause.__class__.__name__}: {cause}. "
            "The transaction was rolled back; prior index contents remain intact."
        )


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

    path = path.expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(format_index_error(path, exc)) from exc
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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if ``table`` has ``column`` (via PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply idempotent, additive migrations to an existing database.

    New installs already have the current schema from schema.sql; databases
    created before a column existed get it added here. Additive only — never
    drops or reshapes existing data.
    """
    if not _column_exists(conn, "session", "real_project_path"):
        conn.execute("ALTER TABLE session ADD COLUMN real_project_path TEXT")
        conn.commit()


def _check_integrity(conn: sqlite3.Connection) -> str | None:
    """Run ``quick_check`` without hiding SQLite's result or exceptions."""
    result = conn.execute("PRAGMA quick_check").fetchone()
    return None if result is None else str(result[0])


def _is_corruption_error_name(error_name: str | None) -> bool:
    """Return whether SQLite explicitly classified an error as database damage."""
    return error_name == "SQLITE_NOTADB" or (
        error_name is not None
        and (error_name == "SQLITE_CORRUPT" or error_name.startswith("SQLITE_CORRUPT_"))
    )


def _permission_targets_index(exc: BaseException, db_path: Path) -> bool:
    """Return whether a filesystem denial identifies the index or its directory."""
    if not isinstance(exc, PermissionError):
        return False
    if exc.filename is None:
        return True
    denied_path = Path(exc.filename).expanduser().resolve()
    directory = db_path.parent
    return (
        denied_path == db_path
        or denied_path == directory
        or denied_path.is_relative_to(directory)
    )


def _latest_indexed_at(
    conn: sqlite3.Connection | None,
) -> tuple[str | None, sqlite3.DatabaseError | None]:
    """Read the newest recorded session indexing time on an existing connection."""
    if conn is None:
        return None, None
    try:
        row = conn.execute("SELECT MAX(indexed_at) FROM session").fetchone()
    except sqlite3.DatabaseError as exc:
        return None, exc
    if row is None or row[0] is None:
        return None, None
    return str(row[0]), None


def format_exception_detail(exc: BaseException) -> str:
    """Name an exception and preserve SQLite's result name when available."""
    error_name = getattr(exc, "sqlite_errorname", None)
    cause = f"{exc.__class__.__name__}: {exc}"
    return cause if error_name is None else f"{error_name} ({cause})"


def is_database_damage(exc: BaseException) -> bool:
    """Return whether SQLite explicitly classified an exception as damage."""
    return _is_corruption_error_name(getattr(exc, "sqlite_errorname", None))


def _index_directory_permission_remedy(db_path: Path) -> str:
    """Describe how to grant the directory access SQLite and unlink require."""
    directory = db_path.parent
    return (
        f"Grant read/write access to the index directory {directory}, then rerun "
        "the failed command. For Codex, add "
        f'"{directory}" to sandbox_workspace_write.writable_roots in '
        "~/.codex/config.toml, then restart the Codex pane."
    )


def discard_damaged_database(
    conn: sqlite3.Connection | None,
    database: DatabaseIdentity,
    detail: str,
    damage_evidence: str,
) -> str:
    """Close and remove damaged SQLite state, reporting only verified outcomes."""
    if conn is not None:
        try:
            conn.close()
        except Exception as close_error:
            if database == IN_MEMORY_DATABASE:
                return (
                    f"Transient in-memory index is damaged ({detail}). "
                    f"{damage_evidence} Could not close the SQLite connection "
                    f"({format_exception_detail(close_error)}). "
                    "The transient in-memory index was abandoned. "
                    "The persistent index was not modified."
                )
            assert isinstance(database, Path)
            close_diagnostic = (
                "SQLite also reported damage while closing "
                f"({format_exception_detail(close_error)})."
                if is_database_damage(close_error)
                else format_index_error(database, close_error)
            )
            return (
                f"Index database is damaged ({detail}). {damage_evidence} "
                "Could not close the SQLite connection. "
                f"{close_diagnostic} "
                f"The damaged database file remains: {database}. "
                "No index files were deleted."
            )

    if database == IN_MEMORY_DATABASE:
        outcome = (
            "The transient in-memory index was discarded."
            if conn is not None
            else "The transient in-memory index was abandoned."
        )
        return (
            f"Transient in-memory index is damaged ({detail}). "
            f"{damage_evidence} {outcome} "
            "The persistent index was not modified."
        )

    assert isinstance(database, Path)
    cleanup_paths = (
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
        database,
    )
    try:
        for cleanup_path in cleanup_paths:
            cleanup_path.unlink(missing_ok=True)
    except OSError as deletion_error:
        message = (
            f"Index database is damaged ({detail}). {damage_evidence} "
            "Could not delete the damaged index state "
            f"({format_exception_detail(deletion_error)}). "
            f"The damaged database file remains: {database}."
        )
        if _permission_targets_index(deletion_error, database):
            return f"{message} {_index_directory_permission_remedy(database)}"
        return message

    return (
        f"Index database is damaged ({detail}). {damage_evidence} "
        f"The damaged index at {database} is no longer present. "
        "Run `cc-search-chats index` to rebuild it."
    )


def format_index_error(
    db_path: Path,
    exc: BaseException,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Render an environmental failure, refusing damage-policy ownership."""
    error_name = getattr(exc, "sqlite_errorname", None)
    cause = format_exception_detail(exc)

    if is_database_damage(exc):
        raise AssertionError(
            "classified damage must be handled by discard_damaged_database"
        )

    if conn is None:
        timestamp_state = (
            "Stored session-indexing timestamp is unavailable because no safe "
            "SQLite connection was available."
        )
    else:
        latest, timestamp_error = _latest_indexed_at(conn)
        if timestamp_error is None:
            timestamp_state = (
                "The index contains no recorded session indexing time."
                if latest is None
                else f"Most recent recorded session indexing: {latest}."
            )
        else:
            timestamp_name = getattr(timestamp_error, "sqlite_errorname", None)
            timestamp_cause = f"{timestamp_error.__class__.__name__}: {timestamp_error}"
            if timestamp_name is not None:
                timestamp_cause = f"{timestamp_name} ({timestamp_cause})"
            if timestamp_cause == cause:
                timestamp_state = (
                    "Stored session-indexing timestamp is unavailable "
                    "for the same reason."
                )
            else:
                if timestamp_name is not None and timestamp_name.startswith(
                    "SQLITE_READONLY"
                ):
                    reason = f"SQLite denied read access ({timestamp_cause})"
                else:
                    reason = f"the safe SQLite read failed ({timestamp_cause})"
                timestamp_state = (
                    "Stored session-indexing timestamp is unavailable because "
                    f"{reason}."
                )

    if error_name is not None and error_name.startswith("SQLITE_BUSY"):
        remedy = (
            "Wait for the concurrent indexing or database operation to finish, "
            "then rerun the failed command."
        )
    elif _permission_targets_index(exc, db_path) or (
        error_name is not None and error_name.startswith("SQLITE_READONLY")
    ):
        remedy = _index_directory_permission_remedy(db_path)
    else:
        remedy = ""

    message = f"Index operation failed: {cause}. {timestamp_state}"
    return f"{message} {remedy}" if remedy else message


def open_db(db_path: DatabaseIdentity | None = None) -> sqlite3.Connection:
    """Open the database, applying pragmas and checking integrity.

    If the database is new (no tables), the schema is applied automatically.
    Proven damage is deleted and reported without rebuilding inline.
    Environmental failures are reported without deleting the file.

    Args:
        db_path: Path to the database file. If None, uses get_db_path().

    Returns:
        A configured sqlite3.Connection with row_factory = sqlite3.Row.
    """
    if db_path is None:
        db_path = get_db_path()
    filesystem_path = db_path if isinstance(db_path, Path) else None

    conn: sqlite3.Connection | None = None
    try:
        if filesystem_path is not None:
            # Ensure parent directory exists (caller may pass an arbitrary path).
            filesystem_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            IN_MEMORY_DATABASE
            if db_path == IN_MEMORY_DATABASE
            else str(filesystem_path)
        )
        conn.row_factory = sqlite3.Row

        # Check integrity first — damaged DBs may fail while applying pragmas.
        if (
            filesystem_path is not None
            and filesystem_path.exists()
            and filesystem_path.stat().st_size > 0
        ):
            integrity_result = _check_integrity(conn)
            if integrity_result != "ok":
                detail = (
                    "PRAGMA quick_check returned no result"
                    if integrity_result is None
                    else f"PRAGMA quick_check returned: {integrity_result}"
                )
                damaged_conn = conn
                conn = None
                raise RuntimeError(
                    discard_damaged_database(
                        damaged_conn,
                        filesystem_path,
                        detail,
                        "SQLite opened the database and completed PRAGMA quick_check, "
                        "but the check did not return ok.",
                    )
                )

        _apply_pragmas(conn)

        if not _has_schema(conn):
            _apply_schema(conn)
        else:
            _migrate_schema(conn)
    except RuntimeError:
        if conn is not None:
            conn.close()
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        if is_database_damage(exc):
            damaged_conn = conn
            conn = None
            raise RuntimeError(
                discard_damaged_database(
                    damaged_conn,
                    db_path,
                    format_exception_detail(exc),
                    "SQLite stopped the operation with an explicit damage result.",
                )
            ) from exc
        if filesystem_path is None:
            message = (
                f"Transient in-memory index operation failed: "
                f"{format_exception_detail(exc)}. "
                "The persistent index was not modified."
            )
        else:
            message = format_index_error(filesystem_path, exc, conn)
        if conn is not None:
            conn.close()
        raise RuntimeError(message) from exc

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


# ============================================================
# Indexing pipeline
# ============================================================


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _mtime_iso(mtime: float) -> str:
    """Convert a Unix mtime float to ISO 8601 string."""
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def _index_session_uncommitted(
    conn: sqlite3.Connection,
    session_meta: SessionMeta,
    *,
    full_content: bool = False,
) -> bool:
    """Replace one session inside the caller's transaction.

    Deletes any existing data for this session_id (CASCADE), then re-indexes
    from the JSONL file. Epoch assignment: epoch starts at 0 and increments
    at each CompactEvent. The user message immediately following a
    CompactEvent is treated as the compression summary.

    When ``full_content`` is True, each message's ``text_content`` carries the
    full searchable text (thinking + tool I/O). This is used only for the
    transient in-memory ``--everything`` index, never the persistent one.

    Returns False, after reporting the cause, when the source file cannot be
    opened. Errors after a successful open remain fatal to the transaction.
    """
    sid = session_meta.session_id
    real_project_path: str | None = None  # recovered from the first record's cwd
    file_path = Path(session_meta.file_path)
    try:
        fh = file_path.open(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(
            f"Warning: Could not open session file {file_path}: {exc}. Skipping.",
            file=sys.stderr,
        )
        return False

    try:
        # 1. Delete existing data (CASCADE removes messages and compact_events)
        conn.execute("DELETE FROM session WHERE session_id = ?", (sid,))

        # 2. Insert session row
        conn.execute(
            "INSERT INTO session (session_id, project_path, file_path, file_size, "
            "modified_at, indexed_at, summary) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                sid,
                session_meta.project_path,
                session_meta.file_path,
                session_meta.file_size,
                _mtime_iso(session_meta.modified_at),
                _now_iso(),
            ),
        )
    except Exception:
        fh.close()
        raise

    # 3. Stream through parse_session
    try:
        lines = fh

        epoch = 0
        awaiting_summary = False  # True after a CompactEvent, until next user message
        last_compact_uuid: str | None = None

        for record in parse_session(lines, sid, full_content=full_content):
            if isinstance(record, CompactEvent):
                epoch += 1
                # OR IGNORE: Claude Code can rewrite the same record into a
                # JSONL on resume/replay (uuid is the record identity); first
                # write wins. Removing this clause re-introduces an
                # IntegrityError on the uuid PK.
                conn.execute(
                    "INSERT OR IGNORE INTO compact_event "
                    "(uuid, session_id, epoch, timestamp, trigger, pre_tokens, summary_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        record.uuid,
                        sid,
                        epoch,
                        record.timestamp,
                        record.trigger,
                        record.pre_tokens,
                    ),
                )
                awaiting_summary = True
                last_compact_uuid = record.uuid

            elif isinstance(record, SessionRecord):
                if real_project_path is None and record.cwd:
                    real_project_path = record.cwd
                if record.record_type == "summary":
                    # Update session.summary with latest summary text
                    conn.execute(
                        "UPDATE session SET summary = ? WHERE session_id = ?",
                        (record.text_content, sid),
                    )
                elif record.role in ("user", "assistant"):
                    # Determine if this is a compression summary
                    is_summary = 0
                    if (
                        awaiting_summary
                        and record.role == "user"
                        and last_compact_uuid is not None
                    ):
                        # Heuristic: first user message after compact_boundary
                        # is the compression summary
                        is_summary = 1
                        # Store summary text on the compact_event
                        conn.execute(
                            "UPDATE compact_event SET summary_text = ? WHERE uuid = ?",
                            (record.text_content, last_compact_uuid),
                        )
                        awaiting_summary = False
                        last_compact_uuid = None

                    # OR IGNORE: see compact_event INSERT above. Same writer
                    # behaviour applies to user/assistant records.
                    conn.execute(
                        "INSERT OR IGNORE INTO message "
                        "(uuid, session_id, parent_uuid, epoch, timestamp, role, "
                        "text_content, is_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.uuid,
                            sid,
                            record.parent_uuid,
                            epoch,
                            record.timestamp,
                            record.role,
                            record.text_content,
                            is_summary,
                        ),
                    )
    finally:
        fh.close()

    # Persist the session's true filesystem path (from cwd) for display.
    if real_project_path is not None:
        conn.execute(
            "UPDATE session SET real_project_path = ? WHERE session_id = ?",
            (real_project_path, sid),
        )
    return True


def index_session(
    conn: sqlite3.Connection,
    session_meta: SessionMeta,
    *,
    full_content: bool = False,
) -> bool:
    """Atomically replace one session, or report and skip an unavailable source."""
    if conn.in_transaction:
        raise sqlite3.ProgrammingError(
            "index_session requires a connection with no active transaction"
        )
    try:
        conn.execute("BEGIN")
        indexed = _index_session_uncommitted(
            conn,
            session_meta,
            full_content=full_content,
        )
        conn.commit()
        return indexed
    except Exception:
        conn.rollback()
        raise


def needs_reindex(conn: sqlite3.Connection, session_meta: SessionMeta) -> bool:
    """Check whether a session file needs (re-)indexing.

    Returns True if the session is not in the DB or the file's mtime is
    newer than what was recorded at last index time.
    """
    row = conn.execute(
        "SELECT modified_at FROM session WHERE session_id = ?",
        (session_meta.session_id,),
    ).fetchone()

    if row is None:
        return True

    indexed_mtime = row["modified_at"]
    current_mtime = _mtime_iso(session_meta.modified_at)
    return current_mtime > indexed_mtime


def reindex_project(
    conn: sqlite3.Connection,
    project_path: str,
    include_subagents: bool = False,
) -> dict[str, int]:
    """Full reindex for a project. Indexes all session files.

    Returns counts of sessions indexed and skipped because their source could
    not be opened.
    """
    if conn.in_transaction:
        raise sqlite3.ProgrammingError(
            "reindex_project requires a connection with no active transaction"
        )

    from cc_search_chats.core.discovery import (
        decode_project_path,
        encode_project_path,
        get_claude_projects_dir,
        list_session_files,
    )

    projects_dir = get_claude_projects_dir()
    encoded = encode_project_path(project_path)
    stored_project_path = decode_project_path(encoded)
    sessions = list_session_files(projects_dir, encoded, include_subagents)

    # Sort by mtime descending (newest first)
    sessions.sort(key=lambda s: s.modified_at, reverse=True)
    counts = {"indexed": 0, "skipped": 0}

    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM session WHERE project_path = ?",
            (stored_project_path,),
        )
        for meta in sessions:
            if _index_session_uncommitted(conn, meta):
                counts["indexed"] += 1
            else:
                counts["skipped"] += 1
        conn.commit()
    except sqlite3.ProgrammingError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise ProjectRebuildError(project_path, exc) from exc

    return counts


def jit_reindex(
    conn: sqlite3.Connection,
    project_path: str,
    include_subagents: bool = False,
) -> dict[str, int]:
    """JIT (just-in-time) reindex: only re-indexes stale sessions.

    Called by search/list/extract before executing queries.
    Returns counts of sessions indexed and skipped because their source could
    not be opened.
    """
    from cc_search_chats.core.discovery import (
        encode_project_path,
        get_claude_projects_dir,
        list_session_files,
    )

    projects_dir = get_claude_projects_dir()
    encoded = encode_project_path(project_path)
    sessions = list_session_files(projects_dir, encoded, include_subagents)

    # Sort by mtime descending (newest first)
    sessions.sort(key=lambda s: s.modified_at, reverse=True)

    counts = {"indexed": 0, "skipped": 0}
    for meta in sessions:
        if needs_reindex(conn, meta):
            if index_session(conn, meta):
                counts["indexed"] += 1
            else:
                counts["skipped"] += 1

    return counts


def index_all_projects(
    conn: sqlite3.Connection,
    projects_dir: Path | None = None,
    include_subagents: bool = False,
) -> dict[str, int]:
    """Incrementally index every project under the Claude projects directory.

    Walks each immediate subdirectory of ``projects_dir`` (each encodes one
    project path), discovers its session files, and indexes only those whose
    file mtime is newer than what is stored (via :func:`needs_reindex`). The
    first run parses every session once; later runs skip unchanged files, so
    it is cheap to re-run from cron.

    Args:
        conn: Open index database connection.
        projects_dir: Claude projects directory. Defaults to
            :func:`get_claude_projects_dir` (injectable for tests).
        include_subagents: If True, also index nested ``subagents/`` files.

    Returns:
        Counts dict: ``projects`` scanned, sessions ``indexed`` (new/changed),
        and ``skipped`` (already current or source could not be opened).
    """
    from cc_search_chats.core.discovery import (
        get_claude_projects_dir,
        list_session_files,
    )

    if projects_dir is None:
        projects_dir = get_claude_projects_dir()

    counts = {"projects": 0, "indexed": 0, "skipped": 0}
    if not projects_dir.is_dir():
        return counts

    for child in sorted(projects_dir.iterdir()):
        if not child.is_dir():
            continue
        counts["projects"] += 1
        sessions = list_session_files(projects_dir, child.name, include_subagents)
        sessions.sort(key=lambda s: s.modified_at, reverse=True)
        for meta in sessions:
            if needs_reindex(conn, meta):
                if index_session(conn, meta):
                    counts["indexed"] += 1
                else:
                    counts["skipped"] += 1
            else:
                counts["skipped"] += 1

    return counts


# ============================================================
# Query execution (Imperative Shell wrappers)
# ============================================================


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    epoch: int | None = None,
    project: str | None = None,
    days: int | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Execute an FTS5 search query with optional filters.

    Returns matching rows. Empty list if no matches (never raises for
    empty results).
    """
    from cc_search_chats.core.search import build_search_query, sanitize_fts5_query

    # A query with no searchable terms sanitises to an empty MATCH expression,
    # which FTS5 rejects as a syntax error. Treat it as "no results".
    if not sanitize_fts5_query(query):
        return []

    sql, params = build_search_query(query, epoch=epoch, project=project, days=days)
    sql += f"\nLIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def search_full_content(
    sessions: list[SessionMeta],
    query: str,
    *,
    epoch: int | None = None,
    days: int | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Search the FULL content of given sessions (text + thinking + tool I/O).

    Builds a throwaway in-memory index from each session's full content, runs
    the standard :func:`search` against it, and discards it. Nothing is
    written to the persistent index. Reuses the query sanitiser, FTS5 ranking,
    and snippet machinery of the normal path.

    This is a live read of the raw JSONL files, so cost scales with the number
    of sessions in scope -- intended for the rare, opt-in ``--everything`` path.
    """
    if not sessions:
        return []

    mem: sqlite3.Connection | None = open_db(IN_MEMORY_DATABASE)
    try:
        for meta in sessions:
            index_session(mem, meta, full_content=True)
        return search(mem, query, epoch=epoch, project=None, days=days, limit=limit)
    except sqlite3.DatabaseError as exc:
        if is_database_damage(exc):
            damaged_mem = mem
            mem = None
            raise RuntimeError(
                discard_damaged_database(
                    damaged_mem,
                    IN_MEMORY_DATABASE,
                    format_exception_detail(exc),
                    "SQLite stopped the transient operation with an explicit "
                    "damage result.",
                )
            ) from exc
        raise
    finally:
        if mem is not None:
            try:
                close_db(mem)
            except sqlite3.DatabaseError as exc:
                if is_database_damage(exc):
                    raise RuntimeError(
                        discard_damaged_database(
                            None,
                            IN_MEMORY_DATABASE,
                            format_exception_detail(exc),
                            "SQLite stopped while closing the transient index with "
                            "an explicit damage result.",
                        )
                    ) from exc
                raise


def extract_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    epoch: int | None = None,
) -> list[sqlite3.Row]:
    """Extract all messages from a session.

    Raises ValueError if the session_id is not found in the database.
    """
    from cc_search_chats.core.search import build_extract_query

    # Verify session exists
    row = conn.execute(
        "SELECT session_id FROM session WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Session not found: {session_id}")

    sql, params = build_extract_query(session_id, epoch=epoch)
    return conn.execute(sql, params).fetchall()


def extract_context(
    conn: sqlite3.Connection,
    uuid: str,
    depth: int = 5,
) -> list[sqlite3.Row]:
    """Extract surrounding context for a message.

    Raises ValueError if the uuid is not found in the database.
    """
    from cc_search_chats.core.search import build_context_query

    # Verify message exists
    row = conn.execute("SELECT uuid FROM message WHERE uuid = ?", (uuid,)).fetchone()
    if row is None:
        raise ValueError(f"Message not found: {uuid}")

    sql, params = build_context_query(uuid, depth)
    return conn.execute(sql, params).fetchall()


def list_sessions(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    days: int | None = None,
) -> list[sqlite3.Row]:
    """List sessions with summary info."""
    from cc_search_chats.core.search import build_list_query

    sql, params = build_list_query(project=project, days=days)
    return conn.execute(sql, params).fetchall()
