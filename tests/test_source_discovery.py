"""Bounded native-source reading and provider-root discovery tests."""

import hashlib
import os
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from cc_search_chats.core.identity import Provider
from cc_search_chats.providers.source_discovery import (
    BoundedReadResult,
    DiscoveryResult,
    SourceDiagnosticCode,
    discover_claude_sources,
    discover_codex_sources,
    probe_git_repository,
    read_bounded_jsonl,
)


def diagnostic_codes(
    result: BoundedReadResult | DiscoveryResult,
) -> set[SourceDiagnosticCode]:
    """Return diagnostic codes from a discovery or bounded-read result."""
    return {diagnostic.code for diagnostic in result.diagnostics}


class TestBoundedJsonlRead:
    @pytest.mark.parametrize(
        "source_file_relative",
        [Path("../outside.jsonl"), Path("project/../../outside.jsonl")],
    )
    def test_rejects_parent_traversal_source_paths(
        self, tmp_path: Path, source_file_relative: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        source.write_text("{}\n")

        with pytest.raises(ValueError, match="source_file_relative"):
            read_bounded_jsonl(
                source,
                source_file_relative=source_file_relative,
                target_size=source.stat().st_size,
            )

    def test_accepts_lexically_normalized_dot_source_path(self, tmp_path: Path) -> None:
        source = tmp_path / "session.jsonl"
        source.write_text("{}\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("project/./session.jsonl"),
            target_size=source.stat().st_size,
        )

        assert result.envelopes[0].source_file_relative == Path("project/session.jsonl")

    def test_stops_at_captured_target_before_later_append(self, tmp_path: Path) -> None:
        source = tmp_path / "session.jsonl"
        first = b'{"type":"user","text":"first"}'
        second = b'{"type":"assistant","text":"second"}'
        source.write_bytes(first + b"\n")
        target_size = source.stat().st_size
        with source.open("ab") as handle:
            handle.write(second + b"\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=target_size,
        )

        assert [envelope.raw_bytes for envelope in result.envelopes] == [first]
        assert result.target_size == target_size
        assert result.final_size == len(first) + len(second) + 2
        assert result.pending_bytes == len(second) + 1
        assert result.diagnostics == ()

    def test_envelope_coordinates_and_digest_use_exact_record_bytes(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        first = b'{"text":"alpha"}'
        second = b'{"text":"beta"}'
        source.write_bytes(first + b"\n" + second + b"\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("project/session.jsonl"),
            target_size=source.stat().st_size,
        )

        assert len(result.envelopes) == 2
        assert result.envelopes[0].record_ordinal == 0
        assert result.envelopes[0].source_line == 1
        assert result.envelopes[0].source_byte_offset == 0
        assert result.envelopes[0].raw_byte_length == len(first)
        assert result.envelopes[0].source_digest == hashlib.sha256(first).hexdigest()
        assert result.envelopes[1].record_ordinal == 1
        assert result.envelopes[1].source_line == 2
        assert result.envelopes[1].source_byte_offset == len(first) + 1
        assert result.envelopes[1].raw_byte_length == len(second)
        assert result.envelopes[1].source_digest == hashlib.sha256(second).hexdigest()

    def test_partial_tail_is_diagnostic_and_consumes_no_ordinal(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "partial.jsonl"
        complete = b'{"complete":true}'
        partial = b'{"partial":'
        source.write_bytes(complete + b"\n" + partial)

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("partial.jsonl"),
            target_size=source.stat().st_size,
        )

        assert [envelope.record_ordinal for envelope in result.envelopes] == [0]
        assert diagnostic_codes(result) == {SourceDiagnosticCode.PARTIAL_TAIL}
        diagnostic = result.diagnostics[0]
        assert diagnostic.record_ordinal == 1
        assert diagnostic.source_line == 2
        assert diagnostic.source_byte_offset == len(complete) + 1

    def test_invalid_json_is_distinct_and_complete_record_remains_available(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "invalid.jsonl"
        invalid = b'{"broken":}'
        source.write_bytes(invalid + b"\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("invalid.jsonl"),
            target_size=source.stat().st_size,
        )

        assert [envelope.raw_bytes for envelope in result.envelopes] == [invalid]
        assert diagnostic_codes(result) == {SourceDiagnosticCode.INVALID_JSON}
        assert result.diagnostics[0].record_ordinal == 0

    def test_unreadable_source_is_reported_instead_of_looking_empty(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "directory.jsonl"
        source.mkdir()

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("directory.jsonl"),
            target_size=1,
        )

        assert result.envelopes == ()
        assert diagnostic_codes(result) == {SourceDiagnosticCode.UNREADABLE_SOURCE}


class TestProviderDiscovery:
    def test_nested_traversal_failure_is_reported_while_siblings_continue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        readable = tmp_path / "readable"
        readable.mkdir()
        (readable / "valid.jsonl").write_text('{"type":"user"}\n')
        unreadable = tmp_path / "unreadable"
        unreadable.mkdir()
        (unreadable / "hidden.jsonl").write_text('{"type":"assistant"}\n')
        original_scandir = os.scandir

        def fail_one_nested_directory(path: os.PathLike[str] | str):
            if Path(path) == unreadable:
                raise PermissionError("injected nested traversal denial")
            return original_scandir(path)

        monkeypatch.setattr(os, "scandir", fail_one_nested_directory)

        result = discover_claude_sources(tmp_path.resolve())

        assert result.provider is Provider.CLAUDE
        assert [source.source_file_relative for source in result.sources] == [
            Path("readable/valid.jsonl")
        ]
        assert len(result.diagnostics) == 1
        diagnostic = result.diagnostics[0]
        assert diagnostic.code is SourceDiagnosticCode.UNREADABLE_PATH
        assert diagnostic.path == unreadable
        assert "injected nested traversal denial" in diagnostic.detail

    def test_claude_discovers_top_level_and_subagent_sources(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "primary.jsonl").write_text('{"type":"user"}\n')
        subagents = tmp_path / "session-id" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent.jsonl").write_text('{"type":"assistant"}\n')
        (tmp_path / "ignored.txt").write_text("not a source")

        result = discover_claude_sources(tmp_path.resolve())

        assert result.provider is Provider.CLAUDE
        assert [source.source_file_relative for source in result.sources] == [
            Path("primary.jsonl"),
            Path("session-id/subagents/agent.jsonl"),
        ]
        assert result.diagnostics == ()

    def test_codex_discovers_only_rollouts_under_date_paths(
        self, tmp_path: Path
    ) -> None:
        day = tmp_path / "2026" / "08" / "11"
        day.mkdir(parents=True)
        (day / "rollout-valid.jsonl").write_text('{"type":"session_meta"}\n')
        (day / "not-a-rollout.jsonl").write_text('{"type":"session_meta"}\n')
        wrong_depth = tmp_path / "2026" / "08" / "rollout-wrong.jsonl"
        wrong_depth.write_text('{"type":"session_meta"}\n')

        result = discover_codex_sources(tmp_path.resolve())

        assert result.provider is Provider.CODEX
        assert [source.source_file_relative for source in result.sources] == [
            Path("2026/08/11/rollout-valid.jsonl")
        ]
        assert "source_session_id" not in {
            field.name for field in fields(result.sources[0])
        }

    def test_rejects_readable_non_native_artifacts_beside_valid_sources(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "valid.jsonl").write_text('{"type":"user"}\n')
        (tmp_path / "agy.jsonl").write_text(
            '{"provider":"agy","messages":[{"role":"user"}]}\n'
        )
        (tmp_path / "rendered.md").write_text("# Rendered transcript\n")
        (tmp_path / "rendered.json").write_text(
            '{"format":"transport_archive","messages":[]}\n'
        )

        result = discover_claude_sources(tmp_path.resolve())

        assert [source.source_file_relative for source in result.sources] == [
            Path("valid.jsonl")
        ]
        assert diagnostic_codes(result) == {
            SourceDiagnosticCode.NON_NATIVE_AGY,
            SourceDiagnosticCode.NON_NATIVE_TRANSPORT_ARCHIVE,
        }
        assert {diagnostic.path.name for diagnostic in result.diagnostics} == {
            "agy.jsonl",
            "rendered.md",
            "rendered.json",
        }

    @pytest.mark.parametrize(
        ("root_kind", "expected"),
        [
            ("missing", SourceDiagnosticCode.MISSING_ROOT),
            ("file", SourceDiagnosticCode.UNREADABLE_ROOT),
        ],
    )
    def test_unavailable_roots_are_reported(
        self,
        tmp_path: Path,
        root_kind: str,
        expected: SourceDiagnosticCode,
    ) -> None:
        root = tmp_path / root_kind
        if root_kind == "file":
            root.write_text("not a directory")

        result = discover_claude_sources(root.resolve())

        assert result.sources == ()
        assert diagnostic_codes(result) == {expected}

    def test_permission_denied_root_is_not_reported_as_empty(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "unreadable"
        root.mkdir()
        root.chmod(0)
        try:
            result = discover_claude_sources(root.resolve())
        finally:
            root.chmod(0o700)

        assert result.sources == ()
        assert diagnostic_codes(result) == {SourceDiagnosticCode.UNREADABLE_ROOT}


def test_git_probe_ignores_ambient_git_routing_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    poison = tmp_path / "poison"
    for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.setenv(variable, str(poison))

    result = probe_git_repository(repository)

    assert result.repository_root == repository.resolve()
    assert result.diagnostics == ()
    assert all(variable in os.environ for variable in ("GIT_DIR", "GIT_WORK_TREE"))
