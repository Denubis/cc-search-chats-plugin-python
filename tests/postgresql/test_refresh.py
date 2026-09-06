"""Fixture-root PostgreSQL refresh behavior."""

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from cc_search_chats.cli import _postgres_envelope
from cc_search_chats.core.identity import Provider
from cc_search_chats.providers import codex as codex_module
from cc_search_chats.providers.source_discovery import (
    ConfiguredSourceRoot,
    SourceDiagnostic,
    SourceDiagnosticCode,
    source_root_id,
)
from cc_search_chats.semantic import SemanticChunk
from cc_search_chats.storage.postgresql import (
    RefreshProgress,
    index_corpus,
    migrate,
    semantic_search,
)
from cc_search_chats.storage.postgresql import refresh as refresh_module
from cc_search_chats.storage.postgresql import (
    refresh_native_sources as inspect_native_sources,
)
from cc_search_chats.storage.postgresql import (
    search_messages as _search_messages,
)

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
_INDEX_QUEUE_LOCK = "cc_search_chats.index_queue"


@pytest.fixture(autouse=True)
def _current_schema(postgres_connection: psycopg.Connection) -> None:
    migrate(postgres_connection)


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


def refresh_native_sources(
    connection: psycopg.Connection,
    **kwargs,
):
    """Exercise parser behavior through the coherent publication consumer."""
    vector = [0.0] * 1024
    vector[0] = 1.0
    return index_corpus(
        connection,
        lambda texts: [vector for _ in texts],
        chunker=lambda texts: tuple(
            (SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts
        ),
        **kwargs,
    )


def _source_root(provider: Provider, path: Path) -> ConfiguredSourceRoot:
    resolved = path.resolve()
    return ConfiguredSourceRoot(
        provider=provider,
        path=resolved,
        source_root_id=source_root_id(provider, resolved),
    )


def _claude_message_bytes(
    *, uuid: str, text: str, cwd: str = "/synthetic/repository"
) -> bytes:
    payload = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T00:01:00Z",
        "cwd": cwd,
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
              (SELECT count(*) FROM cc_search_chats.corpus_generation),
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


def test_same_native_content_with_changed_cwd_keeps_earliest_message_and_aliases(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    source = root / "claude-session-primary.jsonl"
    source.write_bytes(
        _claude_message_bytes(
            uuid="replayed-message",
            text="replayed visible content",
            cwd="/synthetic/earliest",
        )
        + _claude_message_bytes(
            uuid="replayed-message",
            text="replayed visible content",
            cwd="/synthetic/later",
        )
    )
    original_reader = refresh_module.read_bounded_jsonl

    def one_record_reader(path: Path, **kwargs):
        return original_reader(path, max_records_per_batch=1, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", one_record_reader)

    refresh_native_sources(
        postgres_connection,
        source_roots=(_source_root(Provider.CLAUDE, root),),
    )

    assert next(
        postgres_connection.execute(
            """
            SELECT count(*), min(cwd)
            FROM cc_search_chats.message_current
            WHERE logical_message_id = 'replayed-message'
              AND content_class = 'prose'
            """
        )
    ) == (1, "/synthetic/earliest")
    assert tuple(
        row[0]
        for row in postgres_connection.execute(
            """
            SELECT record_ordinal
            FROM cc_search_chats.physical_alias_current
            WHERE logical_message_id = 'replayed-message'
              AND content_class = 'prose'
            ORDER BY record_ordinal
            """
        )
    ) == (0, 1)


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
            SELECT current_corpus_generation,
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

    def unexpected_content_read(*_args, **_kwargs):
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

    assert second.corpus_generation == first.corpus_generation
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
    assert reads
    assert reads[0][1] == 0

    reads.clear()
    second = refresh_native_sources(postgres_connection, source_roots=roots)

    assert reads == []
    assert second.corpus_generation == first.corpus_generation
    assert _corpus_cardinality(postgres_connection) == first_cardinality
    assert _refresh_metadata_versions(postgres_connection) == first_metadata_versions

    _append_claude_message(
        source,
        uuid="claude-incremental-append",
        text="incremental suffix sentinel",
    )
    reads.clear()
    appended = refresh_native_sources(postgres_connection, source_roots=roots)

    assert reads
    assert reads[0][1] == committed_size
    assert all(start >= committed_size for _, start, _ in reads)
    assert appended.corpus_generation != first.corpus_generation
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

    assert reads
    assert reads[0] == committed_size
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

    assert starts
    assert starts[0] == committed_size
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

    assert starts
    assert starts[0] == 0
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

    assert starts
    assert starts[0] == 0
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

    assert starts
    assert starts[0] == 0
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

    assert starts
    assert starts[0] == 0
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


def test_native_record_policy_parser_state_versions() -> None:
    assert refresh_module._PARSER_STATE_VERSIONS == {
        Provider.CLAUDE: 3,
        Provider.CODEX: 5,
    }


def test_codex_added_lifecycle_key_indexes_complete_and_searchable(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    codex_root = tmp_path / "codex"
    day = codex_root / "2026" / "08" / "11"
    day.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "codex_modern_primary_145.jsonl",
        day / "rollout-lifecycle.jsonl",
    )

    result = refresh_native_sources(
        postgres_connection,
        source_roots=(_source_root(Provider.CODEX, codex_root),),
    )

    coverage = cast(
        "dict[str, object]",
        _postgres_envelope(
            postgres_connection,
            "index",
            refresh_result=result,
        )["coverage"],
    )
    assert coverage["completeness"] == "complete"
    assert coverage["blocked_files"] == 0
    hits = search_messages(postgres_connection, "modern visible")
    assert hits
    assert {hit.provider for hit in hits} == {"codex"}
    assert (
        search_messages(
            postgres_connection,
            "synthetic lifecycle developer instructions",
        )
        == ()
    )


def test_codex_parser_state_bump_retries_unchanged_deterministic_failure(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_root = tmp_path / "codex"
    day = codex_root / "2026" / "08" / "11"
    day.mkdir(parents=True)
    source = day / "rollout-retry.jsonl"
    shutil.copy(FIXTURES / "codex_modern_primary_145.jsonl", source)
    root = _source_root(Provider.CODEX, codex_root)
    installed_parser_version = refresh_module._PARSER_STATE_VERSIONS[Provider.CODEX]
    new_matcher = codex_module._matches_excluded_keyset

    def old_exact_matcher(
        payload: dict[str, object], required_keysets: set[frozenset[str]]
    ) -> bool:
        return frozenset(payload) in required_keysets

    monkeypatch.setitem(refresh_module._PARSER_STATE_VERSIONS, Provider.CODEX, 3)
    monkeypatch.setattr(codex_module, "_matches_excluded_keyset", old_exact_matcher)
    before = source.stat()

    failed = inspect_native_sources(
        postgres_connection,
        source_roots=(root,),
    )

    assert failed.blocked_source_count == 1
    assert search_messages(postgres_connection, "modern visible") == ()
    assert next(
        postgres_connection.execute(
            """
            SELECT failure_class, failure_code, parser_state_version
            FROM cc_search_chats.source_failure_current
            """
        )
    ) == ("deterministic", "unknown_event", 3)

    monkeypatch.setattr(codex_module, "_matches_excluded_keyset", new_matcher)
    monkeypatch.setitem(
        refresh_module._PARSER_STATE_VERSIONS,
        Provider.CODEX,
        installed_parser_version,
    )
    retried = refresh_native_sources(
        postgres_connection,
        source_roots=(root,),
    )

    after = source.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    coverage = cast(
        "dict[str, object]",
        _postgres_envelope(
            postgres_connection,
            "index",
            refresh_result=retried,
        )["coverage"],
    )
    assert coverage["completeness"] == "complete"
    assert coverage["blocked_files"] == 0
    assert search_messages(postgres_connection, "modern visible")
    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.source_failure_current"
            )
        )[0]
        == 0
    )


@pytest.mark.parametrize(
    ("metadata_type", "failure_code"),
    [
        ("token_usage_record", "unknown_outer_type"),
        ("ghost_snapshot", "unknown_response_item"),
    ],
)
def test_codex_metadata_parser_bump_recovers_unchanged_blocked_source(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_type: str,
    failure_code: str,
) -> None:
    codex_root = tmp_path / "codex"
    day = codex_root / "2026" / "08" / "11"
    day.mkdir(parents=True)
    source = day / "rollout-metadata.jsonl"
    fixture_records = (
        (FIXTURES / "codex_modern_primary_145.jsonl")
        .read_bytes()
        .splitlines(keepends=True)
    )
    before = fixture_records[0] + fixture_records[3]
    after = fixture_records[5]
    counters = {
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 2,
        "reasoning_output_tokens": 0,
        "total_tokens": 12,
    }
    if metadata_type == "token_usage_record":
        payload = {
            "thread_id": "codex-modern-primary",
            "turn_id": "synthetic-turn",
            "session_id": "codex-modern-primary",
            "root_turn_id": "synthetic-turn",
            "response_id": "synthetic-response",
            "usage": counters,
            "turn_token_usage": counters,
            "thread_token_usage": counters,
            "future_detail": "excluded metadata sentinel",
        }
        outer_type = metadata_type
    else:
        payload = {
            "type": "ghost_snapshot",
            "ghost_commit": {
                "id": "synthetic-commit",
                "parent": None,
                "preexisting_untracked_files": ["excluded metadata sentinel"],
                "preexisting_untracked_dirs": [],
            },
        }
        outer_type = "response_item"
    metadata = (
        json.dumps(
            {
                "timestamp": "2026-08-11T03:00:02Z",
                "type": outer_type,
                "payload": payload,
            }
        ).encode("utf-8")
        + b"\n"
    )
    original_bytes = before + metadata + after
    source.write_bytes(original_bytes)
    original_stat = source.stat()
    root = _source_root(Provider.CODEX, codex_root)
    postgres_connection.execute(
        """
        INSERT INTO cc_search_chats.source_root_current (
            source_root_id, provider, resolved_path, configured_order
        ) VALUES (%s, 'codex', %s, 0)
        """,
        (root.source_root_id, str(root.path)),
    )
    postgres_connection.execute(
        """
        INSERT INTO cc_search_chats.source_failure_current (
            source_root_id, source_file_relative, provider,
            file_device, file_inode, observed_size, observed_mtime_ns,
            parser_state_version, failure_record_ordinal,
            failure_source_line, failure_source_byte_offset,
            failure_code, failure_detail, failure_class,
            attempted_content_bytes, consecutive_failures
        ) VALUES (
            %s, '2026/08/11/rollout-metadata.jsonl', 'codex', %s, %s, %s, %s,
            4, 2, 3, %s, %s, 'metadata unrecognized by parser version 4',
            'deterministic', %s, 1
        )
        """,
        (
            root.source_root_id,
            original_stat.st_dev,
            original_stat.st_ino,
            original_stat.st_size,
            original_stat.st_mtime_ns,
            len(before),
            failure_code,
            len(original_bytes),
        ),
    )
    with monkeypatch.context() as previous_parser:
        previous_parser.setitem(
            refresh_module._PARSER_STATE_VERSIONS, Provider.CODEX, 4
        )
        blocked = inspect_native_sources(postgres_connection, source_roots=(root,))
    assert blocked.blocked_source_count == 1
    assert blocked.attempted_content_bytes == 0
    assert search_messages(postgres_connection, "modern visible") == ()

    retried = refresh_native_sources(postgres_connection, source_roots=(root,))

    current_stat = source.stat()
    assert source.read_bytes() == original_bytes
    assert (
        current_stat.st_dev,
        current_stat.st_ino,
        current_stat.st_size,
        current_stat.st_mtime_ns,
    ) == (
        original_stat.st_dev,
        original_stat.st_ino,
        original_stat.st_size,
        original_stat.st_mtime_ns,
    )
    assert retried.attempted_content_bytes == len(original_bytes)
    coverage = _postgres_envelope(postgres_connection, "index", refresh_result=retried)[
        "coverage"
    ]
    assert isinstance(coverage, dict)
    assert coverage["completeness"] == "complete"
    assert coverage["blocked_files"] == 0
    assert coverage["skipped_records"] == 0
    assert {
        hit.text for hit in search_messages(postgres_connection, "modern visible")
    } == {
        "modern visible user",
        "modern visible assistant",
    }
    assert search_messages(postgres_connection, "excluded metadata sentinel") == ()
    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.source_failure_current"
            )
        )[0]
        == 0
    )
    assert next(
        postgres_connection.execute(
            """
            SELECT parser_state_version, complete_byte_offset
            FROM cc_search_chats.source_file_current
            """
        )
    ) == (5, len(original_bytes))


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

    assert failed.corpus_generation == first.corpus_generation
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
        "dict[str, object]",
        _postgres_envelope(postgres_connection, "index")["coverage"],
    )
    assert failed_coverage["read_files"] == 0
    assert failed_coverage["unreadable_files"] == 1
    assert failed_coverage["completeness"] == "partial"
    assert next(
        postgres_connection.execute(
            """
            SELECT failure_class = 'transient' AND retry_after > now()
            FROM cc_search_chats.source_failure_current
            """
        )
    )[0]

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", original_reader)
    deferred = refresh_native_sources(postgres_connection, source_roots=roots)

    assert deferred.attempted_source_count == 0
    assert deferred.transient_failure_source_count == 1
    assert (
        search_messages(
            postgres_connection,
            "unreadable append sentinel",
            include_agents=True,
        )
        == ()
    )

    refresh_native_sources(
        postgres_connection,
        source_roots=roots,
        force_retry=True,
    )

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

    assert failed.corpus_generation == first.corpus_generation
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

    assert failed.corpus_generation == first.corpus_generation
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
    future_bytes = (
        json.dumps(future_record, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    with source.open("ab") as handle:
        handle.write(future_bytes)

    failed = refresh_native_sources(postgres_connection, source_roots=roots)

    assert failed.corpus_generation == first.corpus_generation
    assert failed.failed_source_count == 1
    assert failed.metadata_checked_source_count == 1
    assert failed.attempted_source_count == 1
    assert failed.attempted_content_bytes == len(future_bytes)
    assert failed.blocked_source_count == 1
    assert failed.transient_failure_source_count == 0
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
    failure = next(
        postgres_connection.execute(
            """
            SELECT failure_class, failure_code, file_device, file_inode,
                   observed_size, observed_mtime_ns, parser_state_version,
                   failure_record_ordinal, attempted_content_bytes,
                   consecutive_failures
            FROM cc_search_chats.source_failure_current
            """
        )
    )
    assert failure[0:2] == ("deterministic", "unknown_conversation_record")
    assert failure[2:7] == (
        source.stat().st_dev,
        source.stat().st_ino,
        source.stat().st_size,
        source.stat().st_mtime_ns,
        refresh_module._PARSER_STATE_VERSIONS[Provider.CLAUDE],
    )
    assert failure[7:] == (checkpoint[2], len(future_bytes), 1)
    failed_envelope = _postgres_envelope(postgres_connection, "search")
    failed_coverage = cast("dict[str, object]", failed_envelope["coverage"])
    assert failed_coverage["unrecognized_conversation_records"] == 1
    assert failed_coverage["skipped_records"] == 0
    assert failed_coverage["completeness"] == "partial"
    assert [
        warning["code"]
        for warning in cast("list[dict[str, object]]", failed_envelope["warnings"])
    ] == ["source_refresh_failed"]

    run_count = next(
        postgres_connection.execute("SELECT count(*) FROM cc_search_chats.refresh_run")
    )[0]
    read_starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        read_starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)

    unchanged = refresh_native_sources(postgres_connection, source_roots=roots)

    assert unchanged.corpus_generation == first.corpus_generation
    assert unchanged.changed_source_count == 0
    assert unchanged.metadata_checked_source_count == 1
    assert unchanged.attempted_source_count == 0
    assert unchanged.attempted_content_bytes == 0
    assert unchanged.blocked_source_count == 1
    unchanged_envelope = _postgres_envelope(
        postgres_connection,
        "index",
        refresh_result=unchanged,
    )
    unchanged_coverage = cast(
        "dict[str, object]",
        unchanged_envelope["coverage"],
    )
    unchanged_refresh = cast(
        "dict[str, object]",
        unchanged_envelope["refresh"],
    )
    assert unchanged_coverage["metadata_checked_files"] == 1
    assert unchanged_coverage["content_read_files"] == 0
    assert unchanged_coverage["content_read_bytes"] == 0
    assert unchanged_coverage["read_files"] == 0
    assert unchanged_coverage["blocked_files"] == 1
    assert unchanged_coverage["skipped_records"] == 0
    assert unchanged_coverage["completeness"] == "partial"
    assert unchanged_refresh["run_id"] is None
    assert unchanged_refresh["state"] == "unchanged"
    assert unchanged_refresh["attempted_sources"] == 0
    assert unchanged_refresh["blocked_sources"] == 1
    assert read_starts == []
    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.refresh_run"
            )
        )[0]
        == run_count
    )

    forced = refresh_native_sources(
        postgres_connection,
        source_roots=roots,
        force_retry=True,
    )

    assert forced.failed_source_count == 1
    assert forced.attempted_content_bytes == len(future_bytes)
    assert read_starts == [checkpoint[1]]
    assert (
        next(
            postgres_connection.execute(
                "SELECT consecutive_failures "
                "FROM cc_search_chats.source_failure_current"
            )
        )[0]
        == 2
    )


