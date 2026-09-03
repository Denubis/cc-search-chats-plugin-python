"""Pure, fail-closed adapter for native Claude JSONL records."""

# pattern: Functional Core

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeIs

from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    MessageIdentity,
    NativeLocator,
    NativeMessage,
    PhysicalAlias,
    Provider,
    SessionEpochBoundary,
    SessionKind,
    SubmittedBy,
    is_unicode_scalar_text,
    repair_unstorable_text,
)
from cc_search_chats.providers.source_discovery import RecordEnvelope

if TYPE_CHECKING:
    from pathlib import Path


_SYSTEM_REMINDER_SPAN = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


class ClaudeDiagnosticCode(StrEnum):
    """Closed Claude parse and exclusion classifications."""

    MALFORMED_JSON = "malformed_json"
    INVALID_ENCODING = "invalid_encoding"
    MISSING_MESSAGE = "missing_message"
    NON_OBJECT_MESSAGE = "non_object_message"
    UNKNOWN_ROLE = "unknown_role"
    UNKNOWN_CONTENT_BLOCK = "unknown_content_block"
    UNKNOWN_CONVERSATION_RECORD = "unknown_conversation_record"
    EXCLUDED_THINKING = "excluded_thinking"
    EXCLUDED_REASONING = "excluded_reasoning"
    EXCLUDED_SYSTEM = "excluded_system"
    EXCLUDED_INJECTED = "excluded_injected"
    EXCLUDED_METADATA = "excluded_metadata"
    EXCLUDED_TOOL_RESULT = "excluded_tool_result"
    EXCLUDED_NON_TEXT_TOOL_RESULT = "excluded_non_text_tool_result"
    EXCLUDED_TOOL_METADATA = "excluded_tool_metadata"
    MISSING_MESSAGE_UUID = "missing_message_uuid"
    INVALID_COMPACT_BOUNDARY = "invalid_compact_boundary"
    DUPLICATE_COMPACT_BOUNDARY = "duplicate_compact_boundary"
    EMPTY_CONTENT = "empty_content"
    REPAIRED_UNICODE = "repaired_unicode"
    INVALID_UNICODE = "invalid_unicode"


@dataclass(frozen=True, slots=True)
class ClaudeDiagnostic:
    """One named outcome tied to a complete native record."""

    code: ClaudeDiagnosticCode
    detail: str
    record_ordinal: int
    source_line: int
    source_byte_offset: int


@dataclass(frozen=True, slots=True)
class ClaudeSessionContext:
    """Explicit provider context that is not encoded in one record."""

    source_session_id: str
    repository: str | None = None
    cwd: str | None = None

    def __post_init__(self) -> None:
        """Reject context that cannot construct provider identity."""
        if not self.source_session_id.strip() or any(
            delimiter in self.source_session_id for delimiter in (":", "\r", "\n")
        ):
            raise ValueError("source_session_id must be a nonempty locator-safe string")


