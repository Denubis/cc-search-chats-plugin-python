"""Pure, fail-closed adapter for native Codex JSONL records."""

# pattern: Functional Core

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeIs

from cc_search_chats.core.canonicalization import (
    CanonicalizationDiagnostic,
    CanonicalizationDiagnosticCode,
    CanonicalizationResult,
    CodexRecordFamily,
    PhysicalMessageCandidate,
    canonicalize_codex_candidates,
    codex_logical_message_id,
    is_valid_native_timestamp,
)
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
)
from cc_search_chats.providers.source_discovery import (
    RecordEnvelope,
    SourceDiagnostic,
    SourceDiagnosticCode,
)

_PRIMARY_SOURCES = {"cli", "exec", "mcp", "vscode"}
_LEGACY_SUBAGENT_ROLES = {"review"}
_TURN_CONTEXT_REQUIRED_FIELDS = {
    "approval_policy",
    "cwd",
    "model",
    "sandbox_policy",
    "summary",
}
_TURN_CONTEXT_STRING_FIELDS = {
    "approval_policy",
    "approvals_reviewer",
    "comp_hash",
    "current_date",
    "cwd",
    "effort",
    "model",
    "multi_agent_mode",
    "multi_agent_version",
    "personality",
    "summary",
    "timezone",
    "turn_id",
    "user_instructions",
}
_TURN_CONTEXT_OBJECT_FIELDS = {
    "collaboration_mode",
    "file_system_sandbox_policy",
    "permission_profile",
    "sandbox_policy",
    "truncation_policy",
}
_TURN_CONTEXT_BOOLEAN_FIELDS = {"realtime_active"}
_TURN_CONTEXT_LIST_FIELDS = {"workspace_roots"}
_TURN_CONTEXT_FIELDS = (
    _TURN_CONTEXT_STRING_FIELDS
    | _TURN_CONTEXT_OBJECT_FIELDS
    | _TURN_CONTEXT_BOOLEAN_FIELDS
    | _TURN_CONTEXT_LIST_FIELDS
)


class CodexDiagnosticCode(StrEnum):
    """Closed Codex parse and exclusion classifications."""

    MALFORMED_JSON = "malformed_json"
    UNSUPPORTED_SOURCE_SHAPE = "unsupported_source_shape"
    EXCLUDED_DEVELOPER = "excluded_developer"
    UNKNOWN_ROLE = "unknown_role"
    UNKNOWN_CONTENT_BLOCK = "unknown_content_block"
    EXCLUDED_REASONING = "excluded_reasoning"
    UNKNOWN_RESPONSE_ITEM = "unknown_response_item"
    UNKNOWN_EVENT = "unknown_event"
    UNKNOWN_OUTER_TYPE = "unknown_outer_type"
    UNKNOWN_COMPACTION = "unknown_compaction"
    DUPLICATE_COMPACTION = "duplicate_compaction"
    EXCLUDED_INTER_AGENT_METADATA = "excluded_inter_agent_metadata"
    PARTIAL_TAIL = "partial_tail"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_UNICODE = "invalid_unicode"
    EMPTY_CONTENT = "empty_content"
    UNKNOWN_METADATA_SHAPE = "unknown_metadata_shape"
    UNSUPPORTED_SESSION_IDENTITY = "unsupported_session_identity"


@dataclass(frozen=True, slots=True)
class CodexDiagnostic:
    """One named Codex outcome with optional physical coordinates."""

    code: CodexDiagnosticCode
    detail: str
    record_ordinal: int | None
    source_line: int | None
    source_byte_offset: int | None


