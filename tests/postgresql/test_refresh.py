"""Fixture-root PostgreSQL refresh behavior."""

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import psycopg
import pytest

from cc_search_chats.cli import _postgres_envelope
from cc_search_chats.core.identity import Provider
from cc_search_chats.providers.source_discovery import (
    ConfiguredSourceRoot,
    SourceDiagnostic,
    SourceDiagnosticCode,
    source_root_id,
)
from cc_search_chats.storage.postgresql import (
    RefreshProgress,
    index_embeddings,
    migrate,
    refresh_native_sources,
    replace_messages,
)
from cc_search_chats.storage.postgresql import refresh as refresh_module
from cc_search_chats.storage.postgresql import (
    search_messages as _search_messages,
)

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
_INDEX_QUEUE_LOCK = "cc_search_chats.index_queue"


def search_messages(
    connection: psycopg.Connection,
    query: str,
    *,
    include_agents: bool = True,
):
    """Keep refresh assertions independent of fail-closed session classification."""
    return _search_messages(
        connection,
        query,
        include_agents=include_agents,
    )


def _source_root(provider: Provider, path: Path) -> ConfiguredSourceRoot:
    resolved = path.resolve()
    return ConfiguredSourceRoot(
        provider=provider,
        path=resolved,
        source_root_id=source_root_id(provider, resolved),
    )


def _claude_message_bytes(*, uuid: str, text: str) -> bytes:
    payload = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T00:01:00Z",
        "cwd": "/synthetic/repository",
        "isSidechain": False,
        "message": {"role": "assistant", "content": text},
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def _append_claude_message(path: Path, *, uuid: str, text: str) -> None:
    with path.open("ab") as handle:
        handle.write(_claude_message_bytes(uuid=uuid, text=text))


def _corpus_cardinality(
    connection: psycopg.Connection,
) -> tuple[int, int, int]:
    return next(
        connection.execute(
            """
            SELECT
              (SELECT count(*) FROM cc_search_chats.corpus_revision),
              (SELECT count(*) FROM cc_search_chats.message_current),
              (SELECT count(*) FROM cc_search_chats.physical_alias_current)
            """
        )
    )


def _message_row_version(connection: psycopg.Connection, canonical_locator: str) -> str:
    return next(
        connection.execute(
            """
            SELECT xmin::text
            FROM cc_search_chats.message_current
            WHERE canonical_locator = %s AND content_class = 'prose'
            """,
            (canonical_locator,),
        )
    )[0]


def _refresh_metadata_versions(
    connection: psycopg.Connection,
) -> tuple[str, str, int]:
    return next(
        connection.execute(
            """
            SELECT root.xmin::text, source.xmin::text,
                   (SELECT count(*) FROM cc_search_chats.refresh_run)
            FROM cc_search_chats.source_root_current AS root
            JOIN cc_search_chats.source_file_current AS source
              USING (source_root_id)
            """
        )
    )


