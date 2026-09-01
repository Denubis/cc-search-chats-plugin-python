"""Durable automatic-refresh admission and launch state."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import psycopg
import pytest

from cc_search_chats.cli import _request_auto_refresh
from cc_search_chats.storage.postgresql import migrate
from cc_search_chats.storage.postgresql.auto_refresh import (
    AutoRefreshRequest,
    admit_auto_refresh,
    claim_auto_refresh_launch,
    mark_auto_refresh_complete,
    mark_auto_refresh_launch_failed,
    mark_auto_refresh_launched,
    mark_auto_refresh_run_failed,
    mark_auto_refresh_running,
)

pytestmark = pytest.mark.postgresql


def _select_completed_corpus(connection: psycopg.Connection) -> int:
    corpus_generation = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.corpus_generation (
                completed_at, status
            ) VALUES (now(), 'complete')
            RETURNING corpus_generation
            """
        )
    )[0]
    semantic_build = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.semantic_build (
                corpus_generation, profile_id, completed_at, status
            ) VALUES (
                %s, 'nemotron-3-embed-8b-bf16:v1', now(), 'complete'
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
        UPDATE cc_search_chats.corpus_state
        SET current_corpus_generation = %s
        WHERE singleton
        """,
        (corpus_generation,),
    )
    return int(corpus_generation)


def test_concurrent_searches_admit_one_request(postgres_cluster) -> None:
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)
    barrier = Barrier(2)

    def admit() -> AutoRefreshRequest:
        with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
            barrier.wait()
            return admit_auto_refresh(connection)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: admit(), range(2)))

    assert sum(result.admitted for result in results) == 1
    assert {result.request_id for result in results} == {1}


def test_concurrent_searches_launch_one_service_request(
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)
    barrier = Barrier(2)
    launch_lock = Lock()
    launches: list[float] = []

    def start(timeout_seconds: float) -> None:
        with launch_lock:
            launches.append(timeout_seconds)

    monkeypatch.setattr("cc_search_chats.cli._start_systemd_refresh", start)

    def request():
        with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
            barrier.wait()
            return _request_auto_refresh(connection, timeout_seconds=0.25)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: request(), range(2)))

    assert len(launches) == 1
    assert {result.request_id for result in results} == {1}
    assert {result.state for result in results} <= {"launching", "launched"}


def test_fresh_corpus_does_not_admit_until_it_is_five_minutes_old(
    postgres_connection: psycopg.Connection,
) -> None:
    migrate(postgres_connection)
    corpus_generation = _select_completed_corpus(postgres_connection)

    fresh = admit_auto_refresh(postgres_connection)

    assert fresh == AutoRefreshRequest(request_id=0, state="idle", admitted=False)
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.corpus_generation
        SET completed_at = now() - interval '5 minutes'
        WHERE corpus_generation = %s
        """,
        (corpus_generation,),
    )

    stale = admit_auto_refresh(postgres_connection)

    assert stale == AutoRefreshRequest(request_id=1, state="pending", admitted=True)


def test_five_minute_cooldown_admits_one_durable_request(
    postgres_connection: psycopg.Connection,
) -> None:
    migrate(postgres_connection)

    first = admit_auto_refresh(postgres_connection)
    repeated = admit_auto_refresh(postgres_connection)

    assert first.admitted is True
    assert first.request_id == 1
    assert first.state == "pending"
    assert repeated.admitted is False
    assert repeated.request_id == 1
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET requested_at = requested_at - interval '10 minutes',
            state = 'complete', completed_at = now()
        WHERE singleton
        """
    )

    still_quiet = admit_auto_refresh(postgres_connection)

    assert still_quiet.admitted is False
    assert still_quiet.request_id == 1
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET completed_at = now() - interval '5 minutes'
        WHERE singleton
        """
    )

    after_cooldown = admit_auto_refresh(postgres_connection)

    assert after_cooldown.admitted is True
    assert after_cooldown.request_id == 2
    assert after_cooldown.state == "pending"


