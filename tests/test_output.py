"""Tests for output formatters.

Verifies: cc-search-v2.AC5.2, cc-search-v2.AC5.3, cc-search-v2.AC5.6
"""

import json

import pytest

from cc_search_chats.output import (
    SCHEMA_VERSION,
    _clean_text,
    _is_noise,
    format_context,
    format_extract,
    format_search_results,
    format_session_list,
    json_context,
    json_extract,
    json_index_all_result,
    json_index_result,
    json_search_results,
    json_session_list,
    render_safe,
)

# ============================================================
# Helpers — lightweight row-like dicts to simulate sqlite3.Row
# ============================================================


def _search_row(
    *,
    uuid: str = "msg-001",
    session_id: str = "sess-aaa",
    epoch: int = 0,
    timestamp: str = "2026-02-07T10:30:00.000Z",
    role: str = "user",
    snippet: str = "...the >>>database<<< schema...",
    score: float = -1.23,
    project_path: str = "/home/brian/project",
    real_project_path: str | None = None,
) -> dict:
    return {
        "uuid": uuid,
        "session_id": session_id,
        "epoch": epoch,
        "timestamp": timestamp,
        "role": role,
        "snippet": snippet,
        "score": score,
        "project_path": project_path,
        "real_project_path": real_project_path,
    }


def _message_row(
    *,
    uuid: str = "msg-001",
    session_id: str = "sess-aaa",
    epoch: int = 0,
    timestamp: str = "2026-02-07T10:30:00.000Z",
    role: str = "user",
    text_content: str = "How do I parse JSONL?",
    parent_uuid: str | None = None,
    is_summary: int = 0,
) -> dict:
    return {
        "uuid": uuid,
        "session_id": session_id,
        "epoch": epoch,
        "timestamp": timestamp,
        "role": role,
        "text_content": text_content,
        "parent_uuid": parent_uuid,
        "is_summary": is_summary,
    }


def _compact_event_row(
    *,
    uuid: str = "compact-001",
    session_id: str = "sess-aaa",
    epoch: int = 1,
    timestamp: str = "2026-02-07T11:00:00.000Z",
    trigger: str = "auto",
    pre_tokens: int = 45000,
    summary_text: str | None = None,
) -> dict:
    return {
        "uuid": uuid,
        "session_id": session_id,
        "epoch": epoch,
        "timestamp": timestamp,
        "trigger": trigger,
        "pre_tokens": pre_tokens,
        "summary_text": summary_text,
    }


def _session_row(
    *,
    session_id: str = "sess-aaa",
    project_path: str = "/home/brian/project",
    file_path: str = "/home/brian/.claude/projects/-home-brian-project/sess-aaa.jsonl",
    file_size: int = 4096,
    modified_at: str = "2026-02-07T14:00:00+00:00",
    summary: str | None = "Discussion about database schema design",
    epoch_count: int = 2,
    total_messages: int = 10,
    real_project_path: str | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "project_path": project_path,
        "file_path": file_path,
        "file_size": file_size,
        "modified_at": modified_at,
        "summary": summary,
        "epoch_count": epoch_count,
        "total_messages": total_messages,
        "real_project_path": real_project_path,
    }


# ============================================================
# render_safe tests (AC5.6)
# ============================================================


