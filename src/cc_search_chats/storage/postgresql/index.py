"""Minimal PostgreSQL revision storage and literal retrieval."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterable

from cc_search_chats.storage.postgresql.guardrails import queued_read_operation
from cc_search_chats.storage.postgresql.migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class SearchHit:
    provider: str
    source_session_id: str
    logical_message_id: str
    canonical_locator: str
    timestamp: str
    role: str
    session_kind: str
    conversation_epoch: int
    content_class: str
    text: str
    repository: str | None
    cwd: str | None
    rank: float
    semantic_chunk_ordinal: int | None = None


@dataclass(frozen=True, slots=True)
class ExhaustiveCursor:
    """Stable position immediately after one exhaustive literal result."""

    canonical_locator: str
    content_order: int
    record_ordinal: int
    source_digest: str
    provider: str
    source_session_id: str
    logical_message_id: str
    content_class: str


@dataclass(frozen=True, slots=True)
class ExhaustivePage:
    """One bounded page of exhaustive literal results."""

    hits: tuple[SearchHit, ...]
    next_cursor: ExhaustiveCursor | None


@dataclass(frozen=True, slots=True)
class StoredAlias:
    """One root-independent physical source coordinate for public identity."""

    source_root_id: str
    locator: str
    source_file_relative: str
    record_ordinal: int
    source_line: int
    source_byte_offset: int
    raw_byte_length: int
    source_digest: str


@dataclass(frozen=True, slots=True)
class StoredMessage:
    provider: str
    source_session_id: str
    logical_message_id: str
    canonical_locator: str
    timestamp: str
    role: str
    session_kind: str
    conversation_epoch: int
    content_class: str
    text: str
    physical_aliases: tuple[StoredAlias, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredSession:
    provider: str
    source_session_id: str
    session_kind: str
    latest_timestamp: str
    message_count: int
    repository: str | None
    cwd: str | None


@dataclass(frozen=True, slots=True)
class MessageResolution:
    locator: str
    messages: tuple[StoredMessage, ...]


def migrate(connection: psycopg.Connection) -> None:
    """Apply ordered, checksummed PostgreSQL schema migrations."""
    apply_migrations(connection)


@queued_read_operation
def search_messages(
    connection: psycopg.Connection,
    query: str,
    *,
    limit: int = 20,
    provider: str | None = None,
    role: str | None = None,
    project: str | None = None,
    since: str | None = None,
    epoch: int | None = None,
    include_agents: bool = False,
    include_tools: bool = False,
) -> tuple[SearchHit, ...]:
    """Search the current revision with PostgreSQL's plain-text query parser."""
    if not query.strip():
        return ()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    filters = [sql.SQL("m.search_vector @@ websearch_to_tsquery('simple', %s)")]
    filters.append(
        sql.SQL("m.session_kind IN ('primary', 'agent', 'unknown')")
        if include_agents
        else sql.SQL("m.session_kind = 'primary'")
    )
    if not include_tools:
        filters.append(sql.SQL("m.content_class = 'prose'"))
    params: list[object] = [query]
    for value, clause in (
        (provider, sql.SQL("m.provider = %s")),
        (role, sql.SQL("m.role = %s")),
        (project, sql.SQL("COALESCE(m.repository, m.cwd) = %s")),
        (since, sql.SQL("m.timestamp_text >= %s")),
        (epoch, sql.SQL("m.conversation_epoch = %s")),
    ):
        if value is not None:
            filters.append(clause)
            params.append(value)
    params.append(limit)
    statement = sql.SQL(
        """
        SELECT m.provider, m.source_session_id, m.logical_message_id,
               m.canonical_locator, m.timestamp_text, m.role, m.session_kind,
               m.conversation_epoch, m.content_class, m.prose_content,
               m.repository, m.cwd,
               ts_rank_cd(
                   m.search_vector,
                   websearch_to_tsquery('simple', %s)
        ) AS rank
        FROM cc_search_chats.message_current AS m
        WHERE {where}
        ORDER BY rank DESC, m.provider, m.source_session_id, m.logical_message_id
        LIMIT %s
        """
    ).format(where=sql.SQL(" AND ").join(filters))
    rows = connection.execute(statement, (query, *params))
    return tuple(SearchHit(*row) for row in rows)