@pytest.mark.parametrize("active_state", ("pending", "running"))
def test_old_active_request_never_admits_a_duplicate(
    postgres_connection: psycopg.Connection,
    active_state: str,
) -> None:
    migrate(postgres_connection)
    request = admit_auto_refresh(postgres_connection)
    if active_state == "running":
        assert claim_auto_refresh_launch(postgres_connection, request.request_id)
        mark_auto_refresh_launched(postgres_connection, request.request_id)
        assert mark_auto_refresh_running(postgres_connection) == request.request_id
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET requested_at = now() - interval '1 day'
        WHERE singleton
        """
    )

    repeated = admit_auto_refresh(postgres_connection)

    assert repeated.admitted is False
    assert repeated.request_id == request.request_id
    assert repeated.state == active_state


def test_successful_noop_completion_starts_the_quiet_period(
    postgres_connection: psycopg.Connection,
) -> None:
    migrate(postgres_connection)
    request = admit_auto_refresh(postgres_connection)
    assert claim_auto_refresh_launch(postgres_connection, request.request_id)
    mark_auto_refresh_launched(postgres_connection, request.request_id)
    assert mark_auto_refresh_running(postgres_connection) == request.request_id
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET requested_at = now() - interval '1 day'
        WHERE singleton
        """
    )

    mark_auto_refresh_complete(
        postgres_connection,
        request.request_id,
        refresh_run_id=None,
    )

    repeated = admit_auto_refresh(postgres_connection)
    assert repeated.admitted is False
    assert repeated.request_id == request.request_id
    assert repeated.state == "complete"


def test_failed_launch_retries_same_request_only_after_backoff(
    postgres_connection: psycopg.Connection,
) -> None:
    migrate(postgres_connection)
    request = admit_auto_refresh(postgres_connection)

    assert claim_auto_refresh_launch(postgres_connection, request.request_id) is True
    mark_auto_refresh_launch_failed(
        postgres_connection,
        request.request_id,
        "fixture systemd failure",
    )

    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET requested_at = requested_at - interval '10 minutes'
        WHERE singleton
        """
    )
    retained = admit_auto_refresh(postgres_connection)

    assert retained.admitted is False
    assert retained.request_id == request.request_id

    assert claim_auto_refresh_launch(postgres_connection, request.request_id) is False
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET next_launch_retry_at = now() - interval '1 second'
        WHERE singleton
        """
    )
    assert claim_auto_refresh_launch(postgres_connection, request.request_id) is True
    assert next(
        postgres_connection.execute(
            """
            SELECT request_id, state, launch_attempt_count
            FROM cc_search_chats.auto_refresh_state
            WHERE singleton
            """
        )
    ) == (1, "launching", 2)


def test_failed_run_retries_the_same_request_only_after_backoff(
    postgres_connection: psycopg.Connection,
) -> None:
    migrate(postgres_connection)
    request = admit_auto_refresh(postgres_connection)
    assert claim_auto_refresh_launch(postgres_connection, request.request_id)
    mark_auto_refresh_launched(postgres_connection, request.request_id)
    assert mark_auto_refresh_running(postgres_connection) == request.request_id
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET requested_at = now() - interval '1 day'
        WHERE singleton
        """
    )

    mark_auto_refresh_run_failed(
        postgres_connection,
        request.request_id,
        "fixture full-index failure",
    )

    retained = admit_auto_refresh(postgres_connection)
    assert retained.admitted is False
    assert retained.request_id == request.request_id
    assert retained.state == "failed"
    assert claim_auto_refresh_launch(postgres_connection, request.request_id) is False
    postgres_connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET next_launch_retry_at = now() - interval '1 second'
        WHERE singleton
        """
    )
    assert claim_auto_refresh_launch(postgres_connection, request.request_id) is True
    assert next(
        postgres_connection.execute(
            """
            SELECT request_id, state, launch_attempt_count
            FROM cc_search_chats.auto_refresh_state
            WHERE singleton
            """
        )
    ) == (1, "launching", 2)
