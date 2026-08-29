"""One PostgreSQL CLI journey across every migrated command."""

import json
import shutil
import sys
from io import StringIO
from pathlib import Path
from typing import cast

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


def _assert_v2_envelope(
    payload: dict[str, object],
    command: str,
    *,
    status: str = "complete",
) -> None:
    assert payload["schema_version"] == 2
    assert payload["command"] == command
    assert payload["status"] == status
    assert isinstance(payload["coverage"], dict)
    assert set(payload["coverage"]) >= {
        "configured_root_count",
        "resolved_root_count",
        "roots",
        "repositories",
        "discovered_files",
        "read_files",
        "indexed_files",
        "skipped_files",
        "excluded_files",
        "unreadable_files",
        "unknown_sessions",
        "unrecognized_conversation_records",
        "source_watermarks",
        "completeness",
    }
    assert isinstance(payload["refresh"], dict)
    assert isinstance(payload["semantic"], dict)
    assert isinstance(payload["warnings"], list)


def _assert_message_identity(message: dict[str, object]) -> None:
    identity = message["identity"]
    assert isinstance(identity, dict)
    identity = cast(dict[str, object], identity)
    assert set(identity) == {
        "provider",
        "source_session_id",
        "logical_message_id",
        "canonical_locator",
        "physical_aliases",
    }
    aliases = identity["physical_aliases"]
    assert isinstance(aliases, list) and aliases
    alias = cast(dict[str, object], aliases[0])
    assert set(alias) == {
        "locator",
        "source_file_relative",
        "record_ordinal",
        "source_line",
        "source_byte_offset",
        "raw_byte_length",
        "source_digest",
    }