@dataclass(frozen=True, slots=True)
class ClaudeParserState:
    """Immutable Claude state required to parse only an appended suffix."""

    next_conversation_epoch: int = 0
    seen_compaction_uuids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject persisted state that cannot identify the next epoch."""
        if (
            isinstance(self.next_conversation_epoch, bool)
            or not isinstance(self.next_conversation_epoch, int)
            or self.next_conversation_epoch < 0
        ):
            raise ValueError("next_conversation_epoch must be a nonnegative integer")
        if not isinstance(self.seen_compaction_uuids, tuple) or any(
            not isinstance(value, str)
            or not value.strip()
            or any(delimiter in value for delimiter in (":", "\r", "\n"))
            for value in self.seen_compaction_uuids
        ):
            raise ValueError("seen_compaction_uuids must contain locator-safe UUIDs")
        if len(set(self.seen_compaction_uuids)) != len(self.seen_compaction_uuids):
            raise ValueError("seen_compaction_uuids must be unique")


@dataclass(frozen=True, slots=True)
class ClaudeParseResult:
    """All retained and excluded outcomes for one bounded Claude source."""

    session_kind: SessionKind
    messages: tuple[NativeMessage, ...]
    boundaries: tuple[SessionEpochBoundary, ...]
    diagnostics: tuple[ClaudeDiagnostic, ...]
    next_state: ClaudeParserState


@dataclass(frozen=True, slots=True)
class _DecodedRecord:
    envelope: RecordEnvelope
    payload: dict[str, object]


_CLAUDE_METADATA_KEYSETS = {
    "progress": {
        frozenset(
            {
                "type",
                "agentId",
                "cwd",
                "data",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "slug",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentId",
                "cwd",
                "data",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "slug",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "cwd",
                "data",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "cwd",
                "data",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentId",
                "cwd",
                "data",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentId",
                "cwd",
                "data",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentName",
                "cwd",
                "data",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "teamName",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "cwd",
                "data",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "slug",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "cwd",
                "data",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "slug",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentName",
                "cwd",
                "data",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "slug",
                "teamName",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "cwd",
                "data",
                "gitBranch",
                "isSidechain",
                "parentToolUseID",
                "parentUuid",
                "sessionId",
                "teamName",
                "timestamp",
                "toolUseID",
                "userType",
                "uuid",
                "version",
            }
        ),
    },
    "attachment": {
        frozenset(
            {
                "type",
                "agentId",
                "attachment",
                "cwd",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentUuid",
                "sessionId",
                "timestamp",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentId",
                "attachment",
                "cwd",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentUuid",
                "sessionId",
                "slug",
                "timestamp",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentId",
                "attachment",
                "cwd",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentUuid",
                "sessionId",
                "sessionKind",
                "timestamp",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentId",
                "attachment",
                "cwd",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentUuid",
                "sessionId",
                "sessionKind",
                "slug",
                "timestamp",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "attachment",
                "cwd",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentUuid",
                "sessionId",
                "timestamp",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "attachment",
                "cwd",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentUuid",
                "sessionId",
                "sessionKind",
                "timestamp",
                "userType",
                "uuid",
                "version",
            }
        ),
        frozenset(
            {
                "type",
                "agentName",
                "attachment",
                "cwd",
                "entrypoint",
                "gitBranch",
                "isSidechain",
                "parentUuid",
                "sessionId",
                "teamName",
                "timestamp",
                "userType",
                "uuid",
                "version",
            }
        ),
    },
    "last-prompt": {
        frozenset({"type", "leafUuid", "sessionId"}),
        frozenset({"type", "lastPrompt", "sessionId"}),
    },
    "file-history-snapshot": {
        frozenset({"type", "isSnapshotUpdate", "messageId", "snapshot"})
    },
    "agent-setting": {frozenset({"type", "agentSetting", "sessionId"})},
    "permission-mode": {frozenset({"type", "permissionMode", "sessionId"})},
    "mode": {frozenset({"type", "mode", "sessionId"})},
    "custom-title": {frozenset({"type", "customTitle", "sessionId"})},
    "fork-context-ref": {
        frozenset(
            {"type", "agentId", "contextLength", "parentLastUuid", "parentSessionId"}
        )
    },
    "queue-operation": {
        frozenset({"type", "content", "operation", "sessionId", "timestamp"}),
        frozenset({"type", "content", "operation", "reason", "sessionId", "timestamp"}),
        frozenset({"type", "operation", "sessionId", "timestamp"}),
    },
    "cost-state": {
        frozenset(
            {
                "type",
                "hasUnknownModelCost",
                "modelUsage",
                "sessionId",
                "startTime",
                "totalAPIDuration",
                "totalAPIDurationWithoutRetries",
                "totalCostUSD",
                "totalDuration",
                "totalLinesAdded",
                "totalLinesRemoved",
                "totalToolDuration",
            }
        )
    },
    "started": {frozenset({"type", "agentId", "key"})},
    "ai-title": {frozenset({"type", "aiTitle", "sessionId"})},
    "atis-latch": {frozenset({"type", "atis", "sessionId"})},
    "agent-name": {frozenset({"type", "agentName", "sessionId"})},
    "result": {frozenset({"type", "agentId", "key", "result"})},
    "pr-link": {
        frozenset(
            {
                "type",
                "prNumber",
                "prRepository",
                "prUrl",
                "sessionId",
                "timestamp",
            }
        )
    },
    "worktree-state": {frozenset({"type", "sessionId", "worktreeSession"})},
    "bridge-session": {
        frozenset({"type", "bridgeSessionId", "lastSequenceNum", "sessionId"})
    },
}

_CLAUDE_METADATA_KEYSETS["progress"].add(
    frozenset(
        {
            "type",
            "cwd",
            "data",
            "gitBranch",
            "isSidechain",
            "parentToolUseID",
            "parentUuid",
            "sessionId",
            "slug",
            "teamName",
            "timestamp",
            "toolUseID",
            "userType",
            "uuid",
            "version",
        }
    )
)
_ATTACHMENT_BASE_KEYS = frozenset(
    {
        "type",
        "attachment",
        "cwd",
        "entrypoint",
        "gitBranch",
        "isSidechain",
        "parentUuid",
        "sessionId",
        "timestamp",
        "userType",
        "uuid",
        "version",
    }
)
_CLAUDE_METADATA_KEYSETS["attachment"].update(
    _ATTACHMENT_BASE_KEYS | frozenset(extras)
    for extras in (
        {"agentName", "session_id", "slug", "teamName"},
        {"agentName", "session_id", "teamName"},
        {"agentName", "slug", "teamName"},
        {"sessionKind", "session_id", "slug"},
        {"sessionKind", "session_id"},
        {"sessionKind", "slug"},
        {"session_id", "slug"},
        {"session_id"},
        {"slug"},
    )
)
_CLAUDE_METADATA_KEYSETS["agent-color"] = {
    frozenset({"type", "agentColor", "sessionId"})
}
_CLAUDE_METADATA_KEYSETS["file-history-delta"] = {
    frozenset(
        {
            "type",
            "backup",
            "messageId",
            "snapshotMessageId",
            "timestamp",
            "trackingPath",
        }
    )
}
_CLAUDE_METADATA_KEYSETS["bridge-session"].add(
    frozenset(
        {
            "type",
            "bridgeSessionId",
            "lastSequenceNum",
            "ownerAccountUuid",
            "ownerOrganizationUuid",
            "sessionId",
        }
    )
)
_CLAUDE_METADATA_KEYSETS["frame-link"] = {
    frozenset({"type", "frameUrl", "path", "sessionId", "timestamp", "title"})
}
_CLAUDE_METADATA_KEYSETS["last-prompt"].add(
    frozenset({"type", "lastPrompt", "leafUuid", "sessionId"})
)
_CLAUDE_METADATA_KEYSETS["relocated"] = {
    frozenset({"type", "relocatedCwd", "sessionId"})
}


def _is_known_metadata_record(payload: dict[str, object]) -> bool:
    record_type = payload.get("type")
    return isinstance(record_type, str) and frozenset(payload) in (
        _CLAUDE_METADATA_KEYSETS.get(record_type, set())
    )


def _is_legacy_flat_message_record(payload: dict[str, object]) -> bool:
    """Recognize the observed legacy visible-message projection exactly."""
    return (
        frozenset(payload) == {"role", "text", "ts", "uuid"}
        and payload.get("role") in {"user", "assistant"}
        and isinstance(payload.get("text"), str)
        and isinstance(payload.get("ts"), str)
        and _message_uuid(payload) is not None
    )


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Narrow an untrusted JSON value after checking its key contract."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _diagnostic(
    code: ClaudeDiagnosticCode, envelope: RecordEnvelope, detail: str
) -> ClaudeDiagnostic:
    """Attach a named Claude outcome to Task 2 source coordinates."""
    return ClaudeDiagnostic(
        code=code,
        detail=detail,
        record_ordinal=envelope.record_ordinal,
        source_line=envelope.source_line,
        source_byte_offset=envelope.source_byte_offset,
    )


def _decode_records(
    envelopes: tuple[RecordEnvelope, ...],
) -> tuple[tuple[_DecodedRecord, ...], tuple[ClaudeDiagnostic, ...]]:
    """Decode complete records without admitting non-object JSON."""
    records: list[_DecodedRecord] = []
    diagnostics: list[ClaudeDiagnostic] = []
    for envelope in envelopes:
        try:
            payload: object = json.loads(envelope.raw_bytes)
        except UnicodeDecodeError:
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.INVALID_ENCODING,
                    envelope,
                    "complete record is not valid UTF-8",
                )
            )
            continue
        except json.JSONDecodeError:
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.MALFORMED_JSON,
                    envelope,
                    "complete record is not valid JSON",
                )
            )
            continue
        if not _is_json_object(payload):
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD,
                    envelope,
                    "top-level JSON value is not an object",
                )
            )
            continue
        records.append(_DecodedRecord(envelope=envelope, payload=payload))
    return tuple(records), tuple(diagnostics)


def _path_kind(path: Path) -> SessionKind:
    """Classify only Claude's understood top-level and subagent layouts."""
    parts = path.parts
    if "subagents" in parts:
        index = parts.index("subagents")
        if index >= 1 and index == len(parts) - 2:
            return SessionKind.AGENT
        return SessionKind.UNKNOWN
    if 1 <= len(parts) <= 2:
        return SessionKind.PRIMARY
    return SessionKind.UNKNOWN