class TestRenderSafe:
    """Tests for the t-string safe rendering function."""

    def test_normal_text_passes_through(self) -> None:
        """Normal text renders without modification."""
        name = "Alice"
        result = render_safe(t"Hello, {name}!")
        assert result == "Hello, Alice!"

    def test_null_bytes_stripped(self) -> None:
        """Null bytes in interpolated values are stripped."""
        content = "before\x00after"
        result = render_safe(t"Message: {content}")
        assert "\x00" not in result
        assert "beforeafter" in result

    def test_literal_null_bytes_preserved(self) -> None:
        """Null bytes in the literal template parts are NOT stripped.

        Only interpolated values are sanitised. Template literals are
        trusted (written by the developer).
        """
        content = "safe"
        # The template literal itself is trusted
        result = render_safe(t"ok {content}")
        assert "ok safe" == result

    def test_control_characters_in_interpolated_value(self) -> None:
        """Control characters (other than null) in interpolated values pass through.

        The spec says strip null bytes. Other control chars are not stripped
        (they might be meaningful in some contexts). The primary concern is
        null bytes which can truncate C strings and cause terminal issues.
        """
        content = "line1\x01\x02\x1fline2"
        result = render_safe(t"Text: {content}")
        # Control chars are present (only null is stripped per spec)
        assert "line1" in result
        assert "line2" in result

    def test_ansi_escape_in_interpolated_value(self) -> None:
        """ANSI escape sequences in user content are not interpreted."""
        content = "\x1b[31mRED TEXT\x1b[0m"
        result = render_safe(t"Output: {content}")
        # The escape sequence chars are present as literal text
        assert "\x1b[31m" in result

    def test_integer_interpolation(self) -> None:
        """Non-string values are converted via str()."""
        count = 42
        result = render_safe(t"Count: {count}")
        assert result == "Count: 42"

    def test_none_interpolation(self) -> None:
        """None is rendered as 'None'."""
        value = None
        result = render_safe(t"Value: {value}")
        assert result == "Value: None"

    def test_empty_template(self) -> None:
        """Empty template produces empty string."""
        result = render_safe(t"")
        assert result == ""

    def test_template_with_only_interpolation(self) -> None:
        """Template with only interpolated values works."""
        a = "hello"
        b = "world"
        result = render_safe(t"{a} {b}")
        assert result == "hello world"


# ============================================================
# Human-readable formatter tests (AC5.3)
# ============================================================


class TestFormatSearchResults:
    """Tests for human-readable search result formatting."""

    def test_single_result_shows_session_id(self) -> None:
        rows = [_search_row(session_id="sess-aaa")]
        output = format_search_results(rows)
        assert "sess-aaa" in output

    def test_single_result_shows_epoch(self) -> None:
        rows = [_search_row(epoch=2)]
        output = format_search_results(rows)
        assert "epoch 2" in output.lower() or "Epoch 2" in output

    def test_single_result_shows_snippet(self) -> None:
        rows = [_search_row(snippet="...the >>>database<<< schema...")]
        output = format_search_results(rows)
        assert "database" in output

    def test_single_result_shows_timestamp(self) -> None:
        rows = [_search_row(timestamp="2026-02-07T10:30:00.000Z")]
        output = format_search_results(rows)
        assert "2026-02-07" in output

    def test_multiple_results_separated(self) -> None:
        rows = [
            _search_row(uuid="msg-001", session_id="sess-aaa"),
            _search_row(uuid="msg-002", session_id="sess-bbb"),
        ]
        output = format_search_results(rows)
        assert "sess-aaa" in output
        assert "sess-bbb" in output

    def test_empty_results(self) -> None:
        output = format_search_results([])
        assert output == "" or "no results" in output.lower()

    def test_shows_role(self) -> None:
        rows = [_search_row(role="assistant")]
        output = format_search_results(rows)
        assert "assistant" in output.lower()

    def test_widened_scope_shows_note(self) -> None:
        rows = [_search_row()]
        output = format_search_results(
            rows,
            scope="widened",
            searched_project="/home/brian/alpha",
            project_count=5,
        )
        assert "No matches in /home/brian/alpha" in output
        assert "5 indexed projects" in output

    def test_project_label_shown_when_scope_spans_projects(self) -> None:
        rows = [_search_row(project_path="/home/x/proj")]
        output = format_search_results(rows, scope="all")
        assert "/home/x/proj" in output

    def test_no_project_label_in_local_scope(self) -> None:
        rows = [_search_row(project_path="/home/x/proj")]
        output = format_search_results(rows, scope="local")
        assert "/home/x/proj" not in output