@queued_read_operation
def exhaustive_search_page(
    connection: psycopg.Connection,
    query: str,
    *,
    page_size: int = 500,
    after: ExhaustiveCursor | None = None,
    provider: str | None = None,
    role: str | None = None,
    project: str | None = None,
    since: str | None = None,
    epoch: int | None = None,
    include_agents: bool = False,
    include_tools: bool = False,
) -> ExhaustivePage:
    """Return one deterministic page containing each matching content row once."""
    if not query.strip():
        return ExhaustivePage((), None)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise ValueError("page_size must be a positive integer")
    filters = [sql.SQL("m.search_vector @@ websearch_to_tsquery('simple', %s)")]
    filters.append(
        sql.SQL("m.session_kind IN ('primary', 'agent', 'unknown')")
        if include_agents
        else sql.SQL("m.session_kind = 'primary'")
    )
    if not include_tools:
        filters.append(sql.SQL("m.content_class = 'prose'"))
    params: list[object] = [query]
    for value, clause in (
        (provider, sql.SQL("m.provider = %s")),
        (role, sql.SQL("m.role = %s")),
        (project, sql.SQL("COALESCE(m.repository, m.cwd) = %s")),
        (since, sql.SQL("m.timestamp_text >= %s")),
        (epoch, sql.SQL("m.conversation_epoch = %s")),
    ):
        if value is not None:
            filters.append(clause)
            params.append(value)
    if after is not None:
        filters.append(
            sql.SQL(
                """(m.canonical_locator, content_order.value,
                   source_alias.record_ordinal, source_alias.source_digest,
                   m.provider, m.source_session_id, m.logical_message_id,
                   m.content_class) > (%s, %s, %s, %s, %s, %s, %s, %s)"""
            )
        )
        params.extend(
            (
                after.canonical_locator,
                after.content_order,
                after.record_ordinal,
                after.source_digest,
                after.provider,
                after.source_session_id,
                after.logical_message_id,
                after.content_class,
            )
        )
    params.append(page_size + 1)
    rows = tuple(
        connection.execute(
            sql.SQL(
                """
            SELECT m.provider, m.source_session_id, m.logical_message_id,
                   m.canonical_locator, m.timestamp_text, m.role, m.session_kind,
                   m.conversation_epoch, m.content_class, m.prose_content,
                   m.repository, m.cwd,
                   ts_rank_cd(
                       m.search_vector,
                       websearch_to_tsquery('simple', %s)
                   ) AS rank,
                   content_order.value, source_alias.record_ordinal,
                   source_alias.source_digest
            FROM cc_search_chats.message_current AS m
            CROSS JOIN LATERAL (
                VALUES (CASE m.content_class
                    WHEN 'prose' THEN 0
                    WHEN 'tool_name' THEN 1
                    WHEN 'tool_input' THEN 2
                    WHEN 'tool_output' THEN 3
                END)
            ) AS content_order(value)
            CROSS JOIN LATERAL (
                SELECT alias.record_ordinal, alias.source_digest
                FROM cc_search_chats.physical_alias_current AS alias
                WHERE (alias.provider, alias.source_session_id,
                       alias.logical_message_id, alias.content_class) =
                      (m.provider, m.source_session_id,
                       m.logical_message_id, m.content_class)
                ORDER BY alias.record_ordinal, alias.source_digest,
                         alias.source_file_relative, alias.source_byte_offset
                LIMIT 1
            ) AS source_alias
            WHERE {where}
            ORDER BY m.canonical_locator, content_order.value,
                     source_alias.record_ordinal, source_alias.source_digest,
                     m.provider, m.source_session_id, m.logical_message_id,
                     m.content_class
            LIMIT %s
            """
            ).format(where=sql.SQL(" AND ").join(filters)),
            (query, *params),
        )
    )
    page_rows = rows[:page_size]
    hits = tuple(SearchHit(*row[:13]) for row in page_rows)
    if len(rows) <= page_size:
        return ExhaustivePage(hits, None)
    last = page_rows[-1]
    return ExhaustivePage(
        hits,
        ExhaustiveCursor(
            canonical_locator=last[3],
            content_order=last[13],
            record_ordinal=last[14],
            source_digest=last[15],
            provider=last[0],
            source_session_id=last[1],
            logical_message_id=last[2],
            content_class=last[8],
        ),
    )


_MESSAGE_COLUMNS = """
    m.provider, m.source_session_id, m.logical_message_id,
    m.canonical_locator, m.timestamp_text, m.role, m.session_kind,
    m.conversation_epoch, m.content_class, m.prose_content,
    COALESCE((
        SELECT jsonb_agg(
            jsonb_build_object(
                'locator', alias.locator,
                'source_root_id', alias.source_root_id,
                'source_file_relative', alias.source_file_relative,
                'record_ordinal', alias.record_ordinal,
                'source_line', alias.source_line,
                'source_byte_offset', alias.source_byte_offset,
                'raw_byte_length', alias.raw_byte_length,
                'source_digest', alias.source_digest
            )
            ORDER BY alias.source_file_relative, alias.record_ordinal,
                     alias.source_byte_offset, alias.locator
        )
        FROM cc_search_chats.physical_alias_current AS alias
        WHERE (alias.provider, alias.source_session_id,
               alias.logical_message_id, alias.content_class) =
              (m.provider, m.source_session_id,
               m.logical_message_id, m.content_class)
    ), '[]'::jsonb)
"""


def _stored_message(row: tuple) -> StoredMessage:
    aliases = tuple(
        StoredAlias(
            source_root_id=value["source_root_id"],
            locator=value["locator"],
            source_file_relative=value["source_file_relative"],
            record_ordinal=value["record_ordinal"],
            source_line=value["source_line"],
            source_byte_offset=value["source_byte_offset"],
            raw_byte_length=value["raw_byte_length"],
            source_digest=value["source_digest"],
        )
        for value in row[-1]
    )
    return StoredMessage(*row[:-1], physical_aliases=aliases)