def _classify_session_kind(
    records: tuple[_DecodedRecord, ...], context: ClaudeSessionContext
) -> SessionKind:
    """Combine native path and origin facts, failing closed on contradiction."""
    path_kinds = {
        _path_kind(record.envelope.source_file_relative) for record in records
    }
    if len(path_kinds) != 1:
        return SessionKind.UNKNOWN
    kind = next(iter(path_kinds), SessionKind.UNKNOWN)
    for record in records:
        payload = record.payload
        if _is_known_metadata_record(payload):
            continue
        recorded_session_id = payload.get("sessionId")
        if "sessionId" in payload and (
            not isinstance(recorded_session_id, str)
            or not recorded_session_id
            or recorded_session_id != context.source_session_id
        ):
            return SessionKind.UNKNOWN
        sidechain = payload.get("isSidechain")
        if "isSidechain" in payload and not isinstance(sidechain, bool):
            return SessionKind.UNKNOWN
        agent_id = payload.get("agentId")
        if "agentId" in payload and (
            not isinstance(agent_id, str) or not agent_id.strip()
        ):
            return SessionKind.UNKNOWN
        has_agent_id = isinstance(agent_id, str) and bool(agent_id.strip())
        if kind is SessionKind.PRIMARY and (sidechain is True or has_agent_id):
            return SessionKind.UNKNOWN
        if kind is SessionKind.AGENT and sidechain is False:
            return SessionKind.UNKNOWN
    return kind


