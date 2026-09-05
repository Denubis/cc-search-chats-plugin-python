"""End-to-end CLI tests with JSONL fixture files.

Verifies: cc-search-v2.AC5.1, cc-search-v2.AC5.2, cc-search-v2.AC5.3,
          cc-search-v2.AC5.4, cc-search-v2.AC5.5

Tests invoke CLI handlers directly (importing main/build_parser) rather
than spawning subprocesses. This is faster and avoids subprocess overhead.
Environment isolation uses:
- CC_SEARCH_DB_PATH env var for test-isolated database
- tmp_path-based directory structure mimicking ~/.claude/projects/
- monkeypatch to override get_claude_projects_dir()
"""

import errno
import fcntl
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Literal

import pytest
from tests.conftest import SESSION_ID_A, SESSION_ID_B, _make_session_lines

import cc_search_chats.cli as cli_module
from cc_search_chats import __version__
from cc_search_chats.cli import (
    _bounded_query_embedding,
    _contain_semantic_index,
    _index_age,
    _ProgressStream,
    build_parser,
    main,
)
from cc_search_chats.core.discovery import encode_project_path
from cc_search_chats.core.models import SessionMeta
from cc_search_chats.queueing import client_admission
from cc_search_chats.semantic import ModelUnavailable, query_embedder
from cc_search_chats.semantic.model import DIMENSIONS
from cc_search_chats.semantic.query_embedder import query_embedder_paths
from cc_search_chats.storage.index import (
    ProjectRebuildError,
    close_db,
    index_session,
    open_db,
    search,
)

if TYPE_CHECKING:
    from collections.abc import Generator

# ============================================================
# Fixture paths
# ============================================================

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_SAMPLE_SESSION = _FIXTURES_DIR / "sample_session.jsonl"
_COMPRESSED_SESSION = _FIXTURES_DIR / "compressed_session.jsonl"

SAMPLE_SESSION_ID = "11111111-1111-1111-1111-111111111111"
COMPRESSED_SESSION_ID = "22222222-2222-2222-2222-222222222222"
FAKE_PROJECT_PATH = "/home/testuser/myproject"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == f"cc-search-chats {__version__}\n"


def test_semantic_failure_names_phase_and_prints_literal_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PGSERVICE", "fixture")
    monkeypatch.delenv("CC_SEARCH_DB_PATH", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle phrase", "--semantic"],
    )
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    monkeypatch.setattr(
        "cc_search_chats.cli.client_admission", lambda _name: nullcontext()
    )

    def unavailable(_args, _dsn, _progress_stream):
        raise ModelUnavailable(
            "fixture model load failed",
            code="model_load_failed",
            phase="model_load",
        )

    monkeypatch.setattr("cc_search_chats.cli._handle_postgres", unavailable)

    with pytest.raises(SystemExit, match="8"):
        main()

    error = capsys.readouterr().err
    assert "model_load_failed" in error
    assert "model_load" in error
    assert "Literal search is required for complete current results" in error
    assert "cc-search-chats search 'needle phrase' --literal" in error


def test_postgresql_search_does_not_wait_on_a_local_admission_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGSERVICE", "fixture")
    monkeypatch.delenv("CC_SEARCH_DB_PATH", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle", "--literal", "--json"],
    )
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    admissions: list[str] = []

    def observed_admission(name: str):
        admissions.append(name)
        return nullcontext()

    monkeypatch.setattr("cc_search_chats.cli.client_admission", observed_admission)
    monkeypatch.setattr(
        "cc_search_chats.cli._handle_postgres",
        lambda _args, _dsn, _progress_stream: 0,
    )

    with pytest.raises(SystemExit, match="0"):
        main()

    assert admissions == []


def test_search_read_deadline_uses_the_single_reserved_answer_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGSERVICE", "fixture")
    monkeypatch.delenv("CC_SEARCH_DB_PATH", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle", "--literal", "--json"],
    )
    monkeypatch.setattr("cc_search_chats.cli.monotonic", lambda: 100.25)
    observed_timeouts: list[int] = []

    def observed_read_deadline(timeout_ms: int):
        observed_timeouts.append(timeout_ms)
        return nullcontext()

    monkeypatch.setattr("cc_search_chats.cli.read_deadline", observed_read_deadline)
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    monkeypatch.setattr(
        "cc_search_chats.cli._handle_postgres",
        lambda _args, _dsn, _progress_stream: 0,
    )

    with pytest.raises(SystemExit, match="0"):
        main(request_started=100.0)

    assert observed_timeouts == [4650]


def test_semantic_search_has_no_answer_or_postgres_read_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGSERVICE", "fixture")
    monkeypatch.delenv("CC_SEARCH_DB_PATH", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle", "--semantic", "--json"],
    )
    observed_timeouts: list[int] = []
    observed_deadlines: list[float | None] = []

    def observed_read_deadline(timeout_ms: int):
        observed_timeouts.append(timeout_ms)
        return nullcontext()

    def observed_handle(args, _dsn, _progress_stream):
        observed_deadlines.append(args.answer_deadline)
        return 0

    monkeypatch.setattr("cc_search_chats.cli.read_deadline", observed_read_deadline)
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    monkeypatch.setattr("cc_search_chats.cli._handle_postgres", observed_handle)

    with pytest.raises(SystemExit, match="0"):
        main(request_started=100.0)

    assert observed_timeouts == []
    assert observed_deadlines == [None]


def test_semantic_connection_has_no_deadline_derived_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(["search", "needle", "--semantic", "--json"])
    cli_module._validate_search_args(args, build_parser(), request_started=100.0)
    connect_calls: list[dict[str, object]] = []

    class Connection:
        def execute(self, _query, *_args, **_kwargs):
            raise AssertionError("semantic connection configured a SQL timeout")

    connection = Connection()

    def connect(_dsn: str, **kwargs):
        connect_calls.append(kwargs)
        return nullcontext(connection)

    monkeypatch.setattr(cli_module.psycopg, "connect", connect)

    with cli_module._postgres_connection(args, "fixture") as opened:
        cli_module._configure_postgres_connection(args, opened)

    assert connect_calls == [{"autocommit": True}]


