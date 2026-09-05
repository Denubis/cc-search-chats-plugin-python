"""Search reads one committed snapshot without triggering index work."""

import json
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import cast

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import main
from cc_search_chats.semantic import SemanticChunk, query_embedder
from cc_search_chats.semantic.query_embedder import QueryEmbeddingResult
from cc_search_chats.storage.postgresql import migrate
from cc_search_chats.storage.postgresql import search_messages as literal_search
from cc_search_chats.storage.postgresql.guardrails import ReadDeadlineExceeded

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


def _completed_corpus(
    connection: psycopg.Connection,
    *,
    completed_at: datetime,
) -> int:
    corpus_generation = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.corpus_generation (
                completed_at, status
            ) VALUES (%s, 'complete')
            RETURNING corpus_generation
            """,
            (completed_at,),
        )
    )[0]
    semantic_build = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.semantic_build (
                corpus_generation, profile_id, completed_at, status
            ) VALUES (
                %s, 'nemotron-3-embed-8b-bf16:v1', %s, 'complete'
            )
            RETURNING semantic_build
            """,
            (corpus_generation, completed_at),
        )
    )[0]
    connection.execute(
        """
        UPDATE cc_search_chats.corpus_generation
        SET semantic_build = %s
        WHERE corpus_generation = %s
        """,
        (semantic_build, corpus_generation),
    )
    return int(corpus_generation)


def _select_corpus(connection: psycopg.Connection, corpus_generation: int) -> None:
    connection.execute(
        """
        UPDATE cc_search_chats.corpus_state
        SET current_corpus_generation = %s
        WHERE singleton
        """,
        (corpus_generation,),
    )


def _configure_cli_connection(
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_info = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection_info[key]))


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *args: str,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", *args])
    with pytest.raises(SystemExit) as stopped:
        main()
    output = capsys.readouterr()
    code = stopped.value.code
    payload = json.loads(output.out)
    assert isinstance(code, int)
    assert isinstance(payload, dict)
    return code, payload


def _assert_v5_error_envelope(payload: dict[str, object]) -> None:
    assert payload["schema_version"] == 5
    refresh = cast("dict[str, object]", payload["refresh"])
    assert refresh["corpus_generation"] is None
    semantic = cast("dict[str, object]", payload["semantic"])
    assert semantic["semantic_build"] is None
    assert semantic["corpus_generation"] is None
    assert payload["indexed_at"] is None
    assert payload["corpus_age_ms"] is None
    assert payload["mode"] == "literal"
    index_state = cast("dict[str, object]", payload["index_state"])
    assert index_state["made_at"] is None
    assert index_state["unindexed"] is None
    assert index_state["unindexed_reason"] == "unavailable"
    assert "background_refresh" not in payload
    assert isinstance(payload["warnings"], list)


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def _passage_embeddings(texts, **kwargs):
    progress = kwargs.get("progress")
    if progress is not None:
        progress("model_preflight", "running")
        progress("model_preflight", "complete")
        progress("model_load", "running")
        progress("model_load", "complete")
    vector = [0.0] * 1024
    vector[0] = 1.0
    return [vector for _ in texts]


def _stub_semantic_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cc_search_chats.cli.embed_passages", _passage_embeddings)
    monkeypatch.setattr("cc_search_chats.cli.chunk_passages", _single_chunks)
    monkeypatch.setattr("cc_search_chats.cli.shutdown_query_embedder", lambda: None)


