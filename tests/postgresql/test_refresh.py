"""Fixture-root PostgreSQL refresh behavior."""

import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from cc_search_chats.storage.postgresql import (
    index_embeddings,
    migrate,
    refresh_native_sources,
    search_messages,
)

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
_INDEX_QUEUE_LOCK = "cc_search_chats.index_queue"


def test_refresh_streams_both_native_roots(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_day = codex_root / "2026" / "08" / "11"
    codex_day.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root)
    shutil.copy(
        FIXTURES / "codex_modern_primary_145.jsonl",
        codex_day / "rollout-modern.jsonl",
    )

    refresh_native_sources(
        postgres_connection, claude_root=claude_root, codex_root=codex_root
    )

    assert {
        hit.provider for hit in search_messages(postgres_connection, "visible")
    } == {
        "claude",
        "codex",
    }

    with pytest.raises(RuntimeError, match="roots are unavailable"):
        refresh_native_sources(
            postgres_connection,
            claude_root=tmp_path / "missing-claude",
            codex_root=codex_root,
        )
    assert search_messages(postgres_connection, "visible")


def test_refresh_waits_in_the_database_index_queue(
    postgres_cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.storage.postgresql.guardrails._LOCK_TIMEOUT", "20ms"
    )
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_root.mkdir()
    with (
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="index-blocker",
        ) as blocker,
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="index-waiter",
        ) as waiter,
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        blocker.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )
        pending = executor.submit(
            refresh_native_sources,
            waiter,
            claude_root=claude_root,
            codex_root=codex_root,
        )
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
                        WHERE activity.application_name = 'index-waiter'
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

        blocker.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )
        assert pending.result(timeout=1).message_count == 0


def test_direct_semantic_index_respects_the_shared_index_owner(
    postgres_cluster,
) -> None:
    with (
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as blocker,
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as waiter,
    ):
        migrate(waiter)
        with blocker.transaction():
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (_INDEX_QUEUE_LOCK,),
            )

            with pytest.raises(RuntimeError, match="indexing is already running"):
                index_embeddings(waiter, lambda texts: [])