@pytest.mark.parametrize(
    "retired_flag",
    ["--background-refresh", "--literal-only", "--semantic-only"],
)
def test_index_rejects_retired_partial_and_automatic_modes(
    retired_flag: str,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        build_parser().parse_args(["index", retired_flag])


def test_explicit_postgresql_migration_is_an_index_maintenance_mode() -> None:
    args = build_parser().parse_args(["index", "--migrate", "--json"])

    assert args.command == "index"
    assert args.migrate is True


def test_cli_query_embedding_seam_reuses_helper_and_observes_idle_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "cli-process-boundary-token"
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CC_SEARCH_SEMANTIC_WARM_SECONDS", "0.2")
    monkeypatch.setenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", token)
    monkeypatch.setenv("CC_SEARCH_MODEL_PATH", str(tmp_path / "missing-model"))
    monkeypatch.setattr(
        query_embedder,
        "_query_embedder_command",
        lambda: [
            sys.executable,
            "-m",
            "cc_search_chats.semantic.query_embedder",
            "--test-embedder-token",
            token,
        ],
    )
    spawned: list[subprocess.Popen[bytes]] = []
    spawn_detached_helper = query_embedder._spawn_detached_helper

    def observed_spawn(paths):
        process = spawn_detached_helper(paths)
        spawned.append(process)
        return process

    monkeypatch.setattr(query_embedder, "_spawn_detached_helper", observed_spawn)

    first = _bounded_query_embedding(
        "private query text",
        progress=lambda _phase, _state: None,
        quiet=True,
    )
    second = _bounded_query_embedding(
        "second private query",
        progress=lambda _phase, _state: None,
        quiet=True,
    )

    assert len(first.embedding) == len(second.embedding) == DIMENSIONS
    assert first.warm_reused is False
    assert second.warm_reused is True
    assert len(spawned) == 1
    deadline = monotonic() + 2
    socket_path = query_embedder_paths().socket
    while socket_path.exists() and monotonic() < deadline:
        Event().wait(0.01)
    spawned[0].wait(timeout=2)
    assert spawned[0].returncode == 0
    assert not socket_path.exists()


def test_search_scope_flags_are_explicit_and_everything_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parsed = build_parser().parse_args(
        [
            "search",
            "needle",
            "--agents",
            "--literal",
            "--tools",
            "--exhaustive",
        ]
    )
    assert parsed.agents is True
    assert parsed.tools is True
    assert parsed.exhaustive is True

    retired = build_parser().parse_args(
        ["search", "needle", "--literal", "--everything"]
    )
    assert retired.everything is True

    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle", "--literal", "--everything"],
    )
    with pytest.raises(SystemExit, match="2"):
        main()
    migration_error = capsys.readouterr().err
    assert "--everything was removed" in migration_error
    assert "--literal --tools --exhaustive" in migration_error
    assert "Reasoning and instructions remain excluded" in migration_error

    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle", "--semantic", "--tools"],
    )
    with pytest.raises(SystemExit, match="2"):
        main()
    assert "--tools requires --literal" in capsys.readouterr().err

    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle", "--semantic", "--exhaustive"],
    )
    with pytest.raises(SystemExit, match="2"):
        main()
    assert "--exhaustive requires --literal" in capsys.readouterr().err

    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "needle", "--literal", "--limit", "201"],
    )
    with pytest.raises(SystemExit, match="2"):
        main()
    assert "--limit must be between 1 and 200" in capsys.readouterr().err


def test_search_requires_one_explicit_described_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="2"):
        build_parser().parse_args(["search", "needle"])

    error = capsys.readouterr().err
    assert "--literal" in error
    assert "exact PostgreSQL full-text search; no model, no GPU" in error
    assert "--semantic" in error
    assert (
        "model-ranked search: hybrid fusion of full-text and embedding candidates "
        "by reciprocal rank; no deadline, and first use takes about 10 s"
    ) in error


def test_required_search_mode_error_does_not_depend_on_argparse_prose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit, match="2"):
        parser.error("arguments --semantic and --literal are required")

    error = capsys.readouterr().err
    assert "exact PostgreSQL full-text search; no model, no GPU" in error
    assert (
        "model-ranked search: hybrid fusion of full-text and embedding candidates "
        "by reciprocal rank; no deadline, and first use takes about 10 s"
    ) in error


@pytest.mark.parametrize(
    ("flag", "literal", "semantic"),
    [("--literal", True, False), ("--semantic", False, True)],
)
def test_search_accepts_exactly_one_explicit_mode(
    flag: str,
    *,
    literal: bool,
    semantic: bool,
) -> None:
    args = build_parser().parse_args(["search", "needle", flag])

    assert args.literal is literal
    assert args.semantic is semantic

    with pytest.raises(SystemExit, match="2"):
        build_parser().parse_args(["search", "needle", "--literal", "--semantic"])


@pytest.mark.parametrize(
    ("age_ms", "rendered"),
    [
        (12 * 60_000, "12m"),
        ((5 * 60 + 42) * 60_000, "5h 42m"),
        (((7 * 24 + 2) * 60 + 3) * 60_000, "7d 2h 3m"),
    ],
)
def test_index_age_renders_minutes_hours_and_days(age_ms: int, rendered: str) -> None:
    assert _index_age(age_ms) == rendered


def test_progress_heartbeat_tracks_the_active_phase() -> None:
    args = build_parser().parse_args(["list", "--json"])
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        progress = _ProgressStream(args)
        with progress.heartbeat(
            "scan",
            interval_seconds=0.01,
        ) as update:
            update("model_load", 12, 3, 7)
            Event().wait(timeout=0.04)

    events = [json.loads(line) for line in stderr.getvalue().splitlines()]
    assert events
    assert all(event["event"] == "heartbeat" for event in events)
    assert all(event["phase"] == "model_load" for event in events)
    assert all(event["run_id"] == 12 for event in events)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


def test_human_progress_renders_one_live_rate_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.0, 1.0, 3.0, 4.0))
    monkeypatch.setattr("cc_search_chats.cli.monotonic", lambda: next(clock))
    args = build_parser().parse_args(["list", "--progress", "human"])
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        progress = _ProgressStream(args)
        progress.emit(
            "semantic_embed",
            "running",
            completed_units=20,
            total_units=100,
        )
        progress.emit(
            "semantic_embed",
            "running",
            completed_units=80,
            total_units=100,
        )
        progress.emit(
            "semantic_embed",
            "complete",
            completed_units=100,
            total_units=100,
        )

    rendered = stderr.getvalue()
    assert "\rsemantic_embed: running 80/100 (80.0%) 30.0 units/s" in rendered
    assert rendered.count("\n") == 1
    assert rendered.endswith("\n")


# ============================================================
# Helpers
# ============================================================


def _install_fake_systemd_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stderr: str = "",
    sleep_seconds: float = 0,
) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    argv_log = tmp_path / "systemd-run-argv.jsonl"
    executable = fake_bin / "systemd-run"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        'log_path = Path(os.environ["CC_SEARCH_SYSTEMD_RUN_LOG"])\n'
        'with log_path.open("a", encoding="utf-8") as stream:\n'
        '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'sleep_seconds = float(os.environ.get("CC_SEARCH_SYSTEMD_RUN_SLEEP", "0"))\n'
        "if sleep_seconds:\n"
        "    time.sleep(sleep_seconds)\n"
        'detail = os.environ.get("CC_SEARCH_SYSTEMD_RUN_STDERR", "")\n'
        "if detail:\n"
        "    print(detail, file=sys.stderr)\n"
        'raise SystemExit(int(os.environ.get("CC_SEARCH_SYSTEMD_RUN_EXIT", "0")))\n',
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("CC_SEARCH_SYSTEMD_RUN_LOG", str(argv_log))
    monkeypatch.setenv("CC_SEARCH_SYSTEMD_RUN_EXIT", str(returncode))
    monkeypatch.setenv("CC_SEARCH_SYSTEMD_RUN_STDERR", stderr)
    monkeypatch.setenv("CC_SEARCH_SYSTEMD_RUN_SLEEP", str(sleep_seconds))
    return argv_log


def _systemd_run_invocations(argv_log: Path) -> list[list[str]]:
    return [
        json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()
    ]


