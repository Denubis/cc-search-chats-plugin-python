"""Pytest fixtures providing sample JSONL record strings."""

import json

import pytest


@pytest.fixture
def user_message_line() -> str:
    """A type=user record with string content."""
    return json.dumps(
        {
            "type": "user",
            "uuid": "msg-user-001",
            "parentUuid": "msg-parent-000",
            "timestamp": "2026-02-07T10:30:00.000Z",
            "sessionId": "session-abc-123",
            "message": {
                "role": "user",
                "content": "How do I parse JSONL files in Python?",
            },
        }
    )


@pytest.fixture
def assistant_message_line() -> str:
    """A type=assistant record with list content containing text and tool_use."""
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "msg-asst-002",
            "parentUuid": "msg-user-001",
            "timestamp": "2026-02-07T10:30:05.000Z",
            "sessionId": "session-abc-123",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "You can use the json module."},
                    {"type": "tool_use", "name": "Read", "id": "tool-1", "input": {}},
                    {"type": "text", "text": "Here is an example:"},
                ],
            },
        }
    )


@pytest.fixture
def compact_boundary_line() -> str:
    """A type=system, subtype=compact_boundary record with compactMetadata."""
    return json.dumps(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "msg-compact-003",
            "timestamp": "2026-02-07T11:00:00.000Z",
            "sessionId": "session-abc-123",
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": 42000,
            },
        }
    )


@pytest.fixture
def summary_line() -> str:
    """A type=summary record with summary text and leafUuid."""
    return json.dumps(
        {
            "type": "summary",
            "uuid": "msg-summary-004",
            "parentUuid": "msg-asst-002",
            "timestamp": "2026-02-07T11:05:00.000Z",
            "sessionId": "session-abc-123",
            "summary": "The user asked about parsing JSONL files in Python.",
            "leafUuid": "msg-asst-002",
        }
    )


@pytest.fixture
def malformed_line() -> str:
    """Invalid JSON (truncated, missing braces)."""
    return '{"type": "user", "uuid": "broken'


@pytest.fixture
def unknown_type_line() -> str:
    """Valid JSON with type=progress (an unknown/skipped type)."""
    return json.dumps(
        {
            "type": "progress",
            "uuid": "msg-progress-005",
            "timestamp": "2026-02-07T11:10:00.000Z",
            "sessionId": "session-abc-123",
        }
    )


@pytest.fixture
def empty_content_line() -> str:
    """A user message with content: '' (empty string)."""
    return json.dumps(
        {
            "type": "user",
            "uuid": "msg-user-006",
            "parentUuid": "msg-user-001",
            "timestamp": "2026-02-07T11:15:00.000Z",
            "sessionId": "session-abc-123",
            "message": {
                "role": "user",
                "content": "",
            },
        }
    )
