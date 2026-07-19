"""Tests for session discovery and path encoding.

Covers:
- encode_project_path / decode_project_path (pure)
- rank_sessions (pure)
- list_session_files (I/O via tmp_path)
"""

import os
import time

from cc_search_chats.core.discovery import (
    decode_project_path,
    encode_project_path,
    list_session_files,
    rank_sessions,
)
from cc_search_chats.core.models import SessionMeta

# ---------------------------------------------------------------------------
# encode_project_path
# ---------------------------------------------------------------------------


class TestEncodeProjectPath:
    """Pure string transformation: replace non-alphanumerics with -."""

    def test_typical_path(self):
        assert encode_project_path("/home/brian/project") == "-home-brian-project"

    def test_root(self):
        assert encode_project_path("/") == "-"

    def test_nested_deep(self):
        result = encode_project_path("/a/b/c/d/e")
        assert result == "-a-b-c-d-e"

    def test_empty_string(self):
        assert encode_project_path("") == ""

    def test_no_leading_slash(self):
        """Paths without leading slash still get encoded."""
        assert encode_project_path("home/brian") == "home-brian"

    def test_trailing_slash(self):
        assert encode_project_path("/home/brian/") == "-home-brian-"

    def test_double_slashes(self):
        assert encode_project_path("//home//brian") == "--home--brian"

    def test_path_with_hyphens(self):
        """Hyphens in path segments are preserved (encoding is lossy)."""
        assert encode_project_path("/my-project/sub-dir") == "-my-project-sub-dir"

    def test_dotfile_directory(self):
        """Dots become '-' too, not just slashes.

        Verified against real ~/.claude/projects dirs: a project under
        ``.worktrees`` is stored as ``...--worktrees-...`` (the ``/.`` pair
        collapses to ``--``). Claude Code replaces every non-alphanumeric
        character, so lookups under dotfile dirs fail if we only map ``/``.
        """
        assert (
            encode_project_path("/home/brian/.worktrees/feature")
            == "-home-brian--worktrees-feature"
        )

    def test_space_in_path(self):
        """Spaces are non-alphanumeric and are replaced with '-'."""
        assert encode_project_path("/home/brian/my project") == "-home-brian-my-project"


# ---------------------------------------------------------------------------
# decode_project_path
# ---------------------------------------------------------------------------


class TestDecodeProjectPath:
    """Inverse of encode -- lossy if original path contained hyphens."""

    def test_typical_encoded(self):
        assert decode_project_path("-home-brian-project") == "/home/brian/project"

    def test_root(self):
        assert decode_project_path("-") == "/"

    def test_empty_string(self):
        assert decode_project_path("") == ""

    def test_roundtrip_simple(self):
        """Roundtrip works for paths without hyphens."""
        original = "/home/brian/project"
        assert decode_project_path(encode_project_path(original)) == original

    def test_roundtrip_lossy_with_hyphens(self):
        """Roundtrip is lossy if the original path contains hyphens."""
        original = "/my-project"
        encoded = encode_project_path(original)
        decoded = decode_project_path(encoded)
        # The hyphen in "my-project" becomes a slash after decode
        assert decoded != original
        assert decoded == "/my/project"


# ---------------------------------------------------------------------------
# rank_sessions
# ---------------------------------------------------------------------------


class TestRankSessions:
    """Pure ranking: top 5 by mtime, then by size within those 5."""

    @staticmethod
    def _make_session(
        session_id: str,
        modified_at: float,
        file_size: int,
    ) -> SessionMeta:
        return SessionMeta(
            session_id=session_id,
            file_path=f"/fake/{session_id}.jsonl",
            project_path="/fake",
            file_size=file_size,
            modified_at=modified_at,
        )

    def test_empty_list(self):
        assert rank_sessions([]) == []

    def test_single_session(self):
        s = self._make_session("a", 100.0, 500)
        assert rank_sessions([s]) == [s]

    def test_biggest_among_most_recent_wins(self):
        """The largest file among the 5 most recent should be first."""
        sessions = [
            self._make_session("old-large", 10.0, 10000),
            self._make_session("new-small", 100.0, 100),
            self._make_session("new-big", 99.0, 5000),
            self._make_session("new-medium", 98.0, 1000),
            self._make_session("new-tiny", 97.0, 50),
            self._make_session("new-avg", 96.0, 500),
        ]
        ranked = rank_sessions(sessions)
        # Top 5 by mtime: new-small(100), new-big(99), new-medium(98),
        #                  new-tiny(97), new-avg(96)
        # Among those 5, sorted by file_size desc:
        #   new-big(5000), new-medium(1000), new-avg(500), new-small(100), new-tiny(50)
        # Then remainder: old-large(10.0)
        assert ranked[0].session_id == "new-big"
        assert ranked[1].session_id == "new-medium"
        assert ranked[-1].session_id == "old-large"

    def test_top_result_is_biggest_of_most_recent_5(self):
        """With 10 sessions, the top 5 by mtime are re-sorted by size."""
        sessions = [
            self._make_session(f"s{i}", float(i), (i % 5 + 1) * 100) for i in range(10)
        ]
        ranked = rank_sessions(sessions)
        # Top 5 by mtime: s9(mtime=9), s8, s7, s6, s5
        # Sizes: s9=500, s8=400, s7=300, s6=200, s5=100
        # Re-sorted by size desc: s9(500), s8(400), s7(300), s6(200), s5(100)
        assert ranked[0].session_id == "s9"
        assert ranked[4].session_id == "s5"
        # Remainder in mtime desc: s4, s3, s2, s1, s0
        assert ranked[5].session_id == "s4"

    def test_fewer_than_5_sessions(self):
        """With fewer than 5 sessions, all are ranked by size."""
        sessions = [
            self._make_session("a", 3.0, 100),
            self._make_session("b", 2.0, 500),
            self._make_session("c", 1.0, 300),
        ]
        ranked = rank_sessions(sessions)
        assert ranked[0].session_id == "b"
        assert ranked[1].session_id == "c"
        assert ranked[2].session_id == "a"

    def test_equal_mtime_stable_order(self):
        """Sessions with equal mtime should still produce a deterministic result."""
        sessions = [
            self._make_session("a", 100.0, 300),
            self._make_session("b", 100.0, 500),
            self._make_session("c", 100.0, 100),
        ]
        ranked = rank_sessions(sessions)
        assert ranked[0].session_id == "b"
        assert ranked[2].session_id == "c"


