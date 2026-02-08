"""Core data models for parsed JSONL records.

These are the Functional Core data types -- no methods with side effects.
All fields use basic types (str, int, float, None) to keep models
serialisation-friendly and pure.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One parsed JSONL line representing a message or summary."""

    record_type: str
    """'user', 'assistant', 'summary', 'compact_boundary', or 'unknown'."""

    uuid: str

    parent_uuid: str | None

    timestamp: str
    """ISO 8601 string, kept as-is from JSONL (not parsed to datetime)."""

    session_id: str

    role: str | None
    """'user' or 'assistant'; None for non-message records."""

    text_content: str
    """Extracted plaintext. Empty string if no text content."""

    leaf_uuid: str | None
    """Only populated for summary records (from JSONL leafUuid field)."""


@dataclass(frozen=True, slots=True)
class CompactEvent:
    """A compression boundary event marking an epoch transition."""

    uuid: str

    session_id: str

    timestamp: str

    trigger: str
    """'auto' or 'manual'."""

    pre_tokens: int


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """File-level metadata for session discovery."""

    session_id: str

    file_path: str
    """Absolute path to the JSONL file."""

    project_path: str
    """Original project directory path."""

    file_size: int
    """Bytes."""

    modified_at: float
    """mtime as Unix timestamp."""
