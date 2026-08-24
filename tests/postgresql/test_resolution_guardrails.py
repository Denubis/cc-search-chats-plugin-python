"""Resource and query-plan guardrails for exact PostgreSQL resolution."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest

from cc_search_chats.cli import _handle_postgres, _ProgressStream, build_parser
from cc_search_chats.storage.postgresql import migrate, resolve_message
from cc_search_chats.storage.postgresql.guardrails import queued_read

pytestmark = pytest.mark.postgresql
_READ_QUEUE_LOCK = "cc_search_chats.read_queue"
_INDEX_QUEUE_LOCK = "cc_search_chats.index_queue"


def test_documented_non_superuser_role_can_apply_read_guardrails(
    postgres_cluster,
) -> None:
    """The documented runtime role can apply and release the temp-file bound."""
    with psycopg.connect(
        postgres_cluster.runtime_dsn,
        autocommit=True,
    ) as connection:
        role, is_superuser = next(
            connection.execute(
                "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
            )
        )

        assert role == "cc_search_chats_test_owner"
        assert is_superuser is False
        with queued_read(connection):
            assert next(connection.execute("SHOW temp_file_limit"))[0] == "64MB"
        assert next(connection.execute("SHOW temp_file_limit"))[0] == "-1"


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


def _seed_representative_revision(connection: psycopg.Connection) -> tuple[str, str]:
    migrate(connection)
    revision_id = next(
        connection.execute(
            "INSERT INTO cc_search_chats.corpus_revision DEFAULT VALUES "
            "RETURNING revision_id"
        )
    )[0]
    connection.execute(
        """
        INSERT INTO cc_search_chats.message_current (
            provider, source_session_id, logical_message_id,
            canonical_locator, timestamp_text, role, session_kind,
            conversation_epoch, content_class, prose_content, submitted_by,
            embedding_input_digest
        )
        SELECT 'codex', 'plan-session', 'message-' || ordinal,
               'canonical-' || ordinal, '2026-08-19T00:00:00Z', 'assistant',
               'primary', 0, 'prose', repeat('representative text ', 8),
               'unknown', repeat('b', 64)
        FROM generate_series(1, 20000) AS ordinal
        """
    )
    connection.execute(
        """
        INSERT INTO cc_search_chats.physical_alias_current (
            provider, source_session_id, logical_message_id, content_class,
            source_root_id, locator, source_file_relative, record_ordinal,
            source_line, source_byte_offset, raw_byte_length, source_digest
        )
        SELECT 'codex', 'plan-session', 'message-' || ordinal, 'prose',
               'test-root', 'alias-' || ordinal, 'rollout.jsonl', ordinal,
               ordinal + 1, ordinal * 100, 100, repeat('a', 64)
        FROM generate_series(1, 20000) AS ordinal
        """
    )
    connection.execute(
        "UPDATE cc_search_chats.corpus_state SET current_revision_id = %s "
        "WHERE singleton",
        (revision_id,),
    )
    connection.execute("ANALYZE cc_search_chats.message_current")
    connection.execute("ANALYZE cc_search_chats.physical_alias_current")
    return "canonical-19999", "alias-19999"


@pytest.mark.parametrize(
    ("target_kind", "expected_index"),
    (
        ("canonical", "message_current_canonical_locator_idx"),
        ("alias", "physical_alias_current_locator_idx"),
    ),
)
def test_exact_resolution_uses_current_locator_indexes(
    postgres_connection: psycopg.Connection,
    target_kind: str,
    expected_index: str,
) -> None:
    """The executed lookup plan reaches each locator through its named index."""
    canonical, alias = _seed_representative_revision(postgres_connection)
    target = canonical if target_kind == "canonical" else alias
    notices: list[str] = []
    postgres_connection.add_notice_handler(
        lambda diagnostic: notices.append(diagnostic.message_primary)
    )
    postgres_connection.execute("LOAD 'auto_explain'")
    postgres_connection.execute("SET auto_explain.log_min_duration = 0")
    postgres_connection.execute("SET auto_explain.log_analyze = on")
    postgres_connection.execute("SET auto_explain.log_buffers = on")
    postgres_connection.execute("SET auto_explain.log_format = 'json'")
    postgres_connection.execute("SET client_min_messages = log")
    postgres_connection.execute("SET work_mem = '64kB'")

    messages = resolve_message(postgres_connection, target)

    assert len(messages) == 1
    assert notices
    plan_notice = next(notice for notice in notices if "physical_alias" in notice)
    explained = json.JSONDecoder().raw_decode(plan_notice[plan_notice.index("{") :])[0]
    plan = explained["Plan"]
    nodes = tuple(_plan_nodes(plan))
    summary = tuple(
        {
            key: node[key]
            for key in (
                "Node Type",
                "Relation Name",
                "Index Name",
                "Actual Rows",
                "Rows Removed by Filter",
                "Hash Batches",
                "Temp Read Blocks",
                "Temp Written Blocks",
            )
            if key in node
        }
        for node in nodes
    )
    assert any(node.get("Index Name") == expected_index for node in nodes), summary
    assert not any(
        node.get("Node Type") == "Seq Scan"
        and node.get("Relation Name") in {"message_current", "physical_alias_current"}
        for node in nodes
    )
    assert sum(node.get("Temp Written Blocks", 0) for node in nodes) == 0
    if os.environ.get("CC_SEARCH_EXPLAIN_REPORT"):
        print(
            json.dumps(
                {
                    "execution_time_ms": plan.get("Actual Total Time"),
                    "plan": summary,
                    "planning_time_ms": explained.get("Planning Time"),
                    "target": target_kind,
                },
                sort_keys=True,
            )
        )


def test_exact_resolution_waits_in_the_database_read_queue(
    postgres_cluster,
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct library caller cannot bypass the cross-process read queue."""
    monkeypatch.setattr(
        "cc_search_chats.storage.postgresql.guardrails._LOCK_TIMEOUT", "20ms"
    )
    canonical, _ = _seed_representative_revision(postgres_connection)
    with (
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="queue-blocker",
        ) as blocker,
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="queue-waiter",
        ) as waiter,
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        with blocker.transaction():
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (_READ_QUEUE_LOCK,),
            )
            granted = next(
                observer.execute(
                    """
                    SELECT count(*)
                    FROM pg_locks AS locks
                    JOIN pg_stat_activity AS activity USING (pid)
                    WHERE activity.application_name = 'queue-blocker'
                      AND locks.locktype = 'advisory' AND locks.granted
                    """
                )
            )[0]
            assert granted == 1

            pending = executor.submit(resolve_message, waiter, canonical)
            deadline = time.monotonic() + 1
            queued = False
            while time.monotonic() < deadline and not pending.done():
                queued = (
                    next(
                        observer.execute(
                            """
                            SELECT count(*)
                            FROM pg_locks AS locks
                            JOIN pg_stat_activity AS activity USING (pid)
                            WHERE activity.application_name = 'queue-waiter'
                              AND locks.locktype = 'advisory' AND NOT locks.granted
                            """
                        )
                    )[0]
                    == 1
                )
                if queued:
                    break
                time.sleep(0.01)
            assert queued
            time.sleep(0.05)
            assert not pending.done()

        assert pending.result(timeout=1)[0].canonical_locator == canonical


