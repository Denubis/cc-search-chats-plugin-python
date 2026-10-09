"""One adapter entry per native provider for every provider-specific dispatch.

The registry owns root defaults and variables, discovery, parser-state version
and serialisation, the parse entry with its blocking, skippable and repaired
codes, and the unindexed-locator scan. Storage modules look a provider up here
instead of branching on ``Provider`` values.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeIs

from cc_search_chats.core.canonicalization import (
    CodexRecordFamily,
    PhysicalMessageCandidate,
)
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
    format_locator,
    permitted_locator_key_kinds,
)
from cc_search_chats.providers.antigravity import (
    AdmissionDecision,
    AdmissionOutcome,
    AntigravityDiagnosticCode,
    AntigravityParseResult,
    AntigravityParserState,
    AntigravitySessionContext,
    admit_antigravity_session,
    parse_antigravity_session,
)
from cc_search_chats.providers.claude import (
    ClaudeDiagnosticCode,
    ClaudeParserState,
    ClaudeSessionContext,
    parse_claude_session,
)
from cc_search_chats.providers.codex import (
    CodexDiagnosticCode,
    CodexParserState,
    CodexSessionContext,
    parse_codex_session,
)
from cc_search_chats.providers.source_discovery import (
    BoundedReadStopReason,
    ConfiguredSourceRoot,
    DiscoveryResult,
    RecordEnvelope,
    SourceDiagnostic,
    discover_antigravity_sources,
    discover_claude_sources,
    discover_codex_sources,
    read_bounded_jsonl,
    source_root_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from enum import StrEnum


class ParseDiagnostic(Protocol):
    """One provider parse outcome with optional physical coordinates."""

    @property
    def code(self) -> StrEnum: ...

    @property
    def detail(self) -> str: ...

    @property
    def record_ordinal(self) -> int | None: ...

    @property
    def source_line(self) -> int | None: ...

    @property
    def source_byte_offset(self) -> int | None: ...


class ParseResult(Protocol):
    """The provider-neutral surface of one bounded parse."""

    @property
    def messages(self) -> tuple[NativeMessage, ...]: ...

    @property
    def diagnostics(self) -> Sequence[ParseDiagnostic]: ...

    @property
    def next_state(self) -> object: ...


class ParseEntry(Protocol):
    """Parse bounded envelopes of one source with explicit continuation state."""

    def __call__(
        self,
        envelopes: tuple[RecordEnvelope, ...],
        *,
        source_session_id: str | None,
        source_diagnostics: tuple[SourceDiagnostic, ...],
        prior_state: object,
    ) -> ParseResult: ...


class DiscoverEntry(Protocol):
    """Discover candidate sources beneath one resolved provider root."""

    def __call__(
        self, resolved_root: Path, *, inspect_content: bool
    ) -> DiscoveryResult: ...


@dataclass(frozen=True, slots=True)
class ScanEvidence:
    """What a direct native scan established about one unindexed locator."""

    recognized: bool = False
    unsupported: bool = False
    incomplete: bool = False


def is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Return whether a decoded value is a JSON object with string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def required_text(value: Mapping[str, object], key: str) -> str:
    """Read one required string field from persisted JSON state."""
    result = value.get(key)
    if not isinstance(result, str):
        raise ValueError(  # noqa: TRY004  # persisted-state ValueError contract
            f"{key} must be a string"
        )
    return result


def optional_text(value: Mapping[str, object], key: str) -> str | None:
    """Read one optional string field from persisted JSON state."""
    result = value.get(key)
    if result is not None and not isinstance(result, str):
        raise ValueError(f"{key} must be a string or null")
    return result


def required_integer(value: Mapping[str, object], key: str) -> int:
    """Read one required integer field from persisted JSON state."""
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValueError(  # noqa: TRY004  # persisted-state ValueError contract
            f"{key} must be an integer"
        )
    return result


def _string_tuple(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = value.get(key)
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"{key} must be a string array")
    return tuple(item for item in raw if isinstance(item, str))


def _serialize_codex_candidate(
    candidate: PhysicalMessageCandidate,
) -> dict[str, object]:
    message = candidate.message
    if len(message.identity.physical_aliases) != 1:
        raise ValueError("persisted Codex carry must have exactly one physical alias")
    alias = message.identity.physical_aliases[0]
    locator = alias.locator
    return {
        "record_family": candidate.record_family.value,
        "logical_message_id": message.identity.logical_message_id,
        "source_session_id": locator.source_session_id,
        "key_kind": locator.key_kind.value,
        "key": locator.key,
        "record_digest": locator.record_digest,
        "source_file_relative": alias.source_file_relative.as_posix(),
        "record_ordinal": alias.record_ordinal,
        "source_line": alias.source_line,
        "source_byte_offset": alias.source_byte_offset,
        "raw_byte_length": alias.raw_byte_length,
        "source_digest": alias.source_digest,
        "timestamp": message.timestamp,
        "role": message.role,
        "session_kind": message.session_kind.value,
        "conversation_epoch": message.conversation_epoch,
        "content_class": message.content_class.value,
        "text": message.text,
        "repository": message.repository,
        "cwd": message.cwd,
        "submitted_by": message.submitted_by.value,
        "submission_evidence": list(message.submission_evidence),
        "submission_match_cardinality": message.submission_match_cardinality,
    }


def _deserialize_codex_candidate(value: object) -> PhysicalMessageCandidate:
    if not is_json_object(value):
        raise ValueError("Codex trailing candidate must be an object")
    key_kind = LocatorKeyKind(required_text(value, "key_kind"))
    raw_key = value.get("key")
    if key_kind is LocatorKeyKind.ORDINAL:
        if isinstance(raw_key, bool) or not isinstance(raw_key, int):
            raise ValueError("ordinal Codex carry key must be an integer")
        key: str | int = raw_key
    else:
        if not isinstance(raw_key, str):
            raise ValueError("ID Codex carry key must be a string")
        key = raw_key
    raw_evidence = value.get("submission_evidence")
    if not isinstance(raw_evidence, list) or any(
        not isinstance(item, str) for item in raw_evidence
    ):
        raise ValueError("submission_evidence must be a string array")
    locator = NativeLocator(
        provider=Provider.CODEX,
        source_session_id=required_text(value, "source_session_id"),
        key_kind=key_kind,
        key=key,
        record_digest=optional_text(value, "record_digest"),
    )
    alias = PhysicalAlias(
        locator=locator,
        source_file_relative=Path(required_text(value, "source_file_relative")),
        record_ordinal=required_integer(value, "record_ordinal"),
        source_line=required_integer(value, "source_line"),
        source_byte_offset=required_integer(value, "source_byte_offset"),
        raw_byte_length=required_integer(value, "raw_byte_length"),
        source_digest=required_text(value, "source_digest"),
    )
    message = NativeMessage(
        identity=MessageIdentity(
            logical_message_id=required_text(value, "logical_message_id"),
            canonical_locator=locator,
            physical_aliases=(alias,),
        ),
        timestamp=required_text(value, "timestamp"),
        role=required_text(value, "role"),
        session_kind=SessionKind(required_text(value, "session_kind")),
        conversation_epoch=required_integer(value, "conversation_epoch"),
        content_class=ContentClass(required_text(value, "content_class")),
        text=required_text(value, "text"),
        repository=optional_text(value, "repository"),
        cwd=optional_text(value, "cwd"),
        submitted_by=SubmittedBy(required_text(value, "submitted_by")),
        submission_evidence=tuple(
            item for item in raw_evidence if isinstance(item, str)
        ),
        submission_match_cardinality=required_integer(
            value, "submission_match_cardinality"
        ),
    )
    return PhysicalMessageCandidate(
        message=message,
        record_family=CodexRecordFamily(required_text(value, "record_family")),
    )


def _serialize_claude_state(state: object) -> dict[str, object]:
    if not isinstance(state, ClaudeParserState):
        raise TypeError("Claude source produced non-Claude parser state")
    return {
        "next_conversation_epoch": state.next_conversation_epoch,
        "seen_compaction_uuids": list(state.seen_compaction_uuids),
    }


def _deserialize_claude_state(value: object) -> ClaudeParserState:
    if not is_json_object(value):
        raise ValueError("parser state must be an object")
    return ClaudeParserState(
        next_conversation_epoch=required_integer(value, "next_conversation_epoch"),
        seen_compaction_uuids=_string_tuple(value, "seen_compaction_uuids"),
    )


def _serialize_codex_state(state: object) -> dict[str, object]:
    if not isinstance(state, CodexParserState):
        raise TypeError("Codex source produced non-Codex parser state")
    return {
        "next_conversation_epoch": state.next_conversation_epoch,
        "session_kind": (
            state.session_kind.value if state.session_kind is not None else None
        ),
        "seen_compaction_digests": list(state.seen_compaction_digests),
        "trailing_candidate": (
            _serialize_codex_candidate(state.trailing_candidate)
            if state.trailing_candidate is not None
            else None
        ),
        "source_session_id": state.source_session_id,
    }


def _deserialize_codex_state(value: object) -> CodexParserState:
    if not is_json_object(value):
        raise ValueError("parser state must be an object")
    raw_kind = value.get("session_kind")
    if raw_kind is not None and not isinstance(raw_kind, str):
        raise ValueError("session_kind must be a string or null")
    return CodexParserState(
        next_conversation_epoch=required_integer(value, "next_conversation_epoch"),
        session_kind=SessionKind(raw_kind) if raw_kind is not None else None,
        seen_compaction_digests=_string_tuple(value, "seen_compaction_digests"),
        trailing_candidate=(
            _deserialize_codex_candidate(value["trailing_candidate"])
            if value.get("trailing_candidate") is not None
            else None
        ),
        source_session_id=optional_text(value, "source_session_id"),
    )


def _serialize_antigravity_state(state: object) -> dict[str, object]:
    if not isinstance(state, AntigravityParserState):
        raise TypeError("Antigravity source produced non-Antigravity parser state")
    return {
        "next_conversation_epoch": state.next_conversation_epoch,
        "cwd": state.cwd,
    }


def _deserialize_antigravity_state(value: object) -> AntigravityParserState:
    if not is_json_object(value):
        raise ValueError("parser state must be an object")
    return AntigravityParserState(
        next_conversation_epoch=required_integer(value, "next_conversation_epoch"),
        cwd=optional_text(value, "cwd"),
    )


def _parse_antigravity(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    source_session_id: str | None,
    source_diagnostics: tuple[SourceDiagnostic, ...],
    prior_state: object,
) -> ParseResult:
    if source_session_id is None:
        raise ValueError(
            "Antigravity sources derive their session ID from the directory"
        )
    if prior_state is not None and not isinstance(prior_state, AntigravityParserState):
        raise TypeError("invalid Antigravity continuation state")
    return parse_antigravity_session(
        envelopes,
        context=AntigravitySessionContext(source_session_id=source_session_id),
        source_diagnostics=source_diagnostics,
        prior_state=prior_state,
    )


def _parse_claude(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    source_session_id: str | None,
    source_diagnostics: tuple[SourceDiagnostic, ...],
    prior_state: object,
) -> ParseResult:
    del source_diagnostics
    if source_session_id is None:
        raise ValueError("Claude sources derive their session ID from the file stem")
    if prior_state is not None and not isinstance(prior_state, ClaudeParserState):
        raise TypeError("invalid Claude continuation state")
    return parse_claude_session(
        envelopes,
        context=ClaudeSessionContext(source_session_id=source_session_id),
        prior_state=prior_state,
    )


def _parse_codex(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    source_session_id: str | None,
    source_diagnostics: tuple[SourceDiagnostic, ...],
    prior_state: object,
) -> ParseResult:
    del source_session_id
    if prior_state is not None and not isinstance(prior_state, CodexParserState):
        raise TypeError("invalid Codex continuation state")
    return parse_codex_session(
        envelopes,
        context=CodexSessionContext(),
        source_diagnostics=source_diagnostics,
        prior_state=prior_state,
    )


def _claude_default_roots(home: Path) -> tuple[Path, ...]:
    ponytail = home / ".claude-ponytail" / "projects"
    return (
        home / ".claude" / "projects",
        *((ponytail,) if ponytail.is_dir() else ()),
    )


def _codex_default_roots(home: Path) -> tuple[Path, ...]:
    ponytail = home / ".codex-ponytail" / "sessions"
    return (
        home / ".codex" / "sessions",
        *((ponytail,) if ponytail.is_dir() else ()),
    )


def _antigravity_default_roots(home: Path) -> tuple[Path, ...]:
    brain = home / ".gemini" / "antigravity-cli" / "brain"
    return (brain,) if brain.is_dir() else ()


def _antigravity_state_cwd(state: object) -> str | None:
    return state.cwd if isinstance(state, AntigravityParserState) else None


def _antigravity_cwd_established(parsed: ParseResult) -> bool:
    return isinstance(parsed, AntigravityParseResult) and parsed.cwd_established


def _no_state_cwd(state: object) -> None:
    del state


def _never_establishes_cwd(parsed: ParseResult) -> bool:
    del parsed
    return False


def _antigravity_source_session_id(source_file_relative: Path) -> str:
    return source_file_relative.parts[0]


def _antigravity_scan_candidate(
    source_file_relative: Path, locator: NativeLocator
) -> bool:
    return source_file_relative.parts[0] == locator.source_session_id


def _ordinal_record_matches(locator: NativeLocator, envelope: RecordEnvelope) -> bool:
    return (
        envelope.record_ordinal == locator.key
        and envelope.source_digest == locator.record_digest
    )


def _decoded_object(envelope: RecordEnvelope) -> dict[str, object] | None:
    try:
        payload = json.loads(envelope.raw_bytes)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    return payload if is_json_object(payload) else None


def _claude_record_matches(locator: NativeLocator, envelope: RecordEnvelope) -> bool:
    payload = _decoded_object(envelope)
    return payload is not None and payload.get("uuid") == locator.key


def _codex_record_matches(locator: NativeLocator, envelope: RecordEnvelope) -> bool:
    if locator.key_kind is LocatorKeyKind.ORDINAL:
        return _ordinal_record_matches(locator, envelope)
    payload = _decoded_object(envelope)
    if payload is None:
        return False
    nested = payload.get("payload")
    return is_json_object(nested) and nested.get("id") == locator.key


def _claude_scan_candidate(source_file_relative: Path, locator: NativeLocator) -> bool:
    return source_file_relative.stem == locator.source_session_id


def _codex_scan_candidate(source_file_relative: Path, locator: NativeLocator) -> bool:
    del source_file_relative, locator
    return True


def _codex_source_session_id(source_file_relative: Path) -> None:
    del source_file_relative


def _claude_source_session_id(source_file_relative: Path) -> str:
    return source_file_relative.stem


def _codex_parsed_session_id(parsed: ParseResult) -> str | None:
    return getattr(parsed, "source_session_id", None)


def _claude_parsed_session_id(parsed: ParseResult) -> str | None:
    del parsed
    return None


def _recognizes_locator(parsed: ParseResult, locator: NativeLocator) -> bool:
    wanted = format_locator(locator)
    return any(
        format_locator(alias.locator) == wanted
        for message in parsed.messages
        for alias in message.identity.physical_aliases
    )


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    """Every provider-specific decision the storage layer must look up."""

    provider: Provider
    plural_variable: str
    singular_variable: str | None
    default_roots: Callable[[Path], tuple[Path, ...]]
    discover: DiscoverEntry
    locator_key_kinds: frozenset[LocatorKeyKind]
    parser_state_version: int
    initial_state: Callable[[], object]
    serialize_state: Callable[[object], dict[str, object]]
    deserialize_state: Callable[[object], object]
    source_session_id: Callable[[Path], str | None]
    parse: ParseEntry
    unsupported_codes: frozenset[StrEnum]
    scan_unsupported_codes: frozenset[StrEnum]
    skippable_codes: frozenset[StrEnum]
    repaired_codes: frozenset[StrEnum]
    inspect_artifacts: bool
    admission: Callable[[bytes], AdmissionDecision] | None
    raw_record_matches: Callable[[NativeLocator, RecordEnvelope], bool]
    scan_candidate: Callable[[Path, NativeLocator], bool]
    parsed_session_id: Callable[[ParseResult], str | None]
    state_cwd: Callable[[object], str | None]
    cwd_established: Callable[[ParseResult], bool]

    def admits(self, envelopes: tuple[RecordEnvelope, ...]) -> bool:
        """Return whether a parse starting at ordinal 0 may proceed."""
        if self.admission is None or not envelopes or envelopes[0].record_ordinal != 0:
            return True
        return (
            self.admission(envelopes[0].raw_bytes).outcome is AdmissionOutcome.ADMITTED
        )

    def _unsupported_at_target(
        self,
        parsed: ParseResult,
        envelopes: tuple[RecordEnvelope, ...],
        locator: NativeLocator,
    ) -> bool:
        parsed_session = self.parsed_session_id(parsed)
        if parsed_session is not None and parsed_session != locator.source_session_id:
            return False
        target_ordinals = {
            envelope.record_ordinal
            for envelope in envelopes
            if self.raw_record_matches(locator, envelope)
        }
        return any(
            diagnostic.record_ordinal in target_ordinals
            and diagnostic.code in self.scan_unsupported_codes
            for diagnostic in parsed.diagnostics
        )

    def scan_unindexed(
        self,
        path: Path,
        *,
        source_file_relative: Path,
        locator: NativeLocator,
    ) -> ScanEvidence:
        """Scan one discovered source for a locator the index does not hold."""
        if not self.scan_candidate(source_file_relative, locator):
            return ScanEvidence()
        try:
            target_size = path.stat().st_size
        except OSError:
            return ScanEvidence(incomplete=True)
        offset, ordinal, source_line = 0, 0, 1
        state: object = None
        unsupported = False
        session_id = self.source_session_id(source_file_relative)
        while offset < target_size:
            batch = read_bounded_jsonl(
                path,
                source_file_relative=source_file_relative,
                target_size=target_size,
                start_byte_offset=offset,
                next_record_ordinal=ordinal,
                next_source_line=source_line,
            )
            if offset == 0 and not self.admits(batch.envelopes):
                return ScanEvidence()
            parsed = self.parse(
                batch.envelopes,
                source_session_id=session_id,
                source_diagnostics=batch.diagnostics,
                prior_state=state,
            )
            if _recognizes_locator(parsed, locator):
                return ScanEvidence(recognized=True)
            unsupported = unsupported or self._unsupported_at_target(
                parsed, batch.envelopes, locator
            )
            state = parsed.next_state
            if batch.stop_reason is not BoundedReadStopReason.BATCH_LIMIT_REACHED:
                incomplete = (
                    batch.stop_reason is not BoundedReadStopReason.TARGET_REACHED
                )
                return ScanEvidence(unsupported=unsupported, incomplete=incomplete)
            if batch.next_source_byte_offset <= offset:
                return ScanEvidence(unsupported=unsupported, incomplete=True)
            offset = batch.next_source_byte_offset
            ordinal = batch.next_record_ordinal
            source_line = batch.next_source_line
        return ScanEvidence(unsupported=unsupported)


_CLAUDE = ProviderAdapter(
    provider=Provider.CLAUDE,
    plural_variable="CC_SEARCH_CLAUDE_ROOTS",
    singular_variable="CC_SEARCH_CLAUDE_ROOT",
    default_roots=_claude_default_roots,
    discover=discover_claude_sources,
    locator_key_kinds=permitted_locator_key_kinds(Provider.CLAUDE),
    parser_state_version=7,
    initial_state=ClaudeParserState,
    serialize_state=_serialize_claude_state,
    deserialize_state=_deserialize_claude_state,
    source_session_id=_claude_source_session_id,
    parse=_parse_claude,
    unsupported_codes=frozenset(
        {
            ClaudeDiagnosticCode.MISSING_MESSAGE,
            ClaudeDiagnosticCode.NON_OBJECT_MESSAGE,
            ClaudeDiagnosticCode.UNKNOWN_ROLE,
            ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
            ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD,
            ClaudeDiagnosticCode.MISSING_MESSAGE_UUID,
        }
    ),
    scan_unsupported_codes=frozenset(
        {
            ClaudeDiagnosticCode.MALFORMED_JSON,
            ClaudeDiagnosticCode.MISSING_MESSAGE,
            ClaudeDiagnosticCode.NON_OBJECT_MESSAGE,
            ClaudeDiagnosticCode.UNKNOWN_ROLE,
            ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
            ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD,
            ClaudeDiagnosticCode.MISSING_MESSAGE_UUID,
            ClaudeDiagnosticCode.INVALID_UNICODE,
        }
    ),
    skippable_codes=frozenset(
        {
            ClaudeDiagnosticCode.MALFORMED_JSON,
            ClaudeDiagnosticCode.INVALID_ENCODING,
            ClaudeDiagnosticCode.INVALID_UNICODE,
        }
    ),
    repaired_codes=frozenset({ClaudeDiagnosticCode.REPAIRED_UNICODE}),
    inspect_artifacts=True,
    admission=None,
    raw_record_matches=_claude_record_matches,
    scan_candidate=_claude_scan_candidate,
    parsed_session_id=_claude_parsed_session_id,
    state_cwd=_no_state_cwd,
    cwd_established=_never_establishes_cwd,
)

_CODEX = ProviderAdapter(
    provider=Provider.CODEX,
    plural_variable="CC_SEARCH_CODEX_ROOTS",
    singular_variable="CC_SEARCH_CODEX_ROOT",
    default_roots=_codex_default_roots,
    discover=discover_codex_sources,
    locator_key_kinds=permitted_locator_key_kinds(Provider.CODEX),
    parser_state_version=5,
    initial_state=CodexParserState,
    serialize_state=_serialize_codex_state,
    deserialize_state=_deserialize_codex_state,
    source_session_id=_codex_source_session_id,
    parse=_parse_codex,
    unsupported_codes=frozenset(
        {
            CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
            CodexDiagnosticCode.UNKNOWN_ROLE,
            CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
            CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
            CodexDiagnosticCode.UNKNOWN_EVENT,
            CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
            CodexDiagnosticCode.INVALID_PAYLOAD,
            CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
        }
    ),
    scan_unsupported_codes=frozenset(
        {
            CodexDiagnosticCode.MALFORMED_JSON,
            CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
            CodexDiagnosticCode.UNKNOWN_ROLE,
            CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
            CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
            CodexDiagnosticCode.UNKNOWN_EVENT,
            CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
            CodexDiagnosticCode.INVALID_PAYLOAD,
            CodexDiagnosticCode.INVALID_UNICODE,
            CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
        }
    ),
    skippable_codes=frozenset(
        {
            CodexDiagnosticCode.MALFORMED_JSON,
            CodexDiagnosticCode.INVALID_ENCODING,
            CodexDiagnosticCode.INVALID_UNICODE,
        }
    ),
    repaired_codes=frozenset({CodexDiagnosticCode.REPAIRED_UNICODE}),
    inspect_artifacts=True,
    admission=None,
    raw_record_matches=_codex_record_matches,
    scan_candidate=_codex_scan_candidate,
    parsed_session_id=_codex_parsed_session_id,
    state_cwd=_no_state_cwd,
    cwd_established=_never_establishes_cwd,
)

_ANTIGRAVITY = ProviderAdapter(
    provider=Provider.ANTIGRAVITY,
    plural_variable="CC_SEARCH_ANTIGRAVITY_ROOTS",
    singular_variable=None,
    default_roots=_antigravity_default_roots,
    discover=discover_antigravity_sources,
    locator_key_kinds=permitted_locator_key_kinds(Provider.ANTIGRAVITY),
    parser_state_version=1,
    initial_state=AntigravityParserState,
    serialize_state=_serialize_antigravity_state,
    deserialize_state=_deserialize_antigravity_state,
    source_session_id=_antigravity_source_session_id,
    parse=_parse_antigravity,
    unsupported_codes=frozenset(
        {
            AntigravityDiagnosticCode.UNKNOWN_RECORD_PAIR,
            AntigravityDiagnosticCode.MISSING_REQUIRED_KEY,
            AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
            AntigravityDiagnosticCode.MISSING_USER_REQUEST,
            AntigravityDiagnosticCode.INVALID_CREATED_AT,
        }
    ),
    scan_unsupported_codes=frozenset(
        {
            AntigravityDiagnosticCode.MALFORMED_JSON,
            AntigravityDiagnosticCode.INVALID_UNICODE,
            AntigravityDiagnosticCode.UNKNOWN_RECORD_PAIR,
            AntigravityDiagnosticCode.MISSING_REQUIRED_KEY,
            AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
            AntigravityDiagnosticCode.MISSING_USER_REQUEST,
            AntigravityDiagnosticCode.INVALID_CREATED_AT,
        }
    ),
    skippable_codes=frozenset(
        {
            AntigravityDiagnosticCode.MALFORMED_JSON,
            AntigravityDiagnosticCode.INVALID_ENCODING,
            AntigravityDiagnosticCode.INVALID_UNICODE,
        }
    ),
    repaired_codes=frozenset(
        {
            AntigravityDiagnosticCode.REPAIRED_UNICODE,
            AntigravityDiagnosticCode.TRUNCATED_PROSE,
        }
    ),
    inspect_artifacts=False,
    admission=admit_antigravity_session,
    raw_record_matches=_ordinal_record_matches,
    scan_candidate=_antigravity_scan_candidate,
    parsed_session_id=_claude_parsed_session_id,
    state_cwd=_antigravity_state_cwd,
    cwd_established=_antigravity_cwd_established,
)

_ADAPTERS: dict[Provider, ProviderAdapter] = {
    Provider.CLAUDE: _CLAUDE,
    Provider.CODEX: _CODEX,
    Provider.ANTIGRAVITY: _ANTIGRAVITY,
}


def provider_adapter(provider: Provider) -> ProviderAdapter:
    """Return the registered adapter, raising ``KeyError`` when none exists."""
    return _ADAPTERS[provider]


def provider_adapters() -> tuple[ProviderAdapter, ...]:
    """Return every registered adapter in ``Provider`` declaration order."""
    return tuple(_ADAPTERS[provider] for provider in Provider if provider in _ADAPTERS)


def _configured_raw_roots(
    adapter: ProviderAdapter, values: Mapping[str, str], home: Path
) -> tuple[str, ...]:
    if adapter.plural_variable in values:
        raw_values = tuple(values[adapter.plural_variable].split(os.pathsep))
        if not raw_values or any(not value.strip() for value in raw_values):
            raise ValueError(f"{adapter.plural_variable} must contain nonempty paths")
        return raw_values
    if adapter.singular_variable is not None and adapter.singular_variable in values:
        raw = values[adapter.singular_variable]
        if not raw.strip():
            raise ValueError(
                f"{adapter.singular_variable} must contain a nonempty path"
            )
        return (raw,)
    return tuple(str(path) for path in adapter.default_roots(home))


def _expand_root(raw_value: str, home: Path) -> Path:
    if raw_value == "~":
        return home
    if raw_value.startswith(f"~{os.sep}"):
        return home / raw_value[2:]
    return Path(raw_value)


def configured_source_roots(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[ConfiguredSourceRoot, ...]:
    """Resolve plural roots, singular compatibility, or present local defaults."""
    values = os.environ if environ is None else environ
    resolved_home = (Path.home() if home is None else home).resolve()
    roots: list[ConfiguredSourceRoot] = []
    for adapter in provider_adapters():
        seen: set[Path] = set()
        for raw_value in _configured_raw_roots(adapter, values, resolved_home):
            resolved = _expand_root(raw_value, resolved_home).resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            roots.append(
                ConfiguredSourceRoot(
                    provider=adapter.provider,
                    path=resolved,
                    source_root_id=source_root_id(adapter.provider, resolved),
                )
            )
    return tuple(roots)
