"""Pytest fixtures providing sample JSONL record strings and database helpers."""

import json
import sqlite3
from pathlib import Path

import pytest

from cc_search_chats.core.models import SessionMeta
from cc_search_chats.storage.index import close_db, index_session, open_db


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


# ============================================================
# Database fixtures for Phase 2 tests
# ============================================================

SESSION_ID_A = "aaaaaaaa-1111-2222-3333-aaaaaaaaaaaa"
SESSION_ID_B = "bbbbbbbb-1111-2222-3333-bbbbbbbbbbbb"


def _make_session_lines(
    session_id: str,
    *,
    compact_boundaries: int = 1,
    project_path: str = "/home/brian/project",
) -> list[str]:
    """Build a list of JSONL lines representing a complete session.

    Creates user/assistant messages across (compact_boundaries + 1) epochs,
    with compact_boundary events in between. Each epoch gets 2 user + 2
    assistant messages.
    """
    lines: list[str] = []
    msg_idx = 0
    parent = None

    for epoch in range(compact_boundaries + 1):
        # If not the first epoch, insert a compact_boundary
        if epoch > 0:
            lines.append(
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "uuid": f"compact-{session_id}-{epoch}",
                        "timestamp": f"2026-02-07T{10 + epoch}:00:00.000Z",
                        "sessionId": session_id,
                        "compactMetadata": {
                            "trigger": "auto",
                            "preTokens": 40000 * epoch,
                        },
                    }
                )
            )
            # Summary user message (compression summary)
            msg_idx += 1
            summary_uuid = f"msg-{session_id}-{msg_idx:04d}"
            lines.append(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": summary_uuid,
                        "parentUuid": None,
                        "timestamp": f"2026-02-07T{10 + epoch}:00:01.000Z",
                        "sessionId": session_id,
                        "message": {
                            "role": "user",
                            "content": (
                                "This session is being continued from a previous "
                                "conversation about database schema migration."
                            ),
                        },
                    }
                )
            )
            parent = summary_uuid

        # Add regular messages for this epoch
        for i in range(2):
            msg_idx += 1
            user_uuid = f"msg-{session_id}-{msg_idx:04d}"
            topics = {
                0: [
                    "How do I design a database schema for indexing?",
                    "What about using SQLite FTS5 for full-text search?",
                ],
                1: [
                    "How does authentication work with OAuth tokens?",
                    "Can you explain JWT token refresh flow?",
                ],
                2: [
                    "What testing frameworks work best with Python?",
                    "How do I write property-based tests with Hypothesis?",
                ],
                3: [
                    "How do I deploy a Python package to PyPI?",
                    "What about continuous integration with GitHub Actions?",
                ],
            }
            epoch_topics = topics.get(epoch, topics[0])

            lines.append(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": user_uuid,
                        "parentUuid": parent,
                        "timestamp": f"2026-02-07T{10 + epoch}:{10 + i * 5:02d}:00.000Z",
                        "sessionId": session_id,
                        "message": {
                            "role": "user",
                            "content": epoch_topics[i],
                        },
                    }
                )
            )

            msg_idx += 1
            asst_uuid = f"msg-{session_id}-{msg_idx:04d}"
            asst_topics = {
                0: [
                    "You can design a schema with normalised tables for session, message, and compact_event.",
                    "FTS5 is excellent for full-text search in SQLite. Use unicode61 tokeniser.",
                ],
                1: [
                    "OAuth authentication uses access tokens and refresh tokens for API access.",
                    "JWT refresh tokens should be stored securely and rotated periodically.",
                ],
                2: [
                    "Pytest is the most popular testing framework for Python projects.",
                    "Hypothesis generates random test inputs to find edge cases automatically.",
                ],
                3: [
                    "Use twine to upload packages to PyPI after building with hatchling.",
                    "GitHub Actions can run tests on every push and deploy on tag creation.",
                ],
            }
            epoch_asst = asst_topics.get(epoch, asst_topics[0])

            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": asst_uuid,
                        "parentUuid": user_uuid,
                        "timestamp": f"2026-02-07T{10 + epoch}:{10 + i * 5 + 1:02d}:00.000Z",
                        "sessionId": session_id,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": epoch_asst[i]},
                            ],
                        },
                    }
                )
            )
            parent = asst_uuid

    return lines


@pytest.fixture
def sample_session_lines() -> list[str]:
    """JSONL lines representing a session with 2 epochs (1 compact_boundary)."""
    return _make_session_lines(SESSION_ID_A, compact_boundaries=1)


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    """In-memory-style SQLite database with schema applied.

    Uses a temp file (not :memory:) so that WAL mode works correctly.
    Yields connection, closes on teardown.
    """
    db_path = tmp_path / "test_index.db"
    conn = open_db(db_path)
    yield conn
    close_db(conn)


@pytest.fixture
def indexed_db(
    db_conn: sqlite3.Connection,
    sample_session_lines: list[str],
    tmp_path: Path,
) -> sqlite3.Connection:
    """db_conn with sample_session_lines already indexed.

    Creates a temporary JSONL file from the sample lines, then calls
    index_session to populate the database.
    """
    # Write JSONL to a temp file
    session_file = tmp_path / f"{SESSION_ID_A}.jsonl"
    session_file.write_text("\n".join(sample_session_lines), encoding="utf-8")

    meta = SessionMeta(
        session_id=SESSION_ID_A,
        file_path=str(session_file),
        project_path="/home/brian/project",
        file_size=session_file.stat().st_size,
        modified_at=session_file.stat().st_mtime,
    )
    index_session(db_conn, meta)
    return db_conn
