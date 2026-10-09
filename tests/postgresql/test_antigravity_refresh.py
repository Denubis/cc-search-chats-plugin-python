"""Antigravity primary sessions: admission, quiet exclusions, identity, content."""

import hashlib
import json
import shutil
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, cast

import pytest
from tests.conftest import (
    ANTIGRAVITY_SESSION_IDS,
    antigravity_transcript,
    build_antigravity_root,
)

from cc_search_chats.cli import _postgres_envelope
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
    unindexed_sources,
)
from cc_search_chats.storage.postgresql import refresh as refresh_module

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.postgresql


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def _embed(texts):
    vector = [0.0] * 1024
    vector[0] = 1.0
    return [vector for _ in texts]


def _root(path: Path) -> ConfiguredSourceRoot:
    resolved = path.resolve()
    return ConfiguredSourceRoot(
        provider=Provider.ANTIGRAVITY,
        path=resolved,
        source_root_id=source_root_id(Provider.ANTIGRAVITY, resolved),
    )


def _index(connection: psycopg.Connection, root: ConfiguredSourceRoot):
    return index_corpus(
        connection, _embed, chunker=_single_chunks, source_roots=(root,)
    )


def _checkpoints(connection: psycopg.Connection) -> dict[str, dict[str, object]]:
    return {
        Path(relative).parts[0]: json.loads(row)
        for relative, row in connection.execute(
            """
            SELECT source_file_relative, row_to_json(source)::text
            FROM cc_search_chats.source_file_current AS source
            ORDER BY source_file_relative
            """
        )
    }


def _failures(connection: psycopg.Connection) -> dict[str, tuple[str, str]]:
    return {
        Path(relative).parts[0]: (failure_class, failure_code)
        for relative, failure_class, failure_code in connection.execute(
            """
            SELECT source_file_relative, failure_class, failure_code
            FROM cc_search_chats.source_failure_current
            """
        )
    }


def _found_anywhere(connection: psycopg.Connection, phrase: str) -> bool:
    return any(
        search_messages(connection, phrase, include_agents=agents, include_tools=tools)
        for agents in (False, True)
        for tools in (False, True)
    )


def _envelope(connection: psycopg.Connection, root: ConfiguredSourceRoot, command: str):
    return _postgres_envelope(
        connection,
        command,
        index_state_roots=(root,),
        index_state_deadline=monotonic() + 30,
    )