def _prepare_postgres_index_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.delenv("CC_SEARCH_DB_PATH", raising=False)
    monkeypatch.delenv("CC_SEARCH_CONTAINED", raising=False)
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "1")
    monkeypatch.setenv("PGSERVICEFILE", "/nonexistent")
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", "index", *argv])
    monkeypatch.setattr(
        cli_module,
        "_handle_postgres",
        lambda *_args: pytest.fail("attempted a PostgreSQL operation"),
    )


def test_index_bus_failure_returns_schema_v4_json_containment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    detail = "Failed to connect to bus: No data available"
    argv_log = _install_fake_systemd_run(
        tmp_path,
        monkeypatch,
        returncode=1,
        stderr=detail,
    )
    _prepare_postgres_index_main(monkeypatch, "--json")
    monkeypatch.setattr(
        os,
        "execvp",
        lambda *_args: pytest.fail("attempted exec after failed preflight"),
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 9
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    envelope = json.loads(captured.out)
    assert envelope["schema_version"] == 4
    assert envelope["command"] == "index"
    assert envelope["status"] == "containment_unavailable"
    assert envelope["error"]["code"] == "systemd_scope_unavailable"
    assert envelope["error"]["phase"] == "containment"
    assert envelope["error"]["systemd_detail"] == detail
    assert "blocked by your sandbox" in envelope["error"]["remedy"]
    assert "ask the user for permission" in envelope["error"]["remedy"]
    progress = [json.loads(line) for line in captured.err.splitlines()]
    assert progress
    assert progress[-1]["event"] == "terminal"
    assert progress[-1]["state"] == envelope["status"]
    assert _systemd_run_invocations(argv_log) == [
        [
            "--user",
            "--scope",
            "--quiet",
            "--setenv=CC_SEARCH_CONTAINED=1",
            "--nice=10",
            "--property=MemoryHigh=24G",
            "--property=MemoryMax=32G",
            "--property=MemorySwapMax=4G",
            "--property=TasksMax=256",
            "--property=CPUWeight=25",
            "--property=IOWeight=25",
            "--",
            "true",
        ]
    ]


def test_index_bus_failure_in_non_tty_mode_emits_terminal_ndjson_with_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_systemd_run(
        tmp_path,
        monkeypatch,
        returncode=1,
        stderr="Failed to connect to bus: No data available",
    )
    _prepare_postgres_index_main(monkeypatch)
    monkeypatch.setattr(
        os,
        "execvp",
        lambda *_args: pytest.fail("attempted exec after failed preflight"),
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 9
    captured = capsys.readouterr()
    assert captured.out == ""
    progress = [json.loads(line) for line in captured.err.splitlines()]
    assert progress
    terminal = progress[-1]
    assert terminal["event"] == "terminal"
    assert terminal["state"] == "containment_unavailable"
    assert terminal["error"]["remedy"] == cli_module._CONTAINMENT_REMEDY


def test_index_bus_failure_with_human_progress_prints_done_line_and_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    detail = "Failed to connect to bus: No data available"
    _install_fake_systemd_run(
        tmp_path,
        monkeypatch,
        returncode=1,
        stderr=detail,
    )
    _prepare_postgres_index_main(monkeypatch, "--progress", "human")
    monkeypatch.setattr(
        os,
        "execvp",
        lambda *_args: pytest.fail("attempted exec after failed preflight"),
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 9
    captured = capsys.readouterr()
    assert captured.out == ""
    error = cli_module.SystemdScopeUnavailable(detail)
    expected = (
        "done: containment_unavailable\n"
        + str(error)
        + "\n"
        + cli_module._CONTAINMENT_REMEDY
        + "\n"
    )
    assert captured.err == expected
    for line in captured.err.splitlines():
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)


def test_index_reexecs_inside_bounded_systemd_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_log = _install_fake_systemd_run(tmp_path, monkeypatch)
    args = build_parser().parse_args(["index"])
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", "index"])
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "os.execvp", lambda _executable, command: launched.append(command)
    )

    _contain_semantic_index(args)

    command = launched[0]
    preflight = ["systemd-run", *_systemd_run_invocations(argv_log)[0]]
    separator = command.index("--")
    assert preflight == [*command[: separator + 1], "true"]
    assert command[:3] == ["systemd-run", "--user", "--scope"]
    assert "--setenv=CC_SEARCH_CONTAINED=1" in command
    assert "--nice=10" in command
    assert "--property=CPUWeight=25" in command
    assert "--property=IOWeight=25" in command
    assert "--property=MemoryMax=32G" in command
    assert "--property=TasksMax=256" in command
    assert command[separator + 1 : separator + 3] == ["ionice", "--class=idle"]
    assert command[separator + 3 :] == ["cc-search-chats", "index"]
    assert "--property=IOSchedulingClass=idle" not in command


def test_index_does_not_nest_scope_inside_packaged_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_log = _install_fake_systemd_run(tmp_path, monkeypatch)
    args = build_parser().parse_args(["index"])
    monkeypatch.setenv("CC_SEARCH_CONTAINED", "1")
    monkeypatch.setattr(
        "os.execvp",
        lambda _executable, _command: pytest.fail("attempted a nested systemd scope"),
    )

    _contain_semantic_index(args)

    assert not argv_log.exists()


def test_index_missing_systemd_run_returns_containment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    _prepare_postgres_index_main(monkeypatch, "--json")

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 9
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "containment_unavailable"
    assert envelope["error"]["code"] == "systemd_scope_unavailable"
    assert envelope["error"]["systemd_detail"] == (
        "systemd-run executable was not found"
    )


def test_index_systemd_scope_preflight_timeout_returns_containment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_systemd_run(tmp_path, monkeypatch, sleep_seconds=0.2)
    _prepare_postgres_index_main(monkeypatch, "--json")
    monkeypatch.setattr(
        cli_module,
        "_CONTAINMENT_PREFLIGHT_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        os,
        "execvp",
        lambda *_args: pytest.fail("attempted exec after timed-out preflight"),
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 9
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "containment_unavailable"
    assert envelope["error"]["code"] == "systemd_scope_unavailable"
    assert "timed out" in envelope["error"]["systemd_detail"]


def test_index_silent_systemd_scope_failure_reports_exact_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_systemd_run(tmp_path, monkeypatch, returncode=3)
    _prepare_postgres_index_main(monkeypatch, "--json")
    monkeypatch.setattr(
        os,
        "execvp",
        lambda *_args: pytest.fail("attempted exec after failed preflight"),
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 9
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["error"]["systemd_detail"] == (
        "systemd-run exited with status 3 without diagnostics"
    )


def test_index_process_boundary_reports_containment_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "Failed to connect to bus: No data available"
    _install_fake_systemd_run(
        tmp_path,
        monkeypatch,
        returncode=1,
        stderr=detail,
    )
    environment = os.environ.copy()
    environment.pop("CC_SEARCH_DB_PATH", None)
    environment.pop("CC_SEARCH_CONTAINED", None)
    environment.update(
        {
            "PGHOST": "127.0.0.1",
            "PGPORT": "1",
            "PGSERVICEFILE": "/nonexistent",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cc_search_chats.bootstrap import main; main()",
            "index",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 9
    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == 4
    assert envelope["command"] == "index"
    assert envelope["status"] == "containment_unavailable"
    assert envelope["error"]["code"] == "systemd_scope_unavailable"
    assert envelope["error"]["phase"] == "containment"
    assert envelope["error"]["systemd_detail"] == detail
    progress = [json.loads(line) for line in result.stderr.splitlines()]
    assert progress[-1]["event"] == "terminal"
    assert progress[-1]["state"] == "containment_unavailable"


def test_client_admission_blocks_once_until_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = runtime_dir / "postgres-read.lock"
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(runtime_dir))
    original_flock = fcntl.flock
    calls: list[int] = []
    attempted = Event()
    entered = Event()

    def observed_flock(file_descriptor, operation: int) -> None:
        calls.append(operation)
        attempted.set()
        original_flock(file_descriptor, operation)

    def contend() -> None:
        with client_admission("read"):
            entered.set()

    with (
        lock_path.open("a+") as held,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        original_flock(held, fcntl.LOCK_EX)
        monkeypatch.setattr("cc_search_chats.queueing.fcntl.flock", observed_flock)
        pending = executor.submit(contend)
        assert attempted.wait(timeout=1)
        try:
            assert calls == [fcntl.LOCK_EX]
            assert not entered.is_set()
        finally:
            original_flock(held, fcntl.LOCK_UN)
        pending.result(timeout=1)

    assert calls == [fcntl.LOCK_EX, fcntl.LOCK_UN]


def _setup_project_dir(tmp_path: Path) -> Path:
    """Create a fake Claude projects directory with fixture JSONL files.

    Returns the projects_dir path (the directory that would be ~/.claude/projects/).
    """
    projects_dir = tmp_path / "claude" / "projects"
    encoded = encode_project_path(FAKE_PROJECT_PATH)
    project_dir = projects_dir / encoded
    project_dir.mkdir(parents=True)

    # Copy fixture files into the project directory
    shutil.copy(_SAMPLE_SESSION, project_dir / f"{SAMPLE_SESSION_ID}.jsonl")
    shutil.copy(_COMPRESSED_SESSION, project_dir / f"{COMPRESSED_SESSION_ID}.jsonl")

    return projects_dir


def _run_cli(
    args: list[str],
    conn: sqlite3.Connection,
) -> tuple[int, str, str]:
    """Run a CLI command by parsing args and calling the handler directly.

    Returns (exit_code, stdout_text, stderr_text).
    """
    parser = build_parser()
    parsed = parser.parse_args(args)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    exit_code = 0
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        try:
            exit_code = parsed.func(parsed, conn)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        except ValueError as exc:
            stderr_buf.write(f"{exc}\n")
            exit_code = 1

    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _index_session_with_text(
    conn: sqlite3.Connection,
    tmp_path: Path,
    project_path: str,
    session_id: str,
    text: str,
) -> None:
    """Index a one-message session carrying ``text`` under ``project_path``."""
    line = json.dumps(
        {
            "type": "user",
            "uuid": f"u-{session_id}",
            "parentUuid": None,
            "timestamp": "2026-02-07T09:00:00.000Z",
            "sessionId": session_id,
            "message": {"role": "user", "content": text},
        }
    )
    session_file = tmp_path / f"{session_id}.jsonl"
    session_file.write_text(line, encoding="utf-8")
    stat = session_file.stat()
    index_session(
        conn,
        SessionMeta(
            session_id=session_id,
            file_path=str(session_file),
            project_path=project_path,
            file_size=stat.st_size,
            modified_at=stat.st_mtime,
        ),
    )


@pytest.fixture
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[sqlite3.Connection]:
    """Set up a fully isolated CLI test environment.

    Creates:
    - Fake ~/.claude/projects/ with fixture JSONL files
    - Isolated SQLite database via CC_SEARCH_DB_PATH
    - Patches get_claude_projects_dir() to return the fake dir

    Yields a database connection with sessions already indexed.
    """
    projects_dir = _setup_project_dir(tmp_path)

    # Patch discovery at the source module (covers lazy imports in storage/index.py)
    monkeypatch.setattr(
        "cc_search_chats.core.discovery.get_claude_projects_dir",
        lambda: projects_dir,
    )
    # Patch the top-level import in cli.py
    monkeypatch.setattr(
        "cc_search_chats.cli.get_claude_projects_dir",
        lambda: projects_dir,
    )

    # Isolated database
    db_path = tmp_path / "test_cli.db"
    conn = open_db(db_path)

    # Index both fixture sessions
    encoded = encode_project_path(FAKE_PROJECT_PATH)
    project_dir = projects_dir / encoded

    for session_id in [SAMPLE_SESSION_ID, COMPRESSED_SESSION_ID]:
        session_file = project_dir / f"{session_id}.jsonl"
        stat = session_file.stat()
        meta = SessionMeta(
            session_id=session_id,
            file_path=str(session_file),
            project_path=FAKE_PROJECT_PATH,
            file_size=stat.st_size,
            modified_at=stat.st_mtime,
        )
        index_session(conn, meta)

    yield conn
    close_db(conn)


class TestIndexAll:
    """index --all walks every project under the projects dir."""

    def test_index_all_runs(self, cli_env: sqlite3.Connection) -> None:
        exit_code, _, stderr = _run_cli(["index", "--all"], cli_env)
        assert exit_code == 0
        assert "project" in stderr.lower()

    def test_index_all_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(["index", "--all", "--json"], cli_env)
        assert exit_code == 0
        payload = json.loads(stdout)
        assert payload["projects"] >= 1
        assert "sessions_indexed" in payload
        assert "sessions_skipped" in payload

    def test_index_all_picks_up_new_project(
        self, cli_env: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A not-yet-indexed project dir is discovered and made searchable."""
        projects_dir = tmp_path / "claude" / "projects"
        new_proj = projects_dir / "-home-testuser-other"
        new_proj.mkdir(parents=True)
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        (new_proj / f"{SESSION_ID_A}.jsonl").write_text(
            "\n".join(lines), encoding="utf-8"
        )

        exit_code, stdout, _ = _run_cli(["index", "--all", "--json"], cli_env)
        assert exit_code == 0
        payload = json.loads(stdout)
        assert payload["projects"] == 2  # original fixture project + new one
        assert payload["sessions_indexed"] >= 1  # the new session

        results = search(cli_env, "database")
        assert SESSION_ID_A in {r["session_id"] for r in results}


class TestMainErrors:
    """Top-level database failures are rendered without a traceback."""

    def test_programming_error_is_not_rendered_as_environment_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Caller bugs remain loud programming errors."""
        conn = open_db(tmp_path / "closed.db")
        close_db(conn)
        monkeypatch.setenv(
            "CC_SEARCH_DB_PATH",
            str(tmp_path / "programming-error.db"),
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "search", "query", "--literal", "--all"],
        )
        monkeypatch.setattr("cc_search_chats.cli.open_db", lambda _db_path: conn)

        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            main()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_open_db_failure_is_caught(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CC_SEARCH_DB_PATH", "/tmp/test-index.sqlite")
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "search", "query", "--literal", "--all"],
        )

        def fail_open(_db_path: Path) -> sqlite3.Connection:
            raise RuntimeError("index diagnostic")

        monkeypatch.setattr("cc_search_chats.cli.open_db", fail_open)

        with pytest.raises(SystemExit) as exc_info:
            main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 1
        assert captured.out == ""
        assert captured.err == "index diagnostic\n"
        assert "Traceback" not in captured.err

    @pytest.mark.parametrize(
        ("error_name", "expect_deleted", "expected_remedy"),
        [
            ("SQLITE_CORRUPT_INDEX", True, None),
            (
                "SQLITE_READONLY_DIRECTORY",
                False,
                "Grant read/write access to the index directory",
            ),
            (
                "SQLITE_BUSY_TIMEOUT",
                False,
                "Wait for the concurrent indexing or database operation to finish",
            ),
        ],
    )
    def test_jit_sqlite_failure_deletes_only_explicit_damage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        error_name: str,
        expect_deleted: bool,
        expected_remedy: str | None,
    ) -> None:
        """Pre-query JIT CORRUPT deletes while READONLY and BUSY preserve."""
        db_path = tmp_path / "jit-index" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        setup_conn = open_db(db_path)
        close_db(setup_conn)
        original = db_path.read_bytes()
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "cc-search-chats",
                "search",
                "query",
                "--literal",
                "--project",
                str(tmp_path),
            ],
        )
        error = sqlite3.OperationalError("forced JIT failure")
        error.sqlite_errorname = error_name

        def fail_jit(
            _conn: sqlite3.Connection,
            _project_path: str,
            _include_subagents: bool = False,
        ) -> int:
            raise error

        monkeypatch.setattr(
            "cc_search_chats.cli._validate_project_dir",
            lambda _project_path: None,
        )
        monkeypatch.setattr("cc_search_chats.cli.jit_reindex", fail_jit)

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 1
        assert captured.out == ""
        assert error_name in captured.err
        assert "OperationalError: forced JIT failure" in captured.err
        assert "Traceback" not in captured.err
        if expect_deleted:
            assert (
                f"The damaged index at {db_path} is no longer present. "
                "Run `cc-search-chats index` to rebuild it."
            ) in captured.err
            assert "was deleted" not in captured.err
            assert not db_path.exists()
        else:
            assert "The database file was deleted" not in captured.err
            assert db_path.read_bytes() == original
            assert expected_remedy is not None
            assert expected_remedy in captured.err
            assert "corrupt" not in captured.err.lower()

    @pytest.mark.parametrize(
        ("error_name", "expect_absent", "expected_remedy"),
        [
            ("SQLITE_CORRUPT_INDEX", True, None),
            (
                "SQLITE_READONLY_DIRECTORY",
                False,
                "Grant read/write access to the index directory",
            ),
            (
                "SQLITE_BUSY_TIMEOUT",
                False,
                "Wait for the concurrent indexing or database operation to finish",
            ),
        ],
    )
    def test_persistent_query_failure_discards_only_explicit_damage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        error_name: str,
        expect_absent: bool,
        expected_remedy: str | None,
    ) -> None:
        """Persistent query CORRUPT discards while READONLY and BUSY preserve."""
        db_path = tmp_path / "query-index" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        setup_conn = open_db(db_path)
        close_db(setup_conn)
        original = db_path.read_bytes()
        error = sqlite3.OperationalError("forced persistent query failure")
        error.sqlite_errorname = error_name

        def fail_search(
            _conn: sqlite3.Connection,
            _query: str,
            *,
            epoch: int | None = None,
            project: str | None = None,
            days: int | None = None,
            limit: int = 50,
        ) -> list[sqlite3.Row]:
            del epoch, project, days, limit
            raise error

        monkeypatch.setattr("cc_search_chats.cli.search", fail_search)
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "search", "query", "--literal", "--all"],
        )

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 1
        assert captured.out == ""
        assert error_name in captured.err
        assert "OperationalError: forced persistent query failure" in captured.err
        assert "Traceback" not in captured.err
        if expect_absent:
            assert (
                f"The damaged index at {db_path} is no longer present. "
                "Run `cc-search-chats index` to rebuild it."
            ) in captured.err
            assert "was deleted" not in captured.err
            assert not db_path.exists()
        else:
            assert "The database file was deleted" not in captured.err
            assert db_path.read_bytes() == original
            assert expected_remedy is not None
            assert expected_remedy in captured.err
            assert "corrupt" not in captured.err.lower()

    def test_indexing_sqlite_failure_is_rendered_with_index_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A READONLY error escaping incremental indexing names its cause."""
        db_path = tmp_path / "all-index" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "index", "--all"],
        )
        error = sqlite3.OperationalError("index write denied")
        error.sqlite_errorname = "SQLITE_READONLY"

        def fail_index(
            _conn: sqlite3.Connection,
            _projects_dir: Path | None = None,
            _include_subagents: bool = False,
        ) -> dict[str, int]:
            raise error

        monkeypatch.setattr(
            "cc_search_chats.cli.index_all_projects",
            fail_index,
        )

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 1
        assert captured.out == ""
        assert "SQLITE_READONLY" in captured.err
        assert "OperationalError: index write denied" in captured.err
        assert "The index contains no recorded session indexing time." in captured.err
        assert str(db_path.parent) in captured.err
        assert "Traceback" not in captured.err
        assert "corrupt" not in captured.err.lower()

    def test_source_file_failure_does_not_recommend_database_permissions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A vanished JSONL is not diagnosed as index-directory denial."""
        projects_dir = _setup_project_dir(tmp_path)
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )
        db_path = tmp_path / "source-race" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "index", "--all"],
        )
        original_open = Path.open

        def fail_jsonl_open(
            path: Path,
            mode: Literal["r"] = "r",
            buffering: int = -1,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> io.TextIOWrapper:
            if path.suffix == ".jsonl":
                raise FileNotFoundError(f"session vanished: {path}")
            return original_open(
                path,
                mode=mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

        monkeypatch.setattr(Path, "open", fail_jsonl_open)

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 0
        assert captured.out == ""
        assert captured.err.count("Warning: Could not open session file") == 2
        assert "session vanished:" in captured.err
        assert ".jsonl" in captured.err
        assert "Skipping." in captured.err
        assert "Indexed 0 sessions (2 skipped) across 1 projects" in captured.err
        assert "Grant read/write access" not in captured.err
        assert "sandbox_workspace_write.writable_roots" not in captured.err
        assert "Traceback" not in captured.err

    def test_source_permission_failure_does_not_recommend_index_permissions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A denied JSONL names that file instead of the database directory."""
        projects_dir = _setup_project_dir(tmp_path)
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )
        db_path = tmp_path / "source-denied" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "index", "--all"],
        )
        original_open = Path.open

        def deny_jsonl_open(
            path: Path,
            mode: Literal["r"] = "r",
            buffering: int = -1,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> io.TextIOWrapper:
            if path.suffix == ".jsonl":
                raise PermissionError(
                    errno.EACCES,
                    "session read denied",
                    str(path),
                )
            return original_open(
                path,
                mode=mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

        monkeypatch.setattr(Path, "open", deny_jsonl_open)

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 0
        assert captured.out == ""
        assert captured.err.count("Warning: Could not open session file") == 2
        assert "session read denied" in captured.err
        assert ".jsonl" in captured.err
        assert "Skipping." in captured.err
        assert "Indexed 0 sessions (2 skipped) across 1 projects" in captured.err
        assert "Grant read/write access" not in captured.err
        assert "sandbox_workspace_write.writable_roots" not in captured.err
        assert "Traceback" not in captured.err

    def test_project_rebuild_readonly_failure_gets_complete_r4_diagnostic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A rebuild preserves its SQLite cause for the shared formatter."""
        projects_dir = _setup_project_dir(tmp_path)
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )
        monkeypatch.setattr(
            "cc_search_chats.cli.get_claude_projects_dir",
            lambda: projects_dir,
        )
        db_path = tmp_path / "readonly-rebuild" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        conn = open_db(db_path)
        conn.execute(
            "INSERT INTO session "
            "(session_id, project_path, file_path, file_size, modified_at, "
            "indexed_at, summary) VALUES "
            "('existing', '/existing', '/existing.jsonl', 1, "
            "'2026-07-31T00:00:00Z', '2026-07-31T03:00:00Z', NULL)"
        )
        conn.commit()
        conn.execute("PRAGMA query_only=ON")
        monkeypatch.setattr("cc_search_chats.cli.open_db", lambda _db_path: conn)
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "index", "--project", FAKE_PROJECT_PATH],
        )

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 1
        assert captured.out == ""
        assert "Project index rebuild failed" in captured.err
        assert "SQLITE_READONLY" in captured.err
        assert "OperationalError: attempt to write a readonly database" in captured.err
        assert (
            "Most recent recorded session indexing: 2026-07-31T03:00:00Z."
            in captured.err
        )
        assert "prior index contents remain intact" in captured.err
        assert str(db_path.parent) in captured.err
        assert "sandbox_workspace_write.writable_roots" in captured.err
        assert "Traceback" not in captured.err
        assert "corrupt" not in captured.err.lower()

    def test_project_rebuild_corruption_deletes_index(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A structured rebuild error transfers cleanup ownership to main."""
        projects_dir = _setup_project_dir(tmp_path)
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )
        monkeypatch.setattr(
            "cc_search_chats.cli.get_claude_projects_dir",
            lambda: projects_dir,
        )
        db_path = tmp_path / "corrupt-rebuild" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorname = "SQLITE_CORRUPT_INDEX"

        def fail_rebuild(
            _conn: sqlite3.Connection,
            project_path: str,
        ) -> dict[str, int]:
            raise ProjectRebuildError(project_path, error)

        monkeypatch.setattr("cc_search_chats.cli.reindex_project", fail_rebuild)
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "index", "--project", FAKE_PROJECT_PATH],
        )

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 1
        assert captured.out == ""
        assert "SQLITE_CORRUPT_INDEX" in captured.err
        assert "DatabaseError: database disk image is malformed" in captured.err
        assert (
            f"The damaged index at {db_path} is no longer present. "
            "Run `cc-search-chats index` to rebuild it."
        ) in captured.err
        assert "was deleted" not in captured.err
        assert "prior index contents remain intact" not in captured.err
        assert "Traceback" not in captured.err
        assert not db_path.exists()

    def test_project_rebuild_busy_failure_gets_wait_and_state_diagnostic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A competing WAL writer reaches the BUSY-specific R4 remedy."""
        projects_dir = _setup_project_dir(tmp_path)
        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )
        monkeypatch.setattr(
            "cc_search_chats.cli.get_claude_projects_dir",
            lambda: projects_dir,
        )
        db_path = tmp_path / "busy-rebuild" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        setup_conn = open_db(db_path)
        setup_conn.execute(
            "INSERT INTO session "
            "(session_id, project_path, file_path, file_size, modified_at, "
            "indexed_at, summary) VALUES "
            "('existing', '/existing', '/existing.jsonl', 1, "
            "'2026-07-31T00:00:00Z', '2026-07-31T04:00:00Z', NULL)"
        )
        setup_conn.commit()
        close_db(setup_conn)

        conn = open_db(db_path)
        conn.execute("PRAGMA busy_timeout=0")
        blocker = open_db(db_path)
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr("cc_search_chats.cli.open_db", lambda _db_path: conn)
        monkeypatch.setattr(
            sys,
            "argv",
            ["cc-search-chats", "index", "--project", FAKE_PROJECT_PATH],
        )
        try:
            with pytest.raises(SystemExit) as caught:
                main()
        finally:
            blocker.rollback()
            close_db(blocker)

        captured = capsys.readouterr()
        assert caught.value.code == 1
        assert captured.out == ""
        assert "Project index rebuild failed" in captured.err
        assert "SQLITE_BUSY" in captured.err
        assert "OperationalError: database is locked" in captured.err
        assert (
            "Most recent recorded session indexing: 2026-07-31T04:00:00Z."
            in captured.err
        )
        assert "prior index contents remain intact" in captured.err
        assert "Wait for the concurrent indexing" in captured.err
        assert "sandbox_workspace_write.writable_roots" not in captured.err
        assert "Traceback" not in captured.err
        assert "corrupt" not in captured.err.lower()


