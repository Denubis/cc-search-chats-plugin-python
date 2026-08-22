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
import shutil
import sqlite3
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Event
from typing import Literal

import pytest

from cc_search_chats import __version__
from cc_search_chats.cli import _contain_semantic_index, build_parser, main
from cc_search_chats.core.discovery import encode_project_path
from cc_search_chats.core.models import SessionMeta
from cc_search_chats.queueing import client_admission
from cc_search_chats.semantic import ModelUnavailable
from cc_search_chats.storage.index import (
    ProjectRebuildError,
    close_db,
    index_session,
    open_db,
    search,
)
from tests.conftest import SESSION_ID_A, SESSION_ID_B, _make_session_lines

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
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", "search", "needle phrase"])
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda args: None
    )
    monkeypatch.setattr(
        "cc_search_chats.cli.client_admission", lambda name: nullcontext()
    )

    def unavailable(args, dsn):
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


# ============================================================
# Helpers
# ============================================================


@pytest.mark.parametrize("mode", [(), ("--literal-only",), ("--semantic-only",)])
def test_index_reexecs_inside_bounded_systemd_scope(
    monkeypatch: pytest.MonkeyPatch,
    mode: tuple[str, ...],
) -> None:
    args = build_parser().parse_args(["index", *mode])
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", "index", *mode])
    launched = []
    monkeypatch.setattr(
        "os.execvp", lambda executable, command: launched.append(command)
    )

    _contain_semantic_index(args)

    command = launched[0]
    assert command[:3] == ["systemd-run", "--user", "--scope"]
    assert "--setenv=CC_SEARCH_CONTAINED=1" in command
    assert "--nice=10" in command
    assert "--property=CPUWeight=25" in command
    assert "--property=IOWeight=25" in command
    assert "--property=MemoryMax=32G" in command
    assert "--property=TasksMax=256" in command
    separator = command.index("--")
    assert command[separator + 1 : separator + 3] == ["ionice", "--class=idle"]
    assert command[separator + 3 :] == ["cc-search-chats", "index", *mode]
    assert "--property=IOSchedulingClass=idle" not in command


def test_index_does_not_nest_scope_inside_packaged_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(["index"])
    monkeypatch.setenv("CC_SEARCH_CONTAINED", "1")
    monkeypatch.setattr(
        "os.execvp",
        lambda executable, command: pytest.fail("attempted a nested systemd scope"),
    )

    _contain_semantic_index(args)


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
            print(str(exc), file=sys.stderr)
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
        exit_code, stdout, stderr = _run_cli(["index", "--all"], cli_env)
        assert exit_code == 0
        assert "project" in stderr.lower()

    def test_index_all_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(["index", "--all", "--json"], cli_env)
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
            ["cc-search-chats", "search", "query", "--all"],
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
            sys, "argv", ["cc-search-chats", "search", "query", "--all"]
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
            ["cc-search-chats", "search", "query", "--all"],
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

    def test_everything_corruption_preserves_persistent_index_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Transient full-content damage never deletes the persistent index."""
        projects_dir = _setup_project_dir(tmp_path)
        monkeypatch.setattr(
            "cc_search_chats.cli.get_claude_projects_dir",
            lambda: projects_dir,
        )
        db_path = tmp_path / "persistent" / "index.db"
        monkeypatch.setenv("CC_SEARCH_DB_PATH", str(db_path))
        conn = open_db(db_path)
        close_db(conn)
        original = db_path.read_bytes()
        error = sqlite3.DatabaseError("transient database disk image is malformed")
        error.sqlite_errorname = "SQLITE_CORRUPT_INDEX"

        def fail_transient_search(
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

        monkeypatch.setattr(
            "cc_search_chats.storage.index.search",
            fail_transient_search,
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "cc-search-chats",
                "search",
                "query",
                "--everything",
                "--project",
                FAKE_PROJECT_PATH,
            ],
        )

        with pytest.raises(SystemExit) as caught:
            main()

        captured = capsys.readouterr()
        assert caught.value.code == 1
        assert captured.out == ""
        assert "SQLITE_CORRUPT_INDEX" in captured.err
        assert (
            "DatabaseError: transient database disk image is malformed" in captured.err
        )
        assert "in-memory" in captured.err.lower()
        assert "The persistent index was not modified." in captured.err
        assert "The database file was deleted" not in captured.err
        assert "`cc-search-chats index`" not in captured.err
        assert "Traceback" not in captured.err
        assert db_path.read_bytes() == original

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
        _, stdout, _ = _run_cli(["search", "database", "--all", "--json"], cli_env)
        parsed = json.loads(stdout)
        assert parsed["scope"] == "all"
        assert len(parsed["results"]) > 0

    def test_local_hit_when_cwd_is_a_project(
        self, cli_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("os.getcwd", lambda: FAKE_PROJECT_PATH)
        _, stdout, _ = _run_cli(["search", "database", "--json"], cli_env)
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
        _, stdout, _ = _run_cli(["search", "pelican", "--json"], cli_env)
        parsed = json.loads(stdout)
        assert parsed["scope"] == "widened"
        assert parsed["searched_project"] == FAKE_PROJECT_PATH
        assert "/home/other/proj" in {r["project_path"] for r in parsed["results"]}

    def test_cwd_not_a_project_searches_all(
        self, cli_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("os.getcwd", lambda: "/not/a/claude/project/xyz")
        _, stdout, _ = _run_cli(["search", "database", "--json"], cli_env)
        parsed = json.loads(stdout)
        assert parsed["scope"] == "all"
        assert len(parsed["results"]) > 0

    def test_widened_miss_hints_index_all(
        self, cli_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A machine-wide miss tells the user to run index --all (human mode)."""
        monkeypatch.setattr("os.getcwd", lambda: FAKE_PROJECT_PATH)
        _, stdout, stderr = _run_cli(["search", "zzqxnope98765term"], cli_env)
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
            ["search", "pelican", "--project", FAKE_PROJECT_PATH, "--json"], cli_env
        )
        parsed = json.loads(stdout)
        assert parsed["scope"] == "local"
        assert parsed["results"] == []