def test_unstorable_text_is_repaired_counted_and_not_warned(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    source.write_bytes(
        _claude_message_bytes(
            uuid="repaired-nul",
            text="before\x00repaired",
        )
    )

    result = refresh_native_sources(
        postgres_connection,
        source_roots=(_source_root(Provider.CLAUDE, claude_root),),
    )

    assert [row.text for row in search_messages(postgres_connection, "repaired")] == [
        "before\ufffdrepaired"
    ]
    assert (
        next(
            postgres_connection.execute(
                "SELECT skipped_record_count FROM cc_search_chats.source_file_current"
            )
        )[0]
        == 0
    )
    diagnostics = next(
        postgres_connection.execute(
            "SELECT diagnostics FROM cc_search_chats.refresh_run WHERE run_id = %s",
            (result.run_id,),
        )
    )[0]
    repaired = [value for value in diagnostics if value["code"] == "record_repaired"]
    assert len(repaired) == 1
    assert repaired[0]["reason"] == "repaired_unicode"
    envelope = _postgres_envelope(
        postgres_connection,
        "index",
        refresh_result=result,
    )
    coverage = cast("dict[str, object]", envelope["coverage"])
    assert coverage["repaired_records"] == 1
    assert coverage["skipped_records"] == 0
    assert coverage["completeness"] == "complete"
    assert envelope["warnings"] == []


@pytest.mark.parametrize(
    ("reason", "bad_record", "record_limit"),
    [
        ("invalid_encoding", b"\xff\n", None),
        ("malformed_json", b'{"type":\n', None),
        (
            "oversized_record",
            b'{"padding":"' + (b"x" * 1_024) + b'"}\n',
            512,
        ),
    ],
)
def test_first_parse_skips_unstorable_record_and_publishes_neighbors(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    bad_record: bytes,
    record_limit: int | None,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    before = _claude_message_bytes(uuid="before-skip", text="visible before skip")
    after = _claude_message_bytes(uuid="after-skip", text="visible after skip")
    source.write_bytes(before + bad_record + after)
    root = _source_root(Provider.CLAUDE, claude_root)
    stat = source.stat()
    postgres_connection.execute(
        """
        INSERT INTO cc_search_chats.source_root_current (
            source_root_id, provider, resolved_path, configured_order
        ) VALUES (%s, 'claude', %s, 0)
        """,
        (root.source_root_id, str(root.path)),
    )
    postgres_connection.execute(
        """
        INSERT INTO cc_search_chats.source_failure_current (
            source_root_id, source_file_relative, provider,
            file_device, file_inode, observed_size, observed_mtime_ns,
            parser_state_version, failure_record_ordinal,
            failure_source_line, failure_source_byte_offset,
            failure_code, failure_detail, failure_class,
            attempted_content_bytes, consecutive_failures
        ) VALUES (
            %s, 'session.jsonl', 'claude', %s, %s, %s, %s,
            %s, 1, 2, %s, %s, 'previous deterministic failure',
            'deterministic', 0, 1
        )
        """,
        (
            root.source_root_id,
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            refresh_module._PARSER_STATE_VERSIONS[Provider.CLAUDE],
            len(before),
            reason,
        ),
    )
    if record_limit is not None:
        original_reader = refresh_module.read_bounded_jsonl

        def bounded_reader(path: Path, **kwargs):
            return original_reader(
                path,
                max_single_record_bytes=record_limit,
                **kwargs,
            )

        monkeypatch.setattr(refresh_module, "read_bounded_jsonl", bounded_reader)

    result = refresh_native_sources(
        postgres_connection,
        source_roots=(root,),
        force_retry=True,
    )

    assert {row.text for row in search_messages(postgres_connection, "visible")} == {
        "visible after skip",
        "visible before skip",
    }
    assert next(
        postgres_connection.execute(
            """
            SELECT complete_byte_offset, next_record_ordinal, next_source_line,
                   skipped_record_count
            FROM cc_search_chats.source_file_current
            """
        )
    ) == (source.stat().st_size, 3, 4, 1)
    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.source_failure_current"
            )
        )[0]
        == 0
    )
    diagnostics = next(
        postgres_connection.execute(
            "SELECT diagnostics FROM cc_search_chats.refresh_run WHERE run_id = %s",
            (result.run_id,),
        )
    )[0]
    skipped = [value for value in diagnostics if value["code"] == "record_skipped"]
    assert len(skipped) == 1
    expected_detail = {
        "invalid_encoding": "complete record is not valid UTF-8",
        "malformed_json": "complete record is not valid JSON",
        "oversized_record": "complete record exceeds the single-record byte limit",
    }[reason]
    assert skipped == [
        {
            "code": "record_skipped",
            "detail": expected_detail,
            "provider": "claude",
            "reason": reason,
            "record_ordinal": 1,
            "source_byte_offset": len(before),
            "source_file_relative": "session.jsonl",
            "source_line": 2,
            "source_root_id": root.source_root_id,
        }
    ]
    coverage = cast(
        "dict[str, object]",
        _postgres_envelope(
            postgres_connection,
            "index",
            refresh_result=result,
        )["coverage"],
    )
    assert coverage["blocked_files"] == 0
    assert coverage["skipped_records"] == 1
    assert coverage["completeness"] == "complete"
    index_warnings = cast(
        "list[dict[str, object]]",
        _postgres_envelope(
            postgres_connection,
            "index",
            refresh_result=result,
        )["warnings"],
    )
    assert [warning["code"] for warning in index_warnings] == ["record_skipped"]
    assert (
        _postgres_envelope(
            postgres_connection,
            "index",
            include_skipped_warnings=False,
        )["warnings"]
        == []
    )
    for command in ("search", "resolve", "events", "list", "extract"):
        assert _postgres_envelope(postgres_connection, command)["warnings"] == []


