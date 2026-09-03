"""Bounded native-source reading and provider-root discovery tests."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from cc_search_chats.core.identity import Provider
from cc_search_chats.providers.source_discovery import (
    BoundedReadResult,
    BoundedReadStopReason,
    DiscoveryResult,
    SourceDiagnosticCode,
    configured_source_roots,
    discover_claude_sources,
    discover_codex_sources,
    read_bounded_jsonl,
)


def diagnostic_codes(
    result: BoundedReadResult | DiscoveryResult,
) -> set[SourceDiagnosticCode]:
    """Return diagnostic codes from a discovery or bounded-read result."""
    return {diagnostic.code for diagnostic in result.diagnostics}


class TestBoundedJsonlRead:
    def test_multiple_batches_equal_one_complete_target_read(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        records = tuple(
            f'{{"ordinal":{ordinal},"padding":"xxxxx"}}'.encode()
            for ordinal in range(7)
        )
        source.write_bytes(b"\n".join(records) + b"\n")
        target_size = source.stat().st_size
        expected = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=target_size,
            max_records_per_batch=100,
            max_batch_bytes=10_000,
            max_single_record_bytes=10_000,
        )

        batches: list[BoundedReadResult] = []
        offset = 0
        ordinal = 0
        source_line = 1
        while offset < target_size:
            batch = read_bounded_jsonl(
                source,
                source_file_relative=Path("session.jsonl"),
                target_size=target_size,
                start_byte_offset=offset,
                next_record_ordinal=ordinal,
                next_source_line=source_line,
                max_records_per_batch=2,
                max_batch_bytes=70,
                max_single_record_bytes=100,
            )
            batches.append(batch)
            assert batch.next_source_byte_offset > offset
            assert batch.next_record_ordinal > ordinal
            assert batch.next_source_line > source_line
            assert len(batch.envelopes) <= 2
            assert sum(value.raw_byte_length for value in batch.envelopes) <= 70
            offset = batch.next_source_byte_offset
            ordinal = batch.next_record_ordinal
            source_line = batch.next_source_line

        assert (
            tuple(envelope for batch in batches for envelope in batch.envelopes)
            == expected.envelopes
        )
        assert all(
            batch.stop_reason is BoundedReadStopReason.BATCH_LIMIT_REACHED
            for batch in batches[:-1]
        )
        assert batches[-1].stop_reason is BoundedReadStopReason.TARGET_REACHED

    def test_batch_byte_limit_leaves_next_record_unconsumed(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        first = b'{"value":"first"}'
        second = b'{"value":"second"}'
        source.write_bytes(first + b"\n" + second + b"\n")

        batch = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=source.stat().st_size,
            max_records_per_batch=10,
            max_batch_bytes=len(first),
            max_single_record_bytes=100,
        )

        assert [value.raw_bytes for value in batch.envelopes] == [first]
        assert batch.next_source_byte_offset == len(first) + 1
        assert batch.next_record_ordinal == 1
        assert batch.next_source_line == 2
        assert batch.stop_reason is BoundedReadStopReason.BATCH_LIMIT_REACHED

    def test_first_record_may_exceed_batch_limit_but_not_single_record_limit(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        record = b'{"padding":"0123456789"}'
        source.write_bytes(record + b"\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=source.stat().st_size,
            max_records_per_batch=10,
            max_batch_bytes=4,
            max_single_record_bytes=len(record),
        )

        assert [value.raw_bytes for value in result.envelopes] == [record]
        assert result.batch_raw_bytes == len(record)
        assert result.stop_reason is BoundedReadStopReason.TARGET_REACHED

    def test_oversized_record_is_named_and_preserves_later_coordinates(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "oversized.jsonl"
        oversized = b'{"padding":"too long"}'
        following = b'{"value":"after"}'
        source.write_bytes(oversized + b"\n" + following + b"\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("oversized.jsonl"),
            target_size=source.stat().st_size,
            max_single_record_bytes=len(following),
        )

        assert [value.raw_bytes for value in result.envelopes] == [following]
        assert result.envelopes[0].record_ordinal == 1
        assert result.envelopes[0].source_line == 2
        assert result.envelopes[0].source_byte_offset == len(oversized) + 1
        assert result.next_source_byte_offset == source.stat().st_size
        assert result.next_record_ordinal == 2
        assert result.next_source_line == 3
        assert result.stop_reason is BoundedReadStopReason.TARGET_REACHED
        assert diagnostic_codes(result) == {SourceDiagnosticCode.OVERSIZED_RECORD}
        diagnostic = result.diagnostics[0]
        assert diagnostic.record_ordinal == 0
        assert diagnostic.source_line == 1
        assert diagnostic.source_byte_offset == 0

    @pytest.mark.parametrize("value", [0, -1, True, False])
    def test_rejects_invalid_batch_limits(self, tmp_path: Path, value: int) -> None:
        source = tmp_path / "session.jsonl"
        source.write_bytes(b"{}\n")

        for limits in (
            {"max_records_per_batch": value},
            {"max_batch_bytes": value},
            {"max_single_record_bytes": value},
        ):
            with pytest.raises(ValueError, match="limit"):
                read_bounded_jsonl(
                    source,
                    source_file_relative=Path("session.jsonl"),
                    target_size=source.stat().st_size,
                    **limits,
                )

    def test_rejects_noninteger_batch_limit(self, tmp_path: Path) -> None:
        source = tmp_path / "session.jsonl"
        source.write_bytes(b"{}\n")
        external_value = json.loads("1.5")

        with pytest.raises(ValueError, match="limit"):
            read_bounded_jsonl(
                source,
                source_file_relative=Path("session.jsonl"),
                target_size=source.stat().st_size,
                max_single_record_bytes=external_value,
            )

    def test_resume_seek_does_not_reclassify_committed_prefix(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        committed_invalid = b'{"invalid":}'
        appended = b'{"valid":true}'
        source.write_bytes(committed_invalid + b"\n" + appended + b"\n")
        committed_offset = len(committed_invalid) + 1

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=source.stat().st_size,
            start_byte_offset=committed_offset,
            next_record_ordinal=1,
            next_source_line=2,
        )

        assert [value.raw_bytes for value in result.envelopes] == [appended]
        assert SourceDiagnosticCode.INVALID_JSON not in diagnostic_codes(result)

    def test_resumes_from_absolute_complete_record_coordinates(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        committed = b'{"text":"committed"}'
        appended = b'{"text":"appended"}'
        source.write_bytes(committed + b"\n" + appended + b"\n")
        committed_offset = len(committed) + 1

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=source.stat().st_size,
            start_byte_offset=committed_offset,
            next_record_ordinal=1,
            next_source_line=2,
        )

        assert [envelope.raw_bytes for envelope in result.envelopes] == [appended]
        assert result.envelopes[0].record_ordinal == 1
        assert result.envelopes[0].source_line == 2
        assert result.envelopes[0].source_byte_offset == committed_offset
        assert result.next_source_byte_offset == source.stat().st_size
        assert result.next_record_ordinal == 2
        assert result.next_source_line == 3

    @pytest.mark.parametrize(
        ("start_byte_offset", "next_record_ordinal", "next_source_line"),
        [
            (-1, 0, 1),
            (0, -1, 1),
            (0, 0, 0),
            (0, 1, 1),
            (4, 0, 1),
        ],
    )
    def test_rejects_inconsistent_resume_coordinates(
        self,
        tmp_path: Path,
        start_byte_offset: int,
        next_record_ordinal: int,
        next_source_line: int,
    ) -> None:
        source = tmp_path / "session.jsonl"
        source.write_bytes(b"{}\n")

        with pytest.raises(ValueError, match=r"coordinate|target_size"):
            read_bounded_jsonl(
                source,
                source_file_relative=Path("session.jsonl"),
                target_size=source.stat().st_size,
                start_byte_offset=start_byte_offset,
                next_record_ordinal=next_record_ordinal,
                next_source_line=next_source_line,
            )

    def test_partial_resumed_suffix_preserves_next_coordinates(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "session.jsonl"
        committed = b'{"text":"committed"}\n'
        source.write_bytes(committed + b'{"text":')

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=source.stat().st_size,
            start_byte_offset=len(committed),
            next_record_ordinal=1,
            next_source_line=2,
        )

        assert result.envelopes == ()
        assert result.next_source_byte_offset == len(committed)
        assert result.next_record_ordinal == 1
        assert result.next_source_line == 2
        diagnostic = result.diagnostics[0]
        assert diagnostic.record_ordinal == 1
        assert diagnostic.source_line == 2
        assert diagnostic.source_byte_offset == len(committed)
        assert result.stop_reason is BoundedReadStopReason.PARTIAL_TAIL

        repeated = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=source.stat().st_size,
            start_byte_offset=result.next_source_byte_offset,
            next_record_ordinal=result.next_record_ordinal,
            next_source_line=result.next_source_line,
        )
        assert repeated.next_source_byte_offset == result.next_source_byte_offset
        assert repeated.next_record_ordinal == result.next_record_ordinal
        assert repeated.next_source_line == result.next_source_line
        assert repeated.stop_reason is BoundedReadStopReason.PARTIAL_TAIL

    def test_resumed_unreadable_source_diagnostic_uses_absolute_coordinates(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "directory.jsonl"
        source.mkdir()

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=12,
            start_byte_offset=8,
            next_record_ordinal=2,
            next_source_line=3,
        )

        diagnostic = result.diagnostics[0]
        assert diagnostic.code is SourceDiagnosticCode.UNREADABLE_SOURCE
        assert diagnostic.record_ordinal == 2
        assert diagnostic.source_line == 3
        assert diagnostic.source_byte_offset == 8
        assert result.stop_reason is BoundedReadStopReason.UNREADABLE_SOURCE

    def test_source_truncation_has_distinct_stop_reason(self, tmp_path: Path) -> None:
        source = tmp_path / "session.jsonl"
        source.write_bytes(b"{}\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("session.jsonl"),
            target_size=12,
        )

        assert result.next_source_byte_offset == 3
        assert result.stop_reason is BoundedReadStopReason.SOURCE_TRUNCATED
        assert diagnostic_codes(result) == {SourceDiagnosticCode.SOURCE_TRUNCATED}

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

    def test_invalid_utf8_is_distinct_and_complete_record_remains_available(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "invalid-encoding.jsonl"
        source.write_bytes(b"\xff\n")

        result = read_bounded_jsonl(
            source,
            source_file_relative=Path("invalid-encoding.jsonl"),
            target_size=source.stat().st_size,
        )

        assert [envelope.raw_bytes for envelope in result.envelopes] == [b"\xff"]
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].code is SourceDiagnosticCode.INVALID_ENCODING
        assert "valid UTF-8" in result.diagnostics[0].detail

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
    def test_undecodable_first_line_is_reported_not_admitted(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "invalid-encoding.jsonl"
        source.write_bytes(b"\xff\n")

        result = discover_claude_sources(tmp_path.resolve())

        assert result.sources == ()
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].code is SourceDiagnosticCode.INVALID_ENCODING
        assert "valid UTF-8" in result.diagnostics[0].detail

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

    def test_claude_does_not_descend_into_hidden_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        hidden = project / ".windowed"
        hidden.mkdir(parents=True)
        (hidden / "x.jsonl").write_text('{"role":"user"}\n')
        visible = project / "visible.jsonl"
        visible.write_text('{"type":"user"}\n')
        original_scandir = os.scandir

        def reject_hidden_directory(path: os.PathLike[str] | str):
            if Path(path) == hidden:
                raise AssertionError("hidden directory was traversed")
            return original_scandir(path)

        monkeypatch.setattr(os, "scandir", reject_hidden_directory)

        result = discover_claude_sources(tmp_path.resolve())

        assert [source.source_file_relative for source in result.sources] == [
            Path("project/visible.jsonl")
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


class TestConfiguredSourceRoots:
    def test_defaults_include_present_standard_and_ponytail_session_roots_only(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        expected = (
            (Provider.CLAUDE, home / ".claude" / "projects"),
            (Provider.CLAUDE, home / ".claude-ponytail" / "projects"),
            (Provider.CODEX, home / ".codex" / "sessions"),
            (Provider.CODEX, home / ".codex-ponytail" / "sessions"),
        )
        for _, path in expected:
            path.mkdir(parents=True)
        (home / ".claude-ponytail" / "credentials.json").write_text("secret")
        (home / ".codex-ponytail" / "config.toml").write_text("secret")

        roots = configured_source_roots(environ={}, home=home)

        assert tuple((root.provider, root.path) for root in roots) == tuple(
            (provider, path.resolve()) for provider, path in expected
        )
        assert len({root.source_root_id for root in roots}) == 4
        assert all(len(root.source_root_id) == 64 for root in roots)
        assert all(root.path.name in {"projects", "sessions"} for root in roots)

    def test_plural_roots_override_singular_values_and_deduplicate(
        self, tmp_path: Path
    ) -> None:
        claude_one = (tmp_path / "claude-one").resolve()
        claude_two = (tmp_path / "claude-two").resolve()
        codex = (tmp_path / "codex").resolve()
        for path in (claude_one, claude_two, codex):
            path.mkdir()
        environment = {
            "CC_SEARCH_CLAUDE_ROOTS": os.pathsep.join(
                (str(claude_one), str(claude_two), str(claude_one))
            ),
            "CC_SEARCH_CODEX_ROOTS": str(codex),
            "CC_SEARCH_CLAUDE_ROOT": str(tmp_path / "singular-claude-poison"),
            "CC_SEARCH_CODEX_ROOT": str(tmp_path / "singular-codex-poison"),
        }

        roots = configured_source_roots(environ=environment, home=tmp_path)

        assert tuple((root.provider, root.path) for root in roots) == (
            (Provider.CLAUDE, claude_one),
            (Provider.CLAUDE, claude_two),
            (Provider.CODEX, codex),
        )

    def test_singular_variables_remain_exclusive_migration_compatibility(
        self, tmp_path: Path
    ) -> None:
        claude = (tmp_path / "legacy-claude").resolve()
        codex = (tmp_path / "legacy-codex").resolve()
        roots = configured_source_roots(
            environ={
                "CC_SEARCH_CLAUDE_ROOT": str(claude),
                "CC_SEARCH_CODEX_ROOT": str(codex),
            },
            home=tmp_path,
        )

        assert tuple((root.provider, root.path) for root in roots) == (
            (Provider.CLAUDE, claude),
            (Provider.CODEX, codex),
        )
