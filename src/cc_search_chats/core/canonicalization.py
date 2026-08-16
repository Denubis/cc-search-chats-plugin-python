"""Pure Codex physical-alias canonicalization."""

# pattern: Functional Core

import hashlib
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    MessageIdentity,
    NativeMessage,
    PhysicalAlias,
    SessionKind,
    SubmittedBy,
    format_locator,
)

type _CompatibilityKey = tuple[str, Path, SessionKind, int, str, ContentClass, str]
type _VisibilityBoundary = tuple[str, Path, int]


class CodexRecordFamily(StrEnum):
    """Physical Codex record families relevant to duplicate pairing."""

    RESPONSE_MESSAGE = "response_message"
    EVENT_MESSAGE = "event_message"
    TOOL = "tool"
    CANONICAL = "canonical"


class CanonicalizationDiagnosticCode(StrEnum):
    """Closed conservative pairing outcomes."""

    NO_COMPATIBLE_PARTNER = "no_compatible_partner"
    MULTIPLE_COMPATIBLE_PARTNERS = "multiple_compatible_partners"


@dataclass(frozen=True, slots=True)
class CanonicalizationDiagnostic:
    """Why one physical duplicate candidate was not paired."""

    code: CanonicalizationDiagnosticCode
    physical_aliases: tuple[PhysicalAlias, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class PhysicalMessageCandidate:
    """One parsed Codex occurrence before conservative pairing."""

    message: NativeMessage
    record_family: CodexRecordFamily

    @property
    def text_digest(self) -> str:
        """Return the exact UTF-8 text digest used only for pairing."""
        return hashlib.sha256(self.message.text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalizationResult:
    """Logical messages and every conservative non-pairing diagnostic."""

    messages: tuple[NativeMessage, ...]
    diagnostics: tuple[CanonicalizationDiagnostic, ...]


def _alias_key(alias: PhysicalAlias) -> tuple[str, int, int, str]:
    """Order physical occurrences independently of caller iteration order."""
    return (
        alias.source_file_relative.as_posix(),
        alias.record_ordinal,
        alias.source_byte_offset,
        format_locator(alias.locator),
    )


def _candidate_key(
    candidate: PhysicalMessageCandidate,
) -> tuple[str, str, int, int, str]:
    """Order candidates by provider session and earliest physical occurrence."""
    alias = min(candidate.message.identity.physical_aliases, key=_alias_key)
    return (
        alias.locator.source_session_id,
        alias.source_file_relative.as_posix(),
        alias.record_ordinal,
        alias.source_byte_offset,
        format_locator(alias.locator),
    )


def _opposite_families(left: CodexRecordFamily, right: CodexRecordFamily) -> bool:
    """Return whether two records are the recognized projection pair."""
    return {left, right} == {
        CodexRecordFamily.RESPONSE_MESSAGE,
        CodexRecordFamily.EVENT_MESSAGE,
    }


def _same_physical_source(
    left: PhysicalMessageCandidate, right: PhysicalMessageCandidate
) -> bool:
    """Require candidate aliases to originate in the same native file."""
    left_alias = min(left.message.identity.physical_aliases, key=_alias_key)
    right_alias = min(right.message.identity.physical_aliases, key=_alias_key)
    return left_alias.source_file_relative == right_alias.source_file_relative


def _native_timestamp(value: object) -> datetime | None:
    """Parse one timezone-aware native timestamp without inventing defaults."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed


def is_valid_native_timestamp(value: object) -> bool:
    """Return whether a native timestamp is timezone-aware ISO-8601 text."""
    return _native_timestamp(value) is not None


def _timestamps_follow_physical_order(
    left: PhysicalMessageCandidate, right: PhysicalMessageCandidate
) -> bool:
    """Require valid timestamps that do not reverse native record order."""
    left_timestamp = _native_timestamp(left.message.timestamp)
    right_timestamp = _native_timestamp(right.message.timestamp)
    if left_timestamp is None or right_timestamp is None:
        return False
    if _candidate_key(left) <= _candidate_key(right):
        return left_timestamp <= right_timestamp
    return right_timestamp <= left_timestamp


def _base_compatible(
    left: PhysicalMessageCandidate, right: PhysicalMessageCandidate
) -> bool:
    """Check exact pair facts before cardinality and intervening-message gates."""
    left_message = left.message
    right_message = right.message
    left_locator = left_message.identity.canonical_locator
    right_locator = right_message.identity.canonical_locator
    return (
        _opposite_families(left.record_family, right.record_family)
        and left_locator.source_session_id == right_locator.source_session_id
        and _same_physical_source(left, right)
        and left_message.session_kind is right_message.session_kind
        and left_message.conversation_epoch == right_message.conversation_epoch
        and left_message.role == right_message.role
        and left_message.content_class is ContentClass.PROSE
        and right_message.content_class is ContentClass.PROSE
        and left.text_digest == right.text_digest
        and _timestamps_follow_physical_order(left, right)
    )


def _compatibility_key(candidate: PhysicalMessageCandidate) -> _CompatibilityKey:
    """Return exact pair facts that can exclude unrelated candidates."""
    message = candidate.message
    alias = min(message.identity.physical_aliases, key=_alias_key)
    return (
        message.identity.canonical_locator.source_session_id,
        alias.source_file_relative,
        message.session_kind,
        message.conversation_epoch,
        message.role,
        message.content_class,
        message.text,
    )


def _compatibility_pairs(
    candidates: tuple[PhysicalMessageCandidate, ...],
) -> Iterator[tuple[PhysicalMessageCandidate, PhysicalMessageCandidate]]:
    """Plan only cross-family comparisons within exact-fact collision buckets."""
    buckets: dict[
        _CompatibilityKey,
        tuple[list[PhysicalMessageCandidate], list[PhysicalMessageCandidate]],
    ] = {}
    for candidate in candidates:
        response_candidates, event_candidates = buckets.setdefault(
            _compatibility_key(candidate), ([], [])
        )
        if candidate.record_family is CodexRecordFamily.RESPONSE_MESSAGE:
            response_candidates.append(candidate)
        elif candidate.record_family is CodexRecordFamily.EVENT_MESSAGE:
            event_candidates.append(candidate)
    for response_candidates, event_candidates in buckets.values():
        for response in response_candidates:
            for event in event_candidates:
                yield response, event


def _compatible_partners(
    candidates: tuple[PhysicalMessageCandidate, ...],
) -> dict[int, tuple[PhysicalMessageCandidate, ...]]:
    """Index symmetric compatible partners after exact-fact bucketing."""
    mutable_partners = {id(candidate): [] for candidate in candidates}
    for response, event in _compatibility_pairs(candidates):
        if _base_compatible(response, event):
            mutable_partners[id(response)].append(event)
            mutable_partners[id(event)].append(response)
    return {
        candidate_id: tuple(candidate_partners)
        for candidate_id, candidate_partners in mutable_partners.items()
    }


def _visibility_boundary(candidate: PhysicalMessageCandidate) -> _VisibilityBoundary:
    """Return the exact boundary used by the visible-message separation gate."""
    alias = min(candidate.message.identity.physical_aliases, key=_alias_key)
    return (
        alias.locator.source_session_id,
        alias.source_file_relative,
        candidate.message.conversation_epoch,
    )


def _visible_ordinals_by_boundary(
    candidates: tuple[PhysicalMessageCandidate, ...],
) -> dict[_VisibilityBoundary, tuple[int, ...]]:
    """Index sorted visible ordinals once for deterministic range queries."""
    mutable_ordinals: dict[_VisibilityBoundary, list[int]] = {}
    for candidate in candidates:
        if candidate.message.content_class is not ContentClass.PROSE:
            continue
        alias = min(candidate.message.identity.physical_aliases, key=_alias_key)
        mutable_ordinals.setdefault(_visibility_boundary(candidate), []).append(
            alias.record_ordinal
        )
    return {
        boundary: tuple(sorted(ordinals))
        for boundary, ordinals in mutable_ordinals.items()
    }


def _has_intervening_visible_message(
    left: PhysicalMessageCandidate,
    right: PhysicalMessageCandidate,
    visible_ordinals: dict[_VisibilityBoundary, tuple[int, ...]],
) -> bool:
    """Reject a pair with a visible ordinal strictly between its occurrences."""
    left_alias = min(left.message.identity.physical_aliases, key=_alias_key)
    right_alias = min(right.message.identity.physical_aliases, key=_alias_key)
    lower, upper = sorted((left_alias.record_ordinal, right_alias.record_ordinal))
    ordinals = visible_ordinals.get(_visibility_boundary(left), ())
    return bisect_right(ordinals, lower) < bisect_left(ordinals, upper)


def _earliest_alias(aliases: tuple[PhysicalAlias, ...]) -> PhysicalAlias:
    """Freeze canonical identity at the earliest physical occurrence."""
    return min(aliases, key=_alias_key)


def codex_logical_message_id(canonical_alias: PhysicalAlias) -> str:
    """Derive one stable Codex logical ID from its canonical physical alias."""
    if canonical_alias.locator.key_kind is LocatorKeyKind.ID:
        return str(canonical_alias.locator.key)
    return f"record-{canonical_alias.record_ordinal}-{canonical_alias.source_digest}"


def _merge_messages(
    candidates: tuple[PhysicalMessageCandidate, ...],
) -> NativeMessage:
    """Merge mutually unique physical candidates without losing evidence."""
    aliases = tuple(
        sorted(
            (
                alias
                for candidate in candidates
                for alias in candidate.message.identity.physical_aliases
            ),
            key=_alias_key,
        )
    )
    canonical_alias = _earliest_alias(aliases)
    representative_candidate = next(
        candidate
        for candidate in candidates
        if canonical_alias in candidate.message.identity.physical_aliases
    )
    representative = representative_candidate.message
    identified = tuple(
        candidate.message
        for candidate in candidates
        if candidate.message.submitted_by is not SubmittedBy.UNKNOWN
    )
    identified_values = {message.submitted_by for message in identified}
    if len(identified_values) == 1:
        submitted_by = next(iter(identified_values))
        evidence = tuple(
            sorted(
                {item for message in identified for item in message.submission_evidence}
            )
        )
        cardinality = max(
            message.submission_match_cardinality for message in identified
        )
    else:
        submitted_by = SubmittedBy.UNKNOWN
        evidence = ()
        cardinality = 0
    return replace(
        representative,
        identity=MessageIdentity(
            logical_message_id=codex_logical_message_id(canonical_alias),
            canonical_locator=canonical_alias.locator,
            physical_aliases=aliases,
        ),
        submitted_by=submitted_by,
        submission_evidence=evidence,
        submission_match_cardinality=cardinality,
    )


def canonicalize_codex_candidates(
    values: tuple[PhysicalMessageCandidate | NativeMessage, ...],
) -> CanonicalizationResult:
    """Conservatively pair event/response projections into logical messages.

    Passing already-canonical ``NativeMessage`` values is an identity operation,
    which makes the normalizer explicitly idempotent.
    """
    candidates = tuple(
        sorted(
            (
                value
                if isinstance(value, PhysicalMessageCandidate)
                else PhysicalMessageCandidate(
                    message=value, record_family=CodexRecordFamily.CANONICAL
                )
                for value in values
            ),
            key=_candidate_key,
        )
    )
    pairable = tuple(
        candidate
        for candidate in candidates
        if candidate.record_family
        in {
            CodexRecordFamily.RESPONSE_MESSAGE,
            CodexRecordFamily.EVENT_MESSAGE,
        }
    )
    partners = _compatible_partners(pairable)
    pairable_ids = set(partners)
    visible_ordinals = _visible_ordinals_by_boundary(candidates)

    paired: set[int] = set()
    messages: list[NativeMessage] = []
    diagnostics: list[CanonicalizationDiagnostic] = []
    for candidate in candidates:
        candidate_id = id(candidate)
        if candidate_id in paired:
            continue
        if candidate_id not in pairable_ids:
            messages.append(candidate.message)
            continue
        compatible = partners[candidate_id]
        if len(compatible) == 1:
            partner = compatible[0]
            reciprocal = partners[id(partner)]
            if (
                len(reciprocal) == 1
                and reciprocal[0] is candidate
                and not _has_intervening_visible_message(
                    candidate, partner, visible_ordinals
                )
            ):
                messages.append(_merge_messages((candidate, partner)))
                paired.update((candidate_id, id(partner)))
                continue
        if len(compatible) > 1 or any(
            len(partners[id(partner)]) > 1 for partner in compatible
        ):
            diagnostic_code = (
                CanonicalizationDiagnosticCode.MULTIPLE_COMPATIBLE_PARTNERS
            )
            detail = "physical candidate has multiple compatible projections"
        else:
            diagnostic_code = CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER
            detail = "physical candidate has no mutually unique adjacent projection"
        diagnostics.append(
            CanonicalizationDiagnostic(
                code=diagnostic_code,
                physical_aliases=candidate.message.identity.physical_aliases,
                detail=detail,
            )
        )
        messages.append(candidate.message)

    return CanonicalizationResult(
        messages=tuple(
            sorted(
                messages,
                key=lambda message: _candidate_key(
                    PhysicalMessageCandidate(
                        message=message, record_family=CodexRecordFamily.CANONICAL
                    )
                ),
            )
        ),
        diagnostics=tuple(diagnostics),
    )