@pytest.mark.parametrize(
    ("reason", "bad_record", "record_limit"),
    [
        ("invalid_encoding", b"\xff\n", None),
        ("malformed_json", b'{"type":\n', None),
        (
            "oversized_record",
            b'{"padding":"' + (b"x" * 1_024) + b'"}\n',
            512,
        ),
    ],
)
def test_append_skips_unstorable_record_and_advances_checkpoint(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    bad_record: bytes,
    record_limit: int | None,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    before = _claude_message_bytes(uuid="append-before", text="append before skip")
    source.write_bytes(before)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    first = refresh_native_sources(postgres_connection, source_roots=roots)
    after = _claude_message_bytes(uuid="append-after", text="append after skip")
    with source.open("ab") as handle:
        handle.write(bad_record + after)
    if record_limit is not None:
        original_reader = refresh_module.read_bounded_jsonl

        def bounded_reader(path: Path, **kwargs):
            return original_reader(
                path,
                max_single_record_bytes=record_limit,
                **kwargs,
            )

        monkeypatch.setattr(refresh_module, "read_bounded_jsonl", bounded_reader)

    result = refresh_native_sources(
        postgres_connection,
        source_roots=roots,
        force_retry=True,
    )

    assert result.corpus_generation > first.corpus_generation
    assert {row.text for row in search_messages(postgres_connection, "append")} == {
        "append after skip",
        "append before skip",
    }
    assert next(
        postgres_connection.execute(
            """
            SELECT complete_byte_offset, next_record_ordinal, next_source_line,
                   skipped_record_count
            FROM cc_search_chats.source_file_current
            """
        )
    ) == (source.stat().st_size, 3, 4, 1)
    diagnostics = next(
        postgres_connection.execute(
            "SELECT diagnostics FROM cc_search_chats.refresh_run WHERE run_id = %s",
            (result.run_id,),
        )
    )[0]
    skipped = [value for value in diagnostics if value["code"] == "record_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == reason
    assert skipped[0]["record_ordinal"] == 1
    assert skipped[0]["source_line"] == 2
    assert skipped[0]["source_byte_offset"] == len(before)
    coverage = cast(
        "dict[str, object]",
        _postgres_envelope(
            postgres_connection,
            "index",
            refresh_result=result,
        )["coverage"],
    )
    assert coverage["skipped_records"] == 1
    assert coverage["completeness"] == "complete"


def test_codex_invalid_encoding_record_is_skipped_without_losing_neighbors(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    codex_root = tmp_path / "codex"
    source = codex_root / "2026" / "09" / "02" / "rollout-session.jsonl"
    source.parent.mkdir(parents=True)
    payloads = (
        {
            "timestamp": "2026-09-02T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "cli_version": "1.0.0",
                "cwd": "/synthetic/repository",
                "id": "codex-skip-session",
                "source": "cli",
                "timestamp": "2026-09-02T00:00:00Z",
            },
        },
        {
            "timestamp": "2026-09-02T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex before skip"}],
            },
        },
        {
            "timestamp": "2026-09-02T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "after-skip",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "codex after skip"}],
            },
        },
    )
    encoded = [
        json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        for payload in payloads
    ]
    source.write_bytes(encoded[0] + encoded[1] + b"\xff\n" + encoded[2])

    result = refresh_native_sources(
        postgres_connection,
        source_roots=(_source_root(Provider.CODEX, codex_root),),
    )

    assert {row.text for row in search_messages(postgres_connection, "codex")} == {
        "codex after skip",
        "codex before skip",
    }
    assert (
        next(
            postgres_connection.execute(
                "SELECT skipped_record_count FROM cc_search_chats.source_file_current"
            )
        )[0]
        == 1
    )
    diagnostics = next(
        postgres_connection.execute(
            "SELECT diagnostics FROM cc_search_chats.refresh_run WHERE run_id = %s",
            (result.run_id,),
        )
    )[0]
    skipped = [value for value in diagnostics if value["code"] == "record_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["provider"] == "codex"
    assert skipped[0]["reason"] == "invalid_encoding"
    assert skipped[0]["record_ordinal"] == 2
    assert skipped[0]["source_line"] == 3


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
    original_publish = refresh_module._publish_coherent_candidate

    def fail_before_publish(*_args, **_kwargs):
        raise RuntimeError("injected pre-publication crash")

    monkeypatch.setattr(
        refresh_module, "_publish_coherent_candidate", fail_before_publish
    )

    with pytest.raises(RuntimeError, match="injected pre-publication crash"):
        refresh_native_sources(postgres_connection, source_roots=roots)

    assert _corpus_cardinality(postgres_connection) == (
        cardinality[0] + 1,
        *cardinality[1:],
    )
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

    monkeypatch.setattr(refresh_module, "_publish_coherent_candidate", original_publish)
    retried = refresh_native_sources(postgres_connection, source_roots=roots)

    assert retried.corpus_generation != first.corpus_generation
    assert len(search_messages(postgres_connection, "crash retry sentinel")) == 1


def test_semantic_failure_keeps_the_previous_coherent_corpus_until_retry(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    vector = [0.0] * 1024
    vector[0] = 1.0

    def embed(texts):
        return [vector for _ in texts]

    def chunks(texts):
        return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)

    index_corpus = refresh_module.index_corpus
    first = index_corpus(
        postgres_connection,
        embed,
        chunker=chunks,
        source_roots=roots,
    )
    selected_before = next(
        postgres_connection.execute(
            """
            SELECT state.current_corpus_generation, generation.semantic_build,
                   (SELECT count(*)
                    FROM cc_search_chats.semantic_chunk_current),
                   (SELECT complete_byte_offset
                    FROM cc_search_chats.source_file_current)
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            """
        )
    )
    unchanged_row_versions = next(
        postgres_connection.execute(
            """
            SELECT message.canonical_locator, message.xmin::text,
                   chunk.xmin::text
            FROM cc_search_chats.message_current AS message
            JOIN cc_search_chats.semantic_chunk_current AS chunk
              USING (provider, source_session_id,
                     logical_message_id, content_class)
            ORDER BY message.canonical_locator, chunk.chunk_ordinal
            LIMIT 1
            """
        )
    )
    assert first.corpus_generation == selected_before[0]
    _append_claude_message(
        source,
        uuid="coherent-semantic-retry",
        text="coherent semantic retry sentinel",
    )

    def fail_semantic(_texts):
        raise RuntimeError("injected semantic candidate failure")

    with pytest.raises(RuntimeError, match="injected semantic candidate failure"):
        index_corpus(
            postgres_connection,
            fail_semantic,
            chunker=chunks,
            source_roots=roots,
        )

    assert (
        next(
            postgres_connection.execute(
                """
            SELECT state.current_corpus_generation, generation.semantic_build,
                   (SELECT count(*)
                    FROM cc_search_chats.semantic_chunk_current),
                   (SELECT complete_byte_offset
                    FROM cc_search_chats.source_file_current)
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            """
            )
        )
        == selected_before
    )
    assert (
        search_messages(postgres_connection, "coherent semantic retry sentinel") == ()
    )

    completed = index_corpus(
        postgres_connection,
        embed,
        chunker=chunks,
        source_roots=roots,
    )

    assert completed.corpus_generation != first.corpus_generation
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT message.canonical_locator, message.xmin::text,
                   chunk.xmin::text
            FROM cc_search_chats.message_current AS message
            JOIN cc_search_chats.semantic_chunk_current AS chunk
              USING (provider, source_session_id,
                     logical_message_id, content_class)
            WHERE message.canonical_locator = %s
            ORDER BY chunk.chunk_ordinal
            LIMIT 1
            """,
                (unchanged_row_versions[0],),
            )
        )
        == unchanged_row_versions
    )
    literal = search_messages(postgres_connection, "coherent semantic retry sentinel")
    assert len(literal) == 1
    semantic = semantic_search(
        postgres_connection,
        vector,
        limit=200,
        include_agents=True,
    )
    assert any(
        hit.canonical_locator == literal[0].canonical_locator for hit in semantic
    )


def test_semantic_candidate_remains_invisible_while_embedding_is_paused(
    postgres_connection: psycopg.Connection,
    postgres_cluster,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    vector = [0.0] * 1024
    vector[0] = 1.0

    def chunks(texts):
        return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)

    baseline = refresh_module.index_corpus(
        postgres_connection,
        lambda texts: [vector for _ in texts],
        chunker=chunks,
        source_roots=roots,
    )
    _append_claude_message(
        source,
        uuid="coherent-paused-candidate",
        text="coherent paused candidate sentinel",
    )
    entered = Event()
    release = Event()

    def paused_embed(texts):
        entered.set()
        if not release.wait(timeout=2):
            raise AssertionError("paused candidate was not released")
        return [vector for _ in texts]

    with (
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as owner,
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        pending = executor.submit(
            refresh_module.index_corpus,
            owner,
            paused_embed,
            chunker=chunks,
            source_roots=roots,
        )
        assert entered.wait(timeout=1)
        try:
            selected_during_pause = next(
                observer.execute(
                    """
                    SELECT state.current_corpus_generation,
                           generation.semantic_build
                    FROM cc_search_chats.corpus_state AS state
                    JOIN cc_search_chats.corpus_generation AS generation
                      ON generation.corpus_generation =
                         state.current_corpus_generation
                    """
                )
            )
            assert selected_during_pause == (
                baseline.corpus_generation,
                baseline.semantic_build,
            )
            assert search_messages(observer, "coherent paused candidate sentinel") == ()
            assert semantic_search(
                observer,
                vector,
                limit=200,
                include_agents=True,
            )
        finally:
            release.set()
        completed = pending.result(timeout=2)

        assert completed.corpus_generation != baseline.corpus_generation
        literal = search_messages(observer, "coherent paused candidate sentinel")
        assert len(literal) == 1
        assert any(
            hit.canonical_locator == literal[0].canonical_locator
            for hit in semantic_search(
                observer,
                vector,
                limit=200,
                include_agents=True,
            )
        )


@pytest.mark.parametrize(
    ("relation", "events", "condition"),
    [
        ("message_current", "INSERT", ""),
        ("physical_alias_current", "INSERT", ""),
        ("source_file_current", "INSERT OR UPDATE", ""),
        ("semantic_chunk_current", "INSERT", ""),
        ("semantic_build", "UPDATE", "WHEN (NEW.status = 'complete')"),
        ("corpus_generation", "UPDATE", "WHEN (NEW.status = 'complete')"),
        ("corpus_state", "UPDATE", ""),
        (
            "refresh_run",
            "UPDATE",
            "WHEN (NEW.status IN ('complete', 'partial'))",
        ),
    ],
)
def test_each_final_publication_mutation_rolls_back_as_one_unit(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    relation: str,
    events: LiteralString,
    condition: LiteralString,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    vector = [0.0] * 1024
    vector[0] = 1.0

    def chunks(texts):
        return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)

    refresh_module.index_corpus(
        postgres_connection,
        lambda texts: [vector for _ in texts],
        chunker=chunks,
        source_roots=roots,
    )
    selected_before = next(
        postgres_connection.execute(
            """
            SELECT state.current_corpus_generation,
                   generation.semantic_build,
                   (SELECT count(*)
                    FROM cc_search_chats.message_current),
                   (SELECT count(*)
                    FROM cc_search_chats.physical_alias_current),
                   (SELECT count(*)
                    FROM cc_search_chats.semantic_chunk_current),
                   (SELECT complete_byte_offset
                    FROM cc_search_chats.source_file_current)
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            """
        )
    )
    _append_claude_message(
        source,
        uuid=f"rollback-{relation}",
        text=f"rollback {relation} sentinel",
    )
    postgres_connection.execute(
        """
        CREATE FUNCTION cc_search_chats.inject_publication_failure()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'injected publication failure';
        END;
        $$
        """
    )
    postgres_connection.execute(
        sql.SQL(
            "CREATE TRIGGER injected_publication_failure "
            "BEFORE {events} ON cc_search_chats.{relation} "
            "FOR EACH ROW {condition} "
            "EXECUTE FUNCTION cc_search_chats.inject_publication_failure()"
        ).format(
            events=sql.SQL(events),
            relation=sql.Identifier(relation),
            condition=sql.SQL(condition),
        )
    )

    with pytest.raises(
        psycopg.errors.RaiseException,
        match="injected publication failure",
    ):
        refresh_module.index_corpus(
            postgres_connection,
            lambda texts: [vector for _ in texts],
            chunker=chunks,
            source_roots=roots,
        )

    assert (
        next(
            postgres_connection.execute(
                """
            SELECT state.current_corpus_generation,
                   generation.semantic_build,
                   (SELECT count(*)
                    FROM cc_search_chats.message_current),
                   (SELECT count(*)
                    FROM cc_search_chats.physical_alias_current),
                   (SELECT count(*)
                    FROM cc_search_chats.semantic_chunk_current),
                   (SELECT complete_byte_offset
                    FROM cc_search_chats.source_file_current)
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            """
            )
        )
        == selected_before
    )
    assert search_messages(postgres_connection, f"rollback {relation} sentinel") == ()
    assert next(
        postgres_connection.execute(
            """
            SELECT generation.status, build.status
            FROM cc_search_chats.corpus_generation AS generation
            JOIN cc_search_chats.semantic_build AS build
              ON build.corpus_generation = generation.corpus_generation
            ORDER BY generation.corpus_generation DESC
            LIMIT 1
            """
        )
    ) == ("failed", "failed")


def test_coherent_noop_reuses_selection_but_missing_selection_forces_publication(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    vector = [0.0] * 1024
    vector[0] = 1.0
    embedded_texts: list[str] = []

    def embed(texts):
        embedded_texts.extend(texts)
        return [vector for _ in texts]

    def chunks(texts):
        return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)

    first = refresh_module.index_corpus(
        postgres_connection,
        embed,
        chunker=chunks,
        source_roots=roots,
    )
    cardinality = next(
        postgres_connection.execute(
            """
            SELECT
              (SELECT count(*) FROM cc_search_chats.corpus_generation),
              (SELECT count(*) FROM cc_search_chats.semantic_build),
              (SELECT count(*) FROM cc_search_chats.embedding_value),
              (SELECT count(*) FROM cc_search_chats.semantic_chunk_current)
            """
        )
    )
    embedded_texts.clear()

    unchanged = refresh_module.index_corpus(
        postgres_connection,
        embed,
        chunker=chunks,
        source_roots=roots,
    )

    assert unchanged.corpus_generation == first.corpus_generation
    assert unchanged.semantic_build == first.semantic_build
    assert embedded_texts == []
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT
              (SELECT count(*) FROM cc_search_chats.corpus_generation),
              (SELECT count(*) FROM cc_search_chats.semantic_build),
              (SELECT count(*) FROM cc_search_chats.embedding_value),
              (SELECT count(*) FROM cc_search_chats.semantic_chunk_current)
            """
            )
        )
        == cardinality
    )

    postgres_connection.execute(
        "UPDATE cc_search_chats.corpus_state SET current_corpus_generation = NULL"
    )
    recovered = refresh_module.index_corpus(
        postgres_connection,
        embed,
        chunker=chunks,
        source_roots=roots,
    )

    assert recovered.corpus_generation != first.corpus_generation
    assert recovered.semantic_build != first.semantic_build
    assert embedded_texts == []
    assert next(
        postgres_connection.execute(
            """
            SELECT
              (SELECT count(*) FROM cc_search_chats.corpus_generation),
              (SELECT count(*) FROM cc_search_chats.semantic_build),
              (SELECT count(*) FROM cc_search_chats.embedding_value),
              (SELECT count(*) FROM cc_search_chats.semantic_chunk_current)
            """
        )
    ) == (cardinality[0] + 1, cardinality[1] + 1, *cardinality[2:])