def _progress_events(stderr: str) -> list[dict[str, object]]:
    events = [json.loads(line) for line in stderr.splitlines() if line.strip()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert all(
        set(event)
        == {
            "schema_version",
            "sequence",
            "event",
            "run_id",
            "phase",
            "state",
            "elapsed_ms",
            "completed_units",
            "total_units",
            "owner",
            "fts_revision",
            "semantic_revision",
            "source_watermark",
            "deadline_ms",
            "retrieval_mode",
            "indexed_at",
            "stale_reasons",
            "background_refresh",
            "warning",
            "error",
            "coverage",
            "refresh",
            "semantic",
        }
        for event in events
    )
    assert sum(event["event"] == "terminal" for event in events) == 1
    assert events[-1]["event"] == "terminal"
    return events


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def test_postgresql_cli_journey_with_events(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda args: None
    )
    monkeypatch.setattr(
        "cc_search_chats.cli._start_systemd_refresh", lambda timeout_seconds: None
    )
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_day = codex_root / "2026" / "08" / "11"
    codex_day.mkdir(parents=True)
    claude_source = Path(
        shutil.copy(
            FIXTURES / "claude_primary.jsonl",
            claude_root / "claude-session-primary.jsonl",
        )
    )
    claude_agent_dir = claude_root / "parent-session" / "subagents"
    claude_agent_dir.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "claude_agent.jsonl",
        claude_agent_dir / "claude-agent-session.jsonl",
    )
    shutil.copy(
        FIXTURES / "codex_modern_primary_145.jsonl",
        codex_day / "rollout-modern.jsonl",
    )
    shutil.copy(
        FIXTURES / "claude_unknown_origin.jsonl",
        claude_root / "claude-session-unknown.jsonl",
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

    code, migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0
    assert json.loads(migrated.out)["applied_schema_version"] == 6
    code, indexed = _run(monkeypatch, capsys, "index", "--literal-only", "--json")
    assert code == 0
    indexed_payload = json.loads(indexed.out)
    _assert_v2_envelope(indexed_payload, "index")
    coverage = indexed_payload["coverage"]
    assert coverage["configured_root_count"] == 2
    assert coverage["resolved_root_count"] == 2
    assert coverage["discovered_files"] == 4
    assert coverage["read_files"] == 4
    assert coverage["indexed_files"] == 4
    assert coverage["skipped_files"] == 0
    assert coverage["excluded_files"] == 0
    assert coverage["unreadable_files"] == 0
    assert coverage["unknown_sessions"] == 1
    assert coverage["unrecognized_conversation_records"] == 0
    assert coverage["repositories"] == [
        "/synthetic/repository",
        "/synthetic/unmapped",
    ]
    assert len(coverage["source_watermarks"]) == 4
    assert coverage["completeness"] == "complete"
    index_progress = _progress_events(indexed.err)
    assert {event["phase"] for event in index_progress} >= {
        "scan",
        "parse",
        "fts_commit",
        "done",
    }
    assert index_progress[-1]["state"] == indexed_payload["status"]
    assert index_progress[-1]["coverage"] == indexed_payload["coverage"]
    assert index_progress[-1]["refresh"] == indexed_payload["refresh"]
    assert index_progress[-1]["semantic"] == indexed_payload["semantic"]

    code, unchanged = _run(monkeypatch, capsys, "index", "--literal-only", "--json")
    assert code == 0
    unchanged_payload = json.loads(unchanged.out)
    _assert_v2_envelope(unchanged_payload, "index")
    unchanged_coverage = unchanged_payload["coverage"]
    unchanged_refresh = unchanged_payload["refresh"]
    assert unchanged_payload["revision_id"] == indexed_payload["revision_id"]
    assert unchanged_coverage["metadata_checked_files"] == 4
    assert unchanged_coverage["content_read_files"] == 0
    assert unchanged_coverage["content_read_bytes"] == 0
    assert unchanged_coverage["read_files"] == 0
    assert unchanged_coverage["completeness"] == "complete"
    assert unchanged_refresh["run_id"] is None
    assert unchanged_refresh["state"] == "unchanged"
    assert unchanged_refresh["attempted_sources"] == 0
    assert unchanged_refresh["attempted_content_bytes"] == 0
    unchanged_progress = _progress_events(unchanged.err)
    assert unchanged_progress[-1]["coverage"] == unchanged_coverage
    assert unchanged_progress[-1]["refresh"] == unchanged_refresh

    code, exported = _run(
        monkeypatch,
        capsys,
        "events",
        "--from",
        "2026-08-11T00:00:00Z",
        "--until",
        "2026-08-12T00:00:00Z",
        "--json",
    )
    assert code == 0
    exported_payload = json.loads(exported.out)
    _assert_v2_envelope(exported_payload, "events")
    assert exported_payload["window"] == {
        "from_utc": "2026-08-11T00:00:00Z",
        "until_utc": "2026-08-12T00:00:00Z",
    }
    assert exported_payload["source_revision"] == indexed_payload["revision_id"]
    assert exported_payload["population"] == {
        "scanned_content_rows": 13,
        "scanned_logical_messages": 11,
        "retained": 2,
        "excluded": 8,
        "unresolved": 1,
        "excluded_by_reason": {
            "identified_harness": 1,
            "non_prose": 1,
            "non_user_role": 6,
        },
        "unresolved_by_reason": {"unknown_authorship": 1},
        "content_rows_by_class": {
            "prose": 10,
            "tool_input": 1,
            "tool_name": 1,
            "tool_output": 1,
        },
    }
    events = exported_payload["events"]
    assert len(events) == 2
    assert all(
        set(event)
        == {
            "event_id",
            "occurred_at_utc",
            "canonical_locator",
            "provider",
            "source_session_id",
            "session_kind",
            "cwd",
            "repository",
            "submitted_by",
            "retention_status",
            "physical_alias_count",
            "source_revision",
        }
        for event in events
    )
    assert [event["submitted_by"] for event in events] == ["human", "human"]
    assert [event["retention_status"] for event in events] == [
        "retained",
        "retained",
    ]
    assert [event["physical_alias_count"] for event in events] == [1, 2]
    assert all(
        event["source_revision"] == indexed_payload["revision_id"] for event in events
    )
    assert "visible primary user" not in exported.out
    assert "modern visible user" not in exported.out

    code, bounded_export = _run(
        monkeypatch,
        capsys,
        "events",
        "--from",
        "2026-08-11T03:00:02Z",
        "--until",
        "2026-08-11T03:00:03Z",
        "--json",
    )
    assert code == 0
    bounded_payload = json.loads(bounded_export.out)
    assert [event["provider"] for event in bounded_payload["events"]] == ["codex"]
    assert bounded_payload["population"]["retained"] == 1

    appended = {
        "type": "assistant",
        "uuid": "claude-search-refresh-append",
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T05:00:00Z",
        "cwd": "/synthetic/repository",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": "on-demand refresh sentinel",
        },
    }
    with claude_source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(appended, separators=(",", ":")) + "\n")

    code, stale_search = _run(
        monkeypatch,
        capsys,
        "search",
        "on-demand refresh sentinel",
        "--literal",
        "--json",
    )
    assert code == 0
    stale_payload = json.loads(stale_search.out)
    _assert_v2_envelope(stale_payload, "search")
    assert stale_payload["results"] == []
    assert stale_payload["stale_reasons"] == [
        "native_sources_not_checked",
        "semantic_revision_stale",
    ]
    assert stale_payload["background_refresh"]["state"] == "launched"
    stale_events = _progress_events(stale_search.err)
    assert {event["phase"] for event in stale_events} == {"retrieve", "done"}
    assert stale_events[-1]["deadline_ms"] == stale_payload["deadline_ms"]
    assert stale_events[-1]["retrieval_mode"] == stale_payload["retrieval_mode"]
    assert stale_events[-1]["indexed_at"] == stale_payload["indexed_at"]
    assert stale_events[-1]["stale_reasons"] == stale_payload["stale_reasons"]
    assert stale_events[-1]["background_refresh"] == stale_payload["background_refresh"]

    code, _background = _run(
        monkeypatch,
        capsys,
        "index",
        "--literal-only",
        "--background-refresh",
        "--json",
    )
    assert code == 0
    code, refreshed_search = _run(
        monkeypatch,
        capsys,
        "search",
        "on-demand refresh sentinel",
        "--literal",
        "--json",
    )
    assert code == 0
    refreshed_payload = json.loads(refreshed_search.out)
    refreshed_results = refreshed_payload["results"]
    assert len(refreshed_results) == 1
    _assert_message_identity(refreshed_results[0])
    assert refreshed_results[0]["logical_message_id"] == (
        "claude-search-refresh-append"
    )
    claude_locator = refreshed_results[0]["locator"]

    vector = [0.0] * 1024
    vector[0] = 1.0
    embedded_texts: list[str] = []

    def embed_passages(texts, **kwargs):
        progress = kwargs.get("progress")
        if progress is not None:
            progress("model_preflight", "running")
            progress("model_preflight", "complete")
            progress("model_load", "running")
            progress("model_load", "complete")
        embedded_texts.extend(texts)
        return [vector for _ in texts]

    def bounded_query_embedding(query, *, timeout_seconds, progress):
        assert 0 < timeout_seconds <= 5
        if progress is not None:
            progress("model_preflight", "running")
            progress("model_preflight", "complete")
            progress("model_load", "running")
            progress("model_load", "complete")
        return vector

    monkeypatch.setattr("cc_search_chats.cli.embed_passages", embed_passages)
    monkeypatch.setattr(
        "cc_search_chats.cli._bounded_query_embedding",
        bounded_query_embedding,
        raising=False,
    )
    monkeypatch.setattr("cc_search_chats.cli.chunk_passages", _single_chunks)
    code, _semantic_index = _run(
        monkeypatch,
        capsys,
        "index",
        "--semantic-only",
        "--json",
    )
    assert code == 0
    embedded_texts.clear()
    code, initial_hybrid = _run(
        monkeypatch,
        capsys,
        "search",
        "modern assistant",
        "--json",
    )
    assert code == 0
    initial_hybrid_payload = json.loads(initial_hybrid.out)
    initial_hybrid_results = initial_hybrid_payload["results"]
    assert initial_hybrid_results
    ranking = initial_hybrid_results[0]["ranking"]
    assert ranking["method"] == "rrf"
    assert ranking["rank_constant"] == 60
    assert ranking["component_depth"] == 100
    assert set(ranking["score"]) == {"numerator", "denominator"}
    assert ranking["literal_rank"] > 0
    assert ranking["semantic_rank"] > 0
    hybrid_events = _progress_events(initial_hybrid.err)
    assert {event["phase"] for event in hybrid_events} >= {
        "model_preflight",
        "model_load",
        "query_embed",
        "retrieve",
        "done",
    }

    appended["uuid"] = "claude-hybrid-refresh-append"
    appended["timestamp"] = "2026-08-11T05:01:00Z"
    appended["message"] = {
        "role": "assistant",
        "content": "hybrid refresh sentinel",
    }
    with claude_source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(appended, separators=(",", ":")) + "\n")
    embedded_texts.clear()

    with psycopg.connect(postgres_cluster.dsn) as database:
        database.execute(
            """
            UPDATE cc_search_chats.auto_refresh_state
            SET requested_at = requested_at - interval '5 minutes'
            WHERE singleton
            """
        )

    code, stale_hybrid = _run(
        monkeypatch,
        capsys,
        "search",
        "hybrid refresh sentinel",
        "--json",
    )
    assert code == 0
    assert all(
        result["logical_message_id"] != "claude-hybrid-refresh-append"
        for result in json.loads(stale_hybrid.out)["results"]
    )
    code, _background = _run(
        monkeypatch,
        capsys,
        "index",
        "--literal-only",
        "--background-refresh",
        "--json",
    )
    assert code == 0
    code, refreshed_hybrid = _run(
        monkeypatch,
        capsys,
        "search",
        "hybrid refresh sentinel",
        "--json",
    )
    assert code == 0
    hybrid_results = json.loads(refreshed_hybrid.out)["results"]
    assert any(
        result["logical_message_id"] == "claude-hybrid-refresh-append"
        for result in hybrid_results
    )
    assert embedded_texts == []

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
    searched_payload = json.loads(searched.out)
    _assert_v2_envelope(searched_payload, "search")
    result = searched_payload["results"][0]
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
    resolved_payload = json.loads(resolved_many.out)
    _assert_v2_envelope(resolved_payload, "resolve", status="partial")
    resolutions = resolved_payload["resolutions"]
    assert [value["locator"] for value in resolutions] == [
        locator,
        missing_locator,
        locator,
    ]
    assert [value["message_count"] for value in resolutions] == [1, 0, 1]
    assert [value["status"] for value in resolutions] == [
        "resolved",
        "no_match",
        "resolved",
    ]
    _assert_message_identity(resolutions[0]["messages"][0])

    code, exhaustive = _run(
        monkeypatch,
        capsys,
        "search",
        "Read OR synthetic.txt OR synthetic tool output",
        "--literal",
        "--tools",
        "--exhaustive",
        "--limit",
        "1",
        "--json",
    )
    assert code == 0
    exhaustive_payload = json.loads(exhaustive.out)
    _assert_v2_envelope(exhaustive_payload, "search")
    assert exhaustive_payload["exhaustive"] is True
    assert exhaustive_payload["result_limit"] is None
    assert [value["content_class"] for value in exhaustive_payload["results"]] == [
        "tool_name",
        "tool_input",
        "tool_output",
    ]

    for command in (
        ("list", "--provider", "codex", "--json"),
        ("extract", "codex-modern-primary", "--provider", "codex", "--json"),
        ("context", locator, "--depth", "1", "--json"),
        ("resolve", locator, "--json"),
    ):
        code, output = _run(monkeypatch, capsys, *command)
        assert code == 0
        payload = json.loads(output.out)
        _assert_v2_envelope(
            payload,
            command[0],
            status=("resolved" if command[0] in {"context", "resolve"} else "complete"),
        )
        if command[0] in {"extract", "context", "resolve"}:
            _assert_message_identity(payload["messages"][0])

    code, reference = _run(
        monkeypatch,
        capsys,
        "resolve",
        locator,
        "--reference-only",
        "--json",
    )
    assert code == 0
    reference_payload = json.loads(reference.out)
    _assert_v2_envelope(reference_payload, "resolve", status="resolved")
    assert reference_payload["messages"]
    assert all("text" not in message for message in reference_payload["messages"])

    code, malformed = _run(
        monkeypatch,
        capsys,
        "resolve",
        "not-a-locator",
        "--json",
    )
    assert code == 2
    malformed_payload = json.loads(malformed.out)
    _assert_v2_envelope(
        malformed_payload,
        "resolve",
        status="malformed_locator",
    )

    original = claude_source.read_bytes()
    changed = original.replace(b"refresh sentinel", b"stale!! sentinel", 1)
    assert changed != original and len(changed) == len(original)
    claude_source.write_bytes(changed)
    code, stale = _run(
        monkeypatch,
        capsys,
        "resolve",
        claude_locator,
        "--json",
    )
    assert code == 3
    _assert_v2_envelope(
        json.loads(stale.out),
        "resolve",
        status="stale_source",
    )


