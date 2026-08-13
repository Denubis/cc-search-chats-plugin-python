"""Fixture-root PostgreSQL refresh behavior."""

import shutil
from pathlib import Path

import psycopg
import pytest

from cc_search_chats.storage.postgresql import refresh_native_sources, search_messages

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


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
