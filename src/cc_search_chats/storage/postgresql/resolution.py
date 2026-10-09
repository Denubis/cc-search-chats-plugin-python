"""Exact locator resolution with direct native-source verification."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg  # noqa: TC002  # keep public annotations runtime-resolvable

from cc_search_chats.core.identity import (
    NativeLocator,
    ResolutionStatus,
    parse_locator,
    validate_source_file_relative,
)
from cc_search_chats.providers.registry import ScanEvidence, provider_adapter
from cc_search_chats.providers.source_discovery import (
    ConfiguredSourceRoot,
    SourceDiagnosticCode,
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


_UNAVAILABLE_DIAGNOSTICS = {
    SourceDiagnosticCode.MISSING_ROOT,
    SourceDiagnosticCode.UNREADABLE_ROOT,
    SourceDiagnosticCode.UNREADABLE_PATH,
    SourceDiagnosticCode.UNREADABLE_SOURCE,
}


def _scan_unindexed_locator(
    locator: NativeLocator,
    source_roots: tuple[ConfiguredSourceRoot, ...],
) -> ResolutionStatus:
    matching_roots = tuple(
        root for root in source_roots if root.provider is locator.provider
    )
    if not matching_roots:
        return ResolutionStatus.SOURCE_UNAVAILABLE
    adapter = provider_adapter(locator.provider)
    evidence: list[ScanEvidence] = []
    incomplete = False
    for root in matching_roots:
        discovery = adapter.discover(root.path, inspect_content=False)
        incomplete = incomplete or any(
            diagnostic.code in _UNAVAILABLE_DIAGNOSTICS
            for diagnostic in discovery.diagnostics
        )
        evidence.extend(
            adapter.scan_unindexed(
                source.path,
                source_file_relative=source.source_file_relative,
                locator=locator,
            )
            for source in discovery.sources
        )
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
