"""Read-only native-source discovery and bounded JSONL reading.

This module owns filesystem and Git I/O but no provider record parsing. It
rejects only positively identified non-native transport artifacts; Claude and
Codex schema classification remains the responsibility of their pure adapters.
"""

# pattern: Imperative Shell

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cc_search_chats.core.identity import Provider, validate_source_file_relative

_ARTIFACT_PROBE_LIMIT = 64 * 1024
_GIT_ROUTING_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


class SourceDiagnosticCode(StrEnum):
    """Named source and discovery failure classifications."""

    PARTIAL_TAIL = "partial_tail"
    INVALID_JSON = "invalid_json"
    UNREADABLE_SOURCE = "unreadable_source"
    SOURCE_TRUNCATED = "source_truncated"
    MISSING_ROOT = "missing_root"
    UNREADABLE_ROOT = "unreadable_root"
    NON_NATIVE_AGY = "non_native_agy"
    NON_NATIVE_TRANSPORT_ARCHIVE = "non_native_transport_archive"
    GIT_PROBE_FAILED = "git_probe_failed"


@dataclass(frozen=True, slots=True)
class SourceDiagnostic:
    """One explicit source-coverage diagnostic."""

    code: SourceDiagnosticCode
    path: Path
    detail: str
    record_ordinal: int | None = None
    source_line: int | None = None
    source_byte_offset: int | None = None


@dataclass(frozen=True, slots=True)
class RecordEnvelope:
    """One complete physical JSONL record, before provider classification."""

    source_file_relative: Path
    record_ordinal: int
    source_line: int
    source_byte_offset: int
    raw_bytes: bytes
    raw_byte_length: int
    source_digest: str


@dataclass(frozen=True, slots=True)
class BoundedReadResult:
    """Complete records and diagnostics observed within one captured target."""

    envelopes: tuple[RecordEnvelope, ...]
    diagnostics: tuple[SourceDiagnostic, ...]
    target_size: int
    final_size: int | None

    @property
    def pending_bytes(self) -> int | None:
        """Return bytes beyond the captured target, when final size is known."""
        if self.final_size is None:
            return None
        return max(0, self.final_size - self.target_size)


