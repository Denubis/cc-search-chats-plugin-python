"""One PostgreSQL CLI journey across every migrated command."""

import json
import shutil
import sys
from io import StringIO
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import main

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


def test_postgresql_cli_journey(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda args: None
    )
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_day = codex_root / "2026" / "08" / "11"
    codex_day.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root)
    shutil.copy(
        FIXTURES / "codex_modern_primary_145.jsonl",
        codex_day / "rollout-modern.jsonl",
    )
    connection = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection[key]))
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))

    code, indexed = _run(monkeypatch, capsys, "index", "--literal-only", "--json")
    assert code == 0
    assert json.loads(indexed.out)["status"] == "complete"

    code, searched = _run(
        monkeypatch,
        capsys,
        "search",
        "modern assistant",
        "--provider",
        "codex",
        "--limit",
        "1",
        "--literal",
        "--json",
    )
    result = json.loads(searched.out)["results"][0]
    assert code == 0
    assert result["provider"] == "codex"
    locator = result["locator"]

    missing_locator = f"{locator[:-1]}{'0' if locator[-1] != '0' else '1'}"
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(f"{locator}\n{missing_locator}\n{locator}\n"),
    )
    code, resolved_many = _run(monkeypatch, capsys, "resolve", "--stdin", "--json")
    assert code == 3
    resolutions = json.loads(resolved_many.out)["resolutions"]
    assert [value["locator"] for value in resolutions] == [
        locator,
        missing_locator,
        locator,
    ]
    assert [value["message_count"] for value in resolutions] == [1, 0, 1]

    code, searched = _run(
        monkeypatch,
        capsys,
        "search",
        "modern assistant",
        "--everything",
        "--project",
        "/synthetic/repository",
        "--epoch",
        "0",
        "--json",
    )
    assert code == 0
    assert json.loads(searched.out)["results"]

    for command in (
        ("list", "--provider", "codex", "--json"),
        ("extract", "codex-modern-primary", "--provider", "codex", "--json"),
        ("context", locator, "--depth", "1", "--json"),
        ("resolve", locator, "--json"),
    ):
        code, output = _run(monkeypatch, capsys, *command)
        assert code == 0
        assert json.loads(output.out)