@pytest.mark.parametrize(
    ("warm_reused", "expected_model_load_states"),
    [
        (False, ["running", "complete"]),
        (True, []),
    ],
)
def test_semantic_ndjson_model_load_events_match_helper_state(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    warm_reused: bool,
    expected_model_load_states: list[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    _stub_semantic_index(monkeypatch)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    assert _run(monkeypatch, capsys, "index", "--json")[0] == 0
    paths = query_embedder.query_embedder_paths(tmp_path / "helper-runtime")
    monkeypatch.setattr(query_embedder, "query_embedder_paths", lambda: paths)
    monkeypatch.setattr(
        query_embedder,
        "_ensure_compatible_helper",
        lambda _paths: None,
    )
    vector = [0.0] * 1024
    vector[0] = 1.0

    def exchange(_path, request, *, progress=None):
        assert request["kind"] == "embed"
        assert progress is not None
        progress("query_embed", "running")
        if not warm_reused:
            progress("model_load", "running")
            progress("model_load", "complete")
        progress("query_embed", "complete")
        return {
            "kind": "result",
            "embedding": vector,
            "model_load_ms": 0 if warm_reused else 12,
            "query_embed_ms": 3,
            "warm_reused": warm_reused,
        }

    monkeypatch.setattr(query_embedder, "_exchange", exchange)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cc-search-chats",
            "search",
            "visible assistant",
            "--semantic",
            "--json",
        ],
    )

    with pytest.raises(SystemExit, match="0"):
        main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    events = [json.loads(line) for line in captured.err.splitlines()]

    assert payload["retrieval_mode"] == "hybrid"
    if warm_reused:
        assert all(event["phase"] != "model_load" for event in events)
    assert [
        event["state"]
        for event in events
        if event["event"] == "progress" and event["phase"] == "model_load"
    ] == expected_model_load_states


def test_search_reports_pending_migration_without_creating_schema(
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli_connection(postgres_cluster, monkeypatch)

    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "migration sentinel",
        "--literal",
        "--json",
    )

    assert code == 6
    _assert_v5_error_envelope(payload)
    assert payload["status"] == "maintenance_required"
    error = cast("dict[str, object]", payload["error"])
    assert error["pending_versions"] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    with psycopg.connect(postgres_cluster.dsn) as connection:
        assert (
            next(connection.execute("SELECT to_regnamespace('cc_search_chats')"))[0]
            is None
        )


def test_search_results_and_reported_corpus_share_one_snapshot(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    _stub_semantic_index(monkeypatch)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))

    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    code, indexed = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0
    selected_corpus = indexed["corpus_generation"]
    published = False

    def publish_after_literal(connection, query, **kwargs):
        nonlocal published
        hits = literal_search(connection, query, **kwargs)
        if not published:
            published = True
            with psycopg.connect(postgres_cluster.dsn) as publisher:
                publisher.execute(
                    """
                    DELETE FROM cc_search_chats.message_current
                    WHERE logical_message_id = 'claude-assistant-1'
                    """
                )
                replacement_corpus = _completed_corpus(
                    publisher,
                    completed_at=datetime.now(UTC),
                )
                _select_corpus(publisher, replacement_corpus)
        return hits

    monkeypatch.setattr("cc_search_chats.cli.search_messages", publish_after_literal)

    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "visible assistant",
        "--literal",
        "--json",
    )

    assert code == 0
    result = cast("list[dict[str, object]]", payload["results"])[0]
    assert result["logical_message_id"] == "claude-assistant-1"
    refresh = cast("dict[str, object]", payload["refresh"])
    assert refresh["corpus_generation"] == selected_corpus


class _ObservedConnection:
    def __init__(
        self,
        connection: psycopg.Connection,
        statements: list[str],
    ) -> None:
        self._connection = connection
        self._statements = statements

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def execute(self, query, *args, **kwargs):
        self._statements.append(
            query if isinstance(query, str) else query.as_string(self._connection)
        )
        return self._connection.execute(query, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def test_stale_search_opens_snapshot_without_refresh_write_listen_or_wait(
    postgres_cluster,
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(postgres_connection)
    current = _completed_corpus(
        postgres_connection,
        completed_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    _select_corpus(postgres_connection, current)
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setenv("PGOPTIONS", "-c default_transaction_read_only=on")

    statements: list[str] = []
    real_connect = psycopg.connect

    def observed_connect(*args, **kwargs):
        return _ObservedConnection(real_connect(*args, **kwargs), statements)

    monkeypatch.setattr("cc_search_chats.cli.psycopg.connect", observed_connect)

    started = monotonic()
    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "stale snapshot sentinel",
        "--literal",
        "--json",
    )
    elapsed = monotonic() - started

    assert code == 0, payload["error"]
    assert payload["status"] == "complete"
    assert payload["results"] == []
    refresh = cast("dict[str, object]", payload["refresh"])
    assert refresh["corpus_generation"] == current
    assert elapsed < 2
    writes = re.compile(
        r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|ALTER|DROP)\b",
        re.IGNORECASE,
    )
    listens = re.compile(r"\b(?:LISTEN|UNLISTEN)\b", re.IGNORECASE)
    assert not any(writes.search(statement) for statement in statements)
    assert not any(listens.search(statement) for statement in statements)


def test_search_deadline_expires_before_connection(
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "deadline sentinel", "--literal", "--json"],
    )

    with pytest.raises(SystemExit) as stopped:
        main(request_started=monotonic() - 6)
    expired = json.loads(capsys.readouterr().out)

    assert stopped.value.code == 7
    _assert_v5_error_envelope(expired)
    assert expired["status"] == "deadline_exceeded"
    assert expired["error"]["code"] == "search_deadline_exceeded"


