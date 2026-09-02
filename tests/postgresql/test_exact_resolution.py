"""Exact native-source verification outcomes."""

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cc_search_chats.core.identity import Provider, ResolutionStatus
from cc_search_chats.providers.source_discovery import (
    ConfiguredSourceRoot,
    source_root_id,
)
from cc_search_chats.semantic import SemanticChunk
from cc_search_chats.storage.postgresql import (
    index_corpus,
    migrate,
    resolve_exact_messages,
    search_messages,
)

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def _passage_embeddings(texts):
    vector = [0.0] * 1024
    vector[0] = 1.0
    return [vector for _ in texts]


def _indexed_claude_source(
    connection: psycopg.Connection,
    tmp_path: Path,
) -> tuple[Path, ConfiguredSourceRoot, str]:
    root_path = tmp_path / "claude"
    root_path.mkdir()
    source = Path(
        shutil.copy(
            FIXTURES / "claude_primary.jsonl",
            root_path / "claude-session-primary.jsonl",
        )
    )
    root = ConfiguredSourceRoot(
        provider=Provider.CLAUDE,
        path=root_path.resolve(),
        source_root_id=source_root_id(Provider.CLAUDE, root_path.resolve()),
    )
    migrate(connection)
    index_corpus(
        connection,
        _passage_embeddings,
        chunker=_single_chunks,
        source_roots=(root,),
    )
    locator = search_messages(connection, "visible primary user")[0].canonical_locator
    return source, root, locator


def test_exact_resolution_verifies_native_bytes_and_groups_content_rows(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    _source, root, locator = _indexed_claude_source(postgres_connection, tmp_path)

    resolution = resolve_exact_messages(
        postgres_connection,
        (locator,),
        source_roots=(root,),
    )[0]

    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.locator == locator
    assert {message.logical_message_id for message in resolution.messages} == {
        "claude-user-1"
    }


def test_exact_resolution_names_malformed_and_missing_locators(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    _source, root, locator = _indexed_claude_source(postgres_connection, tmp_path)
    missing = locator.rsplit(":", 1)[0] + ":missing"

    malformed, no_match = resolve_exact_messages(
        postgres_connection,
        ("not-a-locator", missing),
        source_roots=(root,),
    )

    assert malformed.status is ResolutionStatus.MALFORMED_LOCATOR
    assert malformed.messages == ()
    assert no_match.status is ResolutionStatus.NO_MATCH
    assert no_match.messages == ()


def test_exact_resolution_distinguishes_unavailable_and_changed_sources(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    source, root, locator = _indexed_claude_source(postgres_connection, tmp_path)
    original = source.read_bytes()
    changed = original.replace(b"visible primary user", b"changed primary user", 1)
    assert changed != original
    assert len(changed) == len(original)
    source.write_bytes(changed)

    stale = resolve_exact_messages(
        postgres_connection,
        (locator,),
        source_roots=(root,),
    )[0]
    assert stale.status is ResolutionStatus.STALE_SOURCE

    source.unlink()
    unavailable = resolve_exact_messages(
        postgres_connection,
        (locator,),
        source_roots=(root,),
    )[0]
    assert unavailable.status is ResolutionStatus.SOURCE_UNAVAILABLE


def test_exact_resolution_distinguishes_stale_index_from_unsupported_schema(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    source, root, _locator = _indexed_claude_source(postgres_connection, tmp_path)
    valid = {
        "type": "assistant",
        "uuid": "unindexed-message",
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T06:00:00Z",
        "cwd": "/synthetic/repository",
        "isSidechain": False,
        "message": {"role": "assistant", "content": "not refreshed yet"},
    }
    unsupported = {
        "type": "assistant",
        "uuid": "unsupported-message",
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T06:01:00Z",
        "cwd": "/synthetic/repository",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": [{"type": "future_content_shape", "value": "unknown"}],
        },
    }
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(valid, separators=(",", ":")) + "\n")
        handle.write(json.dumps(unsupported, separators=(",", ":")) + "\n")

    stale_index, unsupported_schema = resolve_exact_messages(
        postgres_connection,
        (
            "ccchat:v1:claude:claude-session-primary:uuid:unindexed-message",
            "ccchat:v1:claude:claude-session-primary:uuid:unsupported-message",
        ),
        source_roots=(root,),
    )

    assert stale_index.status is ResolutionStatus.STALE_INDEX
    assert unsupported_schema.status is ResolutionStatus.UNSUPPORTED_PROVIDER_SCHEMA
