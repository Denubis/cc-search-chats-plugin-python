"""Bounded native-source staleness counts against PostgreSQL checkpoints."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

import pytest

from cc_search_chats.core.identity import Provider
from cc_search_chats.providers.source_discovery import (
    ConfiguredSourceRoot,
    source_root_id,
)
from cc_search_chats.storage.postgresql import migrate, unindexed_sources

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

pytestmark = pytest.mark.postgresql


@pytest.fixture(autouse=True)
def _current_schema(postgres_connection: psycopg.Connection) -> None:
    migrate(postgres_connection)


def _root(path: Path) -> ConfiguredSourceRoot:
    resolved = path.resolve()
    return ConfiguredSourceRoot(
        provider=Provider.CLAUDE,
        path=resolved,
        source_root_id=source_root_id(Provider.CLAUDE, resolved),
    )


def _checkpoint(
    connection: psycopg.Connection,
    root: ConfiguredSourceRoot,
    source: Path,
) -> None:
    stat = source.stat()
    generation = next(
        connection.execute(
            "INSERT INTO cc_search_chats.corpus_generation DEFAULT VALUES "
            "RETURNING corpus_generation"
        )
    )[0]
    connection.execute(
        """
        INSERT INTO cc_search_chats.source_root_current (
            source_root_id, provider, resolved_path, configured_order
        ) VALUES (%s, %s, %s, 0)
        """,
        (root.source_root_id, root.provider.value, str(root.path)),
    )
    connection.execute(
        """
        INSERT INTO cc_search_chats.source_file_current (
            source_root_id, source_file_relative, file_device, file_inode,
            observed_size, observed_mtime_ns, complete_byte_offset,
            next_record_ordinal, next_source_line, parser_state_version,
            parser_state, source_status, pending_bytes, skipped_record_count,
            updated_corpus_generation
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            1, 2, 1, '{}'::jsonb, 'indexed', 0, 0, %s
        )
        """,
        (
            root.source_root_id,
            source.relative_to(root.path).as_posix(),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_size,
            generation,
        ),
    )


def _scan(
    connection: psycopg.Connection,
    root: ConfiguredSourceRoot,
):
    return unindexed_sources(
        connection,
        (root,),
        deadline_monotonic=monotonic() + 1,
    )


def test_fresh_source_counts_the_whole_file(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "claude"
    source_parent = root_path / "project"
    source_parent.mkdir(parents=True)
    source = source_parent / "session.jsonl"
    source.write_bytes(b"fresh bytes\n")

    counts, reason = _scan(postgres_connection, _root(root_path))

    assert reason is None
    assert counts is not None
    assert (counts.files, counts.directories, counts.bytes) == (
        1,
        1,
        source.stat().st_size,
    )


def test_grown_source_counts_bytes_after_the_checkpoint(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "claude"
    root_path.mkdir()
    source = root_path / "session.jsonl"
    source.write_bytes(b"indexed\n")
    root = _root(root_path)
    _checkpoint(postgres_connection, root, source)
    indexed_size = source.stat().st_size
    with source.open("ab") as stream:
        stream.write(b"pending\n")

    counts, reason = _scan(postgres_connection, root)

    assert reason is None
    assert counts is not None
    assert (counts.files, counts.directories, counts.bytes) == (
        1,
        1,
        source.stat().st_size - indexed_size,
    )


def test_unchanged_source_is_not_unindexed(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "claude"
    root_path.mkdir()
    source = root_path / "session.jsonl"
    source.write_bytes(b"indexed\n")
    root = _root(root_path)
    _checkpoint(postgres_connection, root, source)

    counts, reason = _scan(postgres_connection, root)

    assert reason is None
    assert counts is not None
    assert (counts.files, counts.directories, counts.bytes) == (0, 0, 0)


def test_replaced_inode_counts_the_whole_file(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "claude"
    root_path.mkdir()
    source = root_path / "session.jsonl"
    source.write_bytes(b"indexed\n")
    root = _root(root_path)
    _checkpoint(postgres_connection, root, source)
    indexed_inode = source.stat().st_ino
    replacement = root_path / "replacement.jsonl"
    replacement.write_bytes(b"replacement bytes\n")
    replacement.replace(source)
    assert source.stat().st_ino != indexed_inode

    counts, reason = _scan(postgres_connection, root)

    assert reason is None
    assert counts is not None
    assert (counts.files, counts.directories, counts.bytes) == (
        1,
        1,
        source.stat().st_size,
    )


def test_zero_budget_returns_a_closed_unknown_reason(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "claude"
    root_path.mkdir()
    (root_path / "session.jsonl").write_bytes(b"unseen\n")

    counts, reason = unindexed_sources(
        postgres_connection,
        (_root(root_path),),
        deadline_monotonic=monotonic(),
    )

    assert counts is None
    assert reason == "scan_budget_exhausted"