@dataclass(frozen=True, slots=True)
class CodexSessionContext:
    """Caller context plus an optional expected native session identity.

    ``source_session_id`` is retained for compatibility with Phase 1 callers,
    but it is validation evidence only. Native metadata or prior parser state
    owns the identity used to construct locators.
    """

    source_session_id: str | None = None
    repository: str | None = None
    cwd: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed expected identities at the caller boundary."""
        if (
            self.source_session_id is not None
            and _locator_safe_id(self.source_session_id) is None
        ):
            raise ValueError("source_session_id must be a nonempty locator-safe string")


@dataclass(frozen=True, slots=True)
class CodexParserState:
    """Immutable Codex state required to parse only an appended suffix."""

    next_conversation_epoch: int = 0
    session_kind: SessionKind | None = None
    seen_compaction_digests: tuple[str, ...] = ()
    trailing_candidate: PhysicalMessageCandidate | None = None
    source_session_id: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed or cross-epoch persisted parser state."""
        if (
            isinstance(self.next_conversation_epoch, bool)
            or not isinstance(self.next_conversation_epoch, int)
            or self.next_conversation_epoch < 0
        ):
            raise ValueError("next_conversation_epoch must be a nonnegative integer")
        if self.session_kind is not None and not isinstance(
            self.session_kind, SessionKind
        ):
            raise ValueError("session_kind must be a SessionKind or None")
        if (
            self.source_session_id is not None
            and _locator_safe_id(self.source_session_id) is None
        ):
            raise ValueError(
                "source_session_id must be a nonempty locator-safe string or None"
            )
        if (self.session_kind is None) != (self.source_session_id is None):
            raise ValueError(
                "source_session_id and session_kind must be established together"
            )
        if not isinstance(self.seen_compaction_digests, tuple) or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or not set(digest) <= set("0123456789abcdef")
            for digest in self.seen_compaction_digests
        ):
            raise ValueError("seen_compaction_digests must contain SHA-256 hex strings")
        if len(set(self.seen_compaction_digests)) != len(self.seen_compaction_digests):
            raise ValueError("seen_compaction_digests must be unique")
        candidate = self.trailing_candidate
        if candidate is None:
            return
        if not isinstance(candidate, PhysicalMessageCandidate):
            raise ValueError("trailing_candidate must be a physical message candidate")
        message = candidate.message
        if (
            candidate.record_family
            not in {
                CodexRecordFamily.RESPONSE_MESSAGE,
                CodexRecordFamily.EVENT_MESSAGE,
            }
            or message.content_class is not ContentClass.PROSE
            or message.conversation_epoch != self.next_conversation_epoch
            or len(message.identity.physical_aliases) != 1
            or message.identity.canonical_locator.provider is not Provider.CODEX
            or self.session_kind is None
            or message.identity.canonical_locator.source_session_id
            != self.source_session_id
            or message.session_kind is not self.session_kind
        ):
            raise ValueError(
                "trailing_candidate must be one unpaired Codex prose projection "
                "in the continuation epoch and session kind"
            )


@dataclass(frozen=True, slots=True)
class CodexParseResult:
    """Canonical messages plus retained physical and diagnostic outcomes."""

    source_session_id: str | None
    session_kind: SessionKind
    messages: tuple[NativeMessage, ...]
    physical_candidates: tuple[PhysicalMessageCandidate, ...]
    boundaries: tuple[SessionEpochBoundary, ...]
    diagnostics: tuple[CodexDiagnostic, ...]
    canonicalization_diagnostics: tuple[CanonicalizationDiagnostic, ...]
    next_state: CodexParserState


@dataclass(frozen=True, slots=True)
class _DecodedRecord:
    envelope: RecordEnvelope
    payload: dict[str, object]


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Narrow an untrusted JSON value after checking its key contract."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _diagnostic(
    code: CodexDiagnosticCode, envelope: RecordEnvelope, detail: str
) -> CodexDiagnostic:
    """Attach a named Codex outcome to Task 2 source coordinates."""
    return CodexDiagnostic(
        code=code,
        detail=detail,
        record_ordinal=envelope.record_ordinal,
        source_line=envelope.source_line,
        source_byte_offset=envelope.source_byte_offset,
    )


def _decode_records(
    envelopes: tuple[RecordEnvelope, ...],
) -> tuple[tuple[_DecodedRecord, ...], tuple[CodexDiagnostic, ...]]:
    """Decode complete records without admitting non-object JSON."""
    records: list[_DecodedRecord] = []
    diagnostics: list[CodexDiagnostic] = []
    for envelope in envelopes:
        try:
            payload: object = json.loads(envelope.raw_bytes)
        except json.JSONDecodeError, UnicodeDecodeError:
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.MALFORMED_JSON,
                    envelope,
                    "complete record is not valid JSON",
                )
            )
            continue
        if not _is_json_object(payload):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
                    envelope,
                    "top-level JSON value is not an object",
                )
            )
            continue
        records.append(_DecodedRecord(envelope=envelope, payload=payload))
    return tuple(records), tuple(diagnostics)


def _modern_subagent(source: object) -> bool:
    """Recognize the modern source.subagent.thread_spawn lineage shape."""
    if not _is_json_object(source):
        return False
    subagent = source.get("subagent")
    if not _is_json_object(subagent):
        return False
    spawn = subagent.get("thread_spawn")
    if not _is_json_object(spawn):
        return False
    parent = spawn.get("parent_thread_id")
    depth = spawn.get("depth")
    return (
        isinstance(parent, str)
        and bool(parent)
        and not isinstance(depth, bool)
        and isinstance(depth, int)
        and depth >= 1
    )


