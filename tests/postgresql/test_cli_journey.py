"""One PostgreSQL CLI journey across every migrated command."""

import json
import shutil
import sys
from pathlib import Path

import pytest

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
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_day = codex_root / "2026" / "08" / "11"
    codex_day.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root)
    shutil.copy(
        FIXTURES / "codex_modern_primary_145.jsonl",
        codex_day / "rollout-modern.jsonl",
    )
    monkeypatch.setenv("CC_SEARCH_DATABASE_DSN", postgres_cluster.dsn)
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

    for command in (
        ("list", "--provider", "codex", "--json"),
        ("extract", "codex-modern-primary", "--provider", "codex", "--json"),
        ("context", locator, "--depth", "1", "--json"),
        ("resolve", locator, "--json"),
    ):
        code, output = _run(monkeypatch, capsys, *command)
        assert code == 0
        assert json.loads(output.out)