@queued_read_operation
def list_sessions(
    connection: psycopg.Connection,
    *,
    provider: str | None = None,
    project: str | None = None,
    since: str | None = None,
) -> tuple[StoredSession, ...]:
    """List provider-qualified sessions in the selected revision."""
    rows = connection.execute(
        b"""
        SELECT m.provider, m.source_session_id, min(m.session_kind),
               max(m.timestamp_text),
               count(*) FILTER (WHERE m.content_class = 'prose'),
               min(m.repository), min(m.cwd)
        FROM cc_search_chats.message_current AS m
        WHERE (%s::text IS NULL OR m.provider = %s)
          AND (%s::text IS NULL OR COALESCE(m.repository, m.cwd) = %s)
          AND (%s::text IS NULL OR m.timestamp_text >= %s)
        GROUP BY m.provider, m.source_session_id
        ORDER BY max(m.timestamp_text) DESC, m.provider, m.source_session_id
        """,
        (provider, provider, project, project, since, since),
    )
    return tuple(StoredSession(*row) for row in rows)


@queued_read_operation
def extract_session(
    connection: psycopg.Connection,
    source_session_id: str,
    *,
    provider: str | None = None,
    epoch: int | None = None,
) -> tuple[StoredMessage, ...]:
    """Extract one provider-qualified session from the selected revision."""
    rows = connection.execute(
        sql.SQL(
            """
        SELECT {message_columns}
        FROM cc_search_chats.message_current AS m
        WHERE m.source_session_id = %s
          AND (%s::text IS NULL OR m.provider = %s)
          AND (%s::integer IS NULL OR m.conversation_epoch = %s)
        ORDER BY m.timestamp_text, m.logical_message_id, m.content_class
        """
        ).format(message_columns=sql.SQL(_MESSAGE_COLUMNS)),
        (source_session_id, provider, provider, epoch, epoch),
    )
    return tuple(_stored_message(row) for row in rows)


def resolve_message(
    connection: psycopg.Connection, locator: str
) -> tuple[StoredMessage, ...]:
    """Resolve an exact physical or canonical locator in the selected revision."""
    return resolve_messages(connection, (locator,))[0].messages


@queued_read_operation
def resolve_messages(
    connection: psycopg.Connection, locators: Iterable[str]
) -> tuple[MessageResolution, ...]:
    """Resolve ordered physical or canonical locators in one database operation."""
    requested = tuple(locators)
    if not requested:
        return ()
    rows = connection.execute(
        sql.SQL(
            """
        WITH requested(locator, input_order) AS (
            SELECT locator, input_order
            FROM unnest(%s::text[]) WITH ORDINALITY
              AS values(locator, input_order)
        ), matched_identity AS (
            SELECT r.input_order, r.locator, m.provider,
                   m.source_session_id, m.logical_message_id, m.content_class
            FROM requested AS r
            JOIN cc_search_chats.message_current AS m
              ON m.canonical_locator = r.locator
            UNION
            SELECT r.input_order, r.locator, a.provider,
                   a.source_session_id, a.logical_message_id, a.content_class
            FROM requested AS r
            JOIN cc_search_chats.physical_alias_current AS a
              ON a.locator = r.locator
        )
        SELECT r.input_order, r.locator, {message_columns}
        FROM requested AS r
        LEFT JOIN matched_identity AS matched
          ON (matched.input_order, matched.locator) =
             (r.input_order, r.locator)
        LEFT JOIN cc_search_chats.message_current AS m
          ON (m.provider, m.source_session_id,
              m.logical_message_id, m.content_class) =
             (matched.provider, matched.source_session_id,
              matched.logical_message_id, matched.content_class)
        ORDER BY r.input_order, m.provider, m.source_session_id,
                 m.logical_message_id, m.content_class
        """
        ).format(message_columns=sql.SQL(_MESSAGE_COLUMNS)),
        (list(requested),),
    )
    resolved: list[list[StoredMessage]] = [[] for _ in requested]
    for row in rows:
        input_order = row[0]
        if row[2] is not None:
            resolved[input_order - 1].append(_stored_message(row[2:]))
    return tuple(
        MessageResolution(locator, tuple(messages))
        for locator, messages in zip(requested, resolved, strict=True)
    )


@queued_read_operation
def context_messages(
    connection: psycopg.Connection, locator: str, *, depth: int = 5
) -> tuple[StoredMessage, ...]:
    """Return prose surrounding one exact locator."""
    if depth < 0:
        raise ValueError("depth must be nonnegative")
    targets = resolve_message(connection, locator)
    identities = {
        (value.provider, value.source_session_id, value.logical_message_id)
        for value in targets
    }
    if len(identities) != 1:
        return ()
    provider, session_id, logical_id = identities.pop()
    session = tuple(
        value
        for value in extract_session(connection, session_id, provider=provider)
        if value.content_class == "prose"
    )
    target_index = next(
        (
            index
            for index, value in enumerate(session)
            if value.logical_message_id == logical_id
        ),
        None,
    )
    if target_index is None:
        return ()
    return session[max(0, target_index - depth) : target_index + depth + 1]