def _legacy_subagent(source: object) -> bool:
    """Recognize the legacy source.subagent role string."""
    if not _is_json_object(source):
        return False
    subagent = source.get("subagent")
    return isinstance(subagent, str) and subagent in _LEGACY_SUBAGENT_ROLES


def _source_kind(source: object) -> SessionKind | None:
    """Classify only audited primary and child source shapes."""
    if isinstance(source, str) and source in _PRIMARY_SOURCES:
        return SessionKind.PRIMARY
    if _modern_subagent(source) or _legacy_subagent(source):
        return SessionKind.AGENT
    return None


def _session_kind(
    records: tuple[_DecodedRecord, ...],
) -> tuple[SessionKind, tuple[CodexDiagnostic, ...]]:
    """Classify all session_meta sources independently of their identity."""
    kinds: list[SessionKind] = []
    diagnostics: list[CodexDiagnostic] = []
    for record in records:
        if record.payload.get("type") != "session_meta":
            continue
        meta = record.payload.get("payload")
        if not _is_json_object(meta):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
                    record.envelope,
                    "session_meta payload is not an object",
                )
            )
            continue
        kind = _source_kind(meta.get("source"))
        if kind is None:
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
                    record.envelope,
                    "session_meta source is unsupported",
                )
            )
            continue
        kinds.append(kind)
    if not kinds or len(set(kinds)) != 1 or diagnostics:
        return SessionKind.UNKNOWN, tuple(diagnostics)
    return kinds[0], tuple(diagnostics)


def _continued_session_kind(
    records: tuple[_DecodedRecord, ...],
    established: SessionKind | None,
) -> tuple[SessionKind, tuple[CodexDiagnostic, ...]]:
    """Use an established kind when a suffix omits session metadata."""
    if established is None:
        return _session_kind(records)
    metadata = tuple(
        record for record in records if record.payload.get("type") == "session_meta"
    )
    if not metadata:
        return established, ()
    observed, diagnostics = _session_kind(metadata)
    if observed is established and not diagnostics:
        return established, ()
    if diagnostics:
        return SessionKind.UNKNOWN, diagnostics
    return SessionKind.UNKNOWN, (
        _diagnostic(
            CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
            metadata[0].envelope,
            "session_meta changes the established continuation session kind",
        ),
    )


def _session_identity_diagnostic(
    records: tuple[_DecodedRecord, ...], detail: str
) -> CodexDiagnostic:
    """Build an identity diagnostic even when no record decoded successfully."""
    if records:
        return _diagnostic(
            CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
            records[0].envelope,
            detail,
        )
    return CodexDiagnostic(
        code=CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
        detail=detail,
        record_ordinal=None,
        source_line=None,
        source_byte_offset=None,
    )


def _metadata_session_identity(
    records: tuple[_DecodedRecord, ...],
) -> tuple[str | None, bool, tuple[CodexDiagnostic, ...]]:
    """Read one exact identity from every present Codex session_meta record."""
    metadata = tuple(
        record for record in records if record.payload.get("type") == "session_meta"
    )
    if not metadata:
        return None, False, ()

    observed: list[tuple[str, _DecodedRecord]] = []
    diagnostics: list[CodexDiagnostic] = []
    for record in metadata:
        payload = record.payload.get("payload")
        if not _is_json_object(payload):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
                    record.envelope,
                    "session_meta payload cannot establish a native session identity",
                )
            )
            continue
        present = tuple(payload[key] for key in ("id", "session_id") if key in payload)
        validated = tuple(_locator_safe_id(value) for value in present)
        if (
            not present
            or any(value is None for value in validated)
            or len(set(validated)) != 1
        ):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
                    record.envelope,
                    "session_meta id/session_id is missing, malformed, or conflicting",
                )
            )
            continue
        observed.append(
            (next(value for value in validated if value is not None), record)
        )

    if diagnostics:
        return None, True, tuple(diagnostics)
    identities = {identity for identity, _record in observed}
    if len(identities) != 1:
        return (
            None,
            True,
            tuple(
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
                    record.envelope,
                    "session_meta records disagree on native session identity",
                )
                for _identity, record in observed
            ),
        )
    return observed[0][0], True, ()