def test_extract_requires_provider_when_native_session_ids_collide(
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
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    codex_bytes = (FIXTURES / "codex_modern_primary_145.jsonl").read_bytes()
    codex_bytes = codex_bytes.replace(
        b"codex-modern-primary",
        b"claude-session-primary",
    )
    (codex_day / "rollout-colliding.jsonl").write_bytes(codex_bytes)
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

    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0
    code, _indexed = _run(monkeypatch, capsys, "index", "--literal-only", "--json")
    assert code == 0
    code, ambiguous = _run(
        monkeypatch,
        capsys,
        "extract",
        "claude-session-primary",
        "--json",
    )

    assert code == 3
    payload = json.loads(ambiguous.out)
    _assert_v2_envelope(payload, "extract", status="multiple_matches")
    assert payload["messages"] == []
    assert payload["matches"] == [
        {"provider": "claude", "source_session_id": "claude-session-primary"},
        {"provider": "codex", "source_session_id": "claude-session-primary"},
    ]

    code, qualified = _run(
        monkeypatch,
        capsys,
        "extract",
        "claude-session-primary",
        "--provider",
        "codex",
        "--json",
    )
    assert code == 0
    qualified_payload = json.loads(qualified.out)
    _assert_v2_envelope(qualified_payload, "extract")
    assert qualified_payload["messages"]
    assert {
        message["identity"]["provider"] for message in qualified_payload["messages"]
    } == {"codex"}
