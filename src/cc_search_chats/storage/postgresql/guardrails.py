"""PostgreSQL-side bounds for synchronous read operations."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Event, Thread
from time import monotonic
from typing import Concatenate

import psycopg
from psycopg.pq import TransactionStatus

READ_QUEUE_LOCK = "cc_search_chats.read_queue"
INDEX_QUEUE_LOCK = "cc_search_chats.index_queue"
INDEX_NOTIFY_CHANNEL = "cc_search_chats_index_queue"
_LOCK_TIMEOUT = "30s"
_STATEMENT_TIMEOUT = "60s"
_TEMP_FILE_LIMIT = "64MB"
_READ_DEADLINE: ContextVar[float | None] = ContextVar(
    "cc_search_chats_read_deadline",
    default=None,
)


class ReadDeadlineExceeded(TimeoutError):
    """The caller's absolute guarded-read deadline has expired."""


@contextmanager
def read_deadline(timeout_ms: int) -> Iterator[None]:
    """Bound every guarded read in the current request to its remaining budget."""
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or timeout_ms <= 0
    ):
        raise ValueError("timeout_ms must be a positive integer")
    token = _READ_DEADLINE.set(monotonic() + timeout_ms / 1000)
    try:
        yield
    finally:
        _READ_DEADLINE.reset(token)


class DatabaseHeartbeat:
    """Periodically execute one bounded run-heartbeat update on its own connection."""

    def __init__(
        self,
        dsn: str,
        statement: str,
        params: tuple[object, ...],
        *,
        interval_seconds: float,
        label: str,
    ) -> None:
        self._dsn = dsn
        self._statement = statement.encode()
        self._params = params
        self._interval_seconds = interval_seconds
        self._label = label
        self._stop = Event()
        self._ready = Event()
        self._thread: Thread | None = None
        self._failure: OSError | psycopg.Error | None = None

    def start(self) -> None:
        self._thread = Thread(
            target=self._run,
            name=self._label,
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2):
            raise RuntimeError(f"{self._label} connection did not become ready")
        self.raise_if_failed()

    def _run(self) -> None:
        try:
            with psycopg.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=2,
            ) as heartbeat_connection:
                self._ready.set()
                while not self._stop.wait(self._interval_seconds):
                    updated = heartbeat_connection.execute(
                        self._statement,
                        self._params,
                    ).rowcount
                    if updated == 0:
                        break
        except (OSError, psycopg.Error) as error:
            self._failure = error
            self._ready.set()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError(
                f"{self._label} failed: {self._failure}"
            ) from self._failure

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)


@contextmanager
def queued_read(connection: psycopg.Connection) -> Iterator[None]:
    """Serialize one bounded read transaction and release it on every exit path."""
    starts_transaction = connection.info.transaction_status is TransactionStatus.IDLE
    deadline = _READ_DEADLINE.get()
    deadline_ms = None if deadline is None else int((deadline - monotonic()) * 1000)
    if deadline_ms is not None and deadline_ms <= 0:
        raise ReadDeadlineExceeded("PostgreSQL read deadline expired")
    lock_timeout = f"{deadline_ms}ms" if deadline_ms is not None else _LOCK_TIMEOUT
    statement_timeout = (
        f"{deadline_ms}ms" if deadline_ms is not None else _STATEMENT_TIMEOUT
    )
    with connection.transaction():
        if starts_transaction:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
        if deadline_ms is not None:
            connection.execute(
                """
                SELECT set_config('lock_timeout', %s, true),
                       set_config('statement_timeout', %s, true),
                       set_config('temp_file_limit', %s, true)
                """,
                (lock_timeout, statement_timeout, _TEMP_FILE_LIMIT),
            )
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_QUEUE_LOCK,),
        )
        if deadline_ms is None:
            connection.execute(
                """
                SELECT set_config('lock_timeout', %s, true),
                       set_config('statement_timeout', %s, true),
                       set_config('temp_file_limit', %s, true)
                """,
                (lock_timeout, statement_timeout, _TEMP_FILE_LIMIT),
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