def _resolved_session_identity(
    records: tuple[_DecodedRecord, ...],
    context: CodexSessionContext,
    established: str | None,
) -> tuple[str | None, tuple[CodexDiagnostic, ...]]:
    """Resolve identity only from native metadata or immutable prior state."""
    observed, metadata_present, diagnostics = _metadata_session_identity(records)
    if diagnostics:
        return None, diagnostics

    if established is None:
        if not metadata_present or observed is None:
            return None, (
                _session_identity_diagnostic(
                    records,
                    "fresh Codex input lacks session_meta native identity",
                ),
            )
        resolved = observed
    else:
        if metadata_present and observed != established:
            return None, (
                _session_identity_diagnostic(
                    records,
                    "session_meta changes the established native session identity",
                ),
            )
        resolved = established

    expected = context.source_session_id
    if expected is not None and expected != resolved:
        return None, (
            _session_identity_diagnostic(
                records,
                "caller expected session identity does not match native parser state",
            ),
        )
    return resolved, ()


def _locator_safe_id(value: object) -> str | None:
    """Return an opaque native ID only when it is locator-safe."""
    if not isinstance(value, str) or not value.strip():
        return None
    if any(delimiter in value for delimiter in (":", "\r", "\n")):
        return None
    return value


def _physical_alias(
    envelope: RecordEnvelope,
    source_session_id: str,
    native_id: str | None = None,
) -> PhysicalAlias:
    """Construct an ID locator or the exact ordinal/digest fallback."""
    if native_id is None:
        locator = NativeLocator(
            provider=Provider.CODEX,
            source_session_id=source_session_id,
            key_kind=LocatorKeyKind.ORDINAL,
            key=envelope.record_ordinal,
            record_digest=envelope.source_digest,
        )
    else:
        locator = NativeLocator(
            provider=Provider.CODEX,
            source_session_id=source_session_id,
            key_kind=LocatorKeyKind.ID,
            key=native_id,
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


def _native_message(
    *,
    record: _DecodedRecord,
    context: CodexSessionContext,
    source_session_id: str,
    session_kind: SessionKind,
    epoch: int,
    role: str,
    content_class: ContentClass,
    text: str,
    family: CodexRecordFamily,
    native_id: str | None = None,
) -> PhysicalMessageCandidate:
    """Construct one common physical message/content candidate."""
    alias = _physical_alias(record.envelope, source_session_id, native_id)
    identified_agent = role == "user" and session_kind is SessionKind.AGENT
    timestamp = record.payload.get("timestamp")
    return PhysicalMessageCandidate(
        message=NativeMessage(
            identity=MessageIdentity(
                logical_message_id=codex_logical_message_id(alias),
                canonical_locator=alias.locator,
                physical_aliases=(alias,),
            ),
            timestamp=timestamp if isinstance(timestamp, str) else "",
            role=role,
            session_kind=session_kind,
            conversation_epoch=epoch,
            content_class=content_class,
            text=text,
            repository=context.repository,
            cwd=context.cwd,
            submitted_by=(
                SubmittedBy.IDENTIFIED_HARNESS
                if identified_agent
                else SubmittedBy.UNKNOWN
            ),
            submission_evidence=("codex:session_meta.source.subagent",)
            if identified_agent
            else (),
            submission_match_cardinality=1 if identified_agent else 0,
        ),
        record_family=family,
    )


def _message_content(
    content: object,
    role: str,
    envelope: RecordEnvelope,
) -> tuple[str | None, tuple[CodexDiagnostic, ...]]:
    """Extract only role-compatible visible Codex message blocks."""
    if not isinstance(content, list):
        return None, (
            _diagnostic(
                CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                envelope,
                "message content is not a block list",
            ),
        )
    if not content:
        return None, (
            _diagnostic(
                CodexDiagnosticCode.EMPTY_CONTENT,
                envelope,
                "recognized message has an empty content block list",
            ),
        )
    expected = "input_text" if role == "user" else "output_text"
    text_parts: list[str] = []
    diagnostics: list[CodexDiagnostic] = []
    invalid_unicode = False
    for block in content:
        if not _is_json_object(block):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                    envelope,
                    "message content block is not an object",
                )
            )
            continue
        block_type = block.get("type")
        if block_type in {"reasoning", "summary_text"}:
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.EXCLUDED_REASONING,
                    envelope,
                    "reasoning content is deliberately non-searchable",
                )
            )
            continue
        text = block.get("text")
        if block_type != expected or not isinstance(text, str):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                    envelope,
                    f"unrecognized or role-incompatible message block: {block_type!r}",
                )
            )
            continue
        if not is_unicode_scalar_text(text):
            invalid_unicode = True
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.INVALID_UNICODE,
                    envelope,
                    "message text contains a non-scalar Unicode value",
                )
            )
            continue
        text_parts.append(text)
    if invalid_unicode:
        return None, tuple(diagnostics)
    if not text_parts:
        return None, tuple(diagnostics)
    if not any(text_parts):
        diagnostics.append(
            _diagnostic(
                CodexDiagnosticCode.EMPTY_CONTENT,
                envelope,
                "recognized message has empty typed prose",
            )
        )
        return None, tuple(diagnostics)
    return "\n".join(text_parts), tuple(diagnostics)


