"""Minimal PostgreSQL revision storage and literal retrieval."""

from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files

import psycopg

from cc_search_chats.core.identity import NativeMessage, format_locator
from cc_search_chats.storage.postgresql.guardrails import queued_read_operation


@dataclass(frozen=True, slots=True)
class SearchHit:
    provider: str
    source_session_id: str
    logical_message_id: str
    canonical_locator: str
    timestamp: str
    role: str
    session_kind: str
    text: str
    repository: str | None
    cwd: str | None
    rank: float


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
    """Apply the idempotent PostgreSQL schema."""
    schema = (
        files("cc_search_chats.storage.postgresql")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )
    connection.execute(schema.encode())


def replace_messages(
    connection: psycopg.Connection, messages: Iterable[NativeMessage]
) -> int:
    """Atomically replace the selected corpus with one complete revision."""
    with connection.transaction():
        revision_id = next(
            connection.execute(
                "INSERT INTO cc_search_chats.corpus_revision DEFAULT VALUES "
                "RETURNING revision_id"
            )
        )[0]
        connection.execute(
            "CREATE TEMP TABLE staged_message "
            "(LIKE cc_search_chats.message EXCLUDING GENERATED) ON COMMIT DROP"
        )
        connection.execute(
            """ALTER TABLE staged_message ADD COLUMN alias_locator text,
               ADD COLUMN source_file_relative text,
               ADD COLUMN record_ordinal bigint,
               ADD COLUMN source_line bigint,
               ADD COLUMN source_byte_offset bigint,
               ADD COLUMN raw_byte_length bigint,
               ADD COLUMN source_digest text"""
        )
        message_columns = (
            "revision_id, provider, source_session_id, logical_message_id, "
            "canonical_locator, timestamp_text, role, session_kind, "
            "conversation_epoch, content_class, prose_content, repository, cwd, "
            "submitted_by"
        )
        alias_stage_columns = (
            "alias_locator, source_file_relative, record_ordinal, "
            "source_line, source_byte_offset, raw_byte_length, source_digest"
        )
        with connection.cursor().copy(
            f"COPY staged_message ({message_columns}, {alias_stage_columns}) FROM STDIN"
        ) as message_copy:
            for message in messages:
                identity = message.identity
                locator = identity.canonical_locator
                key = (
                    revision_id,
                    locator.provider.value,
                    locator.source_session_id,
                    identity.logical_message_id,
                    message.content_class.value,
                )
                message_row = (
                    *key[:4],
                    format_locator(locator),
                    message.timestamp,
                    message.role,
                    message.session_kind.value,
                    message.conversation_epoch,
                    message.content_class.value,
                    message.text,
                    message.repository,
                    message.cwd,
                    message.submitted_by.value,
                )
                for alias in identity.physical_aliases:
                    message_copy.write_row(
                        (
                            *message_row,
                            format_locator(alias.locator),
                            str(alias.source_file_relative),
                            alias.record_ordinal,
                            alias.source_line,
                            alias.source_byte_offset,
                            alias.raw_byte_length,
                            alias.source_digest,
                        )
                    )
        conflict = next(
            connection.execute(
                """
                SELECT canonical_locator
                FROM staged_message
                GROUP BY revision_id, provider, source_session_id,
                         logical_message_id, content_class, canonical_locator
                HAVING count(DISTINCT jsonb_build_array(
                    timestamp_text, role, session_kind, conversation_epoch,
                    prose_content, submitted_by
                )) > 1
                LIMIT 1
                """
            ),
            None,
        )
        if conflict is not None:
            raise ValueError(f"conflicting observations for {conflict[0]}")
        connection.execute(
            f"""
            INSERT INTO cc_search_chats.message ({message_columns})
            SELECT DISTINCT ON (
                revision_id, provider, source_session_id, logical_message_id,
                content_class
            ) {message_columns}
            FROM staged_message
            ORDER BY revision_id, provider, source_session_id, logical_message_id,
                     content_class, record_ordinal
            """
        )
        connection.execute(
            """
            INSERT INTO cc_search_chats.physical_alias (
                revision_id, provider, source_session_id, logical_message_id,
                content_class, locator, source_file_relative, record_ordinal,
                source_line, source_byte_offset, raw_byte_length, source_digest
            )
            SELECT DISTINCT revision_id, provider, source_session_id,
                   logical_message_id, content_class, alias_locator,
                   source_file_relative, record_ordinal, source_line,
                   source_byte_offset, raw_byte_length, source_digest
            FROM staged_message
            """
        )
        connection.execute(
            "UPDATE cc_search_chats.corpus_state SET current_revision_id = %s "
            "WHERE singleton",
            (revision_id,),
        )
    return revision_id


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
) -> tuple[SearchHit, ...]:
    """Search the current revision with PostgreSQL's plain-text query parser."""
    if not query.strip():
        return ()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    filters = ["m.search_vector @@ plainto_tsquery('simple', %s)"]
    params: list[object] = [query]
    for value, clause in (
        (provider, "m.provider = %s"),
        (role, "m.role = %s"),
        (project, "COALESCE(m.repository, m.cwd) = %s"),
        (since, "m.timestamp_text >= %s"),
        (epoch, "m.conversation_epoch = %s"),
    ):
        if value is not None:
            filters.append(clause)
            params.append(value)
    params.append(limit)
    sql = f"""
        SELECT m.provider, m.source_session_id, m.logical_message_id,
               m.canonical_locator, m.timestamp_text, m.role, m.session_kind,
               m.prose_content, m.repository, m.cwd,
               ts_rank_cd(m.search_vector, plainto_tsquery('simple', %s)) AS rank
        FROM cc_search_chats.message AS m
        JOIN cc_search_chats.corpus_state AS s
          ON s.current_revision_id = m.revision_id
        WHERE {" AND ".join(filters)}
        ORDER BY rank DESC, m.provider, m.source_session_id, m.logical_message_id
        LIMIT %s
        """
    rows = connection.execute(sql.encode(), (query, *params))
    return tuple(SearchHit(*row) for row in rows)


