"""Durable automatic-refresh admission and launch state."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from cc_search_chats.storage.postgresql import migrate
from cc_search_chats.storage.postgresql.auto_refresh import (
    AutoRefreshRequest,
    admit_auto_refresh,
    claim_auto_refresh_launch,
    mark_auto_refresh_launch_failed,
)

pytestmark = pytest.mark.postgresql


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
        SET requested_at = requested_at - interval '5 minutes',
            state = 'complete', completed_at = now()
        WHERE singleton
        """
    )

    after_cooldown = admit_auto_refresh(postgres_connection)

    assert after_cooldown.admitted is True
    assert after_cooldown.request_id == 2
    assert after_cooldown.state == "pending"


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