def _tool_text(value: object) -> str | None:
    """Render recognized tool values without flattening unknown objects."""
    if isinstance(value, str):
        return value
    if value is None:
        return None
    if _is_json_object(value) or isinstance(value, list):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return None


def _parse_response_item(
    record: _DecodedRecord,
    *,
    context: CodexSessionContext,
    source_session_id: str,
    session_kind: SessionKind,
    epoch: int,
) -> tuple[tuple[PhysicalMessageCandidate, ...], tuple[CodexDiagnostic, ...]]:
    """Parse one allowlisted response_item capability shape."""
    payload = record.payload.get("payload")
    if not _is_json_object(payload):
        return (), (
            _diagnostic(
                CodexDiagnosticCode.INVALID_PAYLOAD,
                record.envelope,
                "response_item payload is not an object",
            ),
        )
    item_type = payload.get("type")
    if item_type == "reasoning":
        return (), (
            _diagnostic(
                CodexDiagnosticCode.EXCLUDED_REASONING,
                record.envelope,
                "reasoning item is deliberately non-searchable",
            ),
        )
    if item_type == "message":
        role = payload.get("role")
        if role == "developer":
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.EXCLUDED_DEVELOPER,
                    record.envelope,
                    "developer message is deliberately non-searchable",
                ),
            )
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_ROLE,
                    record.envelope,
                    f"unrecognized Codex message role: {role!r}",
                ),
            )
        text, diagnostics = _message_content(
            payload.get("content"), role, record.envelope
        )
        if text is None:
            return (), diagnostics
        native_id = _locator_safe_id(payload.get("id"))
        return (
            (
                _native_message(
                    record=record,
                    context=context,
                    source_session_id=source_session_id,
                    session_kind=session_kind,
                    epoch=epoch,
                    role=role,
                    content_class=ContentClass.PROSE,
                    text=text,
                    family=CodexRecordFamily.RESPONSE_MESSAGE,
                    native_id=native_id,
                ),
            ),
            diagnostics,
        )
    if item_type in {"function_call", "custom_tool_call"}:
        name = payload.get("name")
        tool_input = _tool_text(
            payload.get("arguments")
            if item_type == "function_call"
            else payload.get("input")
        )
        if (isinstance(name, str) and not is_unicode_scalar_text(name)) or (
            tool_input is not None and not is_unicode_scalar_text(tool_input)
        ):
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.INVALID_UNICODE,
                    record.envelope,
                    "tool call contains a non-scalar Unicode value",
                ),
            )
        if not isinstance(name, str) or not name or tool_input is None:
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
                    record.envelope,
                    "tool call lacks a recognized name/input shape",
                ),
            )
        return (
            (
                _native_message(
                    record=record,
                    context=context,
                    source_session_id=source_session_id,
                    session_kind=session_kind,
                    epoch=epoch,
                    role="assistant",
                    content_class=ContentClass.TOOL_NAME,
                    text=name,
                    family=CodexRecordFamily.TOOL,
                ),
                _native_message(
                    record=record,
                    context=context,
                    source_session_id=source_session_id,
                    session_kind=session_kind,
                    epoch=epoch,
                    role="assistant",
                    content_class=ContentClass.TOOL_INPUT,
                    text=tool_input,
                    family=CodexRecordFamily.TOOL,
                ),
            ),
            (),
        )
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        output = _tool_text(payload.get("output"))
        if output is not None and not is_unicode_scalar_text(output):
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.INVALID_UNICODE,
                    record.envelope,
                    "tool output contains a non-scalar Unicode value",
                ),
            )
        if output is None:
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
                    record.envelope,
                    "tool output lacks a recognized output shape",
                ),
            )
        return (
            (
                _native_message(
                    record=record,
                    context=context,
                    source_session_id=source_session_id,
                    session_kind=session_kind,
                    epoch=epoch,
                    role="tool",
                    content_class=ContentClass.TOOL_OUTPUT,
                    text=output,
                    family=CodexRecordFamily.TOOL,
                ),
            ),
            (),
        )
    return (), (
        _diagnostic(
            CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
            record.envelope,
            f"unrecognized response_item payload type: {item_type!r}",
        ),
    )


