"""Tests for search and query operations.

Acceptance criteria coverage:
- cc-search-v2.AC3.1: Keyword search returns results with expected fields
- cc-search-v2.AC3.2: epoch=0 filter returns only pre-compression messages
- cc-search-v2.AC3.3: project filter scopes search
- cc-search-v2.AC3.4: days filter limits results
- cc-search-v2.AC3.5: No matches returns empty list
- cc-search-v2.AC3.6: JIT reindex before search (integration)
- cc-search-v2.AC4.2: Extract full session
- cc-search-v2.AC4.3: Extract with epoch filter
- cc-search-v2.AC4.4: Extract context for a message UUID
- cc-search-v2.AC4.5: Extract with invalid session_id -> ValueError
- cc-search-v2.AC4.6: Extract from session with no compression -> epoch 0
"""

import json
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cc_search_chats.core.models import SessionMeta
from cc_search_chats.core.search import sanitize_fts5_query
from cc_search_chats.storage.index import (
    extract_context,
    extract_session,
    index_session,
    list_sessions,
    search,
    search_full_content,
)
from tests.conftest import (
    SESSION_ID_A,
    SESSION_ID_B,
    _make_session_lines,
    _write_session_file,
)


class TestSearch:
    """cc-search-v2.AC3.1: Keyword search returns matching messages."""

    def test_search_returns_results(self, indexed_db: sqlite3.Connection) -> None:
        """Search for keyword present in indexed data returns results."""
        results = search(indexed_db, "database")
        assert len(results) > 0

    def test_search_result_fields(self, indexed_db: sqlite3.Connection) -> None:
        """Search results contain expected fields."""
        results = search(indexed_db, "database")
        assert len(results) > 0
        row = results[0]
        # Check all expected columns are present
        assert row["uuid"] is not None
        assert row["session_id"] == SESSION_ID_A
        assert row["epoch"] is not None
        assert row["timestamp"] is not None
        assert row["role"] in ("user", "assistant")
        assert row["snippet"] is not None
        assert row["score"] is not None

    def test_search_snippet_contains_markers(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        """Snippets contain >>> and <<< highlight markers."""
        results = search(indexed_db, "database")
        assert len(results) > 0
        snippet = results[0]["snippet"]
        assert ">>>" in snippet
        assert "<<<" in snippet


class TestSearchEpochFilter:
    """cc-search-v2.AC3.2: epoch=0 returns only pre-compression messages."""

    def test_epoch_0_only(self, indexed_db: sqlite3.Connection) -> None:
        """Search with epoch=0 returns only epoch 0 messages."""
        results = search(indexed_db, "database", epoch=0)
        for row in results:
            assert row["epoch"] == 0

    def test_epoch_1_only(self, indexed_db: sqlite3.Connection) -> None:
        """Search with epoch=1 returns only epoch 1 messages."""
        # "OAuth" or "authentication" is in epoch 1
        results = search(indexed_db, "authentication", epoch=1)
        for row in results:
            assert row["epoch"] == 1

    def test_epoch_filter_excludes_other_epochs(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        """epoch=0 filter does not return epoch 1 messages."""
        # "authentication" is only in epoch 1
        results = search(indexed_db, "authentication", epoch=0)
        assert len(results) == 0


class TestSearchProjectFilter:
    """cc-search-v2.AC3.3: project filter scopes search."""

    def test_project_filter(self, db_conn: sqlite3.Connection, tmp_path: Path) -> None:
        """Search with project filter returns only that project's results."""
        # Index session A for project X
        lines_a = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta_a = _write_session_file(
            tmp_path, SESSION_ID_A, lines_a, project_path="/project/alpha"
        )
        index_session(db_conn, meta_a)

        # Index session B for project Y
        lines_b = _make_session_lines(SESSION_ID_B, compact_boundaries=0)
        meta_b = _write_session_file(
            tmp_path, SESSION_ID_B, lines_b, project_path="/project/beta"
        )
        index_session(db_conn, meta_b)

        # Search scoped to project alpha
        results = search(db_conn, "database", project="/project/alpha")
        assert len(results) > 0
        for row in results:
            assert row["session_id"] == SESSION_ID_A

    def test_project_filter_other_project(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Search with project filter excludes other project's results."""
        lines_a = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta_a = _write_session_file(
            tmp_path, SESSION_ID_A, lines_a, project_path="/project/alpha"
        )
        index_session(db_conn, meta_a)

        lines_b = _make_session_lines(SESSION_ID_B, compact_boundaries=0)
        meta_b = _write_session_file(
            tmp_path, SESSION_ID_B, lines_b, project_path="/project/beta"
        )
        index_session(db_conn, meta_b)

        results = search(db_conn, "database", project="/project/beta")
        assert len(results) > 0
        for row in results:
            assert row["session_id"] == SESSION_ID_B


class TestSearchDaysFilter:
    """cc-search-v2.AC3.4: days filter limits results by timestamp."""

    def test_days_filter_excludes_old_messages(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Search with days=0 excludes messages older than today.

        Our sample messages have timestamps from 2026-02-07. If today is
        after that date, days=0 (today only) should exclude them.
        days=99999 (very long period) should include them.
        """
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        # Without filter, should find results
        results_no_filter = search(db_conn, "database")
        assert len(results_no_filter) > 0

        # days=0 means only today's messages. Our fixture timestamps are
        # 2026-02-07, so this should return empty list (no messages from today).
        results_today = search(db_conn, "database", days=0)
        assert len(results_today) == 0, "days=0 should exclude old fixture messages"

        # With an extremely large days value, should still find results
        results_large = search(db_conn, "database", days=99999)
        assert len(results_large) > 0


class TestSearchNoMatches:
    """cc-search-v2.AC3.5: No matches returns empty list, no error."""

    def test_returns_empty_list(self, indexed_db: sqlite3.Connection) -> None:
        """Search for nonexistent term returns empty list."""
        results = search(indexed_db, "xyzabcnonexistent")
        assert results == []

    def test_no_exception(self, indexed_db: sqlite3.Connection) -> None:
        """Search for nonexistent term does not raise."""
        # Just verify no exception -- the assert above covers the return value
        results = search(indexed_db, "zyxwvu987654")
        assert isinstance(results, list)


class TestSearchPunctuation:
    """Free-text queries with punctuation must not crash FTS5.

    Regression: user input was passed verbatim into ``message_fts MATCH``,
    so FTS5 interpreted query punctuation as its own operator DSL. Decimal
    numbers (``0.90``) raised ``fts5: syntax error near "."`` and a colon
    (``pole:``) raised ``no such column: pole``.
    """

    def test_decimal_numbers_do_not_raise(self, indexed_db: sqlite3.Connection) -> None:
        """The exact query that crashed: decimals trigger no FTS5 syntax error."""
        results = search(
            indexed_db, "true agreement 0.90 passes 0.70 fails 19 times in 20"
        )
        assert isinstance(results, list)

    def test_colon_does_not_raise(self, indexed_db: sqlite3.Connection) -> None:
        """A colon must not be parsed as an FTS5 column filter."""
        results = search(indexed_db, "pole: Ellie")
        assert isinstance(results, list)

    @pytest.mark.parametrize(
        "query",
        ['"unterminated quote', "(decision)", "foo*bar", "a^b", "x AND/OR y"],
    )
    def test_operator_punctuation_does_not_raise(
        self, indexed_db: sqlite3.Connection, query: str
    ) -> None:
        """Assorted FTS5 operator characters are treated as literal text."""
        assert isinstance(search(indexed_db, query), list)

    def test_punctuation_still_matches_terms(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        """Sanitisation neutralises punctuation without dropping real terms."""
        plain = search(indexed_db, "database")
        trailing_period = search(indexed_db, "database.")
        assert len(plain) > 0
        assert len(trailing_period) == len(plain)

    def test_blank_query_returns_empty(self, indexed_db: sqlite3.Connection) -> None:
        """A query with no searchable terms returns [] rather than erroring."""
        assert search(indexed_db, "") == []
        assert search(indexed_db, "   ") == []


class TestSanitizeFts5Query:
    """Unit tests for the pure FTS5 query sanitiser."""

    def test_quotes_each_whitespace_term(self) -> None:
        assert sanitize_fts5_query("alpha beta") == '"alpha" "beta"'

    def test_single_term(self) -> None:
        assert sanitize_fts5_query("pole") == '"pole"'

    def test_decimal_term_is_quoted(self) -> None:
        assert sanitize_fts5_query("0.90") == '"0.90"'

    def test_internal_quotes_are_doubled(self) -> None:
        # Each embedded " becomes "" inside the surrounding quotes.
        assert sanitize_fts5_query('say "hi"') == '"say" """hi"""'

    def test_blank_returns_empty_string(self) -> None:
        assert sanitize_fts5_query("") == ""
        assert sanitize_fts5_query("   ") == ""

    @settings(max_examples=200)
    @given(
        query=st.text(
            alphabet=st.characters(min_codepoint=32, max_codepoint=126),
            max_size=80,
        )
    )
    def test_output_never_raises_fts5_syntax_error(self, query: str) -> None:
        """Property: no ASCII query can produce an FTS5 syntax error.

        Either the sanitised expression is empty (caller short-circuits) or
        it executes cleanly against an FTS5 table using the production
        tokeniser.
        """
        match = sanitize_fts5_query(query)
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE t USING "
                "fts5(body, tokenize='unicode61 remove_diacritics 0')"
            )
            conn.execute("INSERT INTO t(body) VALUES ('alpha 0.90 pole Ellie')")
            if match:
                conn.execute("SELECT rowid FROM t WHERE t MATCH ?", (match,)).fetchall()
        finally:
            conn.close()


class TestSearchFullContent:
    """search_full_content scans thinking + tool I/O without persisting."""

    def _thinking_session(
        self, tmp_path: Path, session_id: str, project_path: str, thinking: str
    ) -> SessionMeta:
        line = json.dumps(
            {
                "type": "assistant",
                "uuid": f"a-{session_id}",
                "timestamp": "2026-02-07T10:00:00.000Z",
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": thinking},
                        {"type": "text", "text": "ordinary visible text"},
                    ],
                },
            }
        )
        f = tmp_path / f"{session_id}.jsonl"
        f.write_text(line, encoding="utf-8")
        stat = f.stat()
        return SessionMeta(
            session_id=session_id,
            file_path=str(f),
            project_path=project_path,
            file_size=stat.st_size,
            modified_at=stat.st_mtime,
        )

    def test_finds_thinking_content(self, tmp_path: Path) -> None:
        meta = self._thinking_session(
            tmp_path, SESSION_ID_A, "/p/a", "secretwombat reasoning"
        )
        results = search_full_content([meta], "secretwombat")
        assert len(results) > 0
        assert results[0]["project_path"] == "/p/a"

    def test_normal_index_misses_thinking(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """The persistent (clean) index does not contain thinking text."""
        meta = self._thinking_session(
            tmp_path, SESSION_ID_A, "/p/a", "secretwombat reasoning"
        )
        index_session(db_conn, meta)  # full_content defaults to False
        assert search(db_conn, "secretwombat") == []

    def test_visible_text_still_matches(self, tmp_path: Path) -> None:
        meta = self._thinking_session(tmp_path, SESSION_ID_A, "/p/a", "secretwombat")
        assert len(search_full_content([meta], "ordinary visible")) > 0

    def test_empty_sessions_returns_empty(self) -> None:
        assert search_full_content([], "anything") == []


class TestExtractSession:
    """cc-search-v2.AC4.2: Extract full session returns all messages."""

    def test_returns_all_messages(self, indexed_db: sqlite3.Connection) -> None:
        """Extract returns all messages in timestamp order."""
        results = extract_session(indexed_db, SESSION_ID_A)
        # 1 compact_boundary: 4 (epoch 0) + 5 (epoch 1) = 9 messages
        assert len(results) == 9

    def test_timestamp_ordering(self, indexed_db: sqlite3.Connection) -> None:
        """Messages are ordered by timestamp."""
        results = extract_session(indexed_db, SESSION_ID_A)
        timestamps = [r["timestamp"] for r in results]
        assert timestamps == sorted(timestamps)

    def test_role_labels(self, indexed_db: sqlite3.Connection) -> None:
        """Each message has a role label."""
        results = extract_session(indexed_db, SESSION_ID_A)
        for row in results:
            assert row["role"] in ("user", "assistant")


class TestExtractEpochFilter:
    """cc-search-v2.AC4.3: Extract with epoch=0 returns only pre-compression."""

    def test_epoch_0_only(self, indexed_db: sqlite3.Connection) -> None:
        """Extract with epoch=0 returns only epoch 0 messages."""
        results = extract_session(indexed_db, SESSION_ID_A, epoch=0)
        for row in results:
            assert row["epoch"] == 0
        # Should have 4 messages in epoch 0
        assert len(results) == 4

    def test_epoch_1_only(self, indexed_db: sqlite3.Connection) -> None:
        """Extract with epoch=1 returns only epoch 1 messages."""
        results = extract_session(indexed_db, SESSION_ID_A, epoch=1)
        for row in results:
            assert row["epoch"] == 1
        assert len(results) == 5


class TestExtractContext:
    """cc-search-v2.AC4.4: Extract context for a message UUID."""

    def test_context_returns_surrounding_messages(
        self, indexed_db: sqlite3.Connection
    ) -> None:
        """Extract context returns messages before and after the target."""
        # Get a message in the middle of the session
        all_msgs = extract_session(indexed_db, SESSION_ID_A)
        # Pick a message that's not at the start or end
        mid_idx = len(all_msgs) // 2
        target_uuid = all_msgs[mid_idx]["uuid"]

        results = extract_context(indexed_db, target_uuid, depth=2)
        # Should have the target + up to 2 before + up to 2 after
        assert len(results) >= 3  # at least target + some context
        assert len(results) <= 5  # at most 2 + 1 + 2

        # Target message should be in the results
        uuids = [r["uuid"] for r in results]
        assert target_uuid in uuids

    def test_context_depth_1(self, indexed_db: sqlite3.Connection) -> None:
        """depth=1 returns 1 before + target + 1 after."""
        all_msgs = extract_session(indexed_db, SESSION_ID_A)
        # Use a message in the middle, not first or last
        mid_idx = len(all_msgs) // 2
        target_uuid = all_msgs[mid_idx]["uuid"]

        results = extract_context(indexed_db, target_uuid, depth=1)
        assert len(results) >= 2  # at least target + 1 neighbor
        assert len(results) <= 3  # at most 1 + 1 + 1


class TestExtractInvalidSession:
    """cc-search-v2.AC4.5: Extract with invalid session_id -> ValueError."""

    def test_raises_value_error(self, indexed_db: sqlite3.Connection) -> None:
        """Extract with nonexistent session_id raises ValueError."""
        with pytest.raises(ValueError, match="Session not found"):
            extract_session(indexed_db, "nonexistent-session-id")

    def test_error_message_contains_id(self, indexed_db: sqlite3.Connection) -> None:
        """Error message includes the bad session_id."""
        with pytest.raises(ValueError, match="invalid-id-123"):
            extract_session(indexed_db, "invalid-id-123")


class TestExtractContextInvalidUuid:
    """Extract context with invalid UUID -> ValueError."""

    def test_raises_value_error(self, indexed_db: sqlite3.Connection) -> None:
        """Extract context with nonexistent UUID raises ValueError."""
        with pytest.raises(ValueError, match="Message not found"):
            extract_context(indexed_db, "nonexistent-uuid")


class TestExtractNoCompression:
    """cc-search-v2.AC4.6: Extract from session with no compression -> epoch 0."""

    def test_entire_conversation_as_epoch_0(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Session with no compact_boundaries: all messages epoch 0."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        results = extract_session(db_conn, SESSION_ID_A)
        assert len(results) > 0
        for row in results:
            assert row["epoch"] == 0

    def test_no_epoch_filter_needed(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Extract without epoch filter returns all messages."""
        lines = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta = _write_session_file(tmp_path, SESSION_ID_A, lines)
        index_session(db_conn, meta)

        all_results = extract_session(db_conn, SESSION_ID_A)
        epoch_0_results = extract_session(db_conn, SESSION_ID_A, epoch=0)
        assert len(all_results) == len(epoch_0_results)


class TestListSessions:
    """Test list_sessions operation."""

    def test_lists_indexed_session(self, indexed_db: sqlite3.Connection) -> None:
        """list_sessions returns the indexed session."""
        results = list_sessions(indexed_db)
        assert len(results) == 1
        assert results[0]["session_id"] == SESSION_ID_A

    def test_includes_epoch_count(self, indexed_db: sqlite3.Connection) -> None:
        """list_sessions includes epoch count from epoch_summary."""
        results = list_sessions(indexed_db)
        row = results[0]
        assert row["epoch_count"] == 2  # epochs 0 and 1

    def test_includes_total_messages(self, indexed_db: sqlite3.Connection) -> None:
        """list_sessions includes total message count."""
        results = list_sessions(indexed_db)
        row = results[0]
        assert row["total_messages"] == 9  # 4 + 5

    def test_project_filter(self, db_conn: sqlite3.Connection, tmp_path: Path) -> None:
        """list_sessions with project filter scopes results."""
        lines_a = _make_session_lines(SESSION_ID_A, compact_boundaries=0)
        meta_a = _write_session_file(
            tmp_path, SESSION_ID_A, lines_a, project_path="/proj/alpha"
        )
        index_session(db_conn, meta_a)

        lines_b = _make_session_lines(SESSION_ID_B, compact_boundaries=0)
        meta_b = _write_session_file(
            tmp_path, SESSION_ID_B, lines_b, project_path="/proj/beta"
        )
        index_session(db_conn, meta_b)

        results = list_sessions(db_conn, project="/proj/alpha")
        assert len(results) == 1
        assert results[0]["session_id"] == SESSION_ID_A


class TestRealProjectPathInQueries:
    """List and search queries expose the display-only real_project_path."""

    def test_list_query_selects_real_project_path(self) -> None:
        from cc_search_chats.core.search import build_list_query

        sql, _ = build_list_query()
        assert "real_project_path" in sql

    def test_search_query_selects_real_project_path(self) -> None:
        from cc_search_chats.core.search import build_search_query

        sql, _ = build_search_query("database")
        assert "real_project_path" in sql