class TestSearchEverything:
    """--everything runs a live full-content scan (thinking + tool calls)."""

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
            ["search", "secretwombat", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert json.loads(stdout)["results"] == []

    def test_everything_finds_thinking(
        self, cli_env: sqlite3.Connection, tmp_path: Path
    ) -> None:
        self._write_thinking_session(tmp_path)
        _, stdout, _ = _run_cli(
            [
                "search",
                "secretwombat",
                "--everything",
                "--project",
                FAKE_PROJECT_PATH,
                "--json",
            ],
            cli_env,
        )
        assert len(json.loads(stdout)["results"]) > 0


# ============================================================
# AC5.1: All five subcommands accessible and produce output
# ============================================================


class TestSubcommandsAccessible:
    """cc-search-v2.AC5.1: Each subcommand produces exit code 0 and output."""

    def test_search_runs(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
            ["search", "database", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_extract_with_session_id(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
            ["list", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_index_runs(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
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

        exit_code, stdout, stderr = _run_cli(
            ["context", uuid],
            cli_env,
        )
        assert exit_code == 0
        assert len(stdout) > 0

    def test_search_no_results_still_zero_exit(
        self, cli_env: sqlite3.Connection
    ) -> None:
        """Search with no matches returns exit code 0 (not an error)."""
        exit_code, stdout, stderr = _run_cli(
            ["search", "xyznonexistentterm", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0


# ============================================================
# AC5.2: --json output is valid JSON
# ============================================================


class TestJsonOutput:
    """cc-search-v2.AC5.2: --json output is valid JSON parseable by json.loads()."""

    def test_search_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
            ["search", "database", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert isinstance(parsed["results"], list)

    def test_extract_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert "session_id" in parsed
        assert "epochs" in parsed

    def test_list_json(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
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

        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
            ["search", "xyznonexistentterm", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert parsed["results"] == []
        assert parsed["scope"] == "local"  # explicit --project never broadens

    def test_extract_json_has_messages(self, cli_env: sqlite3.Connection) -> None:
        """Extract JSON includes actual messages."""
        exit_code, stdout, stderr = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        parsed = json.loads(stdout)
        assert parsed["session_id"] == SAMPLE_SESSION_ID
        total_msgs = sum(len(ep["messages"]) for ep in parsed["epochs"])
        assert total_msgs > 0

    def test_list_json_has_sessions(self, cli_env: sqlite3.Connection) -> None:
        """List JSON includes the indexed sessions."""
        exit_code, stdout, stderr = _run_cli(
            ["list", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        parsed = json.loads(stdout)
        session_ids = {s["session_id"] for s in parsed["sessions"]}
        assert SAMPLE_SESSION_ID in session_ids
        assert COMPRESSED_SESSION_ID in session_ids

    def test_index_json(self, cli_env: sqlite3.Connection) -> None:
        """Index --json distinguishes indexed from skipped sessions."""
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        # Should have both user and assistant labels
        assert "User" in stdout or "user" in stdout
        assert "Assistant" in stdout or "assistant" in stdout

    def test_extract_shows_timestamps(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
            ["extract", SAMPLE_SESSION_ID, "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert "2026-02-07" in stdout

    def test_extract_compressed_shows_epoch_markers(
        self, cli_env: sqlite3.Connection
    ) -> None:
        """Compressed session extract shows epoch markers."""
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
            ["search", "database", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        # Output should contain a session ID
        assert "1111" in stdout or "2222" in stdout

    def test_list_shows_session_ids(self, cli_env: sqlite3.Connection) -> None:
        exit_code, stdout, stderr = _run_cli(
            ["list", "--project", FAKE_PROJECT_PATH],
            cli_env,
        )
        assert exit_code == 0
        assert SAMPLE_SESSION_ID in stdout or COMPRESSED_SESSION_ID in stdout

    def test_context_shows_target_marker(self, cli_env: sqlite3.Connection) -> None:
        row = cli_env.execute("SELECT uuid FROM message LIMIT 1").fetchone()
        uuid = row["uuid"]

        exit_code, stdout, stderr = _run_cli(
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
        with redirect_stdout(stdout_buf):
            try:
                parser.parse_args([subcommand, "--help"])
            except SystemExit:
                pass
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
        exit_code, stdout, stderr = _run_cli(
            ["search", "database", "--project", FAKE_PROJECT_PATH, "--json"],
            large_session_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed["results"], list)
        assert len(parsed["results"]) > 0

    def test_extract_large_session(self, large_session_env: sqlite3.Connection) -> None:
        """Extract a large session completes within reasonable time."""
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
            [
                "search",
                "database",
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
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
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
        exit_code, stdout, stderr = _run_cli(
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

        exit_code, stdout, stderr = _run_cli(
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
