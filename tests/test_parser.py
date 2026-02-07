"""Tests for cc_search_chats.core.parser.

Acceptance criteria coverage:
- AC1.1: User message parsing
- AC1.2: Assistant message with list content
- AC1.3: compact_boundary -> CompactEvent
- AC1.4: Summary record parsing
- AC1.5: Malformed JSON returns None
- AC1.6: Unknown type returns None
- AC1.7: Empty content -> empty text_content
- AC1.8: BLNS / adversarial strings (Hypothesis)
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from cc_search_chats.core.models import CompactEvent, SessionRecord
from cc_search_chats.core.parser import parse_record, parse_session

SESSION_ID = "session-abc-123"


# --- AC1.1: User message parsing ---


class TestParseUserMessage:
    """cc-search-v2.AC1.1: User message with string content."""

    def test_returns_session_record(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)

    def test_record_type(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.record_type == "user"

    def test_role(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.role == "user"

    def test_text_content(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.text_content == "How do I parse JSONL files in Python?"

    def test_uuid(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.uuid == "msg-user-001"

    def test_parent_uuid(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.parent_uuid == "msg-parent-000"

    def test_timestamp(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.timestamp == "2026-02-07T10:30:00.000Z"

    def test_session_id(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.session_id == SESSION_ID

    def test_leaf_uuid_is_none(self, user_message_line: str) -> None:
        result = parse_record(user_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.leaf_uuid is None


# --- AC1.2: Assistant message with list content ---


class TestParseAssistantMessage:
    """cc-search-v2.AC1.2: Assistant message with list content."""

    def test_returns_session_record(self, assistant_message_line: str) -> None:
        result = parse_record(assistant_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)

    def test_record_type(self, assistant_message_line: str) -> None:
        result = parse_record(assistant_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.record_type == "assistant"

    def test_role(self, assistant_message_line: str) -> None:
        result = parse_record(assistant_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.role == "assistant"

    def test_text_content_contains_text_items(
        self, assistant_message_line: str
    ) -> None:
        result = parse_record(assistant_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert "You can use the json module." in result.text_content
        assert "Here is an example:" in result.text_content

    def test_text_content_contains_tool_use_summary(
        self, assistant_message_line: str
    ) -> None:
        result = parse_record(assistant_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert "[tool: Read]" in result.text_content

    def test_text_items_joined_by_newline(self, assistant_message_line: str) -> None:
        result = parse_record(assistant_message_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        expected = "You can use the json module.\n[tool: Read]\nHere is an example:"
        assert result.text_content == expected


# --- AC1.3: compact_boundary -> CompactEvent ---


class TestParseCompactBoundary:
    """cc-search-v2.AC1.3: compact_boundary produces CompactEvent."""

    def test_returns_compact_event(self, compact_boundary_line: str) -> None:
        result = parse_record(compact_boundary_line, SESSION_ID)
        assert isinstance(result, CompactEvent)

    def test_trigger(self, compact_boundary_line: str) -> None:
        result = parse_record(compact_boundary_line, SESSION_ID)
        assert isinstance(result, CompactEvent)
        assert result.trigger == "auto"

    def test_pre_tokens(self, compact_boundary_line: str) -> None:
        result = parse_record(compact_boundary_line, SESSION_ID)
        assert isinstance(result, CompactEvent)
        assert result.pre_tokens == 42000

    def test_uuid(self, compact_boundary_line: str) -> None:
        result = parse_record(compact_boundary_line, SESSION_ID)
        assert isinstance(result, CompactEvent)
        assert result.uuid == "msg-compact-003"

    def test_session_id(self, compact_boundary_line: str) -> None:
        result = parse_record(compact_boundary_line, SESSION_ID)
        assert isinstance(result, CompactEvent)
        assert result.session_id == SESSION_ID

    def test_timestamp(self, compact_boundary_line: str) -> None:
        result = parse_record(compact_boundary_line, SESSION_ID)
        assert isinstance(result, CompactEvent)
        assert result.timestamp == "2026-02-07T11:00:00.000Z"


# --- AC1.4: Summary record ---


class TestParseSummary:
    """cc-search-v2.AC1.4: Summary record extracts text and leafUuid."""

    def test_returns_session_record(self, summary_line: str) -> None:
        result = parse_record(summary_line, SESSION_ID)
        assert isinstance(result, SessionRecord)

    def test_record_type(self, summary_line: str) -> None:
        result = parse_record(summary_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.record_type == "summary"

    def test_text_content(self, summary_line: str) -> None:
        result = parse_record(summary_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert (
            result.text_content == "The user asked about parsing JSONL files in Python."
        )

    def test_leaf_uuid(self, summary_line: str) -> None:
        result = parse_record(summary_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.leaf_uuid == "msg-asst-002"

    def test_role_is_none(self, summary_line: str) -> None:
        result = parse_record(summary_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.role is None


# --- AC1.5: Malformed JSON ---


class TestParseMalformedJSON:
    """cc-search-v2.AC1.5: Malformed JSON returns None, no crash."""

    def test_returns_none(self, malformed_line: str) -> None:
        result = parse_record(malformed_line, SESSION_ID)
        assert result is None

    def test_completely_invalid(self) -> None:
        result = parse_record("not json at all", SESSION_ID)
        assert result is None

    def test_empty_string(self) -> None:
        result = parse_record("", SESSION_ID)
        assert result is None

    def test_just_whitespace(self) -> None:
        result = parse_record("   ", SESSION_ID)
        assert result is None


# --- AC1.6: Unknown type ---


class TestParseUnknownType:
    """cc-search-v2.AC1.6: Record with unknown type returns None."""

    def test_returns_none(self, unknown_type_line: str) -> None:
        result = parse_record(unknown_type_line, SESSION_ID)
        assert result is None

    def test_missing_type_field(self) -> None:
        line = json.dumps({"uuid": "no-type", "timestamp": "2026-01-01T00:00:00Z"})
        result = parse_record(line, SESSION_ID)
        assert result is None

    def test_system_without_compact_boundary(self) -> None:
        """System records that are NOT compact_boundary should return None."""
        line = json.dumps(
            {
                "type": "system",
                "subtype": "other_system_thing",
                "uuid": "sys-001",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "s",
            }
        )
        result = parse_record(line, SESSION_ID)
        assert result is None

    def test_queue_operation_type(self) -> None:
        line = json.dumps(
            {
                "type": "queue-operation",
                "uuid": "q-001",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "s",
            }
        )
        result = parse_record(line, SESSION_ID)
        assert result is None

    def test_file_history_snapshot_type(self) -> None:
        line = json.dumps(
            {
                "type": "file-history-snapshot",
                "uuid": "fhs-001",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "s",
            }
        )
        result = parse_record(line, SESSION_ID)
        assert result is None


# --- AC1.7: Empty content ---


class TestParseEmptyContent:
    """cc-search-v2.AC1.7: Empty content field produces empty text_content."""

    def test_empty_string_content(self, empty_content_line: str) -> None:
        result = parse_record(empty_content_line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.text_content == ""

    def test_missing_content_field(self) -> None:
        """User message with no content key at all."""
        line = json.dumps(
            {
                "type": "user",
                "uuid": "msg-no-content",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "s",
                "message": {"role": "user"},
            }
        )
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.text_content == ""

    def test_assistant_empty_list_content(self) -> None:
        """Assistant message with empty list content."""
        line = json.dumps(
            {
                "type": "assistant",
                "uuid": "msg-empty-list",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "s",
                "message": {"role": "assistant", "content": []},
            }
        )
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.text_content == ""

    def test_missing_message_field(self) -> None:
        """User record with no message key at all."""
        line = json.dumps(
            {
                "type": "user",
                "uuid": "msg-no-message",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "s",
            }
        )
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert result.text_content == ""


# --- AC1.8: BLNS / adversarial strings (Hypothesis) ---


# Strategy for adversarial content strings
adversarial_text = st.one_of(
    # Null bytes
    st.just("hello\x00world"),
    # Control characters
    st.text(
        alphabet=st.characters(
            min_codepoint=0x00,
            max_codepoint=0x1F,
        ),
        min_size=1,
        max_size=50,
    ),
    # Unicode edge cases: emoji sequences, RTL markers, zero-width chars
    st.sampled_from(
        [
            "\U0001f600\U0001f4a9\U0001f525",  # emoji
            "\u200b\u200c\u200d\ufeff",  # zero-width chars
            "\u202a\u202b\u202c\u202d\u202e",  # bidi markers
            "\ud800",  # lone surrogate (invalid in some contexts)
            "A" * 100_000,  # extremely long string
            "Robert'); DROP TABLE Students;--",  # SQL injection
            "<script>alert('xss')</script>",  # XSS attempt
            "\\n\\r\\t\\0",  # escaped escape sequences
            "\n\r\t",  # actual whitespace chars
            '{"nested": "json"}',  # JSON inside content
        ]
    ),
    # Random unicode text
    st.text(min_size=0, max_size=200),
)


def _make_user_record_json(content: str) -> str:
    """Build a valid user JSONL record with arbitrary content."""
    record = {
        "type": "user",
        "uuid": "msg-fuzz-001",
        "parentUuid": None,
        "timestamp": "2026-01-01T00:00:00Z",
        "sessionId": "fuzz-session",
        "message": {
            "role": "user",
            "content": content,
        },
    }
    try:
        return json.dumps(record, ensure_ascii=False)
    except ValueError, UnicodeEncodeError:
        # If json.dumps itself can't handle it, that's a JSON limitation
        # not a parser bug. Return a known-good fallback.
        return json.dumps(
            {
                "type": "user",
                "uuid": "msg-fuzz-fallback",
                "parentUuid": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "fuzz-session",
                "message": {"role": "user", "content": "fallback"},
            }
        )


class TestBLNSAdversarialStrings:
    """cc-search-v2.AC1.8: Adversarial strings parse without error."""

    @given(content=adversarial_text)
    @settings(max_examples=200)
    def test_parse_record_never_raises(self, content: str) -> None:
        """parse_record returns SessionRecord or None, never raises."""
        line = _make_user_record_json(content)
        result = parse_record(line, "fuzz-session")
        assert result is None or isinstance(result, (SessionRecord, CompactEvent))

    @given(content=adversarial_text)
    @settings(max_examples=200)
    def test_valid_records_have_string_text_content(self, content: str) -> None:
        """If a record is returned, text_content is always a string."""
        line = _make_user_record_json(content)
        result = parse_record(line, "fuzz-session")
        if isinstance(result, SessionRecord):
            assert isinstance(result.text_content, str)

    def test_null_bytes_in_content(self) -> None:
        line = _make_user_record_json("hello\x00world")
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert "hello" in result.text_content

    def test_control_characters_in_content(self) -> None:
        line = _make_user_record_json("\x01\x02\x03\x1f")
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)

    def test_extremely_long_content(self) -> None:
        line = _make_user_record_json("A" * 100_000)
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)
        assert len(result.text_content) == 100_000

    def test_zero_width_characters(self) -> None:
        line = _make_user_record_json("\u200b\u200c\u200d\ufeff")
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)

    def test_rtl_markers(self) -> None:
        line = _make_user_record_json("\u202a\u202b\u202c")
        result = parse_record(line, SESSION_ID)
        assert isinstance(result, SessionRecord)


# --- parse_session tests ---


class TestParseSession:
    """Tests for parse_session generator."""

    def test_mixed_valid_and_invalid_lines(
        self,
        user_message_line: str,
        malformed_line: str,
        assistant_message_line: str,
        unknown_type_line: str,
        compact_boundary_line: str,
    ) -> None:
        """Yields only valid records, skipping None results."""
        lines = [
            user_message_line,
            malformed_line,
            assistant_message_line,
            unknown_type_line,
            compact_boundary_line,
        ]
        results = list(parse_session(lines, SESSION_ID))
        assert len(results) == 3
        assert isinstance(results[0], SessionRecord)
        assert isinstance(results[1], SessionRecord)
        assert isinstance(results[2], CompactEvent)

    def test_preserves_order(
        self,
        user_message_line: str,
        assistant_message_line: str,
    ) -> None:
        lines = [user_message_line, assistant_message_line]
        results = list(parse_session(lines, SESSION_ID))
        assert len(results) == 2
        assert isinstance(results[0], SessionRecord)
        assert results[0].record_type == "user"
        assert isinstance(results[1], SessionRecord)
        assert results[1].record_type == "assistant"

    def test_empty_iterable(self) -> None:
        results = list(parse_session([], SESSION_ID))
        assert results == []

    def test_all_invalid_lines(self, malformed_line: str) -> None:
        results = list(parse_session([malformed_line, "bad", ""], SESSION_ID))
        assert results == []

    def test_is_generator(self, user_message_line: str) -> None:
        """parse_session returns an iterator, not a list."""
        import types

        result = parse_session([user_message_line], SESSION_ID)
        assert isinstance(result, types.GeneratorType)
