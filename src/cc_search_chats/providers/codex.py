"""Pure, fail-closed adapter for native Codex JSONL records."""

# pattern: Functional Core

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeIs

from cc_search_chats.core.canonicalization import (
    CanonicalizationDiagnostic,
    CodexRecordFamily,
    PhysicalMessageCandidate,
    canonicalize_codex_candidates,
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
)
from cc_search_chats.providers.source_discovery import (
    RecordEnvelope,
    SourceDiagnostic,
    SourceDiagnosticCode,
)

_PRIMARY_SOURCES = {"cli", "exec", "mcp", "vscode"}


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
    """Explicit provider context that is not inferred from date paths."""

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
class CodexParseResult:
    """Canonical messages plus retained physical and diagnostic outcomes."""

    session_kind: SessionKind
    messages: tuple[NativeMessage, ...]
    physical_candidates: tuple[PhysicalMessageCandidate, ...]
    boundaries: tuple[SessionEpochBoundary, ...]
    diagnostics: tuple[CodexDiagnostic, ...]
    canonicalization_diagnostics: tuple[CanonicalizationDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class _DecodedRecord:
    envelope: RecordEnvelope
    payload: dict[str, object]


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Narrow an untrusted JSON value after checking its key contract."""
    return isinstance(value, dict) and all(
        isinstance(key, str) for key in value
    )


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
        except (json.JSONDecodeError, UnicodeDecodeError):
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
    return isinstance(subagent, str) and bool(subagent.strip())


def _source_kind(source: object) -> SessionKind | None:
    """Classify only audited primary and child source shapes."""
    if isinstance(source, str) and source in _PRIMARY_SOURCES:
        return SessionKind.PRIMARY
    if _modern_subagent(source) or _legacy_subagent(source):
        return SessionKind.AGENT
    return None


def _session_kind(
    records: tuple[_DecodedRecord, ...], context: CodexSessionContext
) -> tuple[SessionKind, tuple[CodexDiagnostic, ...]]:
    """Classify all session_meta records without resetting record state."""
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
        native_ids = tuple(
            value
            for key in ("id", "session_id")
            if isinstance((value := meta.get(key)), str) and value
        )
        kind = _source_kind(meta.get("source"))
        if (
            kind is None
            or not native_ids
            or context.source_session_id not in native_ids
        ):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
                    record.envelope,
                    "session_meta source or session identity is unsupported",
                )
            )
            continue
        kinds.append(kind)
    if not kinds or len(set(kinds)) != 1 or diagnostics:
        return SessionKind.UNKNOWN, tuple(diagnostics)
    return kinds[0], tuple(diagnostics)


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
    session_kind: SessionKind,
    epoch: int,
    role: str,
    content_class: ContentClass,
    text: str,
    family: CodexRecordFamily,
    native_id: str | None = None,
) -> PhysicalMessageCandidate:
    """Construct one common physical message/content candidate."""
    alias = _physical_alias(record.envelope, context.source_session_id, native_id)
    identified_agent = role == "user" and session_kind is SessionKind.AGENT
    timestamp = record.payload.get("timestamp")
    return PhysicalMessageCandidate(
        message=NativeMessage(
            identity=MessageIdentity(
                logical_message_id=(
                    native_id
                    if native_id is not None
                    else f"record-{record.envelope.record_ordinal}-{record.envelope.source_digest}"
                ),
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
    expected = "input_text" if role == "user" else "output_text"
    text_parts: list[str] = []
    diagnostics: list[CodexDiagnostic] = []
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
        text_parts.append(text)
    return ("\n".join(text_parts) if text_parts else None), tuple(diagnostics)


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
    return (
        _native_message(
            record=record,
            context=context,
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
    context: CodexSessionContext,
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
    alias = _physical_alias(record.envelope, context.source_session_id)
    return SessionEpochBoundary(
        provider=Provider.CODEX,
        source_session_id=context.source_session_id,
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


def parse_codex_session(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    context: CodexSessionContext,
    source_diagnostics: tuple[SourceDiagnostic, ...] = (),
) -> CodexParseResult:
    """Adapt bounded Codex envelopes and canonicalize physical projections."""
    records, decode_diagnostics = _decode_records(tuple(envelopes))
    session_kind, kind_diagnostics = _session_kind(records, context)
    candidates: list[PhysicalMessageCandidate] = []
    boundaries: list[SessionEpochBoundary] = []
    diagnostics = [
        *decode_diagnostics,
        *kind_diagnostics,
        *_source_diagnostics(tuple(source_diagnostics)),
    ]
    conversation_epoch = 0
    seen_compaction_digests: set[str] = set()

    for record in records:
        outer_type = record.payload.get("type")
        if outer_type in {"session_meta", "turn_context", "world_state"}:
            continue
        if outer_type == "response_item":
            parsed, item_diagnostics = _parse_response_item(
                record,
                context=context,
                session_kind=session_kind,
                epoch=conversation_epoch,
            )
            candidates.extend(parsed)
            diagnostics.extend(item_diagnostics)
        elif outer_type == "event_msg":
            parsed, event_diagnostic = _parse_event(
                record,
                context=context,
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
                context=context,
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
                    CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
                    record.envelope,
                    f"unrecognized Codex outer record type: {outer_type!r}",
                )
            )

    canonical = canonicalize_codex_candidates(tuple(candidates))
    return CodexParseResult(
        session_kind=session_kind,
        messages=canonical.messages,
        physical_candidates=tuple(candidates),
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
        canonicalization_diagnostics=canonical.diagnostics,
    )
