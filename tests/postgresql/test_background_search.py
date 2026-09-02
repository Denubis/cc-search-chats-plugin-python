"""Search reads committed state while systemd owns incremental refresh."""

import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import cast

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import _coordinate_ranked_refresh, main
from cc_search_chats.providers.source_discovery import configured_source_roots
from cc_search_chats.semantic import SemanticChunk
from cc_search_chats.storage.postgresql import index_corpus, migrate
from cc_search_chats.storage.postgresql import search_messages as literal_search
from cc_search_chats.storage.postgresql.auto_refresh import (
    mark_auto_refresh_complete,
    mark_auto_refresh_running,
)
from cc_search_chats.storage.postgresql.guardrails import acquire_index_session

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


def test_ranked_refresh_skips_launch_and_wait_for_a_fresh_corpus(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate(postgres_connection)
    current = _completed_corpus(
        postgres_connection,
        completed_at=datetime.now(UTC),
    )
    _select_corpus(postgres_connection, current)
    monkeypatch.setattr(
        "cc_search_chats.cli._start_systemd_refresh",
        lambda _timeout_seconds: pytest.fail("fresh corpus launched a refresh"),
    )

    coordinated = _coordinate_ranked_refresh(
        postgres_connection,
        remaining_seconds=lambda: 2.0,
        wait_for_notification=lambda _connection, _timeout: pytest.fail(
            "fresh corpus waited for a notification"
        ),
    )

    assert coordinated.corpus_before == current
    assert coordinated.corpus_after == current
    assert coordinated.background.state == "idle"
    assert coordinated.warning is None


def test_ranked_refresh_uses_durable_publication_after_a_wake(
    postgres_cluster,
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate(postgres_connection)
    old = _completed_corpus(
        postgres_connection,
        completed_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    new = _completed_corpus(
        postgres_connection,
        completed_at=datetime.now(UTC),
    )
    _select_corpus(postgres_connection, old)
    monkeypatch.setattr(
        "cc_search_chats.cli._start_systemd_refresh",
        lambda _timeout_seconds: None,
    )

    def publish(_connection: psycopg.Connection, timeout: float) -> bool:
        assert timeout > 0
        with psycopg.connect(postgres_cluster.dsn) as publisher:
            _select_corpus(publisher, new)
            publisher.execute(
                """
                UPDATE cc_search_chats.auto_refresh_state
                SET state = 'complete', completed_at = now()
                WHERE singleton AND request_id = 1
                """
            )
        return True

    coordinated = _coordinate_ranked_refresh(
        postgres_connection,
        remaining_seconds=lambda: 2.0,
        wait_for_notification=publish,
    )

    assert coordinated.corpus_before == old
    assert coordinated.corpus_after == new
    assert coordinated.background.state == "complete"
    assert coordinated.warning is None


@pytest.mark.parametrize("early_wakes", [0, 2])
def test_ranked_refresh_timeout_keeps_the_old_durable_corpus(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    early_wakes: int,
) -> None:
    migrate(postgres_connection)
    old = _completed_corpus(
        postgres_connection,
        completed_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    _select_corpus(postgres_connection, old)
    monkeypatch.setattr(
        "cc_search_chats.cli._start_systemd_refresh",
        lambda _timeout_seconds: None,
    )
    wakes = iter([True] * early_wakes + [False])

    coordinated = _coordinate_ranked_refresh(
        postgres_connection,
        remaining_seconds=lambda: 2.0,
        wait_for_notification=lambda _connection, _timeout: next(wakes),
    )

    assert coordinated.corpus_before == old
    assert coordinated.corpus_after == old
    assert coordinated.background.state == "launched"
    assert coordinated.warning is None


def test_ranked_refresh_names_unavailable_systemd_without_waiting(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate(postgres_connection)
    old = _completed_corpus(
        postgres_connection,
        completed_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    _select_corpus(postgres_connection, old)

    def unavailable(_timeout_seconds: float) -> None:
        raise RuntimeError("fixture user systemd unavailable")

    monkeypatch.setattr("cc_search_chats.cli._start_systemd_refresh", unavailable)

    coordinated = _coordinate_ranked_refresh(
        postgres_connection,
        remaining_seconds=lambda: 2.0,
        wait_for_notification=lambda _connection, _timeout: pytest.fail(
            "failed launch waited for publication"
        ),
    )

    assert coordinated.corpus_after == old
    assert coordinated.background.state == "failed"
    assert coordinated.warning == {
        "code": "auto_refresh_unavailable",
        "detail": "RuntimeError: fixture user systemd unavailable",
    }


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


def _assert_v3_error_envelope(payload: dict[str, object]) -> None:
    assert payload["schema_version"] == 3
    refresh = cast("dict[str, object]", payload["refresh"])
    assert refresh["corpus_generation"] is None
    semantic = cast("dict[str, object]", payload["semantic"])
    assert semantic["semantic_build"] is None
    assert semantic["corpus_generation"] is None
    assert payload["indexed_at"] is None
    assert payload["corpus_age_ms"] is None
    assert isinstance(payload["background_refresh"], dict)
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


def test_search_reports_pending_migration_without_creating_schema(
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection_info = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection_info[key]))

    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "migration sentinel",
        "--literal",
        "--json",
    )

    assert code == 6
    _assert_v3_error_envelope(payload)
    assert payload["status"] == "maintenance_required"
    error = payload["error"]
    assert isinstance(error, dict)
    error = cast("dict[str, object]", error)
    assert error["pending_versions"] == [1, 2, 3, 4, 5, 6, 7, 8]
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
    connection_info = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection_info[key]))
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    monkeypatch.setattr(
        "cc_search_chats.cli._start_systemd_refresh", lambda _timeout_seconds: None
    )

    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    code, indexed = _run(
        monkeypatch,
        capsys,
        "index",
        "--json",
    )
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
    results = payload["results"]
    assert isinstance(results, list)
    result = results[0]
    assert isinstance(result, dict)
    result = cast("dict[str, object]", result)
    assert result["logical_message_id"] == "claude-assistant-1"
    refresh = payload["refresh"]
    assert isinstance(refresh, dict)
    refresh = cast("dict[str, object]", refresh)
    assert refresh["corpus_generation"] == selected_corpus


def test_ranked_search_uses_corpus_published_during_launch(
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
    source = Path(
        shutil.copy(
            FIXTURES / "claude_primary.jsonl",
            claude_root / "claude-session-primary.jsonl",
        )
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    connection_info = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection_info[key]))
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    code, indexed = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0
    initial_corpus = indexed["corpus_generation"]
    with psycopg.connect(postgres_cluster.dsn) as connection:
        connection.execute(
            """
            UPDATE cc_search_chats.corpus_generation
            SET completed_at = now() - interval '10 minutes'
            WHERE corpus_generation = %s
            """,
            (initial_corpus,),
        )

    appended = {
        "type": "assistant",
        "uuid": "claude-in-budget-refresh-append",
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T05:00:00Z",
        "cwd": "/synthetic/repository",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": "in budget publication sentinel",
        },
    }
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(appended, separators=(",", ":")) + "\n")

    launches: list[float] = []

    def publish_during_launch(timeout_seconds: float) -> None:
        launches.append(timeout_seconds)
        with psycopg.connect(postgres_cluster.dsn, autocommit=True) as worker:
            request_id = mark_auto_refresh_running(worker)
            assert request_id == 1
            acquire_index_session(worker)
            refreshed = index_corpus(
                worker,
                _passage_embeddings,
                chunker=_single_chunks,
                source_roots=configured_source_roots(),
            )
            mark_auto_refresh_complete(
                worker,
                request_id,
                refresh_run_id=refreshed.run_id,
            )

    monkeypatch.setattr(
        "cc_search_chats.cli._start_systemd_refresh",
        publish_during_launch,
    )

    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "in budget publication sentinel",
        "--literal",
        "--json",
    )

    assert code == 0
    assert len(launches) == 1
    result = cast("list[dict[str, object]]", payload["results"])[0]
    assert result["logical_message_id"] == "claude-in-budget-refresh-append"
    refresh = cast("dict[str, object]", payload["refresh"])
    assert refresh["corpus_generation"] != initial_corpus
    background = cast("dict[str, object]", payload["background_refresh"])
    assert background["state"] == "complete"