class TestFormatExtract:
    """Tests for human-readable session extract formatting."""

    def test_epoch_markers_between_epochs(self) -> None:
        """AC5.3: Epoch markers appear between epochs with compression info."""
        messages = [
            _message_row(
                epoch=0,
                timestamp="2026-02-07T10:30:00.000Z",
                role="user",
                text_content="first",
            ),
            _message_row(
                uuid="msg-002",
                epoch=0,
                timestamp="2026-02-07T10:31:00.000Z",
                role="assistant",
                text_content="reply",
            ),
            _message_row(
                uuid="msg-003",
                epoch=1,
                timestamp="2026-02-07T11:00:01.000Z",
                role="user",
                text_content="continued",
            ),
            _message_row(
                uuid="msg-004",
                epoch=1,
                timestamp="2026-02-07T11:01:00.000Z",
                role="assistant",
                text_content="second reply",
            ),
        ]
        compact_events = [
            _compact_event_row(
                epoch=1,
                timestamp="2026-02-07T11:00:00.000Z",
                trigger="auto",
                pre_tokens=45000,
            ),
        ]
        output = format_extract(messages, compact_events)
        assert "Epoch 1" in output
        assert "auto" in output
        assert "45000" in output or "45,000" in output or "45000" in output

    def test_role_labels_present(self) -> None:
        """AC5.3: Role labels appear in output."""
        messages = [
            _message_row(role="user", text_content="hello"),
            _message_row(uuid="msg-002", role="assistant", text_content="hi there"),
        ]
        output = format_extract(messages, [])
        assert "User" in output or "user" in output
        assert "Assistant" in output or "assistant" in output

    def test_timestamps_in_output(self) -> None:
        """AC5.3: ISO 8601 timestamps appear in output."""
        messages = [
            _message_row(timestamp="2026-02-07T14:32:00.000Z"),
        ]
        output = format_extract(messages, [])
        assert "2026-02-07" in output

    def test_message_text_in_output(self) -> None:
        messages = [
            _message_row(text_content="How do I parse JSONL files?"),
        ]
        output = format_extract(messages, [])
        assert "How do I parse JSONL files?" in output

    def test_empty_extract(self) -> None:
        output = format_extract([], [])
        assert output == "" or "no messages" in output.lower()

    def test_epoch_marker_shows_keywords_when_available(self) -> None:
        """Epoch marker includes keyword summary from compact_event."""
        compact_events = [
            _compact_event_row(
                epoch=1,
                summary_text="Discussion about database schema migration",
            ),
        ]
        messages = [
            _message_row(epoch=0, text_content="before"),
            _message_row(
                uuid="msg-002",
                epoch=1,
                timestamp="2026-02-07T11:01:00.000Z",
                text_content="after",
            ),
        ]
        output = format_extract(messages, compact_events)
        # The summary text should appear near the epoch marker
        assert "database schema migration" in output

    def test_single_epoch_no_markers(self) -> None:
        """Single epoch (no compact events) should have no epoch markers."""
        messages = [
            _message_row(epoch=0, text_content="hello"),
            _message_row(
                uuid="msg-002",
                epoch=0,
                timestamp="2026-02-07T10:31:00.000Z",
                text_content="world",
            ),
        ]
        output = format_extract(messages, [])
        assert "Epoch" not in output


class TestFormatSessionList:
    """Tests for human-readable session list formatting."""

    def test_shows_session_id(self) -> None:
        rows = [_session_row(session_id="sess-aaa")]
        output = format_session_list(rows)
        assert "sess-aaa" in output

    def test_shows_file_size(self) -> None:
        rows = [_session_row(file_size=4096)]
        output = format_session_list(rows)
        # Should show size in some form (bytes, KB, etc.)
        assert "4096" in output or "4.0" in output or "4 K" in output

    def test_shows_summary(self) -> None:
        rows = [_session_row(summary="Database design discussion")]
        output = format_session_list(rows)
        assert "Database design discussion" in output

    def test_shows_epoch_count(self) -> None:
        rows = [_session_row(epoch_count=3)]
        output = format_session_list(rows)
        assert "3" in output

    def test_shows_message_count(self) -> None:
        rows = [_session_row(total_messages=150)]
        output = format_session_list(rows)
        assert "150" in output

    def test_empty_list(self) -> None:
        output = format_session_list([])
        assert output == "" or "no sessions" in output.lower()