def test_search_read_deadline_expires_while_read_queue_is_locked(
    postgres_cluster,
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(postgres_connection)
    _configure_cli_connection(postgres_cluster, monkeypatch)

    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as lock_owner:
        lock_owner.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            ("cc_search_chats.read_queue",),
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "cc-search-chats",
                "search",
                "read deadline sentinel",
                "--literal",
                "--json",
            ],
        )
        started = monotonic()
        with pytest.raises(SystemExit) as stopped:
            main(request_started=started - 4.6)
        elapsed = monotonic() - started
        locked = json.loads(capsys.readouterr().out)

    assert stopped.value.code == 7
    _assert_v5_error_envelope(locked)
    assert elapsed < 1
    assert locked["status"] == "deadline_exceeded"
    assert locked["error"]["code"] == "search_deadline_exceeded"


def test_human_search_headers_distinguish_modes_degradation_and_unknown_scan(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    _stub_semantic_index(monkeypatch)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    assert _run(monkeypatch, capsys, "index", "--json")[0] == 0
    vector = [0.0] * 1024
    vector[0] = 1.0

    def query_embedding(_query, *, progress, quiet):
        assert isinstance(quiet, bool)
        progress("query_embed", "running")
        progress("model_load", "complete")
        progress("query_embed", "complete")
        return QueryEmbeddingResult(tuple(vector), 12, 3, False)

    monkeypatch.setattr("cc_search_chats.cli._bounded_query_embedding", query_embedding)

    def human_search(mode: str) -> list[str]:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "cc-search-chats",
                "search",
                "visible assistant",
                mode,
                "--progress",
                "human",
            ],
        )
        with pytest.raises(SystemExit, match="0"):
            main()
        return capsys.readouterr().out.splitlines()

    literal = human_search("--literal")
    assert literal[0] == (
        "literal search (exact full-text, no model): visible assistant"
    )
    assert literal[1].startswith("index made ")
    assert "; now " in literal[1]
    assert "; age " in literal[1]
    assert literal[2] == "missing 0 chats"
    assert "visible assistant" in "\n".join(literal[3:])

    semantic = human_search("--semantic")
    assert semantic[0] == ("semantic search (hybrid model ranking): visible assistant")
    assert semantic[1].startswith("index made ")
    assert semantic[2] == "missing 0 chats"
    assert semantic[3] == (
        "semantic: loading model (first use takes about 10 s; stays warm 30 s "
        "after each query)"
    )
    assert "visible assistant" in "\n".join(semantic[4:])

    code, timed = _run(
        monkeypatch,
        capsys,
        "search",
        "visible assistant",
        "--semantic",
        "--json",
    )
    assert code == 0
    assert timed["deadline_ms"] is None
    semantic_state = cast("dict[str, object]", timed["semantic"])
    assert semantic_state["model_load_ms"] == 12
    assert semantic_state["query_embed_ms"] == 3
    assert semantic_state["warm_reused"] is False

    def warm_embedding(_query, *, progress, quiet):
        assert isinstance(quiet, bool)
        progress("query_embed", "running")
        progress("query_embed", "complete")
        return QueryEmbeddingResult(tuple(vector), 0, 2, True)

    monkeypatch.setattr("cc_search_chats.cli._bounded_query_embedding", warm_embedding)
    warm = human_search("--semantic")
    assert warm[3] == "semantic: warm model reused"
    assert "visible assistant" in "\n".join(warm[4:])

    def failed_embedding(*_args, **_kwargs):
        raise RuntimeError("fixture helper failed")

    monkeypatch.setattr(
        "cc_search_chats.cli._bounded_query_embedding", failed_embedding
    )
    degraded = human_search("--semantic")
    assert degraded[0] == ("semantic search (hybrid model ranking): visible assistant")
    assert degraded[3].startswith(
        "WARNING: semantic ranking unavailable (RuntimeError: fixture helper failed); "
        "these are literal results"
    )
    assert "visible assistant" in "\n".join(degraded[4:])

    code, degraded_payload = _run(
        monkeypatch,
        capsys,
        "search",
        "visible assistant",
        "--semantic",
        "--json",
    )
    assert code == 0
    assert degraded_payload["status"] == "complete"
    assert degraded_payload["deadline_ms"] is None
    assert degraded_payload["retrieval_mode"] == "literal_fallback"
    assert all(
        warning["code"] != "deadline_degraded"
        for warning in cast("list[dict[str, object]]", degraded_payload["warnings"])
    )

    monkeypatch.setattr("cc_search_chats.cli._bounded_query_embedding", query_embedding)
    monkeypatch.setattr(
        "cc_search_chats.cli.semantic_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            psycopg.errors.QueryCanceled("fixture semantic retrieval cancellation")
        ),
    )
    code, database_failure = _run(
        monkeypatch,
        capsys,
        "search",
        "visible assistant",
        "--semantic",
        "--json",
    )
    assert code == 1
    assert database_failure["status"] == "internal_failure"
    database_error = cast("dict[str, object]", database_failure["error"])
    assert database_error["code"] == "postgresql_operation_failed"

    monkeypatch.setattr(
        "cc_search_chats.cli.unindexed_sources",
        lambda *_args, **_kwargs: (None, "scan_budget_exhausted"),
    )
    unknown = human_search("--literal")
    assert unknown[2] == "unindexed chats: unknown (scan_budget_exhausted)"


