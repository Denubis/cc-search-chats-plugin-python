"""PostgreSQL COPY and pgvector compatibility checks."""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.postgresql


def test_copy_round_trip(postgres_connection: psycopg.Connection) -> None:
    postgres_connection.execute("CREATE TEMP TABLE copy_sentinel (value text)")
    with postgres_connection.cursor().copy(
        "COPY copy_sentinel (value) FROM STDIN"
    ) as copy:
        copy.write_row(("punctuation: ' \" ; -- λ",))

    assert (
        next(postgres_connection.execute("SELECT value FROM copy_sentinel"))[0]
        == "punctuation: ' \" ; -- λ"
    )


def test_pgvector_1024_round_trip(vector_connection: psycopg.Connection) -> None:
    vector_connection.execute(
        "CREATE TEMP TABLE vector_sentinel (embedding vector(1024))"
    )
    value = "[" + ",".join("1" if index == 7 else "0" for index in range(1024)) + "]"
    vector_connection.execute("INSERT INTO vector_sentinel VALUES (%s)", (value,))

    dimensions, returned = next(
        vector_connection.execute(
            "SELECT vector_dims(embedding), embedding::text FROM vector_sentinel"
        )
    )
    assert dimensions == 1024
    assert returned == value