def test_duplicate_physical_aliases_resolve_one_logical_message(
    postgres_connection: psycopg.Connection,
) -> None:
    _, alias = _seed_representative_revision(postgres_connection)
    postgres_connection.execute(
        """
        INSERT INTO cc_search_chats.physical_alias_current (
            provider, source_session_id, logical_message_id, content_class,
            source_root_id, locator, source_file_relative, record_ordinal,
            source_line, source_byte_offset, raw_byte_length, source_digest
        )
        SELECT provider, source_session_id, logical_message_id, content_class,
               'duplicate-root', locator, 'duplicate-rollout.jsonl',
               record_ordinal, source_line, source_byte_offset,
               raw_byte_length, source_digest
        FROM cc_search_chats.physical_alias_current AS alias
        WHERE alias.locator = %s
        """,
        (alias,),
    )

    messages = resolve_message(postgres_connection, alias)

    assert len(messages) == 1
    assert messages[0].logical_message_id == "message-19999"


def test_composed_index_holds_the_database_queue_through_embedding(
    postgres_cluster,
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_representative_revision(postgres_connection)
    embedding_started = Event()
    finish_embedding = Event()

    def controlled_embedding(*args, **kwargs) -> int:
        embedding_started.set()
        assert finish_embedding.wait(timeout=1)
        return 20_000

    monkeypatch.setattr("cc_search_chats.cli.index_embeddings", controlled_embedding)
    args = build_parser().parse_args(["index", "--semantic-only"])
    with (
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        pending = executor.submit(
            _handle_postgres,
            args,
            postgres_cluster.dsn,
            _ProgressStream(args),
        )
        assert embedding_started.wait(timeout=1)
        acquired = next(
            observer.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (_INDEX_QUEUE_LOCK,),
            )
        )[0]
        if acquired:
            observer.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (_INDEX_QUEUE_LOCK,),
            )
        finish_embedding.set()

        assert acquired is False
        assert pending.result(timeout=1) == 0
