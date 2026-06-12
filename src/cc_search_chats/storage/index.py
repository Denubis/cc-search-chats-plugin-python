"""Database connection, schema initialisation, and integrity checking.

Imperative Shell module — owns the SQLite connection and all SQL execution.
Pure query-building logic lives in core/search.py.
"""

import math
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from cc_search_chats.core.models import CompactEvent, SessionMeta, SessionRecord
from cc_search_chats.core.parser import parse_session

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


# ============================================================
# Indexing pipeline
# ============================================================


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _mtime_iso(mtime: float) -> str:
    """Convert a Unix mtime float to ISO 8601 string."""
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def index_session(conn: sqlite3.Connection, session_meta: SessionMeta) -> None:
    """Index a single session file into the database.

    Deletes any existing data for this session_id (CASCADE), then re-indexes
    from the JSONL file. Epoch assignment: epoch starts at 0 and increments
    at each CompactEvent. The user message immediately following a
    CompactEvent is treated as the compression summary.

    Commits after the entire session is indexed.
    """
    sid = session_meta.session_id

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

    # 3. Stream through parse_session
    file_path = Path(session_meta.file_path)
    try:
        fh = file_path.open(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(
            f"Warning: Could not open session file {file_path}: {exc.__class__.__name__}: {exc}. Skipping.",
            file=sys.stderr,
        )
        return

    try:
        lines = fh

        epoch = 0
        awaiting_summary = False  # True after a CompactEvent, until next user message
        last_compact_uuid: str | None = None

        for record in parse_session(lines, sid):
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

    conn.commit()


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
) -> int:
    """Full reindex for a project. Indexes all session files.

    Returns count of sessions (re-)indexed.
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

    count = 0
    for meta in sessions:
        index_session(conn, meta)
        count += 1

    return count


def jit_reindex(
    conn: sqlite3.Connection,
    project_path: str,
    include_subagents: bool = False,
) -> int:
    """JIT (just-in-time) reindex: only re-indexes stale sessions.

    Called by search/list/extract before executing queries.
    Returns count of sessions reindexed.
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

    count = 0
    for meta in sessions:
        if needs_reindex(conn, meta):
            index_session(conn, meta)
            count += 1

    return count


# ============================================================
# Deferred epoch keyword extraction via fts5vocab
# ============================================================

# Minimum term length for keyword consideration — single characters
# and very short terms are almost never meaningful keywords.
_MIN_TERM_LENGTH = 2


def compute_epoch_keywords(
    conn: sqlite3.Connection,
    session_id: str,
    top_n: int = 10,
) -> None:
    """Compute TF-IDF keywords for each epoch in a session.

    Uses message_fts_vocab (fts5vocab in instance mode) to get term
    frequencies, computes TF-IDF scores per epoch, and updates
    epoch_summary.keywords with comma-separated top terms.

    Runs after index_session() — called separately, not inline.
    The caller decides when to trigger it.
    """
    # 1. Get total message count across the entire corpus (for IDF).
    total_messages = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]

    if total_messages == 0:
        return

    # 2. Get the epochs for this session.
    epoch_rows = conn.execute(
        "SELECT DISTINCT epoch FROM message WHERE session_id = ? ORDER BY epoch",
        (session_id,),
    ).fetchall()

    if not epoch_rows:
        return

    # 3. For each term appearing in this session's messages, compute:
    #    - per-epoch term frequency (count of occurrences in that epoch's messages)
    #    - document frequency (number of messages corpus-wide containing the term)
    #
    # The fts5vocab 'instance' mode gives (term, doc_rowid, col, offset).
    # We join with message to get epoch and filter by session.
    #
    # Step 3a: Get per-epoch term frequencies for this session.
    epoch_tf_rows = conn.execute(
        """
        SELECT v.term, m.epoch, COUNT(DISTINCT v.doc) AS tf
        FROM message_fts_vocab v
        JOIN message m ON m.rowid = v.doc
        WHERE m.session_id = ?
          AND LENGTH(v.term) >= ?
        GROUP BY v.term, m.epoch
        """,
        (session_id, _MIN_TERM_LENGTH),
    ).fetchall()

    if not epoch_tf_rows:
        return

    # Step 3b: Get corpus-wide document frequency for each term.
    # (number of distinct messages containing each term, across ALL sessions)
    terms = {row[0] for row in epoch_tf_rows}
    df_map: dict[str, int] = {}
    # Batch query — restrict to only the terms that appeared in this session
    # using a WHERE term IN clause.
    if terms:
        placeholders = ",".join("?" * len(terms))
        df_rows = conn.execute(
            f"""
            SELECT term, COUNT(DISTINCT doc) AS df
            FROM message_fts_vocab
            WHERE LENGTH(term) >= ? AND term IN ({placeholders})
            GROUP BY term
            """,
            [_MIN_TERM_LENGTH, *sorted(terms)],
        ).fetchall()
        for row in df_rows:
            df_map[row[0]] = row[1]

    # 4. Compute TF-IDF per (epoch, term) and select top N per epoch.
    #    TF-IDF = tf * log(total_messages / df)
    epoch_scores: dict[int, list[tuple[str, float]]] = {}
    for row in epoch_tf_rows:
        term, epoch, tf = row[0], row[1], row[2]
        df = df_map.get(term, 1)
        idf = math.log(total_messages / df) if df > 0 else 0.0
        tfidf = tf * idf

        if epoch not in epoch_scores:
            epoch_scores[epoch] = []
        epoch_scores[epoch].append((term, tfidf))

    # 5. Update epoch_summary.keywords for each epoch.
    for epoch, scores in epoch_scores.items():
        # Sort by TF-IDF descending, take top N.
        scores.sort(key=lambda x: x[1], reverse=True)
        top_terms = [term for term, _score in scores[:top_n]]
        keywords = ", ".join(top_terms)

        conn.execute(
            "UPDATE epoch_summary SET keywords = ? WHERE session_id = ? AND epoch = ?",
            (keywords, session_id, epoch),
        )

    conn.commit()


def update_all_keywords(
    conn: sqlite3.Connection,
    session_id: str | None = None,
) -> None:
    """Compute keywords for one session or all sessions.

    If session_id is provided, computes keywords for that session's epochs.
    If None, recomputes for all sessions. Intended for the ``index`` command
    (force rebuild).
    """
    if session_id is not None:
        compute_epoch_keywords(conn, session_id)
    else:
        rows = conn.execute("SELECT DISTINCT session_id FROM session").fetchall()
        for row in rows:
            compute_epoch_keywords(conn, row[0])


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