@pytest.mark.parametrize(
    "mode_arguments",
    [("--literal", "--exhaustive"), ("--semantic",)],
)
def test_unbounded_search_starts_a_fresh_staleness_scan_budget(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode_arguments: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    _stub_semantic_index(monkeypatch)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    assert _run(monkeypatch, capsys, "index", "--json")[0] == 0
    monkeypatch.setattr("cc_search_chats.cli._SEARCH_DEADLINE_SECONDS", 0.0)
    monkeypatch.setattr("cc_search_chats.cli._SEARCH_RENDER_RESERVE_SECONDS", 0.0)
    vector = [0.0] * 1024
    vector[0] = 1.0
    monkeypatch.setattr(
        "cc_search_chats.cli._bounded_query_embedding",
        lambda *_args, **_kwargs: QueryEmbeddingResult(tuple(vector), 0, 1, True),
    )

    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "visible assistant",
        *mode_arguments,
        "--json",
    )

    assert code == 0
    assert payload["deadline_ms"] is None
    index_state = cast("dict[str, object]", payload["index_state"])
    assert index_state["unindexed"] == {
        "files": 0,
        "directories": 0,
        "bytes": 0,
    }
    assert index_state["unindexed_reason"] is None


def test_human_index_status_prints_staleness_before_checkpoint(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    _stub_semantic_index(monkeypatch)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    assert _run(monkeypatch, capsys, "index", "--json")[0] == 0
    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "index", "--status", "--progress", "human"],
    )

    with pytest.raises(SystemExit, match="0"):
        main()
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].startswith("index made ")
    assert lines[1] == "missing 0 chats"
    assert lines[2].startswith("Semantic index: ")


def test_literal_deadline_after_retrieval_returns_hits_as_partial(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    _stub_semantic_index(monkeypatch)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    _configure_cli_connection(postgres_cluster, monkeypatch)
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    assert _run(monkeypatch, capsys, "index", "--json")[0] == 0

    arguments = ["search", "visible assistant", "--literal", "--json"]
    monkeypatch.setattr(
        "cc_search_chats.cli.pg_resolve_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ReadDeadlineExceeded("fixture identity-resolution deadline")
        ),
    )

    code, payload = _run(monkeypatch, capsys, *arguments)

    assert code == 0
    assert payload["status"] == "partial"
    assert payload["mode"] == "literal"
    results = cast("list[dict[str, object]]", payload["results"])
    assert results
    assert results[0]["logical_message_id"] == "claude-assistant-1"
    warnings = cast("list[dict[str, object]]", payload["warnings"])
    assert any(warning["code"] == "deadline_degraded" for warning in warnings)
    assert payload["retrieval_mode"] == "literal"