def _record(text: str, created_at: str) -> bytes:
    return (
        json.dumps(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": created_at,
                "status": "DONE",
                "step_index": 9,
                "content": f"<USER_REQUEST>{text}</USER_REQUEST>",
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


@pytest.fixture(autouse=True)
def _current_schema(postgres_connection: psycopg.Connection) -> None:
    migrate(postgres_connection)


def test_ac1_admission_indexes_only_october_human_sessions(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    root = _root(
        build_antigravity_root(
            tmp_path / "brain",
            "october_human",
            "pre_october",
            "boundary_admitted",
            "system_first",
            "unknown_first",
        )
    )

    result = _index(postgres_connection, root)

    rows = set(
        postgres_connection.execute(
            """
            SELECT DISTINCT provider, session_kind, source_session_id, cwd
            FROM cc_search_chats.message_current
            """
        )
    )
    assert rows == {
        (
            "antigravity",
            "primary",
            ANTIGRAVITY_SESSION_IDS["october_human"],
            "/synthetic/orchard",
        ),
        ("antigravity", "primary", ANTIGRAVITY_SESSION_IDS["boundary_admitted"], None),
    }
    checkpoints = _checkpoints(postgres_connection)
    for name, code in (
        ("pre_october", "antigravity_before_scope_start"),
        ("system_first", "antigravity_not_human_initiated"),
    ):
        row = checkpoints[ANTIGRAVITY_SESSION_IDS[name]]
        size = antigravity_transcript(root.path, name).stat().st_size
        assert row["source_status"] == "excluded"
        assert cast("dict[str, object]", row["parser_state"])["excluded_code"] == code
        assert (row["complete_byte_offset"], row["pending_bytes"]) == (size, 0)
    assert _failures(postgres_connection) == {
        ANTIGRAVITY_SESSION_IDS["unknown_first"]: (
            "deterministic",
            "antigravity_unrecognised_first_record",
        )
    }
    assert result.blocked_source_count == 1
    assert not _found_anywhere(postgres_connection, "unknown-first-decoy-service")
    assert not _found_anywhere(postgres_connection, "pre-october-decoy-bullace")
    assert not _found_anywhere(postgres_connection, "system-first-decoy-rowan")
    assert search_messages(postgres_connection, "boundary admitted mulberry")
    assert search_messages(postgres_connection, "orchard ledger")


def test_ac2_excluded_sources_never_loop(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    root = _root(
        build_antigravity_root(
            tmp_path / "brain", "october_human", "pre_october", "system_first"
        )
    )
    first = _index(postgres_connection, root)
    baseline = _checkpoints(postgres_connection)
    excluded_ids = {
        ANTIGRAVITY_SESSION_IDS["pre_october"],
        ANTIGRAVITY_SESSION_IDS["system_first"],
    }
    assert {
        name for name, row in baseline.items() if row["source_status"] == "excluded"
    } == excluded_ids

    for _ in range(3):
        again = _index(postgres_connection, root)
        assert again.corpus_generation == first.corpus_generation
        assert (
            again.changed_source_count,
            again.read_source_count,
            again.attempted_content_bytes,
            again.failed_source_count,
        ) == (0, 0, 0, 0)
        assert _checkpoints(postgres_connection) == baseline
        counts, reason = unindexed_sources(
            postgres_connection, (root,), deadline_monotonic=monotonic() + 30
        )
        assert reason is None
        assert counts is not None
        assert (counts.files, counts.bytes) == (0, 0)
        envelope = _envelope(postgres_connection, root, "search")
        coverage = cast("dict[str, object]", envelope["coverage"])
        refresh = cast("dict[str, object]", envelope["refresh"])
        state = cast("dict[str, object]", envelope["index_state"])
        assert coverage["pending_tail_files"] == 0
        assert coverage["excluded_files"] == 2
        assert coverage["source_issues"] == {
            "retry_after_parser_update": 0,
            "retryable_failures": 0,
            "needs_attention": 0,
        }
        assert refresh["pending_bytes"] == 0
        assert state["unindexed"] == {"files": 0, "directories": 0, "bytes": 0}
        assert state["freshness"] == "no_source_changes"

    excluded_path = antigravity_transcript(root.path, "pre_october")
    with excluded_path.open("ab") as stream:
        stream.write(_record("appended to an excluded session", "2026-10-09T00:00:00Z"))
    grown = _index(postgres_connection, root)
    assert grown.corpus_generation == first.corpus_generation
    assert (grown.changed_source_count, grown.read_source_count) == (0, 0)
    assert _checkpoints(postgres_connection) == baseline
    counts, _reason = unindexed_sources(
        postgres_connection, (root,), deadline_monotonic=monotonic() + 30
    )
    assert counts is not None
    assert (counts.files, counts.bytes) == (0, 0)
    assert not _found_anywhere(postgres_connection, "appended to an excluded session")

    excluded_path.unlink()
    shutil.copy(antigravity_transcript(root.path, "october_human"), excluded_path)
    replaced = _index(postgres_connection, root)
    assert replaced.corpus_generation > first.corpus_generation
    assert replaced.read_source_count == 1
    assert (
        _checkpoints(postgres_connection)[ANTIGRAVITY_SESSION_IDS["pre_october"]][
            "source_status"
        ]
        == "indexed"
    )
    assert {
        hit.source_session_id
        for hit in search_messages(postgres_connection, "orchard ledger")
    } == {
        ANTIGRAVITY_SESSION_IDS["october_human"],
        ANTIGRAVITY_SESSION_IDS["pre_october"],
    }


def test_ac3_append_is_incremental_and_locators_stay_exact(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(
        build_antigravity_root(tmp_path / "brain", "october_human", "pre_october")
    )
    _index(postgres_connection, root)
    transcript = antigravity_transcript(root.path, "october_human")
    watermark = transcript.stat().st_size
    earlier = search_messages(postgres_connection, "orchard ledger")[
        0
    ].canonical_locator

    starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)
    appended = _record("appended persimmon request", "2026-10-03T09:30:00Z")
    with transcript.open("ab") as stream:
        stream.write(appended)
    result = _index(postgres_connection, root)
    assert starts == [watermark]
    assert result.attempted_content_bytes == len(appended)
    assert result.read_source_count == 1
    new_hit = search_messages(postgres_connection, "appended persimmon request")[0]
    assert new_hit.conversation_epoch == 1
    resolved = resolve_exact_messages(
        postgres_connection, (earlier, new_hit.canonical_locator), source_roots=(root,)
    )
    assert [value.status for value in resolved] == [
        ResolutionStatus.RESOLVED,
        ResolutionStatus.RESOLVED,
    ]

    raw = transcript.read_bytes()
    edited = raw.replace(
        b"orchard ledger</USER_REQUEST>", b"orchard LEDGER</USER_REQUEST>", 1
    )
    assert edited != raw
    assert len(edited) == len(raw)
    transcript.write_bytes(edited)
    stale = resolve_exact_messages(
        postgres_connection, (earlier,), source_roots=(root,)
    )[0]
    assert stale.status is ResolutionStatus.STALE_SOURCE

    excluded_first_line = (
        antigravity_transcript(root.path, "pre_october").read_bytes().split(b"\n")[0]
    )
    excluded_locator = (
        f"ccchat:v1:antigravity:{ANTIGRAVITY_SESSION_IDS['pre_october']}"
        f":ordinal:0:sha256:{hashlib.sha256(excluded_first_line).hexdigest()}"
    )
    no_match = resolve_exact_messages(
        postgres_connection, (excluded_locator,), source_roots=(root,)
    )[0]
    assert no_match.status is ResolutionStatus.NO_MATCH


def test_ac4_content_boundary_and_unregistered_pair(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    root = _root(
        build_antigravity_root(tmp_path / "brain", "october_human", "unregistered_pair")
    )
    result = _index(postgres_connection, root)

    assert search_messages(postgres_connection, "orchard ledger")
    assert search_messages(postgres_connection, "ls orchard") == ()
    assert search_messages(postgres_connection, "ls orchard", include_tools=True)
    assert search_messages(postgres_connection, "run_command", include_tools=True)
    apricot = search_messages(postgres_connection, "apricot rows")
    assert {hit.conversation_epoch for hit in apricot} == {1}
    assert any("<truncated 12 bytes>" in hit.text for hit in apricot)
    for decoy in (
        "thinking-decoy-quince",
        "generic-decoy-damson",
        "metadata-decoy-pear",
        "settings-decoy-plum",
        "system-decoy-medlar",
        "checkpoint-decoy-sloe",
        "unregistered-decoy-elder",
        "unregistered pair opening juniper",
    ):
        assert not _found_anywhere(postgres_connection, decoy), decoy

    envelope = _envelope(postgres_connection, root, "index")
    coverage = cast("dict[str, object]", envelope["coverage"])
    assert coverage["repaired_records"] == 1
    assert coverage["source_issues"] == {
        "retry_after_parser_update": 0,
        "retryable_failures": 0,
        "needs_attention": 1,
    }
    assert coverage["completeness"] == "partial"
    assert result.blocked_source_count == 1
    assert _failures(postgres_connection) == {
        ANTIGRAVITY_SESSION_IDS["unregistered_pair"]: (
            "deterministic",
            "antigravity_unknown_record_pair",
        )
    }


def test_ac5_root_and_session_decoys_are_silent_and_unsearchable(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    root = _root(build_antigravity_root(tmp_path / "brain", "october_human"))
    assert (root.path / "antigravity-oauth-token").stat().st_mode & 0o777 == 0

    result = _index(postgres_connection, root)

    assert (result.failed_source_count, result.source_count) == (0, 1)
    envelope = _envelope(postgres_connection, root, "index")
    coverage = cast("dict[str, object]", envelope["coverage"])
    assert coverage["completeness"] == "complete"
    assert coverage["discovered_files"] == 1
    assert envelope["warnings"] == []
    for decoy in (
        "sibling-decoy-greengage",
        "notes-decoy-hawthorn",
        "messages-decoy-cherry",
        "steps-decoy-fig",
        "scratch-decoy-loquat",
        "settings-decoy-yew",
        "token-decoy-holly",
    ):
        assert not _found_anywhere(postgres_connection, decoy), decoy


def _planner_with_cwd(cwd: str, created_at: str) -> bytes:
    return (
        json.dumps(
            {
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": created_at,
                "status": "DONE",
                "step_index": 9,
                "content": "running the tail command",
                "tool_calls": [{"name": "run_command", "args": {"Cwd": cwd}}],
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _embedding_value_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute("SELECT count(*) FROM cc_search_chats.embedding_value")
    )[0]


def _embedding_digests(connection: psycopg.Connection) -> dict[str, str]:
    return dict(
        connection.execute(
            """
            SELECT canonical_locator, embedding_input_digest
            FROM cc_search_chats.message_current
            """
        )
    )


def test_ac6_tail_establishing_cwd_reparses_once_without_changing_identity(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(
        build_antigravity_root(
            tmp_path / "brain", "boundary_admitted", "october_human", "pre_october"
        )
    )
    _index(postgres_connection, root)
    session = ANTIGRAVITY_SESSION_IDS["boundary_admitted"]
    before = {
        hit.canonical_locator: hit.cwd
        for hit in search_messages(postgres_connection, "mulberry")
    }
    assert set(before.values()) == {None}
    assert (
        search_messages(postgres_connection, "mulberry", project="/synthetic/tail")
        == ()
    )
    embeddings_before = _embedding_value_count(postgres_connection)
    digests_before = _embedding_digests(postgres_connection)
    baseline_checkpoints = _checkpoints(postgres_connection)

    starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)
    transcript = antigravity_transcript(root.path, "boundary_admitted")
    watermark = transcript.stat().st_size
    with transcript.open("ab") as stream:
        stream.write(_planner_with_cwd("/synthetic/tail", "2026-10-01T00:05:00Z"))
    result = _index(postgres_connection, root)

    assert starts[:2] == [watermark, 0]
    assert result.read_source_count == 1
    assert result.attempted_content_bytes == transcript.stat().st_size
    after = {
        hit.canonical_locator: hit.cwd
        for hit in search_messages(postgres_connection, "mulberry")
    }
    assert set(after) == set(before)
    assert set(after.values()) == {"/synthetic/tail"}
    tail_rows = search_messages(
        postgres_connection, "running the tail command", project="/synthetic/tail"
    )
    assert [hit.source_session_id for hit in tail_rows] == [session]
    digests_after = _embedding_digests(postgres_connection)
    assert {k: digests_after[k] for k in digests_before} == digests_before
    assert len(digests_after) == len(digests_before) + 1
    assert _embedding_value_count(postgres_connection) == embeddings_before + 1
    checkpoints = _checkpoints(postgres_connection)
    assert (
        checkpoints[ANTIGRAVITY_SESSION_IDS["pre_october"]]
        == baseline_checkpoints[ANTIGRAVITY_SESSION_IDS["pre_october"]]
    )
    assert (
        checkpoints[ANTIGRAVITY_SESSION_IDS["october_human"]]
        == baseline_checkpoints[ANTIGRAVITY_SESSION_IDS["october_human"]]
    )
    assert cast("dict[str, object]", checkpoints[session]["parser_state"])["cwd"] == (
        "/synthetic/tail"
    )

    orchard = search_messages(postgres_connection, "orchard ledger")
    assert {hit.cwd for hit in orchard} == {"/synthetic/orchard"}
    assert search_messages(
        postgres_connection, "orchard ledger", project="/synthetic/orchard"
    )


def test_ac6_small_batches_stamp_the_first_row(
    postgres_connection: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(build_antigravity_root(tmp_path / "brain", "october_human"))
    original_reader = refresh_module.read_bounded_jsonl

    def one_record_reader(path: Path, **kwargs):
        kwargs["max_records_per_batch"] = 1
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", one_record_reader)
    _index(postgres_connection, root)

    rows = tuple(
        postgres_connection.execute(
            """
            SELECT alias.record_ordinal, message.cwd
            FROM cc_search_chats.message_current AS message
            JOIN cc_search_chats.physical_alias_current AS alias
              USING (provider, source_session_id, logical_message_id, content_class)
            WHERE message.content_class = 'prose'
            ORDER BY alias.record_ordinal
            """
        )
    )
    assert next(ordinal for ordinal, _cwd in rows) == 0
    assert {cwd for _ordinal, cwd in rows} == {"/synthetic/orchard"}