class TestSearchScope:
    """Local-first search with broaden-on-miss across projects."""

    def test_all_flag_searches_everything(self, cli_env: sqlite3.Connection) -> None:
        _, stdout, _ = _run_cli(
            ["search", "database", "--literal", "--all", "--json"], cli_env
        )
        parsed = json.loads(stdout)
        assert parsed["scope"] == "all"
        assert len(parsed["results"]) > 0

    def test_local_hit_when_cwd_is_a_project(
        self, cli_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("os.getcwd", lambda: FAKE_PROJECT_PATH)
        _, stdout, _ = _run_cli(["search", "database", "--literal", "--json"], cli_env)
        parsed = json.loads(stdout)
        assert parsed["scope"] == "local"
        assert len(parsed["results"]) > 0

    def test_broaden_on_miss_finds_other_project(
        self,
        cli_env: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A term absent locally is found by widening to all projects."""
        _index_session_with_text(
            cli_env, tmp_path, "/home/other/proj", SESSION_ID_B, "a wandering pelican"
        )
        monkeypatch.setattr("os.getcwd", lambda: FAKE_PROJECT_PATH)
        _, stdout, _ = _run_cli(["search", "pelican", "--literal", "--json"], cli_env)
        parsed = json.loads(stdout)
        assert parsed["scope"] == "widened"
        assert parsed["searched_project"] == FAKE_PROJECT_PATH
        assert "/home/other/proj" in {r["project_path"] for r in parsed["results"]}

    def test_cwd_not_a_project_searches_all(
        self, cli_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("os.getcwd", lambda: "/not/a/claude/project/xyz")
        _, stdout, _ = _run_cli(["search", "database", "--literal", "--json"], cli_env)
        parsed = json.loads(stdout)
        assert parsed["scope"] == "all"
        assert len(parsed["results"]) > 0

    def test_widened_miss_hints_index_all(
        self, cli_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A machine-wide miss tells the user to run index --all (human mode)."""
        monkeypatch.setattr("os.getcwd", lambda: FAKE_PROJECT_PATH)
        _, stdout, stderr = _run_cli(
            ["search", "zzqxnope98765term", "--literal"], cli_env
        )
        assert stdout == ""
        assert "index --all" in stderr

    def test_explicit_project_does_not_broaden(
        self, cli_env: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """--project narrows and never widens, even on a miss."""
        _index_session_with_text(
            cli_env, tmp_path, "/home/other/proj", SESSION_ID_B, "a wandering pelican"
        )
        _, stdout, _ = _run_cli(
            [
                "search",
                "pelican",
                "--literal",
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            cli_env,
        )
        parsed = json.loads(stdout)
        assert parsed["scope"] == "local"
        assert parsed["results"] == []


class TestExcludedThinking:
    """Private thinking remains excluded from every supported search mode."""

    def _write_thinking_session(self, tmp_path: Path) -> None:
        proj = tmp_path / "claude" / "projects" / encode_project_path(FAKE_PROJECT_PATH)
        line = json.dumps(
            {
                "type": "assistant",
                "uuid": "a-think",
                "timestamp": "2026-02-07T10:00:00.000Z",
                "sessionId": SESSION_ID_A,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "secretwombat in thoughts"},
                    ],
                },
            }
        )
        (proj / f"{SESSION_ID_A}.jsonl").write_text(line, encoding="utf-8")

    def test_normal_search_misses_thinking(
        self, cli_env: sqlite3.Connection, tmp_path: Path
    ) -> None:
        self._write_thinking_session(tmp_path)
        _, stdout, _ = _run_cli(
            [
                "search",
                "secretwombat",
                "--literal",
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            cli_env,
        )
        assert json.loads(stdout)["results"] == []


# ============================================================
# AC5.1: All five subcommands accessible and produce output
# ============================================================


class TestSubcommandsAccessible:
    """cc-search-v2.AC5.1: Each subcommand produces exit code 0 and output."""

    def test_search_runs(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["search", "database", "--literal", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_extract_with_session_id(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_extract_auto_discover(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
            ["extract", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0
        assert "Auto-discovered session:" in stderr

    def test_list_runs(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["list", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_index_runs(self, cli_env: sqlite3.Connection) -> None:
        exit_code, _, stderr = _run_cli(
            ["index", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert "Indexed" in stderr

    def test_context_runs(self, cli_env: sqlite3.Connection) -> None:
        # First, get a valid UUID from the indexed data
        row = cli_env.execute("SELECT uuid FROM message LIMIT 1").fetchone()
        assert row is not None
        uuid = row["uuid"]

        exit_code, stdout, _ = _run_cli(
            ["context", uuid],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_search_no_results_still_zero_exit(
        self, cli_env: sqlite3.Connection
    ) -> None:
        """Search with no matches returns exit code 0 (not an error)."""
        exit_code, _, _ = _run_cli(
            [
                "search",
                "xyznonexistentterm",
                "--literal",
                "--project",
                FAKE_PROJECT_PATH,
            ],
            cli_env,
        )
        assert exit_code == 0


# ============================================================
# AC5.2: --json output is valid JSON
# ============================================================


class TestJsonOutput:
    """cc-search-v2.AC5.2: --json output is valid JSON parseable by json.loads()."""

    def test_search_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            [
                "search",
                "database",
                "--literal",
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert isinstance(parsed["results"], list)

    def test_extract_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert "session_id" in parsed
        assert "epochs" in parsed

    def test_list_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["list", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert isinstance(parsed["sessions"], list)

    def test_context_json(self, cli_env: sqlite3.Connection) -> None:
        row = cli_env.execute("SELECT uuid FROM message LIMIT 1").fetchone()
        uuid = row["uuid"]

        exit_code, stdout, _ = _run_cli(
            ["context", uuid, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert "target" in parsed
        assert "before" in parsed
        assert "after" in parsed

    def test_search_json_empty_results(self, cli_env: sqlite3.Connection) -> None:
        """Empty search results still produce valid JSON (empty array)."""
        exit_code, stdout, _ = _run_cli(
            [
                "search",
                "xyznonexistentterm",
                "--literal",
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert parsed["results"] == []
        assert parsed["scope"] == "local"  # explicit --project never broadens

    def test_extract_json_has_messages(self, cli_env: sqlite3.Connection) -> None:
        """Extract JSON includes actual messages."""
        _, stdout, _ = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        parsed = json.loads(stdout)
        assert parsed["session_id"] == SAMPLE_SESSION_ID
        total_msgs = sum(len(ep["messages"]) for ep in parsed["epochs"])
        assert total_msgs > 0

    def test_list_json_has_sessions(self, cli_env: sqlite3.Connection) -> None:
        """List JSON includes the indexed sessions."""
        _, stdout, _ = _run_cli(
            ["list", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        parsed = json.loads(stdout)
        session_ids = {s["session_id"] for s in parsed["sessions"]}
        assert SAMPLE_SESSION_ID in session_ids
        assert COMPRESSED_SESSION_ID in session_ids

    def test_index_json(self, cli_env: sqlite3.Connection) -> None:
        """Index --json distinguishes indexed from skipped sessions."""
        exit_code, stdout, _ = _run_cli(
            ["index", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert "sessions_indexed" in parsed
        assert "sessions_skipped" in parsed
        assert "project_path" in parsed
        assert parsed["project_path"] == FAKE_PROJECT_PATH
        assert parsed["sessions_indexed"] >= 0
        assert parsed["sessions_skipped"] >= 0


# ============================================================
# AC5.3: Human-readable output has role labels, epoch markers, timestamps
# ============================================================


class TestHumanReadableOutput:
    """cc-search-v2.AC5.3: Human-readable output quality checks."""

    def test_extract_shows_role_labels(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        # Should have both user and assistant labels
        assert "User" in stdout or "user" in stdout
        assert "Assistant" in stdout or "assistant" in stdout

    def test_extract_shows_timestamps(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert "2026-02-07" in stdout

    def test_extract_compressed_shows_epoch_markers(
        self, cli_env: sqlite3.Connection
    ) -> None:
        """Compressed session extract shows epoch markers."""
        exit_code, stdout, _ = _run_cli(
            ["extract", COMPRESSED_SESSION_ID, "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert "Epoch" in stdout
        assert "auto" in stdout
        # Should mention tokens
        assert "42000" in stdout

    def test_extract_compressed_json_has_epochs(
        self, cli_env: sqlite3.Connection
    ) -> None:
        """Compressed session has multiple epochs in JSON output."""
        _, stdout, _ = _run_cli(
            [
                "extract",
                COMPRESSED_SESSION_ID,
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            cli_env,
        )
        parsed = json.loads(stdout)
        assert len(parsed["epochs"]) == 2  # epoch 0 and epoch 1

    def test_search_results_show_session_id(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["search", "database", "--literal", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        # Output should contain a session ID
        assert "1111" in stdout or "2222" in stdout

    def test_list_shows_session_ids(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            ["list", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert SAMPLE_SESSION_ID in stdout or COMPRESSED_SESSION_ID in stdout

    def test_context_shows_target_marker(self, cli_env: sqlite3.Connection) -> None:
        row = cli_env.execute("SELECT uuid FROM message LIMIT 1").fetchone()
        uuid = row["uuid"]

        exit_code, stdout, _ = _run_cli(
            ["context", uuid],
            cli_env,
        )
        assert exit_code == 0
        assert "TARGET" in stdout or ">>>" in stdout


# ============================================================
# AC5.4: --help shows usage, flags, and examples
# ============================================================


class TestHelpExamples:
    """cc-search-v2.AC5.4: --help on each subcommand shows examples."""

    @pytest.mark.parametrize(
        "subcommand",
        ["search", "extract", "list", "index", "context"],
    )
    def test_help_contains_examples(self, subcommand: str) -> None:
        parser = build_parser()
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), suppress(SystemExit):
            parser.parse_args([subcommand, "--help"])
        output = stdout_buf.getvalue()
        assert "Examples:" in output
        assert "cc-search-chats" in output


# ============================================================
# AC5.5: Large session performance
# ============================================================


class TestLargeSession:
    """cc-search-v2.AC5.5: 8MB+ session handles without hanging or excessive memory."""

    @pytest.fixture
    def large_session_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[sqlite3.Connection]:
        """Create a ~8MB JSONL session file and index it."""
        projects_dir = tmp_path / "claude" / "projects"
        encoded = encode_project_path(FAKE_PROJECT_PATH)
        project_dir = projects_dir / encoded
        project_dir.mkdir(parents=True)

        session_id = "33333333-3333-3333-3333-333333333333"
        session_file = project_dir / f"{session_id}.jsonl"

        # Generate ~8MB of JSONL lines (~200 bytes per line, ~40000 lines)
        lines = []
        for i in range(45000):
            # Alternate user/assistant
            if i % 2 == 0:
                line = json.dumps(
                    {
                        "type": "user",
                        "uuid": f"msg-{i:06d}",
                        "parentUuid": f"msg-{i - 1:06d}" if i > 0 else None,
                        "timestamp": f"2026-02-07T10:{(i // 60) % 60:02d}:{i % 60:02d}.000Z",
                        "sessionId": session_id,
                        "message": {
                            "role": "user",
                            "content": f"Message number {i} about database indexing and search query optimisation techniques for large datasets",
                        },
                    }
                )
            else:
                line = json.dumps(
                    {
                        "type": "assistant",
                        "uuid": f"msg-{i:06d}",
                        "parentUuid": f"msg-{i - 1:06d}",
                        "timestamp": f"2026-02-07T10:{(i // 60) % 60:02d}:{i % 60:02d}.000Z",
                        "sessionId": session_id,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Response {i} covering performance tuning, index creation, and query plan analysis for optimal results",
                                }
                            ],
                        },
                    }
                )
            lines.append(line)

        session_file.write_text("\n".join(lines), encoding="utf-8")
        file_size = session_file.stat().st_size
        assert file_size > 8_000_000, f"Expected >8MB, got {file_size}"

        monkeypatch.setattr(
            "cc_search_chats.core.discovery.get_claude_projects_dir",
            lambda: projects_dir,
        )
        monkeypatch.setattr(
            "cc_search_chats.cli.get_claude_projects_dir",
            lambda: projects_dir,
        )

        db_path = tmp_path / "test_large.db"
        conn = open_db(db_path)

        stat = session_file.stat()
        meta = SessionMeta(
            session_id=session_id,
            file_path=str(session_file),
            project_path=FAKE_PROJECT_PATH,
            file_size=stat.st_size,
            modified_at=stat.st_mtime,
        )
        index_session(conn, meta)

        yield conn
        close_db(conn)

    def test_search_large_session(self, large_session_env: sqlite3.Connection) -> None:
        """Search against a large session completes within reasonable time."""
        exit_code, stdout, _ = _run_cli(
            [
                "search",
                "database",
                "--literal",
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            large_session_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed["results"], list)
        assert len(parsed["results"]) > 0

    def test_extract_large_session(self, large_session_env: sqlite3.Connection) -> None:
        """Extract a large session completes within reasonable time."""
        exit_code, stdout, _ = _run_cli(
            [
                "extract",
                "33333333-3333-3333-3333-333333333333",
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            large_session_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        total_msgs = sum(len(ep["messages"]) for ep in parsed["epochs"])
        assert total_msgs > 1000


# ============================================================
# Error handling tests
# ============================================================


class TestErrorHandling:
    """Error cases return exit code 1 with messages to stderr."""

    def test_extract_invalid_session_id(self, cli_env: sqlite3.Connection) -> None:
        exit_code, _, stderr = _run_cli(
            [
                "extract",
                "nonexistent-session-id",
                "--project",
                FAKE_PROJECT_PATH,
            ],
            cli_env,
        )
        # extract_session raises ValueError which is caught in main()
        # but since we call the handler directly, we need to catch it ourselves
        assert exit_code == 1 or "not found" in stderr.lower()

    def test_context_invalid_uuid(self, cli_env: sqlite3.Connection) -> None:
        exit_code, _, stderr = _run_cli(
            ["context", "nonexistent-uuid"],
            cli_env,
        )
        assert exit_code == 1 or "not found" in stderr.lower()


# ============================================================
# Epoch filter tests
# ============================================================


class TestEpochFilters:
    """Epoch filters work correctly through the CLI."""

    def test_search_epoch_0(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            [
                "search",
                "database",
                "--literal",
                "--project",
                FAKE_PROJECT_PATH,
                "--epoch",
                "0",
                "--json",
            ],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        for result in parsed["results"]:
            assert result["epoch"] == 0

    def test_extract_epoch_0(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            [
                "extract",
                COMPRESSED_SESSION_ID,
                "--project",
                FAKE_PROJECT_PATH,
                "--epoch",
                "0",
                "--json",
            ],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        # Should only have epoch 0
        for ep in parsed["epochs"]:
            assert ep["epoch"] == 0

    def test_extract_epoch_1(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, _ = _run_cli(
            [
                "extract",
                COMPRESSED_SESSION_ID,
                "--project",
                FAKE_PROJECT_PATH,
                "--epoch",
                "1",
                "--json",
            ],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        for ep in parsed["epochs"]:
            assert ep["epoch"] == 1


# ============================================================
# --verbose flag tests
# ============================================================


class TestVerboseFlag:
    """Tests for --verbose flag on extract and context subcommands."""

    def test_extract_accepts_verbose(self, cli_env: sqlite3.Connection) -> None:
        """--verbose is accepted by the extract subcommand."""
        exit_code, stdout, _ = _run_cli(
            [
                "extract",
                SAMPLE_SESSION_ID,
                "--project",
                FAKE_PROJECT_PATH,
                "--verbose",
            ],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_context_accepts_verbose(self, cli_env: sqlite3.Connection) -> None:
        """--verbose is accepted by the context subcommand."""
        row = cli_env.execute("SELECT uuid FROM message LIMIT 1").fetchone()
        assert row is not None
        uuid = row["uuid"]

        exit_code, stdout, _ = _run_cli(
            ["context", uuid, "--verbose"],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_extract_verbose_help(self) -> None:
        """--verbose appears in extract --help output."""
        parser = build_parser()
        stdout_buf = io.StringIO()
        try:
            with redirect_stdout(stdout_buf):
                parser.parse_args(["extract", "--help"])
        except SystemExit:
            pass
        assert "--verbose" in stdout_buf.getvalue()

    def test_context_verbose_help(self) -> None:
        """--verbose appears in context --help output."""
        parser = build_parser()
        stdout_buf = io.StringIO()
        try:
            with redirect_stdout(stdout_buf):
                parser.parse_args(["context", "--help"])
        except SystemExit:
            pass
        assert "--verbose" in stdout_buf.getvalue()
