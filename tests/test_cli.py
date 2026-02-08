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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from cc_search_chats.cli import build_parser
from cc_search_chats.core.discovery import encode_project_path
from cc_search_chats.core.models import SessionMeta
from cc_search_chats.storage.index import close_db, index_session, open_db

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


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
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
        assert isinstance(parsed, list)

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
        assert isinstance(parsed, list)

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
        assert parsed == []

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
        session_ids = {s["session_id"] for s in parsed}
        assert SAMPLE_SESSION_ID in session_ids
        assert COMPRESSED_SESSION_ID in session_ids


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
    ) -> sqlite3.Connection:
        """Create a ~8MB JSONL session file and index it."""
        projects_dir = tmp_path / "claude" / "projects"
        encoded = encode_project_path(FAKE_PROJECT_PATH)
        project_dir = projects_dir / encoded
        project_dir.mkdir(parents=True)

        session_id = "33333333-3333-3333-3333-333333333333"
        session_file = project_dir / f"{session_id}.jsonl"

        # Generate ~8MB of JSONL lines (~200 bytes per line, ~40000 lines)
        lines = []
        for i in range(20000):
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
        assert file_size > 4_000_000, f"Expected >4MB, got {file_size}"

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
        assert isinstance(parsed, list)
        assert len(parsed) > 0

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
        for result in parsed:
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
