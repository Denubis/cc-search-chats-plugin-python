"""JSONL parser for Claude Code session files.

Pure functions over data -- no file I/O. The caller is responsible for
opening files and providing lines. This is the Functional Core.
"""

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


def _stringify(value: Any) -> str:
    """Flatten a tool_use input or tool_result value into searchable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _extract_full_content_list(content: Any) -> str:
    """Extract full searchable text from list content.

    Unlike :func:`_extract_text_content_list` (the clean conversation view),
    this also includes thinking blocks, tool_use inputs, and tool_result
    outputs. Used only by the transient ``--everything`` index; never
    persisted.
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
        elif item_type == "thinking":
            thinking = item.get("thinking", "")
            if isinstance(thinking, str):
                parts.append(thinking)
        elif item_type == "tool_use":
            name = item.get("name", "unknown")
            parts.append(f"[tool: {name}]")
            parts.append(_stringify(item.get("input")))
        elif item_type == "tool_result":
            parts.append(_stringify(item.get("content")))

    return "\n".join(p for p in parts if p)


def _parse_message_record(
    data: dict[str, Any],
    record_type: str,
    session_id: str,
    *,
    full_content: bool = False,
) -> SessionRecord:
    """Parse a user or assistant message record.

    When ``full_content`` is True, list content is flattened with
    :func:`_extract_full_content_list` (thinking + tool I/O included) rather
    than the clean conversation view.
    """
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
            cwd=_to_str_or_none(data.get("cwd")),
        )

    content = message.get("content", "")
    role = message.get("role", record_type)

    if full_content:
        if isinstance(content, list):
            text_content = _extract_full_content_list(content)
        else:
            text_content = _extract_text_content_string(content)
    elif record_type == "assistant":
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
        cwd=_to_str_or_none(data.get("cwd")),
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
        cwd=_to_str_or_none(data.get("cwd")),
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

    # Coerce pre_tokens to int, handling various malformed types.
    try:
        pre_tokens_int = int(pre_tokens)
    except ValueError, TypeError:
        # Non-numeric string (e.g., "many"), non-coercible type (e.g., [1,2,3])
        pre_tokens_int = 0

    return CompactEvent(
        uuid=str(data.get("uuid", "")),
        session_id=session_id,
        timestamp=str(data.get("timestamp", "")),
        trigger=str(trigger),
        pre_tokens=pre_tokens_int,
    )


def parse_record(
    line: str,
    session_id: str,
    *,
    full_content: bool = False,
) -> SessionRecord | CompactEvent | None:
    """Parse a single JSONL line.

    Returns:
        SessionRecord for user, assistant, summary types.
        CompactEvent for system type with subtype=compact_boundary.
        None for malformed JSON, unknown types, or records without a type field.

    When ``full_content`` is True, message records carry the full searchable
    text (thinking + tool I/O) instead of the clean conversation view.

    Never raises exceptions.
    """
    try:
        data = json.loads(line)

        if not isinstance(data, dict):
            return None

        record_type = data.get("type")
        if not isinstance(record_type, str):
            return None

        if record_type in ("user", "assistant"):
            return _parse_message_record(
                data, record_type, session_id, full_content=full_content
            )

        if record_type == "summary":
            return _parse_summary_record(data, session_id)

        if record_type == "system":
            subtype = data.get("subtype")
            if subtype == "compact_boundary":
                return _parse_compact_boundary(data, session_id)
            return None

        # Unknown type (progress, queue-operation, file-history-snapshot, etc.)
        return None
    except json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError:
        # json.JSONDecodeError: malformed JSON
        # ValueError: could arise from type conversions
        # TypeError: type mismatches in nested structures
        # KeyError: should not happen (we use .get()), but defensive
        # AttributeError: method calls on unexpected types
        return None


def parse_session(
    lines: Iterable[str],
    session_id: str,
    *,
    full_content: bool = False,
) -> Iterator[SessionRecord | CompactEvent]:
    """Parse an iterable of JSONL lines, yielding valid records.

    Skips lines that produce None (malformed or unknown types).
    This is a generator -- no memory accumulation.

    The caller is responsible for file I/O (opening the file, reading lines).
    This function is a pure transformation over an iterable. When
    ``full_content`` is True, records carry the full searchable text.
    """
    for line in lines:
        result = parse_record(line, session_id, full_content=full_content)
        if result is not None:
            yield result
