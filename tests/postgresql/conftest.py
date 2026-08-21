"""Disposable PostgreSQL 18 cluster fixtures with no live-service fallback."""

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

_BIN = Path("/usr/lib/postgresql/18/bin")
_VECTOR_CONTROL = Path("/usr/share/postgresql/18/extension/vector.control")
_PORT = 55432
_RUNTIME_ROLE = "cc_search_chats_test_owner"
_PG_ENV_KEYS = {
    "DATABASE_URL",
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSERVICE",
}


@dataclass(frozen=True, slots=True)
class PostgresCluster:
    """Connection facts for one fixture-owned server."""

    root: Path
    data: Path
    socket: Path
    dsn: str
    runtime_dsn: str


def _row(cursor: psycopg.Cursor[Any]) -> tuple[Any, ...]:
    row = cursor.fetchone()
    assert row is not None
    return row


def _run(command: list[str], *, env: dict[str, str]) -> None:
    subprocess.run(command, check=True, env=env, capture_output=True, text=True)


@pytest.fixture(scope="session")
def postgres_cluster(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PostgresCluster]:
    """Start the exact disposable server and prove its identity before teardown."""
    required = (
        *(_BIN / name for name in ("initdb", "pg_ctl", "pg_isready")),
        _VECTOR_CONTROL,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.fail(f"PostgreSQL 18 test prerequisites missing: {', '.join(missing)}")

    root = tmp_path_factory.mktemp("cc-search-postgresql-18")
    data, socket = root / "data", root / "socket"
    socket.mkdir()
    log = root / "postgres.log"
    env = {key: value for key, value in os.environ.items() if key not in _PG_ENV_KEYS}
    env["LC_ALL"] = "C.utf8"
    _run(
        [
            str(_BIN / "initdb"),
            "-D",
            str(data),
            "--encoding=UTF8",
            "--locale=C.utf8",
            "--auth=trust",
            "--no-sync",
        ],
        env=env,
    )
    options = f"-c listen_addresses='' -c unix_socket_directories={socket} -p {_PORT}"
    _run(
        [
            str(_BIN / "pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(log),
            "-o",
            options,
            "-w",
            "start",
        ],
        env=env,
    )
    dsn = f"host={socket} port={_PORT} dbname=postgres user={os.environ.get('USER', 'postgres')}"
    runtime_dsn = f"host={socket} port={_PORT} dbname=postgres user={_RUNTIME_ROLE}"
    cluster = PostgresCluster(
        root=root,
        data=data.resolve(),
        socket=socket,
        dsn=dsn,
        runtime_dsn=runtime_dsn,
    )
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            observed = Path(
                _row(connection.execute("SHOW data_directory"))[0]
            ).resolve()
            assert observed == cluster.data and observed.is_relative_to(root.resolve())
            connection.execute("CREATE EXTENSION vector")
            connection.execute("CREATE ROLE cc_search_chats_test_owner LOGIN")
            connection.execute(
                "GRANT SET ON PARAMETER temp_file_limit TO cc_search_chats_test_owner"
            )
        yield cluster
    finally:
        if data.exists():
            with psycopg.connect(dsn) as connection:
                observed = Path(
                    _row(connection.execute("SHOW data_directory"))[0]
                ).resolve()
                assert observed == cluster.data and observed.is_relative_to(
                    root.resolve()
                )
            _run(
                [str(_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                env=env,
            )


@pytest.fixture
def clean_postgres_schema(
    postgres_cluster: PostgresCluster,
) -> Iterator[None]:
    """Isolate each test inside the fixture-owned disposable database."""
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS cc_search_chats CASCADE")
    yield
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS cc_search_chats CASCADE")


@pytest.fixture(autouse=True)
def _isolate_postgresql_test(clean_postgres_schema: None) -> None:
    """Require the disposable schema boundary for every PostgreSQL test."""


@pytest.fixture
def postgres_connection(
    postgres_cluster: PostgresCluster,
    clean_postgres_schema: None,
) -> Iterator[psycopg.Connection]:
    """Return a clean autocommit connection to the disposable server."""
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def vector_connection(
    postgres_connection: psycopg.Connection,
) -> psycopg.Connection:
    """Return a disposable connection with pgvector ready."""
    postgres_connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return postgres_connection