def test_refresh_keeps_colliding_relative_paths_distinct_by_source_root(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    first = tmp_path / "standard-claude"
    second = tmp_path / "ponytail-claude"
    first.mkdir()
    second.mkdir()
    shutil.copy(FIXTURES / "claude_primary.jsonl", first / "session.jsonl")
    shutil.copy(FIXTURES / "claude_primary.jsonl", second / "session.jsonl")
    roots = (
        _source_root(Provider.CLAUDE, first),
        _source_root(Provider.CLAUDE, second),
    )

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert next(
        postgres_connection.execute(
            """
            SELECT count(DISTINCT source_root_id), count(*)
            FROM cc_search_chats.source_file_current
            WHERE source_file_relative = 'session.jsonl'
            """
        )
    ) == (2, 2)
    alias_roots = tuple(
        row[0]
        for row in postgres_connection.execute(
            """
            SELECT DISTINCT source_root_id
            FROM cc_search_chats.physical_alias_current
            ORDER BY source_root_id
            """
        )
    )
    assert alias_roots == tuple(sorted(root.source_root_id for root in roots))
    assert len(search_messages(postgres_connection, "visible primary user")) == 1


def test_cross_root_canonical_conflict_aborts_publication(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    first = tmp_path / "standard-claude"
    second = tmp_path / "ponytail-claude"
    first.mkdir()
    second.mkdir()
    original = (FIXTURES / "claude_primary.jsonl").read_bytes()
    (first / "session.jsonl").write_bytes(original)
    conflicting = original.replace(b"visible primary user", b"changed primary user", 1)
    assert len(conflicting) == len(original)
    (second / "session.jsonl").write_bytes(conflicting)
    roots = (
        _source_root(Provider.CLAUDE, first),
        _source_root(Provider.CLAUDE, second),
    )

    with pytest.raises(ValueError, match="conflicting observations"):
        refresh_native_sources(postgres_connection, source_roots=roots)

    assert next(
        postgres_connection.execute(
            """
            SELECT current_revision_id,
                   (SELECT count(*) FROM cc_search_chats.message_current),
                   (SELECT count(*) FROM cc_search_chats.physical_alias_current)
            FROM cc_search_chats.corpus_state
            WHERE singleton
            """
        )
    ) == (None, 0, 0)
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT status
            FROM cc_search_chats.refresh_run
            ORDER BY run_id DESC
            LIMIT 1
            """
            )
        )[0]
        == "failed"
    )


def test_ponytail_root_excludes_adjacent_state_and_caches_excluded_jsonl(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_home = tmp_path / ".claude-ponytail"
    root = isolated_home / "projects"
    root.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude_primary.jsonl", root / "session.jsonl")
    (root / "agy.jsonl").write_text(
        '{"provider":"agy","messages":[{"role":"user",'
        '"content":"agy exclusion sentinel"}]}\n'
    )
    (isolated_home / "credentials.json").write_text(
        '{"secret":"credential exclusion sentinel"}\n'
    )
    roots = (_source_root(Provider.CLAUDE, root),)

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert tuple(
        postgres_connection.execute(
            """
            SELECT source_file_relative, source_status
            FROM cc_search_chats.source_file_current
            ORDER BY source_file_relative
            """
        )
    ) == (("agy.jsonl", "excluded"), ("session.jsonl", "indexed"))
    assert search_messages(postgres_connection, "agy exclusion sentinel") == ()
    assert search_messages(postgres_connection, "credential exclusion sentinel") == ()

    def unexpected_content_read(*args, **kwargs):
        raise AssertionError("unchanged JSONL content was read")

    monkeypatch.setattr(
        refresh_module, "inspect_non_native_artifact", unexpected_content_read
    )
    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", unexpected_content_read)

    refresh_native_sources(postgres_connection, source_roots=roots)


def test_switching_empty_roots_removes_stale_root_without_new_generation(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = refresh_native_sources(
        postgres_connection,
        source_roots=(_source_root(Provider.CLAUDE, first_root),),
    )

    second = refresh_native_sources(
        postgres_connection,
        source_roots=(_source_root(Provider.CLAUDE, second_root),),
    )

    assert second.revision_id == first.revision_id
    assert tuple(
        row[0]
        for row in postgres_connection.execute(
            """
            SELECT source_root_id
            FROM cc_search_chats.source_root_current
            ORDER BY source_root_id
            """
        )
    ) == (source_root_id(Provider.CLAUDE, second_root.resolve()),)


def test_unavailable_configured_root_does_not_replace_last_known_root_metadata(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    available_root = tmp_path / "available"
    available_root.mkdir()
    configured = _source_root(Provider.CLAUDE, available_root)
    refresh_native_sources(postgres_connection, source_roots=(configured,))

    missing_root = tmp_path / "missing"
    missing = _source_root(Provider.CLAUDE, missing_root)
    with pytest.raises(RuntimeError, match="roots are unavailable"):
        refresh_native_sources(postgres_connection, source_roots=(missing,))

    assert tuple(
        postgres_connection.execute(
            """
            SELECT source_root_id, resolved_path
            FROM cc_search_chats.source_root_current
            """
        )
    ) == ((configured.source_root_id, str(available_root.resolve())),)


def test_refresh_rejects_source_root_id_not_derived_from_provider_and_path(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    invalid = ConfiguredSourceRoot(
        provider=Provider.CLAUDE,
        path=root.resolve(),
        source_root_id="a" * 64,
    )

    with pytest.raises(ValueError, match="derived from provider and resolved path"):
        refresh_native_sources(postgres_connection, source_roots=(invalid,))


def test_noop_reads_no_jsonl_and_append_reads_only_suffix(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    reads: list[tuple[Path, int, int]] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        reads.append(
            (
                path,
                kwargs.get("start_byte_offset", 0),
                kwargs["target_size"],
            )
        )
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)

    first = refresh_native_sources(postgres_connection, source_roots=roots)
    first_cardinality = _corpus_cardinality(postgres_connection)
    first_hit = search_messages(postgres_connection, "visible primary user")[0]
    first_row_version = _message_row_version(
        postgres_connection, first_hit.canonical_locator
    )
    first_metadata_versions = _refresh_metadata_versions(postgres_connection)
    committed_size = source.stat().st_size
    assert reads and reads[0][1] == 0

    reads.clear()
    second = refresh_native_sources(postgres_connection, source_roots=roots)

    assert reads == []
    assert second.revision_id == first.revision_id
    assert _corpus_cardinality(postgres_connection) == first_cardinality
    assert _refresh_metadata_versions(postgres_connection) == first_metadata_versions

    _append_claude_message(
        source,
        uuid="claude-incremental-append",
        text="incremental suffix sentinel",
    )
    reads.clear()
    appended = refresh_native_sources(postgres_connection, source_roots=roots)

    assert reads and reads[0][1] == committed_size
    assert all(start >= committed_size for _, start, _ in reads)
    assert appended.revision_id != first.revision_id
    assert len(search_messages(postgres_connection, "incremental suffix sentinel")) == 1
    assert (
        _message_row_version(postgres_connection, first_hit.canonical_locator)
        == first_row_version
    )
    assert _corpus_cardinality(postgres_connection) == (
        first_cardinality[0] + 1,
        first_cardinality[1] + 1,
        first_cardinality[2] + 1,
    )


def test_codex_append_resumes_persisted_session_parser_state(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_root = tmp_path / "codex"
    day = codex_root / "2026" / "08" / "11"
    day.mkdir(parents=True)
    source = day / "rollout-state.jsonl"
    shutil.copy(FIXTURES / "codex_modern_primary_145.jsonl", source)
    roots = (_source_root(Provider.CODEX, codex_root),)
    reads: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        reads.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)
    refresh_native_sources(postgres_connection, source_roots=roots)
    committed_size = source.stat().st_size
    appended = {
        "timestamp": "2026-08-11T03:01:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": "codex-persisted-state-append",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "codex persisted state sentinel",
                }
            ],
        },
    }
    with source.open("ab") as handle:
        handle.write(
            json.dumps(appended, separators=(",", ":")).encode("utf-8") + b"\n"
        )

    reads.clear()
    refresh_native_sources(postgres_connection, source_roots=roots)

    assert reads and reads[0] == committed_size
    hits = search_messages(postgres_connection, "codex persisted state sentinel")
    assert len(hits) == 1
    assert hits[0].provider == "codex"


def test_partial_tail_restarts_at_last_complete_record(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    refresh_native_sources(postgres_connection, source_roots=roots)
    committed_size = source.stat().st_size
    complete_record = _claude_message_bytes(
        uuid="claude-partial-tail",
        text="completed partial tail sentinel",
    )
    split = len(complete_record) // 2
    with source.open("ab") as handle:
        handle.write(complete_record[:split])

    partial = refresh_native_sources(postgres_connection, source_roots=roots)

    assert partial.pending_bytes == split
    assert next(
        postgres_connection.execute(
            """
            SELECT observed_size, complete_byte_offset, pending_bytes
            FROM cc_search_chats.source_file_current
            """
        )
    ) == (committed_size + split, committed_size, split)
    assert search_messages(postgres_connection, "completed partial tail sentinel") == ()

    starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)
    with source.open("ab") as handle:
        handle.write(complete_record[split:])

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert starts and starts[0] == committed_size
    assert (
        len(search_messages(postgres_connection, "completed partial tail sentinel"))
        == 1
    )


def test_source_advance_is_reported_and_left_for_the_next_refresh(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    refresh_native_sources(postgres_connection, source_roots=roots)
    _append_claude_message(
        source,
        uuid="claude-bounded-target",
        text="bounded target sentinel",
    )
    original_reader = refresh_module.read_bounded_jsonl
    injected = False

    def advancing_reader(path: Path, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            _append_claude_message(
                source,
                uuid="claude-active-writer",
                text="active writer pending sentinel",
            )
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", advancing_reader)

    advanced = refresh_native_sources(postgres_connection, source_roots=roots)

    assert advanced.advanced_source_count == 1
    assert advanced.pending_bytes > 0
    assert len(search_messages(postgres_connection, "bounded target sentinel")) == 1
    assert search_messages(postgres_connection, "active writer pending sentinel") == ()

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", original_reader)
    refresh_native_sources(postgres_connection, source_roots=roots)

    assert (
        len(search_messages(postgres_connection, "active writer pending sentinel")) == 1
    )


def test_same_size_edit_reparses_from_zero(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    refresh_native_sources(postgres_connection, source_roots=roots)
    before = source.stat()
    changed = source.read_bytes().replace(
        b"visible primary user", b"changed primary user", 1
    )
    assert len(changed) == before.st_size
    source.write_bytes(changed)
    os.utime(
        source,
        ns=(before.st_atime_ns, max(before.st_mtime_ns + 1, source.stat().st_mtime_ns)),
    )
    assert source.stat().st_ino == before.st_ino
    starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert starts and starts[0] == 0
    assert len(search_messages(postgres_connection, "changed primary user")) == 1


def test_truncation_and_inode_replacement_reparse_from_zero(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    refresh_native_sources(postgres_connection, source_roots=roots)
    starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)
    first_line = source.read_bytes().splitlines(keepends=True)[0]
    source.write_bytes(first_line)

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert starts and starts[0] == 0
    assert search_messages(postgres_connection, "visible assistant") == ()
    old_inode = source.stat().st_ino
    replacement = claude_root / "replacement.jsonl"
    replacement.write_bytes(
        _claude_message_bytes(
            uuid="claude-inode-replacement",
            text="inode replacement sentinel",
        )
    )
    replacement.replace(source)
    assert source.stat().st_ino != old_inode
    starts.clear()

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert starts and starts[0] == 0
    assert len(search_messages(postgres_connection, "inode replacement sentinel")) == 1
    assert search_messages(postgres_connection, "visible primary user") == ()


def test_parser_state_version_change_forces_full_reparse(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    refresh_native_sources(postgres_connection, source_roots=roots)
    starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)
    monkeypatch.setitem(
        refresh_module._PARSER_STATE_VERSIONS,
        Provider.CLAUDE,
        refresh_module._PARSER_STATE_VERSIONS[Provider.CLAUDE] + 1,
    )

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert starts and starts[0] == 0
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT parser_state_version
            FROM cc_search_chats.source_file_current
            """
            )
        )[0]
        == refresh_module._PARSER_STATE_VERSIONS[Provider.CLAUDE]
    )


