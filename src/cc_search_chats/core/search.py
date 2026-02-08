"""Search query builder — Functional Core.

Pure functions that return (SQL, params) tuples. No database connections,
no I/O. The Imperative Shell (storage/index.py) executes these queries.
"""


def build_search_query(
    query: str,
    *,
    epoch: int | None = None,
    project: str | None = None,
    days: int | None = None,
) -> tuple[str, list[str | int]]:
    """Build an FTS5 search query with optional filters.

    Returns (sql, params) for execution against the index database.
    Results are ordered by BM25 rank (ascending — more negative = better).
    Includes snippet extraction for result previews.
    """
    sql_parts = [
        "SELECT m.uuid, m.session_id, m.epoch, m.timestamp, m.role,",
        "  snippet(message_fts, 0, '>>>', '<<<', '...', 20) AS snippet,",
        "  rank AS score",
        "FROM message_fts",
        "JOIN message m ON message_fts.rowid = m.rowid",
        "JOIN session s ON m.session_id = s.session_id",
        "WHERE message_fts MATCH ?",
    ]
    params: list[str | int] = [query]

    if epoch is not None:
        sql_parts.append("AND m.epoch = ?")
        params.append(epoch)

    if project is not None:
        sql_parts.append("AND s.project_path = ?")
        params.append(project)

    if days is not None:
        sql_parts.append("AND m.timestamp >= datetime('now', '-' || ? || ' days')")
        params.append(days)

    sql_parts.append("ORDER BY rank")

    return "\n".join(sql_parts), params


def build_extract_query(
    session_id: str,
    *,
    epoch: int | None = None,
) -> tuple[str, list[str | int]]:
    """Build a query to extract all messages from a session.

    Results are ordered by timestamp. Optional epoch filter.
    """
    sql_parts = [
        "SELECT m.uuid, m.session_id, m.epoch, m.timestamp, m.role,",
        "  m.text_content, m.parent_uuid, m.is_summary",
        "FROM message m",
        "WHERE m.session_id = ?",
    ]
    params: list[str | int] = [session_id]

    if epoch is not None:
        sql_parts.append("AND m.epoch = ?")
        params.append(epoch)

    sql_parts.append("ORDER BY m.timestamp")

    return "\n".join(sql_parts), params


def build_context_query(
    uuid: str,
    depth: int = 5,
) -> tuple[str, list[str | int]]:
    """Build a query to get N messages before and after a given message.

    Uses a subquery to find the target message's session_id and timestamp,
    then selects surrounding messages by timestamp ordering within the
    same session. Returns depth messages before + the target + depth after.
    """
    # The approach: find the target message's session and timestamp, then
    # use a window of messages ordered by timestamp to get surrounding context.
    sql = """
WITH target AS (
    SELECT session_id, timestamp, rowid AS target_rowid
    FROM message
    WHERE uuid = ?
),
ranked AS (
    SELECT m.uuid, m.session_id, m.epoch, m.timestamp, m.role,
        m.text_content, m.parent_uuid, m.is_summary,
        ROW_NUMBER() OVER (ORDER BY m.timestamp, m.rowid) AS rn
    FROM message m
    JOIN target t ON m.session_id = t.session_id
),
target_rn AS (
    SELECT rn FROM ranked WHERE uuid = ?
)
SELECT r.uuid, r.session_id, r.epoch, r.timestamp, r.role,
    r.text_content, r.parent_uuid, r.is_summary
FROM ranked r, target_rn t
WHERE r.rn BETWEEN t.rn - ? AND t.rn + ?
ORDER BY r.timestamp, r.uuid
""".strip()

    return sql, [uuid, uuid, depth, depth]


def build_list_query(
    *,
    project: str | None = None,
    days: int | None = None,
) -> tuple[str, list[str | int]]:
    """Build a query to list sessions with summary info.

    Joins with epoch_summary for epoch count and message counts.
    Ordered by session modified_at descending (newest first).
    """
    sql_parts = [
        "SELECT s.session_id, s.project_path, s.file_path, s.file_size,",
        "  s.modified_at, s.summary,",
        "  COUNT(DISTINCT es.epoch) AS epoch_count,",
        "  COALESCE(SUM(es.message_count), 0) AS total_messages",
        "FROM session s",
        "LEFT JOIN epoch_summary es ON s.session_id = es.session_id",
        "WHERE 1=1",
    ]
    params: list[str | int] = []

    if project is not None:
        sql_parts.append("AND s.project_path = ?")
        params.append(project)

    if days is not None:
        sql_parts.append("AND s.modified_at >= datetime('now', '-' || ? || ' days')")
        params.append(days)

    sql_parts.append("GROUP BY s.session_id")
    sql_parts.append("ORDER BY s.modified_at DESC")

    return "\n".join(sql_parts), params