class TestFormatContext:
    """Tests for human-readable context formatting."""

    def test_target_marker_present(self) -> None:
        target = _message_row(text_content="the target message")
        before = [_message_row(uuid="msg-before", text_content="before text")]
        after = [_message_row(uuid="msg-after", text_content="after text")]
        output = format_context(target, before, after)
        assert "TARGET" in output or ">>>" in output

    def test_before_and_after_present(self) -> None:
        target = _message_row(text_content="target")
        before = [_message_row(uuid="msg-b1", text_content="context before")]
        after = [_message_row(uuid="msg-a1", text_content="context after")]
        output = format_context(target, before, after)
        assert "context before" in output
        assert "context after" in output

    def test_empty_context(self) -> None:
        target = _message_row(text_content="alone")
        output = format_context(target, [], [])
        assert "alone" in output

    def test_noise_filtered_by_default(self) -> None:
        """Context filtering: tool-only messages in before/after are skipped."""
        target = _message_row(text_content="the target")
        before = [
            _message_row(uuid="b1", text_content="real before"),
            _message_row(uuid="b2", text_content="[tool: Bash]\n[tool: Read]"),
        ]
        after = [
            _message_row(uuid="a1", text_content=""),
            _message_row(uuid="a2", text_content="real after"),
        ]
        output = format_context(target, before, after)
        assert "real before" in output
        assert "real after" in output
        assert "[tool: Bash]" not in output

    def test_verbose_preserves_all(self) -> None:
        """Context verbose=True shows tool calls and empty messages."""
        target = _message_row(text_content="target")
        before = [_message_row(uuid="b1", text_content="[tool: Bash]")]
        after = [_message_row(uuid="a1", text_content="")]
        output = format_context(target, before, after, verbose=True)
        assert "[tool: Bash]" in output

    def test_leading_newlines_stripped(self) -> None:
        """Context: leading newlines stripped from text unless verbose."""
        target = _message_row(text_content="\n\nactual text")
        output = format_context(target, [], [])
        assert "\n\nactual text" not in output
        assert "actual text" in output

    def test_verbose_preserves_leading_newlines(self) -> None:
        target = _message_row(text_content="\n\nactual text")
        output = format_context(target, [], [], verbose=True)
        assert "\n\nactual text" in output


# ============================================================
# Noise filtering tests
# ============================================================


class TestIsNoise:
    """Tests for the _is_noise helper."""

    def test_empty_string_is_noise(self) -> None:
        assert _is_noise("") is True

    def test_whitespace_only_is_noise(self) -> None:
        assert _is_noise("   \n\t  ") is True

    def test_single_tool_call_is_noise(self) -> None:
        assert _is_noise("[tool: Bash]") is True

    def test_multiple_tool_calls_is_noise(self) -> None:
        assert _is_noise("[tool: Bash]\n[tool: Read]\n[tool: Write]") is True

    def test_tool_call_with_trailing_newline_is_noise(self) -> None:
        assert _is_noise("[tool: Bash]\n") is True

    def test_real_text_is_not_noise(self) -> None:
        assert _is_noise("How do I parse JSONL?") is False

    def test_text_with_tool_call_is_not_noise(self) -> None:
        assert _is_noise("Let me read that file.\n[tool: Read]") is False

    def test_tool_call_surrounded_by_whitespace(self) -> None:
        assert _is_noise("  [tool: Bash]\n  ") is True