_MESSAGE_COLUMNS = """
    m.provider, m.source_session_id, m.logical_message_id,
    m.canonical_locator, m.timestamp_text, m.role, m.session_kind,
    m.conversation_epoch, m.content_class, m.prose_content
"""


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
        FROM cc_search_chats.message AS m
        JOIN cc_search_chats.corpus_state AS s
          ON s.current_revision_id = m.revision_id
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
        f"""
        SELECT {_MESSAGE_COLUMNS}
        FROM cc_search_chats.message AS m
        JOIN cc_search_chats.corpus_state AS s
          ON s.current_revision_id = m.revision_id
        WHERE m.source_session_id = %s
          AND (%s::text IS NULL OR m.provider = %s)
          AND (%s::integer IS NULL OR m.conversation_epoch = %s)
        ORDER BY m.timestamp_text, m.logical_message_id, m.content_class
        """.encode(),
        (source_session_id, provider, provider, epoch, epoch),
    )
    return tuple(StoredMessage(*row) for row in rows)


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
        f"""
        WITH requested(locator, input_order) AS (
            SELECT locator, input_order
            FROM unnest(%s::text[]) WITH ORDINALITY
              AS values(locator, input_order)
        ), matched_identity AS (
            SELECT r.input_order, r.locator, m.revision_id, m.provider,
                   m.source_session_id, m.logical_message_id, m.content_class
            FROM requested AS r
            JOIN cc_search_chats.corpus_state AS s ON s.singleton
            JOIN cc_search_chats.message AS m
              ON m.revision_id = s.current_revision_id
             AND m.canonical_locator = r.locator
            UNION
            SELECT r.input_order, r.locator, a.revision_id, a.provider,
                   a.source_session_id, a.logical_message_id, a.content_class
            FROM requested AS r
            JOIN cc_search_chats.corpus_state AS s ON s.singleton
            JOIN cc_search_chats.physical_alias AS a
              ON a.revision_id = s.current_revision_id
             AND a.locator = r.locator
        )
        SELECT r.input_order, r.locator, {_MESSAGE_COLUMNS}
        FROM requested AS r
        LEFT JOIN matched_identity AS matched
          ON (matched.input_order, matched.locator) =
             (r.input_order, r.locator)
        LEFT JOIN cc_search_chats.message AS m
          ON (m.revision_id, m.provider, m.source_session_id,
              m.logical_message_id, m.content_class) =
             (matched.revision_id, matched.provider, matched.source_session_id,
              matched.logical_message_id, matched.content_class)
        ORDER BY r.input_order, m.provider, m.source_session_id,
                 m.logical_message_id, m.content_class
        """.encode(),
        (list(requested),),
    )
    resolved: list[list[StoredMessage]] = [[] for _ in requested]
    for row in rows:
        input_order = row[0]
        if row[2] is not None:
            resolved[input_order - 1].append(StoredMessage(*row[2:]))
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
