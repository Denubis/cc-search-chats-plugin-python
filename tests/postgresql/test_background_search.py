"""Search reads committed state while systemd owns incremental refresh."""

import json
import shutil
import sys
from pathlib import Path
from time import monotonic
from typing import cast

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import main
from cc_search_chats.storage.postgresql import search_messages as literal_search

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


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
    assert payload["status"] == "maintenance_required"
    error = payload["error"]
    assert isinstance(error, dict)
    error = cast(dict[str, object], error)
    assert error["pending_versions"] == [1, 2, 3, 4, 5, 6]
    with psycopg.connect(postgres_cluster.dsn) as connection:
        assert (
            next(connection.execute("SELECT to_regnamespace('cc_search_chats')"))[0]
            is None
        )


def test_search_results_and_reported_revision_share_one_snapshot(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda args: None
    )
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
        "cc_search_chats.cli._start_systemd_refresh", lambda timeout_seconds: None
    )

    assert _run(monkeypatch, capsys, "index", "--migrate", "--json")[0] == 0
    code, indexed = _run(
        monkeypatch,
        capsys,
        "index",
        "--literal-only",
        "--json",
    )
    assert code == 0
    selected_revision = indexed["revision_id"]
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
                replacement_revision = next(
                    publisher.execute(
                        "INSERT INTO cc_search_chats.corpus_revision DEFAULT VALUES "
                        "RETURNING revision_id"
                    )
                )[0]
                publisher.execute(
                    """
                    UPDATE cc_search_chats.corpus_state
                    SET current_revision_id = %s
                    WHERE singleton
                    """,
                    (replacement_revision,),
                )
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
    result = cast(dict[str, object], result)
    assert result["logical_message_id"] == "claude-assistant-1"
    refresh = payload["refresh"]
    assert isinstance(refresh, dict)
    refresh = cast(dict[str, object], refresh)
    assert refresh["fts_revision"] == selected_revision


def test_search_returns_committed_snapshot_then_service_refreshes(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda args: None
    )
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
    assert migrated["applied_schema_version"] == 6

    code, indexed = _run(
        monkeypatch,
        capsys,
        "index",
        "--literal-only",
        "--json",
    )
    assert code == 0
    committed_revision = indexed["revision_id"]

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
        lambda timeout_seconds: launch_budgets.append(timeout_seconds),
        raising=False,
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
    stale_refresh = cast(dict[str, object], stale_refresh)
    assert stale_refresh["fts_revision"] == committed_revision
    assert stale["deadline_ms"] == 5000
    assert stale["retrieval_mode"] == "literal"
    assert stale["stale_reasons"] == [
        "native_sources_not_checked",
        "semantic_revision_stale",
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
        "--literal-only",
        "--background-refresh",
        "--json",
    )

    assert code == 0
    assert refreshed["revision_id"] != committed_revision
    refreshed_state = refreshed["refresh"]
    assert isinstance(refreshed_state, dict)
    refreshed_state = cast(dict[str, object], refreshed_state)
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
        "--literal-only",
        "--background-refresh",
        "--json",
    )
    assert code == 0
    assert duplicate["background_noop"] is True
    duplicate_refresh = duplicate["refresh"]
    assert isinstance(duplicate_refresh, dict)
    duplicate_refresh = cast(dict[str, object], duplicate_refresh)
    assert duplicate_refresh["fts_revision"] == refreshed["revision_id"]

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
    current_result = cast(dict[str, object], current_result)
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
    assert elapsed < 1
    assert locked["status"] == "deadline_exceeded"
    assert locked["error"]["code"] == "search_deadline_exceeded"