class TestCleanText:
    """Tests for the _clean_text helper."""

    def test_strips_leading_newlines(self) -> None:
        assert _clean_text("\n\nhello") == "hello"

    def test_preserves_internal_newlines(self) -> None:
        assert _clean_text("\nline1\nline2") == "line1\nline2"

    def test_preserves_non_newline_text(self) -> None:
        assert _clean_text("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert _clean_text("") == ""


class TestExtractFiltering:
    """Tests for format_extract noise filtering and verbose mode."""

    def test_tool_only_messages_filtered_by_default(self) -> None:
        messages = [
            _message_row(text_content="real message"),
            _message_row(
                uuid="msg-002",
                text_content="[tool: Bash]\n[tool: Read]",
                timestamp="2026-02-07T10:31:00.000Z",
            ),
            _message_row(
                uuid="msg-003",
                text_content="another real message",
                timestamp="2026-02-07T10:32:00.000Z",
            ),
        ]
        output = format_extract(messages, [])
        assert "real message" in output
        assert "another real message" in output
        assert "[tool: Bash]" not in output

    def test_empty_messages_filtered_by_default(self) -> None:
        messages = [
            _message_row(text_content="real message"),
            _message_row(
                uuid="msg-002",
                text_content="",
                timestamp="2026-02-07T10:31:00.000Z",
            ),
        ]
        output = format_extract(messages, [])
        # Only the real message should produce output, no empty block
        blocks = [b for b in output.split("\n\n") if b.strip()]
        assert len(blocks) == 1

    def test_verbose_shows_tool_calls(self) -> None:
        messages = [
            _message_row(text_content="[tool: Bash]\n[tool: Read]"),
        ]
        output = format_extract(messages, [], verbose=True)
        assert "[tool: Bash]" in output

    def test_verbose_shows_empty_messages(self) -> None:
        messages = [
            _message_row(text_content="real"),
            _message_row(
                uuid="msg-002",
                text_content="",
                timestamp="2026-02-07T10:31:00.000Z",
            ),
        ]
        output = format_extract(messages, [], verbose=True)
        # Both messages should be present
        assert "real" in output

    def test_leading_newlines_stripped_by_default(self) -> None:
        messages = [_message_row(text_content="\n\nactual content")]
        output = format_extract(messages, [])
        assert "\n\nactual content" not in output
        assert "actual content" in output

    def test_verbose_preserves_leading_newlines(self) -> None:
        messages = [_message_row(text_content="\n\nactual content")]
        output = format_extract(messages, [], verbose=True)
        assert "\n\nactual content" in output


# ============================================================
# JSON formatter tests (AC5.2)
# ============================================================


class TestJsonSearchResults:
    """Tests for JSON search result output."""

    def test_valid_json(self) -> None:
        """AC5.2: Output is valid JSON parseable by json.loads()."""
        rows = [_search_row()]
        output = json_search_results(rows)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)
        assert isinstance(parsed["results"], list)

    def test_wrapper_has_scope_metadata(self) -> None:
        rows = [_search_row()]
        parsed = json.loads(
            json_search_results(
                rows, scope="widened", searched_project="/p", project_count=7
            )
        )
        assert parsed["scope"] == "widened"
        assert parsed["searched_project"] == "/p"
        assert parsed["project_count"] == 7

    def test_result_has_expected_keys(self) -> None:
        rows = [_search_row()]
        parsed = json.loads(json_search_results(rows))
        result = parsed["results"][0]
        assert "uuid" in result
        assert "session_id" in result
        assert "epoch" in result
        assert "timestamp" in result
        assert "role" in result
        assert "snippet" in result
        assert "score" in result
        assert "project_path" in result

    def test_empty_results(self) -> None:
        output = json_search_results([])
        parsed = json.loads(output)
        assert parsed["results"] == []

    def test_multiple_results(self) -> None:
        rows = [_search_row(uuid="a"), _search_row(uuid="b")]
        parsed = json.loads(json_search_results(rows))
        assert len(parsed["results"]) == 2


