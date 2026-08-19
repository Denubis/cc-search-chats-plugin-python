"""PostgreSQL-side bounds for synchronous read operations."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Concatenate

import psycopg
from psycopg.pq import TransactionStatus

READ_QUEUE_LOCK = "cc_search_chats.read_queue"
INDEX_QUEUE_LOCK = "cc_search_chats.index_queue"
_LOCK_TIMEOUT = "30s"
_STATEMENT_TIMEOUT = "60s"
_TEMP_FILE_LIMIT = "64MB"


@contextmanager
def queued_read(connection: psycopg.Connection) -> Iterator[None]:
    """Serialize one bounded read transaction and release it on every exit path."""
    starts_transaction = connection.info.transaction_status is TransactionStatus.IDLE
    with connection.transaction():
        if starts_transaction:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_QUEUE_LOCK,),
        )
        connection.execute(
            """
            SELECT set_config('lock_timeout', %s, true),
                   set_config('statement_timeout', %s, true),
                   set_config('temp_file_limit', %s, true)
            """,
            (_LOCK_TIMEOUT, _STATEMENT_TIMEOUT, _TEMP_FILE_LIMIT),
        )
        yield


def queued_read_operation[**P, R](
    operation: Callable[Concatenate[psycopg.Connection, P], R],
) -> Callable[Concatenate[psycopg.Connection, P], R]:
    """Apply the database read queue to one public storage operation."""

    @wraps(operation)
    def guarded(connection: psycopg.Connection, *args: P.args, **kwargs: P.kwargs) -> R:
        with queued_read(connection):
            return operation(connection, *args, **kwargs)

    return guarded


@contextmanager
def queued_index(connection: psycopg.Connection) -> Iterator[None]:
    """Serialize a refresh transaction across every client of the database."""
    with connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (INDEX_QUEUE_LOCK,),
        )
        connection.execute(
            "SELECT set_config('lock_timeout', %s, true)", (_LOCK_TIMEOUT,)
        )
        yield


def queued_index_operation[**P, R](
    operation: Callable[Concatenate[psycopg.Connection, P], R],
) -> Callable[Concatenate[psycopg.Connection, P], R]:
    """Apply the database index queue to one complete refresh operation."""

    @wraps(operation)
    def guarded(connection: psycopg.Connection, *args: P.args, **kwargs: P.kwargs) -> R:
        with queued_index(connection):
            return operation(connection, *args, **kwargs)

    return guarded


def acquire_index_session(connection: psycopg.Connection) -> None:
    """Hold the index queue until the caller-owned connection closes."""
    connection.execute(
        "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
        (INDEX_QUEUE_LOCK,),
    )
    connection.execute("SELECT set_config('lock_timeout', %s, false)", (_LOCK_TIMEOUT,))
