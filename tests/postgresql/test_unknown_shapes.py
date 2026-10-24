"""Unknown record shapes through the PostgreSQL CLI: ignore mode plumbing."""

import json
import shutil
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import main
from cc_search_chats.semantic import SemanticChunk

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
):
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", *args])
    with pytest.raises(SystemExit) as stopped:
        main()
    output = capsys.readouterr()
    return stopped.value.code, output


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def _unknown_item(ordinal: int, kind: str = "future_item_kind") -> bytes:
    record = {
        "timestamp": "2026-08-11T03:00:02Z",
        "type": "response_item",
        "ordinal": ordinal,
        "payload": {"type": kind, "id": "fi_synthetic", "text": "cli-decoy-rue"},
    }
    return json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"


@pytest.fixture
def cli_corpus(postgres_cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the CLI at the disposable cluster and an empty-but-valid root set."""
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    connection = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection[key]))
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_day = codex_root / "2026" / "10" / "24"
    codex_day.mkdir(parents=True)
    antigravity_root = tmp_path / "brain"
    antigravity_root.mkdir()
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOTS", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOTS", str(codex_root))
    monkeypatch.setenv("CC_SEARCH_ANTIGRAVITY_ROOTS", str(antigravity_root))
    vector = [0.0] * 1024
    vector[0] = 1.0
    monkeypatch.setattr(
        "cc_search_chats.cli.embed_passages",
        lambda texts, **_kwargs: [vector for _ in texts],
    )
    monkeypatch.setattr("cc_search_chats.cli.chunk_passages", _single_chunks)
    return claude_root, codex_day, antigravity_root


def _codex_source_with_unknown_item(codex_day: Path, name: str) -> Path:
    fixture_records = (
        (FIXTURES / "codex_modern_primary_145.jsonl")
        .read_bytes()
        .splitlines(keepends=True)
    )
    source = codex_day / name
    source.write_bytes(
        fixture_records[0] + fixture_records[3] + _unknown_item(2) + fixture_records[5]
    )
    return source


@pytest.mark.parametrize("route", ["flag", "environment"])
def test_index_ignore_mode_reaches_the_refresh_through_flag_or_variable(
    cli_corpus,
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
) -> None:
    """AC13: either route enables ignore mode; the default blocks."""
    claude_root, codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    _codex_source_with_unknown_item(codex_day, "rollout-unknown.jsonl")
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0

    code, blocked = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (blocked.out, blocked.err)
    blocked_payload = json.loads(blocked.out)
    assert blocked_payload["coverage"]["blocked_files"] == 1
    assert blocked_payload["coverage"]["completeness"] == "partial"

    if route == "flag":
        arguments = ("index", "--ignore-unknown-records", "--json")
    else:
        monkeypatch.setenv("CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS", "1")
        arguments = ("index", "--json")
    code, ignored = _run(monkeypatch, capsys, *arguments)
    assert code == 0, (ignored.out, ignored.err)
    ignored_payload = json.loads(ignored.out)
    assert ignored_payload["coverage"]["blocked_files"] == 0
    assert ignored_payload["coverage"]["completeness"] == "complete"
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        assert next(
            connection.execute(
                "SELECT ignored_unknown_record_count FROM cc_search_chats.refresh_run "
                "WHERE run_id = %s",
                (ignored_payload["refresh"]["run_id"],),
            )
        ) == (1,)
        stored = [
            text
            for (text,) in connection.execute(
                "SELECT prose_content FROM cc_search_chats.message_current"
            )
        ]
    assert {"modern visible assistant", "modern visible user"} <= set(stored)
    assert not any("cli-decoy-rue" in text for text in stored)