@dataclass(frozen=True, slots=True)
class DiscoveredSource:
    """One provider candidate located relative to an explicit provider root."""

    provider: Provider
    path: Path
    source_file_relative: Path


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Deterministic source candidates plus explicit coverage diagnostics."""

    provider: Provider
    resolved_root: Path
    sources: tuple[DiscoveredSource, ...]
    diagnostics: tuple[SourceDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class GitProbeResult:
    """A resolved repository root or a named probe failure."""

    repository_root: Path | None
    diagnostics: tuple[SourceDiagnostic, ...]


def _diagnostic(
    code: SourceDiagnosticCode,
    path: Path,
    detail: str,
    *,
    record_ordinal: int | None = None,
    source_line: int | None = None,
    source_byte_offset: int | None = None,
) -> SourceDiagnostic:
    """Build a source diagnostic without duplicating coordinate plumbing."""
    return SourceDiagnostic(
        code=code,
        path=path,
        detail=detail,
        record_ordinal=record_ordinal,
        source_line=source_line,
        source_byte_offset=source_byte_offset,
    )


def _record_diagnostic(
    code: SourceDiagnosticCode,
    path: Path,
    detail: str,
    ordinal: int,
    offset: int,
) -> SourceDiagnostic:
    """Build a diagnostic at one zero-based complete-record coordinate."""
    return _diagnostic(
        code,
        path,
        detail,
        record_ordinal=ordinal,
        source_line=ordinal + 1,
        source_byte_offset=offset,
    )


def read_bounded_jsonl(
    path: Path,
    *,
    source_file_relative: Path,
    target_size: int,
) -> BoundedReadResult:
    """Read complete JSONL records ending at or before ``target_size``.

    The digest is SHA-256 over exact record bytes after removing the JSONL line
    delimiter. A fragment without a terminating newline is diagnostic only and
    does not consume an ordinal.
    """
    if target_size < 0:
        raise ValueError("target_size must be nonnegative")
    validate_source_file_relative(source_file_relative)

    envelopes: list[RecordEnvelope] = []
    diagnostics: list[SourceDiagnostic] = []
    offset = 0
    ordinal = 0

    try:
        with path.open("rb") as handle:
            while offset < target_size:
                line = handle.readline(target_size - offset)
                if not line:
                    diagnostics.append(
                        _record_diagnostic(
                            SourceDiagnosticCode.SOURCE_TRUNCATED,
                            path,
                            "source ended before the captured target size",
                            ordinal,
                            offset,
                        )
                    )
                    break
                if not line.endswith(b"\n"):
                    diagnostics.append(
                        _record_diagnostic(
                            SourceDiagnosticCode.PARTIAL_TAIL,
                            path,
                            "captured target ends with an incomplete JSONL record",
                            ordinal,
                            offset,
                        )
                    )
                    break

                record_bytes = line[:-1]
                if record_bytes.endswith(b"\r"):
                    record_bytes = record_bytes[:-1]
                envelope = RecordEnvelope(
                    source_file_relative=source_file_relative,
                    record_ordinal=ordinal,
                    source_line=ordinal + 1,
                    source_byte_offset=offset,
                    raw_bytes=record_bytes,
                    raw_byte_length=len(record_bytes),
                    source_digest=hashlib.sha256(record_bytes).hexdigest(),
                )
                envelopes.append(envelope)
                try:
                    json.loads(record_bytes)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    diagnostics.append(
                        _record_diagnostic(
                            SourceDiagnosticCode.INVALID_JSON,
                            path,
                            f"complete record is not valid JSON: {error}",
                            ordinal,
                            offset,
                        )
                    )
                offset += len(line)
                ordinal += 1
    except OSError as error:
        diagnostics.append(
            _diagnostic(
                SourceDiagnosticCode.UNREADABLE_SOURCE,
                path,
                f"source could not be read: {error}",
            )
        )

    try:
        final_size = path.stat().st_size
    except OSError:
        final_size = None
    return BoundedReadResult(
        envelopes=tuple(envelopes),
        diagnostics=tuple(diagnostics),
        target_size=target_size,
        final_size=final_size,
    )


def _positive_non_native_diagnostic(path: Path) -> SourceDiagnostic | None:
    """Identify only explicit Agy or rendered-transport signatures."""
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(_ARTIFACT_PROBE_LIMIT)
    except OSError as error:
        return _diagnostic(
            SourceDiagnosticCode.UNREADABLE_SOURCE,
            path,
            f"source could not be inspected: {error}",
        )

    try:
        payload = json.loads(first_line)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    provider_marker = payload.get("provider")
    if "agy" in payload or (
        isinstance(provider_marker, str) and provider_marker.casefold() == "agy"
    ):
        return _diagnostic(
            SourceDiagnosticCode.NON_NATIVE_AGY,
            path,
            "source carries an explicit Agy artifact signature",
        )

    format_marker = payload.get("format")
    if isinstance(format_marker, str) and format_marker.casefold() in {
        "rendered_transcript",
        "transcript_archive",
        "transport_archive",
    }:
        return _diagnostic(
            SourceDiagnosticCode.NON_NATIVE_TRANSPORT_ARCHIVE,
            path,
            "source carries an explicit rendered transport signature",
        )
    return None


def _archive_diagnostic(path: Path) -> SourceDiagnostic:
    """Classify readable non-JSONL archive extensions without parsing content."""
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as error:
        return _diagnostic(
            SourceDiagnosticCode.UNREADABLE_SOURCE,
            path,
            f"non-native artifact could not be inspected: {error}",
        )
    return _diagnostic(
        SourceDiagnosticCode.NON_NATIVE_TRANSPORT_ARCHIVE,
        path,
        "rendered Markdown/JSON is not a native JSONL source",
    )


def _discovery_failure(
    provider: Provider,
    root: Path,
    code: SourceDiagnosticCode,
    detail: str,
) -> DiscoveryResult:
    """Build an empty discovery result with positive failure evidence."""
    return DiscoveryResult(
        provider=provider,
        resolved_root=root,
        sources=(),
        diagnostics=(_diagnostic(code, root, detail),),
    )


def _root_failure(provider: Provider, root: Path) -> DiscoveryResult | None:
    """Return an explicit root failure, or ``None`` for an accessible directory."""
    if not root.is_absolute():
        raise ValueError("resolved_root must be absolute")
    if not root.exists():
        return _discovery_failure(
            provider,
            root,
            SourceDiagnosticCode.MISSING_ROOT,
            "configured provider root does not exist",
        )
    if not root.is_dir():
        return _discovery_failure(
            provider,
            root,
            SourceDiagnosticCode.UNREADABLE_ROOT,
            "configured provider root is not a readable directory",
        )
    try:
        mode = root.stat().st_mode
    except OSError as error:
        return _discovery_failure(
            provider,
            root,
            SourceDiagnosticCode.UNREADABLE_ROOT,
            f"configured provider root could not be inspected: {error}",
        )
    if mode & 0o444 == 0 or mode & 0o111 == 0 or not os.access(root, os.R_OK | os.X_OK):
        return _discovery_failure(
            provider,
            root,
            SourceDiagnosticCode.UNREADABLE_ROOT,
            "configured provider root is not readable and traversable",
        )
    return None


def _discover(
    provider: Provider,
    root: Path,
    candidate_paths: tuple[Path, ...],
) -> DiscoveryResult:
    """Filter provider-shaped paths while rejecting positive artifact signatures."""
    sources: list[DiscoveredSource] = []
    diagnostics: list[SourceDiagnostic] = []
    for path in candidate_paths:
        artifact = _positive_non_native_diagnostic(path)
        if artifact is not None:
            diagnostics.append(artifact)
            continue
        sources.append(
            DiscoveredSource(
                provider=provider,
                path=path,
                source_file_relative=path.relative_to(root),
            )
        )

    for archive in sorted((*root.rglob("*.md"), *root.rglob("*.json"))):
        diagnostics.append(_archive_diagnostic(archive))

    return DiscoveryResult(
        provider=provider,
        resolved_root=root,
        sources=tuple(sources),
        diagnostics=tuple(diagnostics),
    )


def _is_codex_rollout(path: Path, root: Path) -> bool:
    """Return whether a path has the native Codex date/rollout layout."""
    relative = path.relative_to(root)
    parts = relative.parts
    return (
        len(parts) == 4
        and len(parts[0]) == 4
        and len(parts[1]) == 2
        and len(parts[2]) == 2
        and all(part.isascii() and part.isdigit() for part in parts[:3])
        and parts[3].startswith("rollout-")
        and parts[3].endswith(".jsonl")
    )


def _discover_provider_sources(
    provider: Provider, resolved_root: Path
) -> DiscoveryResult:
    """Traverse one explicit provider root and classify candidate paths."""
    failure = _root_failure(provider, resolved_root)
    if failure is not None:
        return failure
    try:
        if provider is Provider.CLAUDE:
            candidates = tuple(sorted(resolved_root.rglob("*.jsonl")))
        else:
            candidates = tuple(
                path
                for path in sorted(resolved_root.rglob("rollout-*.jsonl"))
                if _is_codex_rollout(path, resolved_root)
            )
    except OSError as error:
        return _discovery_failure(
            provider,
            resolved_root,
            SourceDiagnosticCode.UNREADABLE_ROOT,
            f"configured provider root could not be traversed: {error}",
        )
    return _discover(provider, resolved_root, candidates)


def discover_claude_sources(resolved_root: Path) -> DiscoveryResult:
    """Discover Claude top-level and nested subagent JSONL candidates."""
    return _discover_provider_sources(Provider.CLAUDE, resolved_root)


def discover_codex_sources(resolved_root: Path) -> DiscoveryResult:
    """Discover Codex ``YYYY/MM/DD/rollout-*.jsonl`` candidates."""
    return _discover_provider_sources(Provider.CODEX, resolved_root)


def probe_git_repository(
    candidate: Path, *, timeout_seconds: float = 5.0
) -> GitProbeResult:
    """Resolve a Git repository without trusting ambient Git routing state."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    environment = os.environ.copy()
    for variable in _GIT_ROUTING_VARIABLES:
        environment.pop(variable, None)

    try:
        completed = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            shell=False,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return GitProbeResult(
            repository_root=None,
            diagnostics=(
                _diagnostic(
                    SourceDiagnosticCode.GIT_PROBE_FAILED,
                    candidate,
                    f"Git repository probe failed: {error}",
                ),
            ),
        )

    output = completed.stdout.strip()
    if completed.returncode != 0 or not output:
        detail = completed.stderr.strip() or "Git did not return a repository root"
        return GitProbeResult(
            repository_root=None,
            diagnostics=(
                _diagnostic(
                    SourceDiagnosticCode.GIT_PROBE_FAILED,
                    candidate,
                    detail,
                ),
            ),
        )
    return GitProbeResult(repository_root=Path(output).resolve(), diagnostics=())
