"""Durable admission and launch state for search-triggered refresh."""

from dataclasses import dataclass

import psycopg

_AUTO_REFRESH_COOLDOWN_SECONDS = 300


@dataclass(frozen=True, slots=True)
class AutoRefreshRequest:
    """Current durable request state after an admission decision."""

    request_id: int
    state: str
    admitted: bool


@dataclass(frozen=True, slots=True)
class AutoRefreshStatus:
    """Durable background state exposed to search callers."""

    request_id: int
    state: str
    refresh_run_id: int | None
    last_error: str | None


def auto_refresh_status(
    connection: psycopg.Connection,
) -> AutoRefreshStatus:
    """Read the singleton automatic-refresh state."""
    row = next(
        connection.execute(
            """
            SELECT request_id, state, refresh_run_id, last_error
            FROM cc_search_chats.auto_refresh_state
            WHERE singleton
            """
        )
    )
    return AutoRefreshStatus(*row)


def admit_auto_refresh(
    connection: psycopg.Connection,
    *,
    cooldown_seconds: int = _AUTO_REFRESH_COOLDOWN_SECONDS,
) -> AutoRefreshRequest:
    """Admit at most one new request after the completed-request cooldown."""
    if (
        isinstance(cooldown_seconds, bool)
        or not isinstance(cooldown_seconds, int)
        or cooldown_seconds <= 0
    ):
        raise ValueError("cooldown_seconds must be a positive integer")
    with connection.transaction():
        admitted = next(
            connection.execute(
                """
                UPDATE cc_search_chats.auto_refresh_state
                SET request_id = request_id + 1,
                    requested_at = now(), state = 'pending',
                    launch_attempt_count = 0,
                    last_launch_attempt_at = NULL,
                    next_launch_retry_at = NULL,
                    launched_at = NULL, completed_at = NULL,
                    refresh_run_id = NULL, last_error = NULL
                WHERE singleton
                  AND (
                      state IN ('idle', 'complete')
                      OR (state = 'failed' AND next_launch_retry_at IS NULL)
                  )
                  AND (
                      requested_at IS NULL
                      OR requested_at <=
                         now() - make_interval(secs => %s)
                  )
                RETURNING request_id, state
                """,
                (cooldown_seconds,),
            ),
            None,
        )
        if admitted is not None:
            return AutoRefreshRequest(admitted[0], admitted[1], True)
        current = next(
            connection.execute(
                """
                SELECT request_id, state
                FROM cc_search_chats.auto_refresh_state
                WHERE singleton
                """
            )
        )
        return AutoRefreshRequest(current[0], current[1], False)


def claim_auto_refresh_launch(
    connection: psycopg.Connection,
    request_id: int,
) -> bool:
    """Atomically claim one pending or retry-eligible launch attempt."""
    claimed = connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET state = 'launching',
            launch_attempt_count = launch_attempt_count + 1,
            last_launch_attempt_at = now(), last_error = NULL
        WHERE singleton AND request_id = %s
          AND (
              state = 'pending'
              OR (
                  state = 'failed'
                  AND next_launch_retry_at IS NOT NULL
                  AND next_launch_retry_at <= now()
              )
          )
        """,
        (request_id,),
    )
    return claimed.rowcount == 1


def mark_auto_refresh_launched(
    connection: psycopg.Connection,
    request_id: int,
) -> None:
    """Record that systemd accepted the claimed request."""
    connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET state = 'launched', launched_at = now(),
            next_launch_retry_at = NULL, last_error = NULL
        WHERE singleton AND request_id = %s AND state = 'launching'
        """,
        (request_id,),
    )


def mark_auto_refresh_launch_failed(
    connection: psycopg.Connection,
    request_id: int,
    detail: str,
) -> None:
    """Retain the request and give its next launch attempt a bounded backoff."""
    connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET state = 'failed', last_error = %s,
            next_launch_retry_at = now() + make_interval(
                secs => LEAST(
                    300.0,
                    5.0 * power(
                        2.0,
                        GREATEST(launch_attempt_count - 1, 0)
                    )
                )
            )
        WHERE singleton AND request_id = %s AND state = 'launching'
        """,
        (detail, request_id),
    )


def mark_auto_refresh_running(
    connection: psycopg.Connection,
) -> int | None:
    """Claim the launched request, or report a duplicate service activation."""
    request = next(
        connection.execute(
            """
            UPDATE cc_search_chats.auto_refresh_state
            SET state = 'running', last_error = NULL
            WHERE singleton AND state IN ('launching', 'launched')
            RETURNING request_id
            """
        ),
        None,
    )
    return None if request is None else request[0]


def mark_auto_refresh_complete(
    connection: psycopg.Connection,
    request_id: int,
    *,
    refresh_run_id: int | None,
) -> None:
    """Finish the current service-owned request after literal maintenance."""
    connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET state = 'complete', completed_at = now(),
            refresh_run_id = %s, last_error = NULL
        WHERE singleton AND request_id = %s AND state = 'running'
        """,
        (refresh_run_id, request_id),
    )


def mark_auto_refresh_run_failed(
    connection: psycopg.Connection,
    request_id: int,
    detail: str,
) -> None:
    """Record a service-owned refresh failure without losing its request."""
    connection.execute(
        """
        UPDATE cc_search_chats.auto_refresh_state
        SET state = 'failed', completed_at = now(), last_error = %s,
            next_launch_retry_at = NULL
        WHERE singleton AND request_id = %s AND state = 'running'
        """,
        (detail, request_id),
    )