def _message_uuid(payload: dict[str, object]) -> str | None:
    """Return an opaque Claude UUID only when it is locator-safe."""
    value = payload.get("uuid")
    if not isinstance(value, str) or not value.strip():
        return None
    if any(delimiter in value for delimiter in (":", "\r", "\n")):
        return None
    return value


def _physical_alias(
    envelope: RecordEnvelope, source_session_id: str, message_uuid: str
) -> PhysicalAlias:
    """Construct the sole supported Claude physical locator."""
    locator = NativeLocator(
        provider=Provider.CLAUDE,
        source_session_id=source_session_id,
        key_kind=LocatorKeyKind.UUID,
        key=message_uuid,
    )
    return PhysicalAlias(
        locator=locator,
        source_file_relative=envelope.source_file_relative,
        record_ordinal=envelope.record_ordinal,
        source_line=envelope.source_line,
        source_byte_offset=envelope.source_byte_offset,
        raw_byte_length=envelope.raw_byte_length,
        source_digest=envelope.source_digest,
    )


def _stringify_tool_value(value: object) -> str:
    """Render a recognized tool value deterministically for lexical search."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_known_non_text_tool_item(value: object) -> bool:
    if not _is_json_object(value):
        return False
    if value.get("type") == "tool_reference":
        return frozenset(value) == {"type", "tool_name"}
    if value.get("type") != "image" or frozenset(value) != {"type", "source"}:
        return False
    source = value.get("source")
    return _is_json_object(source) and frozenset(source) == {
        "type",
        "media_type",
        "data",
    }


def _is_advisor_tool_result(value: object) -> bool:
    """Recognize only the observed non-searchable advisor result shapes."""
    if (
        not _is_json_object(value)
        or value.get("type") != "advisor_tool_result"
        or frozenset(value) != {"type", "tool_use_id", "content"}
        or not isinstance(value.get("tool_use_id"), str)
    ):
        return False
    content = value.get("content")
    if not _is_json_object(content):
        return False
    return (
        frozenset(content) == {"type", "text"}
        and content.get("type") == "advisor_result"
        and isinstance(content.get("text"), str)
    ) or (
        frozenset(content) == {"type", "error_code"}
        and content.get("type") == "advisor_tool_result_error"
        and isinstance(content.get("error_code"), str)
    )


def _without_system_reminders(value: str) -> tuple[str, bool]:
    """Remove every exact Claude system-reminder span from user prose."""
    cleaned, count = _SYSTEM_REMINDER_SPAN.subn("", value)
    return cleaned, count > 0


def _extract_string_content(
    content: str, envelope: RecordEnvelope, *, user_content: bool
) -> tuple[list[tuple[ContentClass, str]], list[ClaudeDiagnostic]]:
    """Classify a bare message string after ruled filtering and repair."""
    removed_injected = False
    if user_content:
        content, removed_injected = _without_system_reminders(content)
    if removed_injected and not content.strip():
        return [], [
            _diagnostic(
                ClaudeDiagnosticCode.EXCLUDED_INJECTED,
                envelope,
                "system-reminder content is deliberately non-searchable",
            )
        ]
    content, repaired = repair_unstorable_text(content)
    if not is_unicode_scalar_text(content):
        return [], [
            _diagnostic(
                ClaudeDiagnosticCode.INVALID_UNICODE,
                envelope,
                "message text contains a non-scalar Unicode value",
            )
        ]
    if not content:
        return [], [
            _diagnostic(
                ClaudeDiagnosticCode.EMPTY_CONTENT,
                envelope,
                "recognized message has empty string content",
            )
        ]
    diagnostics = (
        [
            _diagnostic(
                ClaudeDiagnosticCode.REPAIRED_UNICODE,
                envelope,
                "message text contained unstorable code points repaired with U+FFFD",
            )
        ]
        if repaired
        else []
    )
    return [(ContentClass.PROSE, content)], diagnostics


def _extract_text_block(
    block: dict[str, object],
    envelope: RecordEnvelope,
    *,
    user_content: bool,
) -> tuple[str | None, bool, bool, list[ClaudeDiagnostic]]:
    """Return one visible text block and its repair/filter state."""
    text = block.get("text")
    if not isinstance(text, str):
        return (
            None,
            False,
            False,
            [
                _diagnostic(
                    ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                    envelope,
                    "text block lacks string text",
                )
            ],
        )
    removed_injected = False
    if user_content:
        text, removed_injected = _without_system_reminders(text)
        if removed_injected and not text.strip():
            return None, False, True, []
    text, repaired = repair_unstorable_text(text)
    if not is_unicode_scalar_text(text):
        return (
            None,
            True,
            removed_injected,
            [
                _diagnostic(
                    ClaudeDiagnosticCode.INVALID_UNICODE,
                    envelope,
                    "text block contains a non-scalar Unicode value",
                )
            ],
        )
    diagnostics = (
        [
            _diagnostic(
                ClaudeDiagnosticCode.REPAIRED_UNICODE,
                envelope,
                "text block contained unstorable code points repaired with U+FFFD",
            )
        ]
        if repaired
        else []
    )
    return text, False, removed_injected, diagnostics


def _extract_tool_use_block(
    block: dict[str, object], envelope: RecordEnvelope
) -> tuple[list[tuple[ContentClass, str]], list[ClaudeDiagnostic]]:
    """Return searchable name/input rows for one Claude tool invocation."""
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return [], [
            _diagnostic(
                ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                envelope,
                "tool_use block lacks a nonempty name",
            )
        ]
    rendered_input = _stringify_tool_value(block["input"]) if "input" in block else None
    name, repaired_name = repair_unstorable_text(name)
    repaired_input = False
    if rendered_input is not None:
        rendered_input, repaired_input = repair_unstorable_text(rendered_input)
    if not is_unicode_scalar_text(name) or (
        rendered_input is not None and not is_unicode_scalar_text(rendered_input)
    ):
        return [], [
            _diagnostic(
                ClaudeDiagnosticCode.INVALID_UNICODE,
                envelope,
                "tool_use block contains a non-scalar Unicode value",
            )
        ]
    rows = [(ContentClass.TOOL_NAME, name)]
    if rendered_input is not None:
        rows.append((ContentClass.TOOL_INPUT, rendered_input))
    diagnostics = (
        [
            _diagnostic(
                ClaudeDiagnosticCode.REPAIRED_UNICODE,
                envelope,
                "tool_use block contained unstorable code points repaired with U+FFFD",
            )
        ]
        if repaired_name or repaired_input
        else []
    )
    return rows, diagnostics


def _excluded_content_block_diagnostic(
    block: dict[str, object], block_type: str, envelope: RecordEnvelope
) -> ClaudeDiagnostic:
    """Name a ruled exclusion or an unrecognized Claude content block."""
    if block_type == "tool_result":
        code = ClaudeDiagnosticCode.EXCLUDED_TOOL_RESULT
        detail = "tool result content is deliberately non-searchable"
    elif _is_advisor_tool_result(block):
        code = ClaudeDiagnosticCode.EXCLUDED_TOOL_METADATA
        detail = "advisor tool metadata is deliberately non-searchable"
    elif _is_known_non_text_tool_item(block):
        code = ClaudeDiagnosticCode.EXCLUDED_NON_TEXT_TOOL_RESULT
        detail = "non-text image content is deliberately non-searchable"
    elif (
        block_type == "fallback"
        and frozenset(block) == {"type", "from", "to"}
        and _is_json_object(block.get("from"))
        and _is_json_object(block.get("to"))
    ):
        code = ClaudeDiagnosticCode.EXCLUDED_TOOL_METADATA
        detail = "model fallback metadata is deliberately non-searchable"
    elif block_type == "server_tool_use" and frozenset(block) == {
        "type",
        "id",
        "input",
        "name",
    }:
        code = ClaudeDiagnosticCode.EXCLUDED_TOOL_METADATA
        detail = "server tool metadata is deliberately non-searchable"
    elif block_type == "thinking":
        code = ClaudeDiagnosticCode.EXCLUDED_THINKING
        detail = "thinking content is deliberately non-searchable"
    elif block_type == "reasoning":
        code = ClaudeDiagnosticCode.EXCLUDED_REASONING
        detail = "reasoning content is deliberately non-searchable"
    elif block_type in {"system", "developer", "injected"}:
        code = ClaudeDiagnosticCode.EXCLUDED_INJECTED
        detail = f"{block_type} content is deliberately non-searchable"
    else:
        code = ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK
        detail = f"unrecognized Claude content block: {block_type}"
    return _diagnostic(code, envelope, detail)


def _extract_content(
    content: object, envelope: RecordEnvelope, *, user_content: bool = False
) -> tuple[list[tuple[ContentClass, str]], list[ClaudeDiagnostic]]:
    """Classify searchable blocks and name every excluded/unknown shape."""
    if isinstance(content, str):
        return _extract_string_content(content, envelope, user_content=user_content)
    if not isinstance(content, list):
        return [], [
            _diagnostic(
                ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                envelope,
                "message content is neither a string nor a block list",
            )
        ]
    if not content:
        return [], [
            _diagnostic(
                ClaudeDiagnosticCode.EMPTY_CONTENT,
                envelope,
                "recognized message has an empty content block list",
            )
        ]

    rows: list[tuple[ContentClass, str]] = []
    prose: list[str] = []
    invalid_prose = False
    removed_injected = False
    diagnostics: list[ClaudeDiagnostic] = []
    for block in content:
        if not _is_json_object(block) or not isinstance(block.get("type"), str):
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                    envelope,
                    "content block is not a typed object",
                )
            )
            continue
        block_type = block["type"]
        if block_type == "text":
            text, invalid, removed, text_diagnostics = _extract_text_block(
                block, envelope, user_content=user_content
            )
            diagnostics.extend(text_diagnostics)
            invalid_prose = invalid_prose or invalid
            removed_injected = removed_injected or removed
            if text is not None:
                prose.append(text)
        elif block_type == "tool_use":
            tool_rows, tool_diagnostics = _extract_tool_use_block(block, envelope)
            rows.extend(tool_rows)
            diagnostics.extend(tool_diagnostics)
        else:
            diagnostics.append(
                _excluded_content_block_diagnostic(block, block_type, envelope)
            )
    if prose and not invalid_prose:
        if not any(prose):
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.EMPTY_CONTENT,
                    envelope,
                    "recognized message has empty typed prose",
                )
            )
        else:
            rows.insert(0, (ContentClass.PROSE, "\n".join(prose)))
    if removed_injected and not rows and not invalid_prose:
        diagnostics.append(
            _diagnostic(
                ClaudeDiagnosticCode.EXCLUDED_INJECTED,
                envelope,
                "system-reminder content is deliberately non-searchable",
            )
        )
    grouped: dict[ContentClass, list[str]] = {}
    for content_class, text in rows:
        grouped.setdefault(content_class, []).append(text)
    return [
        (content_class, "\n".join(texts)) for content_class, texts in grouped.items()
    ], diagnostics


def _native_message(
    *,
    alias: PhysicalAlias,
    payload: dict[str, object],
    role: str,
    session_kind: SessionKind,
    conversation_epoch: int,
    content_class: ContentClass,
    text: str,
    context: ClaudeSessionContext,
) -> NativeMessage:
    """Construct one common immutable message/content row."""
    identified_agent = (
        role == "user"
        and session_kind is SessionKind.AGENT
        and payload.get("isSidechain") is True
        and isinstance(payload.get("agentId"), str)
        and bool(str(payload["agentId"]).strip())
    )
    timestamp = payload.get("timestamp")
    cwd = payload.get("cwd")
    return NativeMessage(
        identity=MessageIdentity(
            logical_message_id=str(alias.locator.key),
            canonical_locator=alias.locator,
            physical_aliases=(alias,),
        ),
        timestamp=timestamp if isinstance(timestamp, str) else "",
        role=role,
        session_kind=session_kind,
        conversation_epoch=conversation_epoch,
        content_class=content_class,
        text=text,
        repository=context.repository,
        cwd=cwd if isinstance(cwd, str) else context.cwd,
        submitted_by=(
            SubmittedBy.IDENTIFIED_HARNESS if identified_agent else SubmittedBy.UNKNOWN
        ),
        submission_evidence=("claude:isSidechain+agentId",) if identified_agent else (),
        submission_match_cardinality=1 if identified_agent else 0,
    )


def _parse_boundary(
    record: _DecodedRecord,
    *,
    context: ClaudeSessionContext,
    session_kind: SessionKind,
    next_epoch: int,
) -> tuple[SessionEpochBoundary | None, ClaudeDiagnostic | None]:
    """Parse one allowlisted compact boundary without speculative coercion."""
    payload = record.payload
    metadata = payload.get("compactMetadata")
    if not _is_json_object(metadata):
        return None, _diagnostic(
            ClaudeDiagnosticCode.INVALID_COMPACT_BOUNDARY,
            record.envelope,
            "compact_boundary lacks object compactMetadata",
        )
    trigger = metadata.get("trigger")
    token_count = metadata.get("preTokens")
    message_uuid = _message_uuid(payload)
    if (
        not isinstance(trigger, str)
        or not trigger
        or isinstance(token_count, bool)
        or not isinstance(token_count, int)
        or token_count < 0
        or message_uuid is None
    ):
        return None, _diagnostic(
            ClaudeDiagnosticCode.INVALID_COMPACT_BOUNDARY,
            record.envelope,
            "compact_boundary metadata or UUID is not canonical",
        )
    timestamp = payload.get("timestamp")
    alias = _physical_alias(record.envelope, context.source_session_id, message_uuid)
    return (
        SessionEpochBoundary(
            provider=Provider.CLAUDE,
            source_session_id=context.source_session_id,
            session_kind=session_kind,
            conversation_epoch=next_epoch,
            physical_alias=alias,
            timestamp=timestamp if isinstance(timestamp, str) else "",
            trigger=trigger,
            token_count=token_count,
        ),
        None,
    )


def parse_claude_session(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    context: ClaudeSessionContext,
    prior_state: ClaudeParserState | None = None,
) -> ClaudeParseResult:
    """Adapt bounded Claude envelopes into searchable and diagnostic outcomes."""
    if prior_state is not None and not isinstance(prior_state, ClaudeParserState):
        raise TypeError("prior_state must be ClaudeParserState or None")
    state = prior_state if prior_state is not None else ClaudeParserState()
    records, decode_diagnostics = _decode_records(tuple(envelopes))
    session_kind = _classify_session_kind(records, context)
    messages: list[NativeMessage] = []
    boundaries: list[SessionEpochBoundary] = []
    diagnostics = list(decode_diagnostics)
    conversation_epoch = state.next_conversation_epoch
    seen_compaction_uuids = set(state.seen_compaction_uuids)

    for record in records:
        payload = record.payload
        record_type = payload.get("type")
        if _is_known_metadata_record(payload):
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.EXCLUDED_METADATA,
                    record.envelope,
                    f"{record_type} metadata is deliberately non-searchable",
                )
            )
            continue
        if _is_legacy_flat_message_record(payload):
            content_rows, content_diagnostics = _extract_content(
                payload["text"],
                record.envelope,
                user_content=payload.get("role") == "user",
            )
            diagnostics.extend(content_diagnostics)
            message_uuid = _message_uuid(payload)
            if message_uuid is None:
                raise AssertionError("validated legacy UUID became unavailable")
            role = payload.get("role")
            if not isinstance(role, str):
                raise AssertionError("validated legacy role became unavailable")
            alias = _physical_alias(
                record.envelope, context.source_session_id, message_uuid
            )
            normalized_payload = {"timestamp": payload["ts"]}
            messages.extend(
                _native_message(
                    alias=alias,
                    payload=normalized_payload,
                    role=role,
                    session_kind=session_kind,
                    conversation_epoch=conversation_epoch,
                    content_class=content_class,
                    text=text,
                    context=context,
                )
                for content_class, text in content_rows
            )
            continue
        if record_type == "system":
            if payload.get("subtype") == "compact_boundary":
                boundary, diagnostic = _parse_boundary(
                    record,
                    context=context,
                    session_kind=session_kind,
                    next_epoch=conversation_epoch + 1,
                )
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                elif boundary is not None:
                    boundary_uuid = str(boundary.physical_alias.locator.key)
                    if boundary_uuid in seen_compaction_uuids:
                        diagnostics.append(
                            _diagnostic(
                                ClaudeDiagnosticCode.DUPLICATE_COMPACT_BOUNDARY,
                                record.envelope,
                                "duplicate compact_boundary does not advance the epoch",
                            )
                        )
                    else:
                        seen_compaction_uuids.add(boundary_uuid)
                        conversation_epoch += 1
                        boundaries.append(boundary)
            else:
                diagnostics.append(
                    _diagnostic(
                        ClaudeDiagnosticCode.EXCLUDED_SYSTEM,
                        record.envelope,
                        "system record is deliberately non-searchable",
                    )
                )
            continue
        if record_type not in {"user", "assistant"}:
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD,
                    record.envelope,
                    f"unrecognized Claude record type: {record_type!r}",
                )
            )
            continue
        if payload.get("isMeta") is True:
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.EXCLUDED_INJECTED,
                    record.envelope,
                    "meta/injected user material is deliberately non-searchable",
                )
            )
            continue
        if "message" not in payload:
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.MISSING_MESSAGE,
                    record.envelope,
                    "conversation record has no message field",
                )
            )
            continue
        message = payload["message"]
        if not _is_json_object(message):
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.NON_OBJECT_MESSAGE,
                    record.envelope,
                    "message field is not an object",
                )
            )
            continue
        role = message.get("role")
        if (
            not isinstance(role, str)
            or role not in {"user", "assistant"}
            or role != record_type
        ):
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.UNKNOWN_ROLE,
                    record.envelope,
                    f"unrecognized or contradictory message role: {role!r}",
                )
            )
            continue
        message_uuid = _message_uuid(payload)
        if message_uuid is None:
            diagnostics.append(
                _diagnostic(
                    ClaudeDiagnosticCode.MISSING_MESSAGE_UUID,
                    record.envelope,
                    "Claude conversation record lacks a locator-safe UUID",
                )
            )
            continue
        content_rows, content_diagnostics = _extract_content(
            message.get("content"), record.envelope, user_content=role == "user"
        )
        diagnostics.extend(content_diagnostics)
        alias = _physical_alias(
            record.envelope, context.source_session_id, message_uuid
        )
        messages.extend(
            _native_message(
                alias=alias,
                payload=payload,
                role=role,
                session_kind=session_kind,
                conversation_epoch=conversation_epoch,
                content_class=content_class,
                text=text,
                context=context,
            )
            for content_class, text in content_rows
        )

    return ClaudeParseResult(
        session_kind=session_kind,
        messages=tuple(messages),
        boundaries=tuple(boundaries),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda value: (
                    value.record_ordinal,
                    value.code.value,
                    value.detail,
                ),
            )
        ),
        next_state=ClaudeParserState(
            next_conversation_epoch=conversation_epoch,
            seen_compaction_uuids=tuple(sorted(seen_compaction_uuids)),
        ),
    )
