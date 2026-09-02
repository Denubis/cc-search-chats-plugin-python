"""Exact locator resolution with direct native-source verification."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg  # noqa: TC002  # keep public annotations runtime-resolvable

from cc_search_chats.core.identity import (
    NativeLocator,
    ResolutionStatus,
    format_locator,
    parse_locator,
    validate_source_file_relative,
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
    RecordEnvelope,
    SourceDiagnosticCode,
    discover_claude_sources,
    discover_codex_sources,
    read_bounded_jsonl,
)
from cc_search_chats.storage.postgresql.index import (
    StoredAlias,
    StoredMessage,
    resolve_messages,
)


@dataclass(frozen=True, slots=True)
class ExactResolution:
    """One named terminal result for an exact locator request."""

    locator: str
    status: ResolutionStatus
    messages: tuple[StoredMessage, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _ScanEvidence:
    recognized: bool = False
    unsupported: bool = False
    incomplete: bool = False


_UNAVAILABLE_DIAGNOSTICS = {
    SourceDiagnosticCode.MISSING_ROOT,
    SourceDiagnosticCode.UNREADABLE_ROOT,
    SourceDiagnosticCode.UNREADABLE_PATH,
    SourceDiagnosticCode.UNREADABLE_SOURCE,
}
_UNSUPPORTED_CLAUDE_DIAGNOSTICS = {
    ClaudeDiagnosticCode.MALFORMED_JSON,
    ClaudeDiagnosticCode.MISSING_MESSAGE,
    ClaudeDiagnosticCode.NON_OBJECT_MESSAGE,
    ClaudeDiagnosticCode.UNKNOWN_ROLE,
    ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
    ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD,
    ClaudeDiagnosticCode.MISSING_MESSAGE_UUID,
    ClaudeDiagnosticCode.INVALID_UNICODE,
}
_UNSUPPORTED_CODEX_DIAGNOSTICS = {
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


def _raw_record_matches(locator: NativeLocator, envelope: RecordEnvelope) -> bool:
    if locator.key_kind.value == "ordinal":
        return (
            envelope.record_ordinal == locator.key
            and envelope.source_digest == locator.record_digest
        )
    try:
        payload = json.loads(envelope.raw_bytes)
    except json.JSONDecodeError, UnicodeDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    if locator.provider.value == "claude":
        return payload.get("uuid") == locator.key
    nested = payload.get("payload")
    return isinstance(nested, dict) and nested.get("id") == locator.key


def _scan_claude_source(
    path: Path,
    *,
    source_file_relative: Path,
    locator: NativeLocator,
) -> _ScanEvidence:
    try:
        target_size = path.stat().st_size
    except OSError:
        return _ScanEvidence(incomplete=True)
    offset, ordinal, source_line = 0, 0, 1
    state: ClaudeParserState | None = None
    unsupported = False
    while offset < target_size:
        batch = read_bounded_jsonl(
            path,
            source_file_relative=source_file_relative,
            target_size=target_size,
            start_byte_offset=offset,
            next_record_ordinal=ordinal,
            next_source_line=source_line,
        )
        target_ordinals = {
            envelope.record_ordinal
            for envelope in batch.envelopes
            if _raw_record_matches(locator, envelope)
        }
        parsed = parse_claude_session(
            batch.envelopes,
            context=ClaudeSessionContext(source_session_id=source_file_relative.stem),
            prior_state=state,
        )
        if any(
            format_locator(alias.locator) == format_locator(locator)
            for message in parsed.messages
            for alias in message.identity.physical_aliases
        ):
            return _ScanEvidence(recognized=True)
        unsupported = unsupported or any(
            diagnostic.record_ordinal in target_ordinals
            and diagnostic.code in _UNSUPPORTED_CLAUDE_DIAGNOSTICS
            for diagnostic in parsed.diagnostics
        )
        state = parsed.next_state
        next_offset = batch.next_source_byte_offset
        ordinal = batch.next_record_ordinal
        source_line = batch.next_source_line
        if batch.stop_reason is BoundedReadStopReason.BATCH_LIMIT_REACHED:
            if next_offset <= offset:
                return _ScanEvidence(unsupported=unsupported, incomplete=True)
            offset = next_offset
            continue
        incomplete = batch.stop_reason is not BoundedReadStopReason.TARGET_REACHED
        return _ScanEvidence(unsupported=unsupported, incomplete=incomplete)
    return _ScanEvidence(unsupported=unsupported)


def _scan_codex_source(
    path: Path,
    *,
    source_file_relative: Path,
    locator: NativeLocator,
) -> _ScanEvidence:
    try:
        target_size = path.stat().st_size
    except OSError:
        return _ScanEvidence(incomplete=True)
    offset, ordinal, source_line = 0, 0, 1
    state: CodexParserState | None = None
    unsupported = False
    while offset < target_size:
        batch = read_bounded_jsonl(
            path,
            source_file_relative=source_file_relative,
            target_size=target_size,
            start_byte_offset=offset,
            next_record_ordinal=ordinal,
            next_source_line=source_line,
        )
        target_ordinals = {
            envelope.record_ordinal
            for envelope in batch.envelopes
            if _raw_record_matches(locator, envelope)
        }
        parsed = parse_codex_session(
            batch.envelopes,
            context=CodexSessionContext(),
            source_diagnostics=batch.diagnostics,
            prior_state=state,
        )
        if any(
            format_locator(alias.locator) == format_locator(locator)
            for message in parsed.messages
            for alias in message.identity.physical_aliases
        ):
            return _ScanEvidence(recognized=True)
        if parsed.source_session_id == locator.source_session_id:
            unsupported = unsupported or any(
                diagnostic.record_ordinal in target_ordinals
                and diagnostic.code in _UNSUPPORTED_CODEX_DIAGNOSTICS
                for diagnostic in parsed.diagnostics
            )
        state = parsed.next_state
        next_offset = batch.next_source_byte_offset
        ordinal = batch.next_record_ordinal
        source_line = batch.next_source_line
        if batch.stop_reason is BoundedReadStopReason.BATCH_LIMIT_REACHED:
            if next_offset <= offset:
                return _ScanEvidence(unsupported=unsupported, incomplete=True)
            offset = next_offset
            continue
        incomplete = batch.stop_reason is not BoundedReadStopReason.TARGET_REACHED
        return _ScanEvidence(unsupported=unsupported, incomplete=incomplete)
    return _ScanEvidence(unsupported=unsupported)


def _scan_unindexed_locator(
    locator: NativeLocator,
    source_roots: tuple[ConfiguredSourceRoot, ...],
) -> ResolutionStatus:
    matching_roots = tuple(
        root for root in source_roots if root.provider is locator.provider
    )
    if not matching_roots:
        return ResolutionStatus.SOURCE_UNAVAILABLE
    evidence: list[_ScanEvidence] = []
    incomplete = False
    for root in matching_roots:
        discovery = (
            discover_claude_sources(root.path, inspect_content=False)
            if locator.provider.value == "claude"
            else discover_codex_sources(root.path, inspect_content=False)
        )
        incomplete = incomplete or any(
            diagnostic.code in _UNAVAILABLE_DIAGNOSTICS
            for diagnostic in discovery.diagnostics
        )
        for source in discovery.sources:
            if (
                locator.provider.value == "claude"
                and source.source_file_relative.stem != locator.source_session_id
            ):
                continue
            scanned = (
                _scan_claude_source(
                    source.path,
                    source_file_relative=source.source_file_relative,
                    locator=locator,
                )
                if locator.provider.value == "claude"
                else _scan_codex_source(
                    source.path,
                    source_file_relative=source.source_file_relative,
                    locator=locator,
                )
            )
            evidence.append(scanned)
    if any(value.recognized for value in evidence):
        return ResolutionStatus.STALE_INDEX
    if any(value.unsupported for value in evidence):
        return ResolutionStatus.UNSUPPORTED_PROVIDER_SCHEMA
    if incomplete or any(value.incomplete for value in evidence):
        return ResolutionStatus.SOURCE_UNAVAILABLE
    return ResolutionStatus.NO_MATCH


def _verify_alias(
    alias: StoredAlias,
    *,
    provider: str,
    roots_by_id: dict[str, ConfiguredSourceRoot],
) -> Literal[
    ResolutionStatus.RESOLVED,
    ResolutionStatus.SOURCE_UNAVAILABLE,
    ResolutionStatus.STALE_SOURCE,
    ResolutionStatus.STALE_INDEX,
]:
    root = roots_by_id.get(alias.source_root_id)
    if root is None or root.provider.value != provider:
        return ResolutionStatus.SOURCE_UNAVAILABLE
    relative = Path(alias.source_file_relative)
    try:
        validate_source_file_relative(relative)
    except ValueError:
        return ResolutionStatus.STALE_INDEX
    source = root.path / relative
    try:
        resolved_source = source.resolve(strict=True)
        if not resolved_source.is_relative_to(root.path):
            return ResolutionStatus.STALE_INDEX
        with resolved_source.open("rb") as handle:
            handle.seek(alias.source_byte_offset)
            raw_bytes = handle.read(alias.raw_byte_length)
    except OSError:
        return ResolutionStatus.SOURCE_UNAVAILABLE
    if len(raw_bytes) != alias.raw_byte_length:
        return ResolutionStatus.STALE_SOURCE
    if hashlib.sha256(raw_bytes).hexdigest() != alias.source_digest:
        return ResolutionStatus.STALE_SOURCE
    return ResolutionStatus.RESOLVED


def _verify_database_resolution(
    locator: str,
    messages: tuple[StoredMessage, ...],
    *,
    roots_by_id: dict[str, ConfiguredSourceRoot],
) -> ExactResolution:
    identities = {
        (message.provider, message.source_session_id, message.logical_message_id)
        for message in messages
    }
    if len(identities) > 1:
        return ExactResolution(
            locator,
            ResolutionStatus.MULTIPLE_MATCHES,
            messages,
            "locator matched more than one logical message",
        )
    target_aliases = tuple(
        alias
        for message in messages
        for alias in message.physical_aliases
        if alias.locator == locator
    )
    if not target_aliases:
        return ExactResolution(
            locator,
            ResolutionStatus.STALE_INDEX,
            messages,
            "indexed locator has no matching physical alias",
        )
    provider = messages[0].provider
    statuses = tuple(
        _verify_alias(alias, provider=provider, roots_by_id=roots_by_id)
        for alias in target_aliases
    )
    if ResolutionStatus.RESOLVED in statuses:
        return ExactResolution(locator, ResolutionStatus.RESOLVED, messages)
    if ResolutionStatus.STALE_SOURCE in statuses:
        return ExactResolution(
            locator,
            ResolutionStatus.STALE_SOURCE,
            messages,
            "native record bytes no longer match the indexed digest",
        )
    if ResolutionStatus.STALE_INDEX in statuses:
        return ExactResolution(
            locator,
            ResolutionStatus.STALE_INDEX,
            messages,
            "indexed physical source coordinate is invalid",
        )
    return ExactResolution(
        locator,
        ResolutionStatus.SOURCE_UNAVAILABLE,
        messages,
        "no matching configured native source is readable",
    )


def resolve_exact_messages(
    connection: psycopg.Connection,
    locators: tuple[str, ...],
    *,
    source_roots: tuple[ConfiguredSourceRoot, ...],
) -> tuple[ExactResolution, ...]:
    """Resolve locators in one database read, then verify native record bytes."""
    parsed = tuple(parse_locator(locator) for locator in locators)
    valid = tuple(
        locator
        for locator, value in zip(locators, parsed, strict=True)
        if isinstance(value, NativeLocator)
    )
    database = iter(resolve_messages(connection, valid))
    roots_by_id = {root.source_root_id: root for root in source_roots}
    results: list[ExactResolution] = []
    for locator, value in zip(locators, parsed, strict=True):
        if not isinstance(value, NativeLocator):
            results.append(ExactResolution(locator, ResolutionStatus.MALFORMED_LOCATOR))
            continue
        resolution = next(database)
        if not resolution.messages:
            results.append(
                ExactResolution(
                    locator,
                    _scan_unindexed_locator(value, source_roots),
                )
            )
            continue
        results.append(
            _verify_database_resolution(
                locator,
                resolution.messages,
                roots_by_id=roots_by_id,
            )
        )
    return tuple(results)
