"""JSONL parser for Claude Code session files.

Pure functions over data -- no file I/O. The caller is responsible for
opening files and providing lines. This is the Functional Core.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from cc_search_chats.core.models import CompactEvent, SessionRecord


def _to_str_or_none(value: Any) -> str | None:
    """Convert a value to str or None."""
    if value is None:
        return None
    return str(value)


def _extract_text_content_string(content: Any) -> str:
    """Extract text from a string content field (user messages)."""
    if isinstance(content, str):
        return content
    return ""


def _extract_text_content_list(content: Any) -> str:
    """Extract text from a list content field (assistant messages).

    For each item in the list:
    - type == "text": append item["text"]
    - type == "tool_use": append [tool: {item["name"]}]
    - Other types: skip
    Join all parts with newline.
    """
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        elif item_type == "tool_use":
            name = item.get("name", "unknown")
            parts.append(f"[tool: {name}]")

    return "\n".join(parts)


def _parse_message_record(
    data: dict[str, Any],
    record_type: str,
    session_id: str,
) -> SessionRecord:
    """Parse a user or assistant message record."""
    message = data.get("message")
    if not isinstance(message, dict):
        # No message field -- produce record with empty content
        return SessionRecord(
            record_type=record_type,
            uuid=str(data.get("uuid", "")),
            parent_uuid=_to_str_or_none(data.get("parentUuid")),
            timestamp=str(data.get("timestamp", "")),
            session_id=session_id,
            role=record_type,
            text_content="",
            leaf_uuid=None,
        )

    content = message.get("content", "")
    role = message.get("role", record_type)

    if record_type == "assistant":
        text_content = _extract_text_content_list(content)
    else:
        text_content = _extract_text_content_string(content)

    return SessionRecord(
        record_type=record_type,
        uuid=str(data.get("uuid", "")),
        parent_uuid=_to_str_or_none(data.get("parentUuid")),
        timestamp=str(data.get("timestamp", "")),
        session_id=session_id,
        role=str(role) if role is not None else record_type,
        text_content=text_content,
        leaf_uuid=None,
    )


def _parse_summary_record(
    data: dict[str, Any],
    session_id: str,
) -> SessionRecord:
    """Parse a summary record."""
    summary_text = data.get("summary", "")
    leaf_uuid = data.get("leafUuid")

    return SessionRecord(
        record_type="summary",
        uuid=str(data.get("uuid", "")),
        parent_uuid=_to_str_or_none(data.get("parentUuid")),
        timestamp=str(data.get("timestamp", "")),
        session_id=session_id,
        role=None,
        text_content=str(summary_text) if summary_text else "",
        leaf_uuid=_to_str_or_none(leaf_uuid),
    )


def _parse_compact_boundary(
    data: dict[str, Any],
    session_id: str,
) -> CompactEvent | None:
    """Parse a system/compact_boundary record."""
    metadata = data.get("compactMetadata")
    if not isinstance(metadata, dict):
        return None

    trigger = metadata.get("trigger", "")
    pre_tokens = metadata.get("preTokens", 0)

    return CompactEvent(
        uuid=str(data.get("uuid", "")),
        session_id=session_id,
        timestamp=str(data.get("timestamp", "")),
        trigger=str(trigger),
        pre_tokens=int(pre_tokens),
    )


def parse_record(
    line: str,
    session_id: str,
) -> SessionRecord | CompactEvent | None:
    """Parse a single JSONL line.

    Returns:
        SessionRecord for user, assistant, summary types.
        CompactEvent for system type with subtype=compact_boundary.
        None for malformed JSON, unknown types, or records without a type field.

    Never raises exceptions.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError, ValueError:
        return None

    if not isinstance(data, dict):
        return None

    record_type = data.get("type")
    if not isinstance(record_type, str):
        return None

    if record_type in ("user", "assistant"):
        return _parse_message_record(data, record_type, session_id)

    if record_type == "summary":
        return _parse_summary_record(data, session_id)

    if record_type == "system":
        subtype = data.get("subtype")
        if subtype == "compact_boundary":
            return _parse_compact_boundary(data, session_id)
        return None

    # Unknown type (progress, queue-operation, file-history-snapshot, etc.)
    return None


def parse_session(
    lines: Iterable[str],
    session_id: str,
) -> Iterator[SessionRecord | CompactEvent]:
    """Parse an iterable of JSONL lines, yielding valid records.

    Skips lines that produce None (malformed or unknown types).
    This is a generator -- no memory accumulation.

    The caller is responsible for file I/O (opening the file, reading lines).
    This function is a pure transformation over an iterable.
    """
    for line in lines:
        result = parse_record(line, session_id)
        if result is not None:
            yield result
