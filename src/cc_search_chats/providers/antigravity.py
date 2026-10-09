"""Pure, fail-closed adapter for primary Antigravity CLI transcript records.

Admission is decided on the first complete record of a session. Admitted
sessions project visible user requests, assistant prose and tool calls;
thinking, tool results, injected context and checkpoints are excluded. Any
unregistered record shape blocks the source.
"""

# pattern: Functional Core

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeIs

from cc_search_chats.core.canonicalization import codex_logical_message_id
from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    MessageIdentity,
    NativeLocator,
    NativeMessage,
    PhysicalAlias,
    Provider,
    SessionKind,
    SubmittedBy,
    is_unicode_scalar_text,
    repair_unstorable_text,
)

if TYPE_CHECKING:
    from cc_search_chats.providers.source_discovery import (
        RecordEnvelope,
        SourceDiagnostic,
    )

SCOPE_START = "2026-10-01T00:00:00Z"
"""First ``created_at`` admitted, inclusive; changing it advances the parser version."""

_CREATED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z").fullmatch
_USER_REQUEST = re.compile(r"\A\s*<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_TRUNCATION_MARKER = re.compile(r"<truncated \d+ bytes>")


class AntigravityDiagnosticCode(StrEnum):
    """Closed Antigravity admission, parse and exclusion classifications."""

    MALFORMED_JSON = "malformed_json"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_UNICODE = "invalid_unicode"
    REPAIRED_UNICODE = "repaired_unicode"
    UNRECOGNISED_FIRST_RECORD = "antigravity_unrecognised_first_record"
    BEFORE_SCOPE_START = "antigravity_before_scope_start"
    NOT_HUMAN_INITIATED = "antigravity_not_human_initiated"
    UNKNOWN_RECORD_PAIR = "antigravity_unknown_record_pair"
    MISSING_REQUIRED_KEY = "antigravity_missing_required_key"
    INVALID_FIELD_TYPE = "antigravity_invalid_field_type"
    MISSING_USER_REQUEST = "antigravity_missing_user_request"
    INVALID_CREATED_AT = "antigravity_invalid_created_at"
    TRUNCATED_PROSE = "antigravity_truncated_prose"
    EXCLUDED_THINKING = "excluded_thinking"
    EXCLUDED_TOOL_RESULT = "excluded_tool_result"
    EXCLUDED_INJECTED = "excluded_injected"
    EMPTY_CONTENT = "empty_content"


class AdmissionOutcome(StrEnum):
    """What the first complete record decides for the whole source."""

    ADMITTED = "admitted"
    EXCLUDED = "excluded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """One admission outcome with the code and detail that explain it."""

    outcome: AdmissionOutcome
    code: AntigravityDiagnosticCode | None
    detail: str


@dataclass(frozen=True, slots=True)
class AntigravityDiagnostic:
    """One named outcome tied to a complete native record."""

    code: AntigravityDiagnosticCode
    detail: str
    record_ordinal: int
    source_line: int
    source_byte_offset: int


@dataclass(frozen=True, slots=True)
class AntigravitySessionContext:
    """The session directory name, which is the provider session identity."""

    source_session_id: str

    def __post_init__(self) -> None:
        """Reject context that cannot construct provider identity."""
        if not self.source_session_id.strip() or any(
            delimiter in self.source_session_id for delimiter in (":", "\r", "\n")
        ):
            raise ValueError("source_session_id must be a nonempty locator-safe string")


@dataclass(frozen=True, slots=True)
class AntigravityParserState:
    """Immutable state required to parse only an appended suffix."""

    next_conversation_epoch: int = 0
    cwd: str | None = None

    def __post_init__(self) -> None:
        """Reject persisted state that cannot identify the next epoch."""
        if (
            isinstance(self.next_conversation_epoch, bool)
            or not isinstance(self.next_conversation_epoch, int)
            or self.next_conversation_epoch < 0
        ):
            raise ValueError("next_conversation_epoch must be a nonnegative integer")
        if self.cwd is not None and not isinstance(self.cwd, str):
            raise ValueError("cwd must be a string or None")


@dataclass(frozen=True, slots=True)
class AntigravityParseResult:
    """All retained and excluded outcomes for one bounded Antigravity source."""

    messages: tuple[NativeMessage, ...]
    diagnostics: tuple[AntigravityDiagnostic, ...]
    next_state: AntigravityParserState
    cwd_established: bool


@dataclass(frozen=True, slots=True)
class _DecodedRecord:
    envelope: RecordEnvelope
    payload: dict[str, object]


class _RecordRejectedError(Exception):
    """A record whose shape is not registered; it blocks the source."""

    def __init__(self, code: AntigravityDiagnosticCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _diagnostic(
    code: AntigravityDiagnosticCode, envelope: RecordEnvelope, detail: str
) -> AntigravityDiagnostic:
    return AntigravityDiagnostic(
        code=code,
        detail=detail,
        record_ordinal=envelope.record_ordinal,
        source_line=envelope.source_line,
        source_byte_offset=envelope.source_byte_offset,
    )


def _common_shape(payload: object) -> tuple[str, str, str]:
    """Return ``(source, type, created_at)`` or raise the blocking rejection."""
    if not _is_json_object(payload):
        raise _RecordRejectedError(
            AntigravityDiagnosticCode.UNKNOWN_RECORD_PAIR,
            "record is not a JSON object",
        )
    values: list[str] = []
    for key in ("source", "type", "created_at"):
        if key not in payload:
            raise _RecordRejectedError(
                AntigravityDiagnosticCode.MISSING_REQUIRED_KEY,
                f"record lacks required key {key}",
            )
        value = payload[key]
        if not isinstance(value, str):
            raise _RecordRejectedError(
                AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
                f"record key {key} must be a string",
            )
        values.append(value)
    source, record_type, created_at = values
    if _CREATED_AT(created_at) is None:
        raise _RecordRejectedError(
            AntigravityDiagnosticCode.INVALID_CREATED_AT,
            "created_at must be YYYY-MM-DDTHH:MM:SSZ",
        )
    return source, record_type, created_at


def admit_antigravity_session(first_record_bytes: bytes) -> AdmissionDecision:
    """Decide the whole source from its first complete record."""
    try:
        payload = json.loads(first_record_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        return AdmissionDecision(
            AdmissionOutcome.BLOCKED,
            AntigravityDiagnosticCode.UNRECOGNISED_FIRST_RECORD,
            f"first record is not a JSON object: {error}",
        )
    try:
        source, record_type, created_at = _common_shape(payload)
    except _RecordRejectedError as rejected:
        return AdmissionDecision(
            AdmissionOutcome.BLOCKED,
            AntigravityDiagnosticCode.UNRECOGNISED_FIRST_RECORD,
            rejected.detail,
        )
    if created_at < SCOPE_START:
        return AdmissionDecision(
            AdmissionOutcome.EXCLUDED,
            AntigravityDiagnosticCode.BEFORE_SCOPE_START,
            f"first record {created_at} precedes scope start {SCOPE_START}",
        )
    if (source, record_type) == ("SYSTEM", "SYSTEM_MESSAGE"):
        return AdmissionDecision(
            AdmissionOutcome.EXCLUDED,
            AntigravityDiagnosticCode.NOT_HUMAN_INITIATED,
            "session opens with a system message rather than a user request",
        )
    if (source, record_type) == ("USER_EXPLICIT", "USER_INPUT"):
        return AdmissionDecision(AdmissionOutcome.ADMITTED, None, "primary session")
    return AdmissionDecision(
        AdmissionOutcome.BLOCKED,
        AntigravityDiagnosticCode.UNRECOGNISED_FIRST_RECORD,
        f"first record pair {source}/{record_type} is not registered",
    )


def _decode_records(
    envelopes: tuple[RecordEnvelope, ...],
) -> tuple[tuple[_DecodedRecord, ...], tuple[AntigravityDiagnostic, ...]]:
    records: list[_DecodedRecord] = []
    diagnostics: list[AntigravityDiagnostic] = []
    for envelope in envelopes:
        try:
            payload = json.loads(envelope.raw_bytes)
        except UnicodeDecodeError as error:
            diagnostics.append(
                _diagnostic(
                    AntigravityDiagnosticCode.INVALID_ENCODING,
                    envelope,
                    f"record is not valid UTF-8: {error}",
                )
            )
            continue
        except json.JSONDecodeError as error:
            diagnostics.append(
                _diagnostic(
                    AntigravityDiagnosticCode.MALFORMED_JSON,
                    envelope,
                    f"record is not valid JSON: {error}",
                )
            )
            continue
        records.append(_DecodedRecord(envelope=envelope, payload=payload))
    return tuple(records), tuple(diagnostics)


def _physical_alias(envelope: RecordEnvelope, source_session_id: str) -> PhysicalAlias:
    locator = NativeLocator(
        provider=Provider.ANTIGRAVITY,
        source_session_id=source_session_id,
        key_kind=LocatorKeyKind.ORDINAL,
        key=envelope.record_ordinal,
        record_digest=envelope.source_digest,
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


def _storable_text(
    text: str, envelope: RecordEnvelope
) -> tuple[str | None, tuple[AntigravityDiagnostic, ...]]:
    """Return storable text (or ``None`` for no row) with its diagnostics."""
    repaired_text, repaired = repair_unstorable_text(text)
    if not is_unicode_scalar_text(repaired_text):
        return None, (
            _diagnostic(
                AntigravityDiagnosticCode.INVALID_UNICODE,
                envelope,
                "text contains a non-scalar Unicode value",
            ),
        )
    if not repaired_text.strip():
        return None, (
            _diagnostic(
                AntigravityDiagnosticCode.EMPTY_CONTENT,
                envelope,
                "recognized record has blank content",
            ),
        )
    diagnostics: list[AntigravityDiagnostic] = []
    if repaired:
        diagnostics.append(
            _diagnostic(
                AntigravityDiagnosticCode.REPAIRED_UNICODE,
                envelope,
                "text contained unstorable code points repaired with U+FFFD",
            )
        )
    if _TRUNCATION_MARKER.search(repaired_text) is not None:
        diagnostics.append(
            _diagnostic(
                AntigravityDiagnosticCode.TRUNCATED_PROSE,
                envelope,
                "indexed prose carries an upstream truncation marker",
            )
        )
    return repaired_text, tuple(diagnostics)


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _RecordRejectedError(
            AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
            f"record key {key} must be a string when present",
        )
    return value


def _render_args(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tool_calls(payload: dict[str, object]) -> tuple[tuple[str, object], ...]:
    """Validate ``tool_calls`` and return ``(name, args)`` pairs in call order."""
    raw = payload.get("tool_calls")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _RecordRejectedError(
            AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
            "tool_calls must be a list when present",
        )
    calls: list[tuple[str, object]] = []
    for item in raw:
        if not _is_json_object(item) or not isinstance(item.get("name"), str):
            raise _RecordRejectedError(
                AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
                "each tool call must be an object with a string name",
            )
        calls.append((item["name"], item.get("args")))
    return tuple(calls)


def _user_request_text(
    payload: dict[str, object], envelope: RecordEnvelope
) -> tuple[str | None, tuple[AntigravityDiagnostic, ...]]:
    content = payload.get("content")
    if content is None:
        raise _RecordRejectedError(
            AntigravityDiagnosticCode.MISSING_REQUIRED_KEY,
            "USER_INPUT lacks required key content",
        )
    if not isinstance(content, str):
        raise _RecordRejectedError(
            AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
            "USER_INPUT content must be a string",
        )
    match = _USER_REQUEST.match(content)
    if match is None:
        raise _RecordRejectedError(
            AntigravityDiagnosticCode.MISSING_USER_REQUEST,
            "USER_INPUT content does not open with a USER_REQUEST element",
        )
    diagnostics: list[AntigravityDiagnostic] = []
    if content[match.end() :].strip():
        diagnostics.append(
            _diagnostic(
                AntigravityDiagnosticCode.EXCLUDED_INJECTED,
                envelope,
                "context after the USER_REQUEST element is deliberately non-searchable",
            )
        )
    text, text_diagnostics = _storable_text(match.group(1), envelope)
    return text, (*diagnostics, *text_diagnostics)


@dataclass(frozen=True, slots=True)
class _Projection:
    rows: tuple[tuple[ContentClass, str], ...]
    diagnostics: tuple[AntigravityDiagnostic, ...]
    role: str | None = None
    epoch_boundary: bool = False
    run_command_cwd: str | None = None


def _first_run_command_cwd(calls: tuple[tuple[str, object], ...]) -> str | None:
    """Return the ``Cwd`` argument of the first ``run_command`` call, if a string."""
    for name, args in calls:
        if name != "run_command":
            continue
        cwd = args.get("Cwd") if _is_json_object(args) else None
        return cwd if isinstance(cwd, str) and cwd else None
    return None


def _project_user_input(
    payload: dict[str, object], envelope: RecordEnvelope
) -> _Projection:
    text, diagnostics = _user_request_text(payload, envelope)
    rows = () if text is None else ((ContentClass.PROSE, text),)
    return _Projection(rows=rows, diagnostics=diagnostics, role="user")


def _project_planner_response(
    payload: dict[str, object], envelope: RecordEnvelope
) -> _Projection:
    content = _optional_string(payload, "content")
    thinking = _optional_string(payload, "thinking")
    calls = _tool_calls(payload)
    rows: list[tuple[ContentClass, str]] = []
    diagnostics: list[AntigravityDiagnostic] = []
    if content is not None:
        text, text_diagnostics = _storable_text(content, envelope)
        diagnostics.extend(text_diagnostics)
        if text is not None:
            rows.append((ContentClass.PROSE, text))
    if thinking is not None:
        diagnostics.append(
            _diagnostic(
                AntigravityDiagnosticCode.EXCLUDED_THINKING,
                envelope,
                "thinking is deliberately non-searchable",
            )
        )
    if calls:
        rows.append((ContentClass.TOOL_NAME, " ".join(name for name, _args in calls)))
        rows.append(
            (
                ContentClass.TOOL_INPUT,
                "\n".join(_render_args(args) for _name, args in calls),
            )
        )
    return _Projection(
        rows=tuple(rows),
        diagnostics=tuple(diagnostics),
        role="assistant",
        run_command_cwd=_first_run_command_cwd(calls),
    )


def _project_record(record: _DecodedRecord) -> _Projection:
    """Project one decoded record or raise the blocking rejection."""
    source, record_type, _created_at = _common_shape(record.payload)
    pair = (source, record_type)
    if pair == ("USER_EXPLICIT", "USER_INPUT"):
        return _project_user_input(record.payload, record.envelope)
    if pair == ("MODEL", "PLANNER_RESPONSE"):
        return _project_planner_response(record.payload, record.envelope)
    if pair == ("MODEL", "GENERIC"):
        return _Projection(
            rows=(),
            diagnostics=(
                _diagnostic(
                    AntigravityDiagnosticCode.EXCLUDED_TOOL_RESULT,
                    record.envelope,
                    "tool results are deliberately non-searchable",
                ),
            ),
        )
    if pair == ("SYSTEM", "SYSTEM_MESSAGE"):
        return _Projection(
            rows=(),
            diagnostics=(
                _diagnostic(
                    AntigravityDiagnosticCode.EXCLUDED_INJECTED,
                    record.envelope,
                    "system messages are deliberately non-searchable",
                ),
            ),
        )
    if pair == ("SYSTEM", "CHECKPOINT"):
        return _Projection(rows=(), diagnostics=(), epoch_boundary=True)
    raise _RecordRejectedError(
        AntigravityDiagnosticCode.UNKNOWN_RECORD_PAIR,
        f"record pair {source}/{record_type} is not registered",
    )


def _native_messages(
    record: _DecodedRecord,
    projection: _Projection,
    *,
    context: AntigravitySessionContext,
    epoch: int,
    cwd: str | None,
) -> tuple[NativeMessage, ...]:
    if not projection.rows or projection.role is None:
        return ()
    alias = _physical_alias(record.envelope, context.source_session_id)
    identity = MessageIdentity(
        logical_message_id=codex_logical_message_id(alias),
        canonical_locator=alias.locator,
        physical_aliases=(alias,),
    )
    created_at = record.payload.get("created_at")
    return tuple(
        NativeMessage(
            identity=identity,
            timestamp=created_at if isinstance(created_at, str) else "",
            role=projection.role,
            session_kind=SessionKind.PRIMARY,
            conversation_epoch=epoch,
            content_class=content_class,
            text=text,
            repository=None,
            cwd=cwd,
            submitted_by=SubmittedBy.UNKNOWN,
        )
        for content_class, text in projection.rows
    )


def parse_antigravity_session(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    context: AntigravitySessionContext,
    source_diagnostics: tuple[SourceDiagnostic, ...] = (),
    prior_state: AntigravityParserState | None = None,
) -> AntigravityParseResult:
    """Adapt bounded Antigravity envelopes into searchable and diagnostic outcomes."""
    del source_diagnostics
    if prior_state is not None and not isinstance(prior_state, AntigravityParserState):
        raise TypeError("prior_state must be AntigravityParserState or None")
    state = prior_state if prior_state is not None else AntigravityParserState()
    records, decode_diagnostics = _decode_records(tuple(envelopes))
    projected: list[tuple[_DecodedRecord, _Projection, int]] = []
    diagnostics = list(decode_diagnostics)
    epoch = state.next_conversation_epoch
    cwd = state.cwd
    for record in records:
        try:
            projection = _project_record(record)
        except _RecordRejectedError as rejected:
            diagnostics.append(
                _diagnostic(rejected.code, record.envelope, rejected.detail)
            )
            continue
        diagnostics.extend(projection.diagnostics)
        if projection.epoch_boundary:
            epoch += 1
            continue
        if cwd is None and projection.run_command_cwd is not None:
            cwd = projection.run_command_cwd
        projected.append((record, projection, epoch))
    messages = tuple(
        message
        for record, projection, record_epoch in projected
        for message in _native_messages(
            record, projection, context=context, epoch=record_epoch, cwd=cwd
        )
    )
    return AntigravityParseResult(
        messages=messages,
        diagnostics=tuple(diagnostics),
        next_state=AntigravityParserState(next_conversation_epoch=epoch, cwd=cwd),
        cwd_established=state.cwd is None and cwd is not None,
    )
