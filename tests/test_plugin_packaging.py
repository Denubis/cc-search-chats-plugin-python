"""Executable contracts for the shared Claude Code and Codex plugin."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
ANTIGRAVITY_MANIFEST = REPO_ROOT / "plugin.json"
CODEX_RULE = REPO_ROOT / "rules" / "cc-search-chats.rules"
CODEX_RULE_INSTALLER = REPO_ROOT / "scripts" / "install_codex_rule.py"
EXPECTED_CLI_VERSION = "2.0.5"
SEARCH_SKILL = REPO_ROOT / "skills" / "search-chat" / "SKILL.md"
SEARCH_COMMAND = REPO_ROOT / "commands" / "search-chat.md"
SEARCH_UAT = REPO_ROOT / "docs" / "uat" / "cross-vendor-search-wip.md"


def test_cli_release_version_is_synchronized() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lockfile = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    claude_plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    claude_marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    codex_plugin = json.loads(
        (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    locked_project = next(
        package
        for package in lockfile["package"]
        if package["name"] == "cc-search-chats"
    )

    versions = {
        "pyproject": project["project"]["version"],
        "uv lockfile": locked_project["version"],
        "Claude plugin": claude_plugin["version"],
        "Claude marketplace": claude_marketplace["plugins"][0]["version"],
        "Codex plugin": codex_plugin["version"],
    }
    assert versions == dict.fromkeys(versions, EXPECTED_CLI_VERSION)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"cc-search-chats {EXPECTED_CLI_VERSION}" in readme
    assert f"## cc-search-chats {EXPECTED_CLI_VERSION}" in changelog
    assert project["project"]["scripts"]["cc-search-chats"] == (
        "cc_search_chats.bootstrap:main"
    )


def test_codex_marketplace_resolves_the_shared_plugin_source() -> None:
    marketplace = json.loads(CODEX_MARKETPLACE.read_text(encoding="utf-8"))
    assert marketplace["name"] == "cc-search-chats-marketplace"
    assert len(marketplace["plugins"]) == 1

    entry = marketplace["plugins"][0]
    source = entry["source"]
    plugin_root = (REPO_ROOT / source["path"]).resolve()
    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert source["source"] == "local"
    assert entry["name"] == manifest["name"] == "cc-search-chats"
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert entry["category"] == "Productivity"


def test_antigravity_adapter_mirrors_the_canonical_plugin_name() -> None:
    claude_plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    antigravity_plugin = json.loads(ANTIGRAVITY_MANIFEST.read_text(encoding="utf-8"))

    assert antigravity_plugin == {"name": claude_plugin["name"]}


def test_search_skill_has_intentional_codex_discovery_metadata() -> None:
    metadata_path = REPO_ROOT / "skills" / "search-chat" / "agents" / "openai.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))

    assert metadata["interface"]["display_name"] == "Search Chats"
    assert 25 <= len(metadata["interface"]["short_description"]) <= 64
    assert "$search-chat" in metadata["interface"]["default_prompt"]
    assert metadata["policy"] == {"allow_implicit_invocation": True}


def test_search_guidance_describes_the_v3_coherent_corpus_contract() -> None:
    documents = {
        "README": (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        "CLAUDE": (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8"),
        "skill": SEARCH_SKILL.read_text(encoding="utf-8"),
        "command": SEARCH_COMMAND.read_text(encoding="utf-8"),
    }

    for name, document in documents.items():
        schema_markers = ("schema version 3", "schema v3", "`schema_version: 3`")
        assert any(marker in document.lower() for marker in schema_markers), name
        assert "PostgreSQL" in document, name
        assert "Ponytail" in document, name
        assert "--literal" in document, name
        assert "--tools" in document, name
        assert "--exhaustive" in document, name
        assert "corpus_generation" in document, name
        assert "semantic_build" in document, name
        assert "corpus_age_ms" in document, name

    assert (
        "A stale ranked search admits or joins one full background update before "
        "opening its result snapshot" in documents["command"]
    )
    assert (
        "A stale ranked search may wait within the command deadline for that "
        "update before opening its result snapshot" in documents["skill"]
    )
    assert "--everything` is retired" in documents["README"]


@pytest.mark.skipif(shutil.which("fish") is None, reason="fish is unavailable")
def test_cross_vendor_uat_fish_script_has_valid_syntax() -> None:
    document = SEARCH_UAT.read_text(encoding="utf-8")
    fence_start = document.index("```fish\n") + len("```fish\n")
    script_start = document.index("```fish\n", fence_start) + len("```fish\n")
    script_end = document.index("\n```", script_start)
    script = document[script_start:script_end]

    subprocess.run(
        ["fish", "--no-execute"],
        input=script,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is unavailable")
@pytest.mark.parametrize("subcommand", ["search", "extract", "list", "index"])
def test_codex_rule_routes_chat_commands_through_approval(subcommand: str) -> None:
    result = subprocess.run(
        [
            "codex",
            "execpolicy",
            "check",
            "--rules",
            str(CODEX_RULE),
            "cc-search-chats",
            subcommand,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["decision"] == "prompt"
    assert payload["matchedRules"][0]["prefixRuleMatch"]["matchedPrefix"] == [
        "cc-search-chats"
    ]


def test_codex_rule_installer_is_idempotent(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"

    first = subprocess.run(
        [sys.executable, str(CODEX_RULE_INSTALLER), "--codex-home", str(codex_home)],
        check=True,
        capture_output=True,
        text=True,
    )
    target = codex_home / "rules" / CODEX_RULE.name
    first_mtime = target.stat().st_mtime_ns

    second = subprocess.run(
        [sys.executable, str(CODEX_RULE_INSTALLER), "--codex-home", str(codex_home)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert target.read_bytes() == CODEX_RULE.read_bytes()
    assert target.stat().st_mtime_ns == first_mtime
    assert first.stdout.startswith("installed ")
    assert second.stdout.startswith("unchanged ")


def test_codex_rule_installer_refuses_a_symlink_target(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    rules_dir = codex_home / "rules"
    rules_dir.mkdir(parents=True)
    sentinel = tmp_path / "sentinel.rules"
    sentinel.write_text("preserve\n", encoding="utf-8")
    (rules_dir / CODEX_RULE.name).symlink_to(sentinel)

    result = subprocess.run(
        [sys.executable, str(CODEX_RULE_INSTALLER), "--codex-home", str(codex_home)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