def _parse_event(
    record: _DecodedRecord,
    *,
    context: CodexSessionContext,
    source_session_id: str,
    session_kind: SessionKind,
    epoch: int,
) -> tuple[PhysicalMessageCandidate | None, CodexDiagnostic | None]:
    """Parse only visible user_message and agent_message event projections."""
    payload = record.payload.get("payload")
    if not _is_json_object(payload):
        return None, _diagnostic(
            CodexDiagnosticCode.INVALID_PAYLOAD,
            record.envelope,
            "event_msg payload is not an object",
        )
    event_type = payload.get("type")
    role = {"user_message": "user", "agent_message": "assistant"}.get(event_type)
    message = payload.get("message")
    if role is None or not isinstance(message, str):
        return None, _diagnostic(
            CodexDiagnosticCode.UNKNOWN_EVENT,
            record.envelope,
            f"unrecognized event_msg payload type: {event_type!r}",
        )
    if not is_unicode_scalar_text(message):
        return None, _diagnostic(
            CodexDiagnosticCode.INVALID_UNICODE,
            record.envelope,
            "event message contains a non-scalar Unicode value",
        )
    if not message:
        return None, _diagnostic(
            CodexDiagnosticCode.EMPTY_CONTENT,
            record.envelope,
            "recognized event message has empty content",
        )
    return (
        _native_message(
            record=record,
            context=context,
            source_session_id=source_session_id,
            session_kind=session_kind,
            epoch=epoch,
            role=role,
            content_class=ContentClass.PROSE,
            text=message,
            family=CodexRecordFamily.EVENT_MESSAGE,
        ),
        None,
    )


def _parse_compaction(
    record: _DecodedRecord,
    *,
    source_session_id: str,
    session_kind: SessionKind,
    next_epoch: int,
) -> SessionEpochBoundary | None:
    """Recognize the audited message/replacement_history compaction shape."""
    payload = record.payload.get("payload")
    if (
        not _is_json_object(payload)
        or not isinstance(payload.get("message"), str)
        or not isinstance(payload.get("replacement_history"), list)
    ):
        return None
    timestamp = record.payload.get("timestamp")
    alias = _physical_alias(record.envelope, source_session_id)
    return SessionEpochBoundary(
        provider=Provider.CODEX,
        source_session_id=source_session_id,
        session_kind=session_kind,
        conversation_epoch=next_epoch,
        physical_alias=alias,
        timestamp=timestamp if isinstance(timestamp, str) else "",
        trigger="codex_compacted",
        token_count=None,
    )


def _source_diagnostics(
    values: tuple[SourceDiagnostic, ...],
) -> tuple[CodexDiagnostic, ...]:
    """Carry relevant Task 2 source failures into provider coverage."""
    return tuple(
        CodexDiagnostic(
            code=CodexDiagnosticCode.PARTIAL_TAIL,
            detail=value.detail,
            record_ordinal=value.record_ordinal,
            source_line=value.source_line,
            source_byte_offset=value.source_byte_offset,
        )
        for value in values
        if value.code is SourceDiagnosticCode.PARTIAL_TAIL
    )


def _metadata_outer_shape(record: _DecodedRecord) -> bool:
    """Require the exact audited metadata wrapper and a valid timestamp."""
    return set(record.payload) == {"type", "timestamp", "payload"} and (
        isinstance(record.payload.get("timestamp"), str)
        and is_valid_native_timestamp(record.payload["timestamp"])
    )