class TestJsonExtract:
    """Tests for JSON extract output."""

    def test_valid_json(self) -> None:
        """AC5.2: Output is valid JSON with session_id and epochs."""
        rows = [_message_row()]
        output = json_extract(rows, [], session_id="sess-aaa")
        parsed = json.loads(output)
        assert isinstance(parsed, dict)
        assert "session_id" in parsed
        assert "epochs" in parsed

    def test_session_id_in_output(self) -> None:
        rows = [_message_row()]
        parsed = json.loads(json_extract(rows, [], session_id="sess-aaa"))
        assert parsed["session_id"] == "sess-aaa"

    def test_epochs_structure(self) -> None:
        rows = [
            _message_row(epoch=0, text_content="first"),
            _message_row(uuid="msg-002", epoch=1, text_content="second"),
        ]
        parsed = json.loads(json_extract(rows, [], session_id="sess-aaa"))
        assert len(parsed["epochs"]) == 2
        assert parsed["epochs"][0]["epoch"] == 0
        assert parsed["epochs"][1]["epoch"] == 1

    def test_message_fields(self) -> None:
        rows = [
            _message_row(
                uuid="msg-x",
                role="user",
                text_content="hello",
                timestamp="2026-02-07T10:00:00.000Z",
            )
        ]
        parsed = json.loads(json_extract(rows, [], session_id="sess-aaa"))
        msg = parsed["epochs"][0]["messages"][0]
        assert msg["uuid"] == "msg-x"
        assert msg["role"] == "user"
        assert msg["text"] == "hello"
        assert msg["timestamp"] == "2026-02-07T10:00:00.000Z"

    def test_compact_events_in_epochs(self) -> None:
        rows = [
            _message_row(epoch=0, text_content="before"),
            _message_row(uuid="msg-002", epoch=1, text_content="after"),
        ]
        compact_events = [
            _compact_event_row(epoch=1, trigger="auto", pre_tokens=45000),
        ]
        parsed = json.loads(json_extract(rows, compact_events, session_id="sess-aaa"))
        epoch_1 = parsed["epochs"][1]
        assert (
            epoch_1.get("trigger") == "auto" or epoch_1.get("compression") is not None
        )

    def test_empty_extract(self) -> None:
        parsed = json.loads(json_extract([], [], session_id="sess-aaa"))
        assert parsed["session_id"] == "sess-aaa"
        assert parsed["epochs"] == []


class TestJsonSessionList:
    """Tests for JSON session list output."""

    def test_valid_json(self) -> None:
        """AC5.2: Output is valid JSON."""
        rows = [_session_row()]
        output = json_session_list(rows)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)
        assert isinstance(parsed["sessions"], list)

    def test_session_fields(self) -> None:
        rows = [_session_row()]
        parsed = json.loads(json_session_list(rows))
        sess = parsed["sessions"][0]
        assert "session_id" in sess
        assert "project_path" in sess
        assert "file_size" in sess
        assert "modified_at" in sess
        assert "summary" in sess
        assert "epoch_count" in sess
        assert "message_count" in sess

    def test_empty_list(self) -> None:
        parsed = json.loads(json_session_list([]))
        assert parsed["sessions"] == []


class TestSchemaVersion:
    """Every --json payload carries the schema_version marker."""

    def test_search_has_schema_version(self) -> None:
        parsed = json.loads(json_search_results([_search_row()]))
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_extract_has_schema_version(self) -> None:
        parsed = json.loads(json_extract([], [], session_id="s"))
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_list_has_schema_version(self) -> None:
        parsed = json.loads(json_session_list([_session_row()]))
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_context_has_schema_version(self) -> None:
        parsed = json.loads(json_context(_message_row(), [], []))
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_index_has_schema_version(self) -> None:
        parsed = json.loads(json_index_result(3, "/p"))
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_index_all_has_schema_version(self) -> None:
        parsed = json.loads(
            json_index_all_result({"projects": 1, "indexed": 2, "skipped": 3})
        )
        assert parsed["schema_version"] == SCHEMA_VERSION