def test_unreadable_changed_source_retains_committed_rows_and_checkpoint(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    hidden = claude_root / "session.hidden"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    first = refresh_native_sources(postgres_connection, source_roots=roots)
    checkpoint = next(
        postgres_connection.execute(
            """
            SELECT xmin::text, complete_byte_offset
            FROM cc_search_chats.source_file_current
            """
        )
    )
    _append_claude_message(
        source,
        uuid="claude-unreadable-append",
        text="unreadable append sentinel",
    )
    original_reader = refresh_module.read_bounded_jsonl

    def unavailable_reader(path: Path, **kwargs):
        source.rename(hidden)
        try:
            return original_reader(path, **kwargs)
        finally:
            hidden.rename(source)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", unavailable_reader)

    failed = refresh_native_sources(postgres_connection, source_roots=roots)

    assert failed.revision_id == first.revision_id
    assert failed.failed_source_count == 1
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT xmin::text, complete_byte_offset
            FROM cc_search_chats.source_file_current
            """
            )
        )
        == checkpoint
    )
    assert (
        search_messages(
            postgres_connection,
            "unreadable append sentinel",
            include_agents=True,
        )
        == ()
    )
    failed_coverage = cast(
        dict[str, object],
        _postgres_envelope(postgres_connection, "index")["coverage"],
    )
    assert failed_coverage["read_files"] == 0
    assert failed_coverage["unreadable_files"] == 1
    assert failed_coverage["completeness"] == "partial"

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", original_reader)
    refresh_native_sources(postgres_connection, source_roots=roots)

    assert (
        len(
            search_messages(
                postgres_connection,
                "unreadable append sentinel",
                include_agents=True,
            )
        )
        == 1
    )


def test_unreadable_artifact_probe_retains_committed_rows_and_checkpoint(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    first = refresh_native_sources(postgres_connection, source_roots=roots)
    checkpoint = next(
        postgres_connection.execute(
            """
            SELECT xmin::text, complete_byte_offset, source_status
            FROM cc_search_chats.source_file_current
            """
        )
    )
    _append_claude_message(
        source,
        uuid="claude-unreadable-probe",
        text="unreadable probe sentinel",
    )
    monkeypatch.setattr(
        refresh_module,
        "inspect_non_native_artifact",
        lambda path: SourceDiagnostic(
            code=SourceDiagnosticCode.UNREADABLE_SOURCE,
            path=path,
            detail="injected artifact probe denial",
        ),
    )

    failed = refresh_native_sources(postgres_connection, source_roots=roots)

    assert failed.revision_id == first.revision_id
    assert failed.failed_source_count == 1
    assert (
        next(
            postgres_connection.execute(
                """
                SELECT xmin::text, complete_byte_offset, source_status
                FROM cc_search_chats.source_file_current
                """
            )
        )
        == checkpoint
    )
    assert len(search_messages(postgres_connection, "visible primary user")) == 1
    assert search_messages(postgres_connection, "unreadable probe sentinel") == ()


def test_source_stat_failure_cannot_be_mistaken_for_deletion(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    first = refresh_native_sources(postgres_connection, source_roots=roots)
    checkpoint = next(
        postgres_connection.execute(
            """
            SELECT xmin::text, complete_byte_offset
            FROM cc_search_chats.source_file_current
            """
        )
    )
    original_stat = Path.stat

    def denied_source_stat(path: Path, *, follow_symlinks: bool = True):
        if path == source:
            raise PermissionError("injected source stat denial")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", denied_source_stat)

    failed = refresh_native_sources(postgres_connection, source_roots=roots)

    assert failed.revision_id == first.revision_id
    assert failed.failed_source_count == 1
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT xmin::text, complete_byte_offset
            FROM cc_search_chats.source_file_current
            """
            )
        )
        == checkpoint
    )
    assert len(search_messages(postgres_connection, "visible primary user")) == 1