def _is_string_list(value: object) -> bool:
    """Narrow one audited metadata field to a list of strings."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _turn_context_shape(record: _DecodedRecord) -> bool:
    """Recognize audited turn-context capabilities and field types."""
    payload = record.payload.get("payload")
    if not _metadata_outer_shape(record) or not _is_json_object(payload):
        return False
    fields = set(payload)
    if (
        not _TURN_CONTEXT_REQUIRED_FIELDS <= fields
        or not fields <= _TURN_CONTEXT_FIELDS
    ):
        return False
    if any(
        not isinstance(payload[field], str)
        for field in fields & _TURN_CONTEXT_STRING_FIELDS
    ):
        return False
    if any(
        not _is_json_object(payload[field])
        for field in fields & _TURN_CONTEXT_OBJECT_FIELDS
    ):
        return False
    if any(
        not isinstance(payload[field], bool)
        for field in fields & _TURN_CONTEXT_BOOLEAN_FIELDS
    ):
        return False
    return all(
        _is_string_list(payload[field]) for field in fields & _TURN_CONTEXT_LIST_FIELDS
    )


def _world_state_shape(record: _DecodedRecord) -> bool:
    """Recognize the audited full/state world-state wrapper."""
    payload = record.payload.get("payload")
    return (
        _metadata_outer_shape(record)
        and _is_json_object(payload)
        and set(payload) == {"full", "state"}
        and isinstance(payload.get("full"), bool)
        and _is_json_object(payload.get("state"))
    )


def _inter_agent_metadata_shape(record: _DecodedRecord) -> bool:
    """Recognize the audited trigger-turn inter-agent metadata wrapper."""
    payload = record.payload.get("payload")
    return (
        _metadata_outer_shape(record)
        and _is_json_object(payload)
        and set(payload) == {"trigger_turn"}
        and isinstance(payload.get("trigger_turn"), bool)
    )


def _candidate_alias(candidate: PhysicalMessageCandidate) -> PhysicalAlias:
    """Return the sole physical occurrence of an unpaired parser candidate."""
    return candidate.message.identity.physical_aliases[0]


def _messages_touching_new_candidates(
    canonical: CanonicalizationResult,
    candidates: tuple[PhysicalMessageCandidate, ...],
) -> tuple[NativeMessage, ...]:
    """Return only canonical rows created or changed by this parsed suffix."""
    aliases = {_candidate_alias(candidate) for candidate in candidates}
    return tuple(
        message
        for message in canonical.messages
        if aliases.intersection(message.identity.physical_aliases)
    )


def _diagnostics_touching_new_candidates(
    canonical: CanonicalizationResult,
    candidates: tuple[PhysicalMessageCandidate, ...],
) -> tuple[CanonicalizationDiagnostic, ...]:
    """Avoid re-emitting a prior suffix's already-recorded non-pairing outcome."""
    aliases = {_candidate_alias(candidate) for candidate in candidates}
    return tuple(
        diagnostic
        for diagnostic in canonical.diagnostics
        if aliases.intersection(diagnostic.physical_aliases)
    )


def _next_trailing_candidate(
    canonical: CanonicalizationResult,
    candidates: tuple[PhysicalMessageCandidate, ...],
    conversation_epoch: int,
) -> PhysicalMessageCandidate | None:
    """Carry only the last still-unpaired projection in the current epoch."""
    current = tuple(
        candidate
        for candidate in candidates
        if candidate.message.conversation_epoch == conversation_epoch
        and candidate.record_family
        in {
            CodexRecordFamily.RESPONSE_MESSAGE,
            CodexRecordFamily.EVENT_MESSAGE,
        }
    )
    if not current:
        return None
    candidate = max(
        current,
        key=lambda value: (
            _candidate_alias(value).source_file_relative.as_posix(),
            _candidate_alias(value).record_ordinal,
            _candidate_alias(value).source_byte_offset,
        ),
    )
    alias = _candidate_alias(candidate)
    canonical_message = next(
        message
        for message in canonical.messages
        if alias in message.identity.physical_aliases
    )
    if len(canonical_message.identity.physical_aliases) != 1:
        return None
    diagnostic = next(
        (value for value in canonical.diagnostics if alias in value.physical_aliases),
        None,
    )
    if (
        diagnostic is None
        or diagnostic.code is not CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER
    ):
        return None
    return candidate


