"""Resource and behavior guardrails for PostgreSQL event export."""

from datetime import UTC, datetime

import psycopg
import pytest

from cc_search_chats.storage.postgresql import migrate
from cc_search_chats.storage.postgresql.events import export_human_message_events

pytestmark = pytest.mark.postgresql


def _seed_wide_event_population(
    connection: psycopg.Connection,
    *,
    logical_messages: int,
) -> int:
    migrate(connection)
    corpus_generation = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.corpus_generation (
                completed_at, status, message_count, alias_count
            ) VALUES (now(), 'complete', %s, %s)
            RETURNING corpus_generation
            """,
            (logical_messages * 4, logical_messages * 4),
        )
    )[0]
    semantic_build = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.semantic_build (
                corpus_generation, profile_id, completed_at, status,
                embedded_count
            ) VALUES (
                %s, 'nemotron-3-embed-8b-bf16:v1', now(), 'complete', 0
            )
            RETURNING semantic_build
            """,
            (corpus_generation,),
        )
    )[0]
    connection.execute(
        """
        UPDATE cc_search_chats.corpus_generation
        SET semantic_build = %s
        WHERE corpus_generation = %s
        """,
        (semantic_build, corpus_generation),
    )
    connection.execute(
        """
        INSERT INTO cc_search_chats.message_current (
            provider, source_session_id, logical_message_id,
            canonical_locator, timestamp_text, role, session_kind,
            conversation_epoch, content_class, prose_content, repository,
            cwd, submitted_by, embedding_input_digest
        )
        SELECT 'codex', 'wide-event-session',
               'message-' || lpad(ordinal::text, 6, '0'),
               repeat('canonical/', 32) || ordinal,
               '2026-08-11T03:00:02Z', 'user', 'primary', 0,
               content_class, 'positive event payload',
               repeat('/wide/repository', 16), repeat('/wide/cwd', 24),
               'human', repeat('b', 64)
        FROM generate_series(1, %s) AS ordinal
        CROSS JOIN (
            VALUES ('prose'), ('tool_input'), ('tool_name'), ('tool_output')
        ) AS classes(content_class)
        """,
        (logical_messages,),
    )
    connection.execute(
        """
        INSERT INTO cc_search_chats.physical_alias_current (
            provider, source_session_id, logical_message_id, content_class,
            source_root_id, locator, source_file_relative, record_ordinal,
            source_line, source_byte_offset, raw_byte_length, source_digest
        )
        SELECT 'codex', 'wide-event-session',
               'message-' || lpad(ordinal::text, 6, '0'), content_class,
               'test-root', 'alias-' || ordinal,
               repeat('rollout/', 32) || ordinal, ordinal, ordinal + 1,
               ordinal * 100, 100, repeat('a', 64)
        FROM generate_series(1, %s) AS ordinal
        CROSS JOIN (
            VALUES ('prose'), ('tool_input'), ('tool_name'), ('tool_output')
        ) AS classes(content_class)
        """,
        (logical_messages,),
    )
    connection.execute(
        """
        UPDATE cc_search_chats.corpus_state
        SET current_corpus_generation = %s
        WHERE singleton
        """,
        (corpus_generation,),
    )
    connection.execute("ANALYZE cc_search_chats.message_current")
    connection.execute("ANALYZE cc_search_chats.physical_alias_current")
    return int(corpus_generation)


def test_event_export_stays_within_the_temporary_file_guardrail(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wide event population does not require one corpus-sized SQL sort."""
    logical_messages = 4_000
    corpus_generation = _seed_wide_event_population(
        postgres_connection,
        logical_messages=logical_messages,
    )
    postgres_connection.execute("SET work_mem = '4MB'")
    monkeypatch.setattr(
        "cc_search_chats.storage.postgresql.guardrails._TEMP_FILE_LIMIT",
        "4MB",
    )

    exported = export_human_message_events(
        postgres_connection,
        from_utc=datetime(2026, 8, 11, tzinfo=UTC),
        until_utc=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert exported.source_corpus_generation == corpus_generation
    assert exported.population.scanned_content_rows == logical_messages * 4
    assert exported.population.scanned_logical_messages == logical_messages
    assert exported.population.retained == logical_messages
    assert len(exported.events) == logical_messages
    assert {event.physical_alias_count for event in exported.events} == {1}


def test_event_export_folds_page_boundaries_before_timestamp_ordering(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_wide_event_population(postgres_connection, logical_messages=2)
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.message_current
        SET timestamp_text = '2026-08-11T02:00:00Z'
        WHERE logical_message_id = 'message-000002'
        """
    )
    monkeypatch.setattr(
        "cc_search_chats.storage.postgresql.events._EVENT_PAGE_SIZE",
        3,
    )

    exported = export_human_message_events(
        postgres_connection,
        from_utc=datetime(2026, 8, 11, tzinfo=UTC),
        until_utc=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert exported.population.scanned_logical_messages == 2
    assert [event.canonical_locator[-1] for event in exported.events] == ["2", "1"]
    assert [event.physical_alias_count for event in exported.events] == [1, 1]


def test_event_export_rejects_conflicting_logical_message_metadata(
    postgres_connection: psycopg.Connection,
) -> None:
    _seed_wide_event_population(postgres_connection, logical_messages=1)
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.message_current
        SET cwd = '/conflicting/cwd'
        WHERE logical_message_id = 'message-000001'
          AND content_class = 'tool_input'
        """
    )

    with pytest.raises(
        ValueError,
        match="conflicting metadata for one logical message",
    ):
        export_human_message_events(
            postgres_connection,
            from_utc=datetime(2026, 8, 11, tzinfo=UTC),
            until_utc=datetime(2026, 8, 12, tzinfo=UTC),
        )