def test_unsupported_appended_shape_does_not_advance_checkpoint(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    first = refresh_native_sources(postgres_connection, source_roots=roots)
    checkpoint = next(
        postgres_connection.execute(
            """
            SELECT xmin::text, complete_byte_offset, next_record_ordinal
            FROM cc_search_chats.source_file_current
            """
        )
    )
    future_record = {
        "type": "future_conversation_record",
        "uuid": "future-shape",
        "message": {
            "role": "assistant",
            "content": "future shape must not disappear behind a checkpoint",
        },
    }
    with source.open("ab") as handle:
        handle.write(
            json.dumps(future_record, separators=(",", ":")).encode("utf-8") + b"\n"
        )

    failed = refresh_native_sources(postgres_connection, source_roots=roots)

    assert failed.revision_id == first.revision_id
    assert failed.failed_source_count == 1
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT xmin::text, complete_byte_offset, next_record_ordinal
            FROM cc_search_chats.source_file_current
            """
            )
        )
        == checkpoint
    )
    diagnostics = next(
        postgres_connection.execute(
            """
            SELECT diagnostics
            FROM cc_search_chats.refresh_run
            ORDER BY run_id DESC
            LIMIT 1
            """
        )
    )[0]
    assert diagnostics[0]["code"] == "source_refresh_failed"
    assert "unknown_conversation_record" in diagnostics[0]["detail"]
    failed_coverage = cast(
        dict[str, object],
        _postgres_envelope(postgres_connection, "index")["coverage"],
    )
    assert failed_coverage["unrecognized_conversation_records"] == 1
    assert failed_coverage["completeness"] == "partial"


def test_failed_publication_keeps_checkpoint_and_corpus_then_retry_cleans_stage(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    first = refresh_native_sources(postgres_connection, source_roots=roots)
    checkpoint = next(
        postgres_connection.execute(
            """
            SELECT xmin::text, complete_byte_offset
            FROM cc_search_chats.source_file_current
            """
        )
    )
    cardinality = _corpus_cardinality(postgres_connection)
    _append_claude_message(
        source,
        uuid="claude-crash-retry",
        text="crash retry sentinel",
    )
    original_publish = refresh_module._publish_staged_refresh

    def fail_before_publish(*args, **kwargs):
        raise RuntimeError("injected pre-publication crash")

    monkeypatch.setattr(refresh_module, "_publish_staged_refresh", fail_before_publish)

    with pytest.raises(RuntimeError, match="injected pre-publication crash"):
        refresh_native_sources(postgres_connection, source_roots=roots)

    assert _corpus_cardinality(postgres_connection) == cardinality
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT xmin::text, complete_byte_offset
            FROM cc_search_chats.source_file_current
            """
            )
        )
        == checkpoint
    )
    assert search_messages(postgres_connection, "crash retry sentinel") == ()
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT status
            FROM cc_search_chats.refresh_run
            ORDER BY run_id DESC
            LIMIT 1
            """
            )
        )[0]
        == "failed"
    )
    assert next(
        postgres_connection.execute(
            """
            SELECT to_regclass('pg_temp.refresh_stage_message') IS NULL
               AND to_regclass('pg_temp.refresh_stage_source') IS NULL
            """
        )
    )[0]

    monkeypatch.setattr(refresh_module, "_publish_staged_refresh", original_publish)
    retried = refresh_native_sources(postgres_connection, source_roots=roots)

    assert retried.revision_id != first.revision_id
    assert len(search_messages(postgres_connection, "crash retry sentinel")) == 1


def test_removed_source_deletes_only_its_committed_aliases(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_source = first_root / "first.jsonl"
    second_source = second_root / "second.jsonl"
    first_source.write_bytes(
        _claude_message_bytes(uuid="first-source", text="first source sentinel")
    )
    second_source.write_bytes(
        _claude_message_bytes(uuid="second-source", text="second source sentinel")
    )
    roots = (
        _source_root(Provider.CLAUDE, first_root),
        _source_root(Provider.CLAUDE, second_root),
    )
    refresh_native_sources(postgres_connection, source_roots=roots)
    first_source.unlink()

    refresh_native_sources(postgres_connection, source_roots=roots)

    assert search_messages(postgres_connection, "first source sentinel") == ()
    assert len(search_messages(postgres_connection, "second source sentinel")) == 1
    assert tuple(
        row[0]
        for row in postgres_connection.execute(
            """
            SELECT source_root_id
            FROM cc_search_chats.source_file_current
            ORDER BY source_root_id
            """
        )
    ) == (roots[1].source_root_id,)


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


def test_refresh_waits_in_the_database_index_queue(
    postgres_cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh_module, "_WAIT_HEARTBEAT_SECONDS", 0.02, raising=False)
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_root.mkdir()
    waiting = Event()
    events: list[object] = []

    def progress(*values: object) -> None:
        events.extend(values)
        if any(
            getattr(value, "state", None) == "waiting_for_index" for value in values
        ):
            waiting.set()

    with (
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="index-blocker",
        ) as blocker,
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="index-waiter",
        ) as waiter,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        blocker.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )
        pending = executor.submit(
            refresh_native_sources,
            waiter,
            claude_root=claude_root,
            codex_root=codex_root,
            progress=progress,
        )
        reported_waiting = waiting.wait(timeout=1)
        still_pending = not pending.done()
        blocker.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )
        assert pending.result(timeout=1).message_count == 0
        assert reported_waiting
        assert still_pending
        assert any(
            getattr(event, "state", None) == "waiting_for_index" for event in events
        )


def test_refresh_run_exposes_owner_phase_heartbeat_and_terminal_progress(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh_module, "_RUN_HEARTBEAT_SECONDS", 0.02, raising=False)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    source.write_bytes(
        _claude_message_bytes(uuid="heartbeat-source", text="heartbeat sentinel")
    )
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    entered = Event()
    release = Event()
    original = refresh_module._parse_and_stage_source

    def paused_parse(connection, plan):
        entered.set()
        if not release.wait(timeout=2):
            raise AssertionError("heartbeat test did not release paused parse")
        return original(connection, plan)

    monkeypatch.setattr(refresh_module, "_parse_and_stage_source", paused_parse)
    with (
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="heartbeat-owner",
        ) as owner,
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        pending = executor.submit(
            refresh_native_sources,
            owner,
            source_roots=roots,
        )
        assert entered.wait(timeout=1)
        try:
            building = next(
                observer.execute(
                    """
                    SELECT owner_pid, phase, heartbeat_at,
                           completed_units, total_units, status
                    FROM cc_search_chats.refresh_run
                    ORDER BY run_id DESC
                    LIMIT 1
                    """
                )
            )
            deadline = time.monotonic() + 1
            heartbeat_advanced = False
            while time.monotonic() < deadline:
                current_heartbeat = next(
                    observer.execute(
                        """
                        SELECT heartbeat_at
                        FROM cc_search_chats.refresh_run
                        ORDER BY run_id DESC
                        LIMIT 1
                        """
                    )
                )[0]
                if current_heartbeat > building[2]:
                    heartbeat_advanced = True
                    break
                time.sleep(0.01)
        finally:
            release.set()
        assert pending.result(timeout=2).message_count == 1
        assert building[:2] == (owner.info.backend_pid, "parse")
        assert building[2] is not None
        assert building[3:] == (0, 1, "building")
        assert heartbeat_advanced
        assert next(
            observer.execute(
                """
                SELECT phase, completed_units, total_units, status
                FROM cc_search_chats.refresh_run
                ORDER BY run_id DESC
                LIMIT 1
                """
            )
        ) == ("done", 1, 1, "complete")


def test_refresh_waiter_reports_the_active_refresh_run_and_owner(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh_module, "_WAIT_HEARTBEAT_SECONDS", 0.02, raising=False)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    (claude_root / "session.jsonl").write_bytes(
        _claude_message_bytes(uuid="owner-source", text="owner sentinel")
    )
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    owner_entered = Event()
    release_owner = Event()
    waiter_reported = Event()
    waiting_events: list[RefreshProgress] = []
    original = refresh_module._parse_and_stage_source

    def paused_owner(connection, plan):
        owner_entered.set()
        if not release_owner.wait(timeout=2):
            raise AssertionError("waiter test did not release active owner")
        return original(connection, plan)

    def waiter_progress(event: RefreshProgress) -> None:
        if getattr(event, "state", None) == "waiting_for_index":
            waiting_events.append(event)
            waiter_reported.set()

    monkeypatch.setattr(refresh_module, "_parse_and_stage_source", paused_owner)
    with (
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="active-refresh-owner",
        ) as owner,
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="active-refresh-waiter",
        ) as waiter,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        owner_result = executor.submit(
            refresh_native_sources,
            owner,
            source_roots=roots,
        )
        assert owner_entered.wait(timeout=1)
        waiter_result = executor.submit(
            refresh_native_sources,
            waiter,
            source_roots=roots,
            progress=waiter_progress,
        )
        reported = waiter_reported.wait(timeout=1)
        try:
            event = waiting_events[0] if waiting_events else None
        finally:
            release_owner.set()
        assert owner_result.result(timeout=2).message_count == 1
        assert waiter_result.result(timeout=2).message_count == 1
        assert reported
        assert event is not None
        assert event.owner_pid == owner.info.backend_pid
        assert isinstance(event.run_id, int)


def test_next_owner_marks_abandoned_building_refresh_failed(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    migrate(postgres_connection)
    abandoned_run = next(
        postgres_connection.execute(
            """
            INSERT INTO cc_search_chats.refresh_run (
                status, source_count, changed_source_count, owner_pid,
                phase, heartbeat_at, completed_units, total_units
            ) VALUES ('building', 1, 1, 999999, 'parse', now(), 0, 1)
            RETURNING run_id
            """
        )
    )[0]
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    (claude_root / "session.jsonl").write_bytes(
        _claude_message_bytes(uuid="recovery-source", text="recovery sentinel")
    )

    refresh_native_sources(
        postgres_connection,
        source_roots=(_source_root(Provider.CLAUDE, claude_root),),
    )

    recovered = next(
        postgres_connection.execute(
            """
            SELECT status, phase, completed_units, total_units, diagnostics
            FROM cc_search_chats.refresh_run
            WHERE run_id = %s
            """,
            (abandoned_run,),
        )
    )
    assert recovered[:4] == ("failed", "done", 1, 1)
    assert recovered[4][-1]["code"] == "abandoned_refresh"


def test_direct_semantic_index_waits_for_the_shared_index_owner(
    postgres_cluster,
) -> None:
    with (
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as blocker,
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="semantic-waiter",
        ) as waiter,
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        migrate(waiter)
        replace_messages(waiter, ())
        blocker.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )
        pending = executor.submit(
            index_embeddings,
            waiter,
            lambda texts: [],
            chunker=lambda texts: (),
        )
        deadline = time.monotonic() + 1
        queued = False
        while time.monotonic() < deadline and not pending.done():
            queued = next(
                observer.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_locks AS lock
                        JOIN pg_stat_activity AS activity USING (pid)
                        WHERE activity.application_name = 'semantic-waiter'
                          AND lock.locktype = 'advisory'
                          AND NOT lock.granted
                    )
                    """
                )
            )[0]
            if queued:
                break
            time.sleep(0.01)
        still_pending = not pending.done()
        blocker.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )

        assert queued
        assert still_pending
        assert pending.result(timeout=1) == 0