def parse_codex_session(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    context: CodexSessionContext,
    source_diagnostics: tuple[SourceDiagnostic, ...] = (),
    prior_state: CodexParserState | None = None,
) -> CodexParseResult:
    """Adapt bounded Codex envelopes and canonicalize physical projections."""
    if prior_state is not None and not isinstance(prior_state, CodexParserState):
        raise TypeError("prior_state must be CodexParserState or None")
    state = prior_state if prior_state is not None else CodexParserState()
    records, decode_diagnostics = _decode_records(tuple(envelopes))
    if context.cwd is None:
        cwd = None
        for record in records:
            payload = record.payload.get("payload")
            if _is_json_object(payload):
                candidate = payload.get("cwd")
                if isinstance(candidate, str):
                    cwd = candidate
                    break
        context = CodexSessionContext(
            source_session_id=context.source_session_id,
            repository=context.repository,
            cwd=cwd,
        )
    session_kind, kind_diagnostics = _continued_session_kind(
        records, state.session_kind
    )
    source_session_id, identity_diagnostics = _resolved_session_identity(
        records, context, state.source_session_id
    )
    diagnostics = [
        *decode_diagnostics,
        *identity_diagnostics,
        *kind_diagnostics,
        *_source_diagnostics(tuple(source_diagnostics)),
    ]
    if source_session_id is None:
        return CodexParseResult(
            source_session_id=None,
            session_kind=SessionKind.UNKNOWN,
            messages=(),
            physical_candidates=(),
            boundaries=(),
            diagnostics=tuple(
                sorted(
                    diagnostics,
                    key=lambda value: (
                        value.record_ordinal
                        if value.record_ordinal is not None
                        else -1,
                        value.code.value,
                        value.detail,
                    ),
                )
            ),
            canonicalization_diagnostics=(),
            next_state=state,
        )
    if state.trailing_candidate is not None:
        carry_locator = state.trailing_candidate.message.identity.canonical_locator
        if carry_locator.source_session_id != source_session_id:
            raise ValueError("trailing_candidate belongs to a different source session")
    candidates: list[PhysicalMessageCandidate] = []
    boundaries: list[SessionEpochBoundary] = []
    conversation_epoch = state.next_conversation_epoch
    seen_compaction_digests = set(state.seen_compaction_digests)

    for record in records:
        outer_type = record.payload.get("type")
        if outer_type == "session_meta":
            continue
        if outer_type == "turn_context":
            if not _turn_context_shape(record):
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE,
                        record.envelope,
                        "turn_context does not match an audited metadata shape",
                    )
                )
        elif outer_type == "world_state":
            if not _world_state_shape(record):
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE,
                        record.envelope,
                        "world_state does not match an audited metadata shape",
                    )
                )
        elif outer_type == "response_item":
            parsed, item_diagnostics = _parse_response_item(
                record,
                context=context,
                source_session_id=source_session_id,
                session_kind=session_kind,
                epoch=conversation_epoch,
            )
            candidates.extend(parsed)
            diagnostics.extend(item_diagnostics)
        elif outer_type == "event_msg":
            parsed, event_diagnostic = _parse_event(
                record,
                context=context,
                source_session_id=source_session_id,
                session_kind=session_kind,
                epoch=conversation_epoch,
            )
            if parsed is not None:
                candidates.append(parsed)
            if event_diagnostic is not None:
                diagnostics.append(event_diagnostic)
        elif outer_type == "compacted":
            boundary = _parse_compaction(
                record,
                source_session_id=source_session_id,
                session_kind=session_kind,
                next_epoch=conversation_epoch + 1,
            )
            if boundary is None:
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.UNKNOWN_COMPACTION,
                        record.envelope,
                        "compacted record does not match an audited boundary shape",
                    )
                )
            elif record.envelope.source_digest in seen_compaction_digests:
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.DUPLICATE_COMPACTION,
                        record.envelope,
                        "duplicate compacted record does not advance the epoch",
                    )
                )
            else:
                seen_compaction_digests.add(record.envelope.source_digest)
                conversation_epoch += 1
                boundaries.append(boundary)
        elif outer_type == "inter_agent_communication_metadata":
            if _inter_agent_metadata_shape(record):
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.EXCLUDED_INTER_AGENT_METADATA,
                        record.envelope,
                        "inter-agent metadata is retained as provenance, not searchable text",
                    )
                )
            else:
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE,
                        record.envelope,
                        "inter-agent metadata does not match an audited shape",
                    )
                )
        else:
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
                    record.envelope,
                    f"unrecognized Codex outer record type: {outer_type!r}",
                )
            )

    new_candidates = tuple(candidates)
    established_kind = (
        state.session_kind if state.session_kind is not None else session_kind
    )
    compatible_carry = (
        state.trailing_candidate if session_kind is established_kind else None
    )
    candidates_with_carry = (
        *((compatible_carry,) if compatible_carry is not None else ()),
        *new_candidates,
    )
    canonical = canonicalize_codex_candidates(candidates_with_carry)
    next_candidate = (
        _next_trailing_candidate(canonical, candidates_with_carry, conversation_epoch)
        if session_kind is established_kind
        else None
    )
    return CodexParseResult(
        source_session_id=source_session_id,
        session_kind=session_kind,
        messages=_messages_touching_new_candidates(canonical, new_candidates),
        physical_candidates=new_candidates,
        boundaries=tuple(boundaries),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda value: (
                    value.record_ordinal if value.record_ordinal is not None else -1,
                    value.code.value,
                    value.detail,
                ),
            )
        ),
        canonicalization_diagnostics=_diagnostics_touching_new_candidates(
            canonical, new_candidates
        ),
        next_state=CodexParserState(
            next_conversation_epoch=conversation_epoch,
            source_session_id=source_session_id,
            session_kind=established_kind,
            seen_compaction_digests=tuple(sorted(seen_compaction_digests)),
            trailing_candidate=next_candidate,
        ),
    )