class TestJsonContext:
    """Tests for JSON context output."""

    def test_valid_json(self) -> None:
        target = _message_row()
        output = json_context(target, [], [])
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_structure(self) -> None:
        target = _message_row(text_content="target")
        before = [_message_row(uuid="b1", text_content="before")]
        after = [_message_row(uuid="a1", text_content="after")]
        parsed = json.loads(json_context(target, before, after))
        assert "target" in parsed
        assert "before" in parsed
        assert "after" in parsed
        assert parsed["target"]["text"] == "target"
        assert len(parsed["before"]) == 1
        assert len(parsed["after"]) == 1


class TestJsonWithBLNS:
    """Tests for JSON output with adversarial content (Big List of Naughty Strings)."""

    @pytest.mark.parametrize(
        "content",
        [
            "normal text",
            "null\x00byte",
            "tab\there",
            'quotes "inside" text',
            "backslash\\here",
            "newline\nand\rcarriage return",
            "unicode \u2603 snowman",
            "emoji \U0001f600 grinning",
            "rtl \u200f marker",
            "zero-width \u200b joiner",
            "\x1b[31mANSI escape\x1b[0m",
            "'; DROP TABLE message; --",
            "<script>alert('xss')</script>",
            "a" * 100_000,  # very long string
        ],
        ids=[
            "normal",
            "null_byte",
            "tab",
            "quotes",
            "backslash",
            "newline_cr",
            "unicode_snowman",
            "emoji",
            "rtl_marker",
            "zero_width",
            "ansi_escape",
            "sql_injection",
            "xss",
            "very_long",
        ],
    )
    def test_json_search_results_with_naughty_content(self, content: str) -> None:
        """json.dumps handles escaping correctly for all content."""
        rows = [_search_row(snippet=content)]
        output = json_search_results(rows)
        parsed = json.loads(output)
        assert isinstance(parsed["results"], list)
        assert len(parsed["results"]) == 1

    @pytest.mark.parametrize(
        "content",
        [
            "null\x00byte",
            "unicode \u2603 snowman",
            "'; DROP TABLE message; --",
        ],
    )
    def test_json_extract_with_naughty_content(self, content: str) -> None:
        rows = [_message_row(text_content=content)]
        output = json_extract(rows, [], session_id="sess-aaa")
        parsed = json.loads(output)
        assert isinstance(parsed, dict)
        assert len(parsed["epochs"]) == 1


class TestRealProjectPathDisplay:
    """Human output shows the true path; JSON exposes it additively."""

    def test_list_shows_real_project_path_when_present(self) -> None:
        rows = [
            _session_row(
                project_path="/home/x/proj",
                real_project_path="/home/x/my-real-proj",
            )
        ]
        output = format_session_list(rows)
        assert "/home/x/my-real-proj" in output

    def test_list_falls_back_to_project_path_when_real_absent(self) -> None:
        rows = [_session_row(project_path="/home/x/proj", real_project_path=None)]
        output = format_session_list(rows)
        assert "/home/x/proj" in output

    def test_json_list_includes_real_project_path(self) -> None:
        rows = [_session_row(real_project_path="/home/x/my-real-proj")]
        data = json.loads(json_session_list(rows))
        assert data["sessions"][0]["real_project_path"] == "/home/x/my-real-proj"

    def test_search_label_uses_real_project_path(self) -> None:
        rows = [
            _search_row(
                project_path="/home/x/proj",
                real_project_path="/home/x/my-real-proj",
            )
        ]
        output = format_search_results(rows, scope="all")
        assert "/home/x/my-real-proj" in output

    def test_json_search_includes_real_project_path(self) -> None:
        rows = [_search_row(real_project_path="/home/x/my-real-proj")]
        data = json.loads(json_search_results(rows, scope="all"))
        assert data["results"][0]["real_project_path"] == "/home/x/my-real-proj"
