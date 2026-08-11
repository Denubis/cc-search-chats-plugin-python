"""Provider-neutral native-message identity values.

Canonical identity deliberately contains provider-relative coordinates only.
Provider roots remain an imperative-shell concern so relocating native stores
does not invalidate references.
"""

# pattern: Functional Core

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_CANONICAL_SHA256 = re.compile(r"[0-9a-f]{64}").fullmatch
_CANONICAL_ORDINAL = re.compile(r"(?:0|[1-9][0-9]*)").fullmatch


class Provider(StrEnum):
    """Native chat providers understood by the index."""

    CLAUDE = "claude"
    CODEX = "codex"


class SessionKind(StrEnum):
    """Closed session-origin classification."""

    PRIMARY = "primary"
    AGENT = "agent"
    UNKNOWN = "unknown"


class ContentClass(StrEnum):
    """Searchable native content classes."""

    PROSE = "prose"
    TOOL_NAME = "tool_name"
    TOOL_INPUT = "tool_input"
    TOOL_OUTPUT = "tool_output"


class SubmittedBy(StrEnum):
    """Evidence-based submission classification, separate from vendor role."""

    HUMAN = "human"
    IDENTIFIED_HARNESS = "identified_harness"
    UNKNOWN = "unknown"


class LocatorKeyKind(StrEnum):
    """Provider-specific physical record key kinds."""

    UUID = "uuid"
    ID = "id"
    ORDINAL = "ordinal"


class ResolutionStatus(StrEnum):
    """Terminal exact-resolution outcomes."""

    RESOLVED = "resolved"
    NO_MATCH = "no_match"
    MULTIPLE_MATCHES = "multiple_matches"
    SOURCE_UNAVAILABLE = "source_unavailable"
    STALE_SOURCE = "stale_source"
    STALE_INDEX = "stale_index"
    MALFORMED_LOCATOR = "malformed_locator"
    UNSUPPORTED_PROVIDER_SCHEMA = "unsupported_provider_schema"


