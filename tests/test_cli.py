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

import io
import json
import shutil
import sqlite3
import sys
from collections.abc import Generator
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from cc_search_chats.cli import build_parser
from cc_search_chats.core.discovery import encode_project_path
from cc_search_chats.core.models import SessionMeta
from cc_search_chats.storage.index import close_db, index_session, open_db, search
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


# ============================================================
# Helpers
# ============================================================


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
        exit_code, stdout, stderr = _run_cli(
            ["index", "--all", "--json"], cli_env
        )
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


class TestSearchScope:
    """Local-first search with broaden-on-miss across projects."""

    def test_all_flag_searches_everything(
        self, cli_env: sqlite3.Connection
    ) -> None:
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
        proj = (
            tmp_path / "claude" / "projects" / encode_project_path(FAKE_PROJECT_PATH)
        )
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
        """Index --json produces valid JSON with sessions_indexed and project_path."""
        exit_code, stdout, stderr = _run_cli(
            ["index", "--project", FAKE_PROJECT_PATH, "--json"],
            cli_env,
        )
        assert exit_code == 0
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)
        assert "sessions_indexed" in parsed
        assert "project_path" in parsed
        assert parsed["project_path"] == FAKE_PROJECT_PATH
        assert parsed["sessions_indexed"] >= 0


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