def test_literal_candidate_diagnostic_cannot_advance_selected_state(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    shutil.copy(FIXTURES / "claude_primary.jsonl", source)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    vector = [0.0] * 1024
    vector[0] = 1.0

    def chunks(texts):
        return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)

    baseline = refresh_module.index_corpus(
        postgres_connection,
        lambda texts: [vector for _ in texts],
        chunker=chunks,
        source_roots=roots,
    )
    current_before = next(
        postgres_connection.execute(
            """
            SELECT state.current_corpus_generation,
                   generation.semantic_build,
                   (SELECT count(*)
                    FROM cc_search_chats.message_current),
                   (SELECT count(*)
                    FROM cc_search_chats.semantic_chunk_current),
                   (SELECT complete_byte_offset
                    FROM cc_search_chats.source_file_current)
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            """
        )
    )
    _append_claude_message(
        source,
        uuid="literal-candidate-only",
        text="literal candidate only sentinel",
    )

    diagnostic = inspect_native_sources(postgres_connection, source_roots=roots)

    assert diagnostic.corpus_generation == baseline.corpus_generation
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT state.current_corpus_generation,
                   generation.semantic_build,
                   (SELECT count(*)
                    FROM cc_search_chats.message_current),
                   (SELECT count(*)
                    FROM cc_search_chats.semantic_chunk_current),
                   (SELECT complete_byte_offset
                    FROM cc_search_chats.source_file_current)
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            """
            )
        )
        == current_before
    )
    assert search_messages(postgres_connection, "literal candidate only sentinel") == ()
    assert next(
        postgres_connection.execute(
            """
            SELECT diagnostics -> -1 ->> 'code'
            FROM cc_search_chats.refresh_run
            ORDER BY run_id DESC
            LIMIT 1
            """
        )
    ) == ("literal_candidate_not_published",)


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
        blocker.execute(
            "SELECT pg_notify(%s, %s)",
            (refresh_module.INDEX_NOTIFY_CHANNEL, "released"),
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


def test_composed_index_waits_for_the_shared_index_owner(
    postgres_cluster,
) -> None:
    with (
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as blocker,
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="semantic-waiter",
        ) as waiter,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        migrate(waiter)
        blocker.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )
        waiting = Event()

        def progress(event: RefreshProgress) -> None:
            if event.state == "waiting_for_index":
                waiting.set()

        pending = executor.submit(
            index_corpus,
            waiter,
            lambda _texts: [],
            chunker=lambda _texts: (),
            source_roots=(),
            progress=progress,
        )
        reported_wait = waiting.wait(timeout=1)
        still_pending = not pending.done()
        blocker.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (_INDEX_QUEUE_LOCK,),
        )

        assert reported_wait
        assert still_pending
        assert pending.result(timeout=6).embedding_count == 0