def _validate_opaque_identifier(value: str, field_name: str) -> None:
    """Validate a nonempty colon-safe locator component."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")
    if any(delimiter in value for delimiter in (":", "\r", "\n")):
        raise ValueError(f"{field_name} contains a locator delimiter")


def _validate_digest(value: str, field_name: str) -> None:
    """Validate canonical lowercase hexadecimal SHA-256 text."""
    if not isinstance(value, str) or _CANONICAL_SHA256(value) is None:
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class NativeLocator:
    """Provider-qualified physical native-record locator."""

    provider: Provider
    source_session_id: str
    key_kind: LocatorKeyKind
    key: str | int
    record_digest: str | None = None

    def __post_init__(self) -> None:
        """Enforce the provider-specific version-1 locator grammar."""
        _validate_opaque_identifier(self.source_session_id, "source_session_id")

        if self.provider is Provider.CLAUDE:
            if self.key_kind is not LocatorKeyKind.UUID:
                raise ValueError("Claude locators require a uuid key")
        elif self.provider is Provider.CODEX:
            if self.key_kind not in (LocatorKeyKind.ID, LocatorKeyKind.ORDINAL):
                raise ValueError("Codex locators require an id or ordinal key")
        else:
            raise ValueError(f"unsupported provider: {self.provider!r}")

        if self.key_kind in (LocatorKeyKind.UUID, LocatorKeyKind.ID):
            if not isinstance(self.key, str):
                raise ValueError(f"{self.key_kind.value} key must be a string")
            _validate_opaque_identifier(self.key, "key")
            if self.record_digest is not None:
                raise ValueError(
                    "record_digest is only part of ordinal fallback locators"
                )
            return

        if isinstance(self.key, bool) or not isinstance(self.key, int) or self.key < 0:
            raise ValueError("ordinal key must be a nonnegative integer")
        if self.record_digest is None:
            raise ValueError("ordinal locators require record_digest")
        _validate_digest(self.record_digest, "record_digest")


def format_locator(locator: NativeLocator) -> str:
    """Render a canonical version-1 locator string."""
    prefix = (
        f"ccchat:v1:{locator.provider.value}:{locator.source_session_id}:"
        f"{locator.key_kind.value}:{locator.key}"
    )
    if locator.key_kind is LocatorKeyKind.ORDINAL:
        return f"{prefix}:sha256:{locator.record_digest}"
    return prefix


def parse_locator(value: str) -> NativeLocator | ResolutionStatus:
    """Parse a canonical locator or return the malformed-locator outcome.

    Unsupported provider *schemas* are detected only after a syntactically valid
    provider locator reaches source verification. Unknown provider tokens here
    are locator syntax errors, not schema outcomes.
    """
    if not isinstance(value, str):
        return ResolutionStatus.MALFORMED_LOCATOR

    parts = value.split(":")
    if len(parts) not in (6, 8) or parts[:2] != ["ccchat", "v1"]:
        return ResolutionStatus.MALFORMED_LOCATOR

    try:
        provider = Provider(parts[2])
        key_kind = LocatorKeyKind(parts[4])
        if key_kind is LocatorKeyKind.ORDINAL:
            if len(parts) != 8 or parts[6] != "sha256":
                return ResolutionStatus.MALFORMED_LOCATOR
            ordinal_text = parts[5]
            if _CANONICAL_ORDINAL(ordinal_text) is None:
                return ResolutionStatus.MALFORMED_LOCATOR
            key: str | int = int(ordinal_text)
            digest = parts[7]
        else:
            if len(parts) != 6:
                return ResolutionStatus.MALFORMED_LOCATOR
            key = parts[5]
            digest = None
        return NativeLocator(
            provider=provider,
            source_session_id=parts[3],
            key_kind=key_kind,
            key=key,
            record_digest=digest,
        )
    except ValueError:
        return ResolutionStatus.MALFORMED_LOCATOR


@dataclass(frozen=True, slots=True)
class PhysicalAlias:
    """One verified physical occurrence of a logical native message."""

    locator: NativeLocator
    source_file_relative: Path
    record_ordinal: int
    source_line: int
    source_byte_offset: int
    raw_byte_length: int
    source_digest: str

    def __post_init__(self) -> None:
        """Validate root-independent physical source coordinates."""
        if not isinstance(self.source_file_relative, Path):
            raise ValueError("source_file_relative must be a Path")
        if self.source_file_relative.is_absolute() or self.source_file_relative == Path(
            "."
        ):
            raise ValueError("source_file_relative must be a nonempty relative path")
        if self.record_ordinal < 0:
            raise ValueError("record_ordinal must be nonnegative")
        if self.source_line < 1:
            raise ValueError("source_line must be one-based")
        if self.source_line != self.record_ordinal + 1:
            raise ValueError("source_line must equal record_ordinal + 1")
        if self.source_byte_offset < 0:
            raise ValueError("source_byte_offset must be nonnegative")
        if self.raw_byte_length < 0:
            raise ValueError("raw_byte_length must be nonnegative")
        _validate_digest(self.source_digest, "source_digest")
        if (
            self.locator.key_kind is LocatorKeyKind.ORDINAL
            and self.locator.record_digest != self.source_digest
        ):
            raise ValueError("ordinal locator digest must match source_digest")


@dataclass(frozen=True, slots=True)
class MessageIdentity:
    """Canonical logical identity and every retained native occurrence."""

    logical_message_id: str
    canonical_locator: NativeLocator
    physical_aliases: tuple[PhysicalAlias, ...]

    def __post_init__(self) -> None:
        """Validate alias scope and canonical-locator membership."""
        _validate_opaque_identifier(self.logical_message_id, "logical_message_id")
        aliases = tuple(self.physical_aliases)
        object.__setattr__(self, "physical_aliases", aliases)
        if not aliases:
            raise ValueError("physical_aliases must contain at least one alias")
        if self.canonical_locator not in {value.locator for value in aliases}:
            raise ValueError("canonical_locator must be retained as a physical alias")
        canonical_scope = (
            self.canonical_locator.provider,
            self.canonical_locator.source_session_id,
        )
        if any(
            (value.locator.provider, value.locator.source_session_id) != canonical_scope
            for value in aliases
        ):
            raise ValueError("all physical aliases must share provider and session")


@dataclass(frozen=True, slots=True)
class NativeMessage:
    """One searchable logical message with separate role and authorship."""

    identity: MessageIdentity
    timestamp: str
    role: str
    session_kind: SessionKind
    conversation_epoch: int
    content_class: ContentClass
    text: str
    repository: str | None = None
    cwd: str | None = None
    submitted_by: SubmittedBy = SubmittedBy.UNKNOWN
    submission_evidence: tuple[str, ...] = ()
    submission_match_cardinality: int = 0

    def __post_init__(self) -> None:
        """Validate message epoch and evidence cardinality."""
        if not self.role:
            raise ValueError("role must be nonempty")
        if self.conversation_epoch < 0:
            raise ValueError("conversation_epoch must be nonnegative")
        if self.submission_match_cardinality < 0:
            raise ValueError("submission_match_cardinality must be nonnegative")
        evidence = tuple(self.submission_evidence)
        object.__setattr__(self, "submission_evidence", evidence)
        if self.submitted_by is not SubmittedBy.UNKNOWN and not evidence:
            raise ValueError("identified submissions require positive evidence")


@dataclass(frozen=True, slots=True)
class SessionEpochBoundary:
    """Non-searchable provider compaction boundary metadata."""

    provider: Provider
    source_session_id: str
    session_kind: SessionKind
    conversation_epoch: int
    physical_alias: PhysicalAlias
    timestamp: str
    trigger: str
    token_count: int | None = None

    def __post_init__(self) -> None:
        """Validate boundary identity and zero-based epoch semantics."""
        _validate_opaque_identifier(self.source_session_id, "source_session_id")
        if self.conversation_epoch < 0:
            raise ValueError("conversation_epoch must be nonnegative")
        if self.token_count is not None and self.token_count < 0:
            raise ValueError("token_count must be nonnegative")
        locator = self.physical_alias.locator
        if (locator.provider, locator.source_session_id) != (
            self.provider,
            self.source_session_id,
        ):
            raise ValueError("physical_alias must share boundary provider and session")