def test_search_returns_committed_snapshot_then_service_refreshes(
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
    source = Path(
        shutil.copy(
            FIXTURES / "claude_primary.jsonl",
            claude_root / "claude-session-primary.jsonl",
        )
    )
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    connection_info = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection_info[key]))
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))

    code, migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0
    assert migrated["applied_schema_version"] == 7

    code, indexed = _run(
        monkeypatch,
        capsys,
        "index",
        "--json",
    )
    assert code == 0
    committed_corpus = indexed["corpus_generation"]
    with psycopg.connect(postgres_cluster.dsn) as connection:
        connection.execute(
            """
            UPDATE cc_search_chats.corpus_generation
            SET completed_at = now() - interval '10 minutes'
            WHERE corpus_generation = %s
            """,
            (committed_corpus,),
        )

    appended = {
        "type": "assistant",
        "uuid": "claude-background-refresh-append",
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T05:00:00Z",
        "cwd": "/synthetic/repository",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": "background refresh sentinel",
        },
    }
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(appended, separators=(",", ":")) + "\n")

    launch_budgets: list[float] = []
    monkeypatch.setattr(
        "cc_search_chats.cli._start_systemd_refresh",
        launch_budgets.append,
        raising=False,
    )
    monkeypatch.setattr(
        "cc_search_chats.cli._wait_for_index_notification",
        lambda _connection, _timeout_seconds: False,
    )

    code, stale = _run(
        monkeypatch,
        capsys,
        "search",
        "background refresh sentinel",
        "--literal",
        "--json",
    )

    assert code == 0
    assert stale["results"] == []
    stale_refresh = stale["refresh"]
    assert isinstance(stale_refresh, dict)
    stale_refresh = cast("dict[str, object]", stale_refresh)
    assert stale_refresh["corpus_generation"] == committed_corpus
    assert stale["deadline_ms"] == 5000
    assert stale["retrieval_mode"] == "literal"
    assert stale["stale_reasons"] == [
        "native_sources_not_checked",
    ]
    assert stale["background_refresh"] == {
        "request_id": 1,
        "state": "launched",
        "refresh_run_id": None,
        "last_error": None,
    }
    assert len(launch_budgets) == 1
    assert 0 < launch_budgets[0] <= 5

    code, refreshed = _run(
        monkeypatch,
        capsys,
        "index",
        "--background-refresh",
        "--json",
    )

    assert code == 0
    assert refreshed["corpus_generation"] != committed_corpus
    refreshed_state = refreshed["refresh"]
    assert isinstance(refreshed_state, dict)
    refreshed_state = cast("dict[str, object]", refreshed_state)
    with psycopg.connect(postgres_cluster.dsn) as connection:
        assert next(
            connection.execute(
                """
                SELECT request_id, state, refresh_run_id, last_error
                FROM cc_search_chats.auto_refresh_state
                WHERE singleton
                """
            )
        ) == (1, "complete", refreshed_state["run_id"], None)

    code, duplicate = _run(
        monkeypatch,
        capsys,
        "index",
        "--background-refresh",
        "--json",
    )
    assert code == 0
    assert duplicate["background_noop"] is True
    duplicate_refresh = duplicate["refresh"]
    assert isinstance(duplicate_refresh, dict)
    duplicate_refresh = cast("dict[str, object]", duplicate_refresh)
    assert duplicate_refresh["corpus_generation"] == refreshed["corpus_generation"]

    code, current = _run(
        monkeypatch,
        capsys,
        "search",
        "background refresh sentinel",
        "--literal",
        "--json",
    )

    assert code == 0
    current_results = current["results"]
    assert isinstance(current_results, list)
    current_result = current_results[0]
    assert isinstance(current_result, dict)
    current_result = cast("dict[str, object]", current_result)
    assert current_result["logical_message_id"] == ("claude-background-refresh-append")
    assert len(launch_budgets) == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cc-search-chats",
            "search",
            "background refresh sentinel",
            "--literal",
            "--json",
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        main(request_started=monotonic() - 6)
    expired = json.loads(capsys.readouterr().out)

    assert stopped.value.code == 7
    _assert_v3_error_envelope(expired)
    assert expired["status"] == "deadline_exceeded"
    assert expired["error"]["code"] == "search_deadline_exceeded"

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
                "background refresh sentinel",
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
    _assert_v3_error_envelope(locked)
    assert elapsed < 1
    assert locked["status"] == "deadline_exceeded"
    assert locked["error"]["code"] == "search_deadline_exceeded"