# ---------------------------------------------------------------------------
# list_session_files
# ---------------------------------------------------------------------------


class TestListSessionFiles:
    """I/O tests using tmp_path fixture for filesystem operations."""

    @staticmethod
    def _make_uuid_file(directory, uuid_str, content=b"{}"):
        """Create a UUID-named JSONL file in the given directory."""
        f = directory / f"{uuid_str}.jsonl"
        f.write_bytes(content)
        return f

    def test_discovers_uuid_files(self, tmp_path):
        """Finds UUID-named .jsonl files at the top level."""
        project_dir = tmp_path / "-home-brian-project"
        project_dir.mkdir()

        uuid1 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        uuid2 = "11111111-2222-3333-4444-555555555555"
        self._make_uuid_file(project_dir, uuid1, b'{"test": 1}')
        self._make_uuid_file(project_dir, uuid2, b'{"test": 2}')

        results = list_session_files(tmp_path, "-home-brian-project")
        session_ids = {r.session_id for r in results}
        assert session_ids == {uuid1, uuid2}

    def test_ignores_non_uuid_files(self, tmp_path):
        """Files that don't match UUID pattern are excluded."""
        project_dir = tmp_path / "-home-project"
        project_dir.mkdir()

        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._make_uuid_file(project_dir, uuid)
        (project_dir / "not-a-uuid.jsonl").write_bytes(b"{}")
        (project_dir / "readme.md").write_bytes(b"# Hi")
        (project_dir / "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE.jsonl").write_bytes(b"{}")

        results = list_session_files(tmp_path, "-home-project")
        assert len(results) == 1
        assert results[0].session_id == uuid

    def test_excludes_subagents_by_default(self, tmp_path):
        """Subagent files are excluded when include_subagents=False."""
        project_dir = tmp_path / "-proj"
        project_dir.mkdir()

        uuid_top = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._make_uuid_file(project_dir, uuid_top)

        # Subagent structure: <uuid>/subagents/agent-file.jsonl
        subagent_dir = project_dir / "some-session" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-task.jsonl").write_bytes(b"{}")

        results = list_session_files(tmp_path, "-proj", include_subagents=False)
        assert len(results) == 1
        assert results[0].session_id == uuid_top

    def test_includes_subagents_when_requested(self, tmp_path):
        """include_subagents=True finds subagent files too."""
        project_dir = tmp_path / "-proj"
        project_dir.mkdir()

        uuid_top = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._make_uuid_file(project_dir, uuid_top)

        subagent_dir = project_dir / "some-session" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-task.jsonl").write_bytes(b"{}")

        results = list_session_files(tmp_path, "-proj", include_subagents=True)
        assert len(results) == 2

    def test_nonexistent_directory_returns_empty(self, tmp_path):
        """Missing project directory returns empty list, not error."""
        results = list_session_files(tmp_path, "-nonexistent")
        assert results == []

    def test_empty_directory_returns_empty(self, tmp_path):
        """Directory with no JSONL files returns empty list."""
        project_dir = tmp_path / "-empty"
        project_dir.mkdir()

        results = list_session_files(tmp_path, "-empty")
        assert results == []

    def test_session_meta_fields_populated(self, tmp_path):
        """Verify all SessionMeta fields are correctly populated."""
        project_dir = tmp_path / "-home-brian-proj"
        project_dir.mkdir()

        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        content = b'{"some": "data", "more": "stuff"}'
        self._make_uuid_file(project_dir, uuid, content)

        results = list_session_files(tmp_path, "-home-brian-proj")
        assert len(results) == 1

        meta = results[0]
        assert meta.session_id == uuid
        assert meta.file_path == str(project_dir / f"{uuid}.jsonl")
        assert meta.project_path == "/home/brian/proj"
        assert meta.file_size == len(content)
        assert meta.modified_at > 0

    def test_file_size_reflects_actual_size(self, tmp_path):
        """File size in SessionMeta matches actual file bytes."""
        project_dir = tmp_path / "-proj"
        project_dir.mkdir()

        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        content = b"x" * 1234
        self._make_uuid_file(project_dir, uuid, content)

        results = list_session_files(tmp_path, "-proj")
        assert results[0].file_size == 1234

    def test_mtime_reflects_modification(self, tmp_path):
        """modified_at changes when file is modified."""
        project_dir = tmp_path / "-proj"
        project_dir.mkdir()

        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        f = self._make_uuid_file(project_dir, uuid, b"original")

        results1 = list_session_files(tmp_path, "-proj")
        mtime1 = results1[0].modified_at

        # Ensure a measurable time difference
        time.sleep(0.05)
        f.write_bytes(b"updated content that is longer")
        # Force mtime update by setting it explicitly
        new_mtime = mtime1 + 10.0
        os.utime(str(f), (new_mtime, new_mtime))

        results2 = list_session_files(tmp_path, "-proj")
        assert results2[0].modified_at > mtime1
