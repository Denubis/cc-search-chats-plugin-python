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
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cc_search_chats.core.identity import Provider, validate_source_file_relative

_ARTIFACT_PROBE_LIMIT = 64 * 1024
_GIT_ROUTING_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
DEFAULT_MAX_RECORDS_PER_BATCH = 1_024
DEFAULT_MAX_BATCH_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_SINGLE_RECORD_BYTES = 16 * 1024 * 1024


class SourceDiagnosticCode(StrEnum):
    """Named source and discovery failure classifications."""

    PARTIAL_TAIL = "partial_tail"
    INVALID_JSON = "invalid_json"
    UNREADABLE_SOURCE = "unreadable_source"
    OVERSIZED_RECORD = "oversized_record"
    SOURCE_TRUNCATED = "source_truncated"
    MISSING_ROOT = "missing_root"
    UNREADABLE_ROOT = "unreadable_root"
    UNREADABLE_PATH = "unreadable_path"
    NON_NATIVE_AGY = "non_native_agy"
    NON_NATIVE_TRANSPORT_ARCHIVE = "non_native_transport_archive"
    GIT_PROBE_FAILED = "git_probe_failed"


class BoundedReadStopReason(StrEnum):
    """Closed reason why one bounded batch stopped."""

    TARGET_REACHED = "target_reached"
    BATCH_LIMIT_REACHED = "batch_limit_reached"
    PARTIAL_TAIL = "partial_tail"
    OVERSIZED_RECORD = "oversized_record"
    SOURCE_TRUNCATED = "source_truncated"
    UNREADABLE_SOURCE = "unreadable_source"


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
    next_source_byte_offset: int
    next_record_ordinal: int
    next_source_line: int
    stop_reason: BoundedReadStopReason
    batch_raw_bytes: int

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
    source_line: int,
    offset: int,
) -> SourceDiagnostic:
    """Build a diagnostic at one zero-based complete-record coordinate."""
    return _diagnostic(
        code,
        path,
        detail,
        record_ordinal=ordinal,
        source_line=source_line,
        source_byte_offset=offset,
    )


def _envelope(
    *,
    source_file_relative: Path,
    record_bytes: bytes,
    ordinal: int,
    source_line: int,
    offset: int,
) -> RecordEnvelope:
    """Construct one exact physical envelope from a complete record."""
    return RecordEnvelope(
        source_file_relative=source_file_relative,
        record_ordinal=ordinal,
        source_line=source_line,
        source_byte_offset=offset,
        raw_bytes=record_bytes,
        raw_byte_length=len(record_bytes),
        source_digest=hashlib.sha256(record_bytes).hexdigest(),
    )


def _invalid_json_diagnostic(
    record_bytes: bytes,
    *,
    path: Path,
    ordinal: int,
    source_line: int,
    offset: int,
) -> SourceDiagnostic | None:
    """Classify malformed complete JSON without excluding its envelope."""
    try:
        json.loads(record_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        return _record_diagnostic(
            SourceDiagnosticCode.INVALID_JSON,
            path,
            f"complete record is not valid JSON: {error}",
            ordinal,
            source_line,
            offset,
        )
    return None


def read_bounded_jsonl(
    path: Path,
    *,
    source_file_relative: Path,
    target_size: int,
    start_byte_offset: int = 0,
    next_record_ordinal: int = 0,
    next_source_line: int = 1,
    max_records_per_batch: int = DEFAULT_MAX_RECORDS_PER_BATCH,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    max_single_record_bytes: int = DEFAULT_MAX_SINGLE_RECORD_BYTES,
) -> BoundedReadResult:
    """Read one bounded batch ending at or before ``target_size``.

    The digest is SHA-256 over exact record bytes after removing the JSONL line
    delimiter. A fragment without a terminating newline is diagnostic only and
    does not consume an ordinal. The first complete record may exceed the batch
    byte limit, but never the separate single-record limit, so every valid
    record can make progress without allowing unbounded allocation.
    """
    coordinates = (start_byte_offset, next_record_ordinal, next_source_line)
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in coordinates
    ):
        raise ValueError("resume coordinates must be integers")
    if isinstance(target_size, bool) or not isinstance(target_size, int):
        raise ValueError("target_size must be an integer")
    if target_size < 0:
        raise ValueError("target_size must be nonnegative")
    limits = (
        max_records_per_batch,
        max_batch_bytes,
        max_single_record_bytes,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in limits
    ):
        raise ValueError("batch and single-record limits must be positive integers")
    if start_byte_offset < 0 or next_record_ordinal < 0 or next_source_line < 1:
        raise ValueError("resume coordinates must be nonnegative with a one-based line")
    if next_source_line != next_record_ordinal + 1:
        raise ValueError("resume coordinates require source line = record ordinal + 1")
    if start_byte_offset > target_size:
        raise ValueError("start_byte_offset cannot exceed target_size")
    validate_source_file_relative(source_file_relative)

    envelopes: list[RecordEnvelope] = []
    diagnostics: list[SourceDiagnostic] = []
    offset = start_byte_offset
    ordinal = next_record_ordinal
    source_line = next_source_line
    batch_raw_bytes = 0
    stop_reason = BoundedReadStopReason.TARGET_REACHED

    try:
        with path.open("rb") as handle:
            handle.seek(start_byte_offset)
            while offset < target_size:
                if len(envelopes) >= max_records_per_batch:
                    stop_reason = BoundedReadStopReason.BATCH_LIMIT_REACHED
                    break

                remaining = target_size - offset
                read_limit = min(remaining, max_single_record_bytes + 2)
                line = handle.readline(read_limit)
                if not line:
                    code = SourceDiagnosticCode.SOURCE_TRUNCATED
                    stop_reason = BoundedReadStopReason.SOURCE_TRUNCATED
                    detail = "source ended before the captured target size"
                elif not line.endswith(b"\n"):
                    if len(line) == read_limit and read_limit < remaining:
                        code = SourceDiagnosticCode.OVERSIZED_RECORD
                        stop_reason = BoundedReadStopReason.OVERSIZED_RECORD
                        detail = "complete record exceeds the single-record byte limit"
                    elif len(line) < remaining:
                        code = SourceDiagnosticCode.SOURCE_TRUNCATED
                        stop_reason = BoundedReadStopReason.SOURCE_TRUNCATED
                        detail = "source ended before the captured target size"
                    else:
                        code = SourceDiagnosticCode.PARTIAL_TAIL
                        stop_reason = BoundedReadStopReason.PARTIAL_TAIL
                        detail = "captured target ends with an incomplete JSONL record"
                else:
                    record_bytes = line[:-1].removesuffix(b"\r")
                    if len(record_bytes) > max_single_record_bytes:
                        code = SourceDiagnosticCode.OVERSIZED_RECORD
                        stop_reason = BoundedReadStopReason.OVERSIZED_RECORD
                        detail = "complete record exceeds the single-record byte limit"
                    elif (
                        envelopes
                        and batch_raw_bytes + len(record_bytes) > max_batch_bytes
                    ):
                        stop_reason = BoundedReadStopReason.BATCH_LIMIT_REACHED
                        break
                    else:
                        envelopes.append(
                            _envelope(
                                source_file_relative=source_file_relative,
                                record_bytes=record_bytes,
                                ordinal=ordinal,
                                source_line=source_line,
                                offset=offset,
                            )
                        )
                        batch_raw_bytes += len(record_bytes)
                        diagnostic = _invalid_json_diagnostic(
                            record_bytes,
                            path=path,
                            ordinal=ordinal,
                            source_line=source_line,
                            offset=offset,
                        )
                        if diagnostic is not None:
                            diagnostics.append(diagnostic)
                        offset += len(line)
                        ordinal += 1
                        source_line += 1
                        continue

                diagnostics.append(
                    _record_diagnostic(code, path, detail, ordinal, source_line, offset)
                )
                break
    except OSError as error:
        diagnostics.append(
            _diagnostic(
                SourceDiagnosticCode.UNREADABLE_SOURCE,
                path,
                f"source could not be read: {error}",
                record_ordinal=ordinal,
                source_line=source_line,
                source_byte_offset=offset,
            )
        )
        stop_reason = BoundedReadStopReason.UNREADABLE_SOURCE

    try:
        final_size = path.stat().st_size
    except OSError:
        final_size = None
    return BoundedReadResult(
        envelopes=tuple(envelopes),
        diagnostics=tuple(diagnostics),
        target_size=target_size,
        final_size=final_size,
        next_source_byte_offset=offset,
        next_record_ordinal=ordinal,
        next_source_line=source_line,
        stop_reason=stop_reason,
        batch_raw_bytes=batch_raw_bytes,
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
    archive_paths: tuple[Path, ...],
    traversal_diagnostics: tuple[SourceDiagnostic, ...],
) -> DiscoveryResult:
    """Filter provider-shaped paths while rejecting positive artifact signatures."""
    sources: list[DiscoveredSource] = []
    diagnostics = list(traversal_diagnostics)
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

    for archive in archive_paths:
        diagnostics.append(_archive_diagnostic(archive))

    return DiscoveryResult(
        provider=provider,
        resolved_root=root,
        sources=tuple(sources),
        diagnostics=tuple(diagnostics),
    )


def _walk_regular_files(
    root: Path,
) -> tuple[tuple[Path, ...], tuple[SourceDiagnostic, ...]]:
    """Walk ``root`` deterministically and report every traversal failure."""
    pending = deque([root])
    files: list[Path] = []
    diagnostics: list[SourceDiagnostic] = []

    while pending:
        directory = pending.popleft()
        try:
            with os.scandir(directory) as entries:
                ordered_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError as error:
            code = (
                SourceDiagnosticCode.UNREADABLE_ROOT
                if directory == root
                else SourceDiagnosticCode.UNREADABLE_PATH
            )
            diagnostics.append(
                _diagnostic(
                    code,
                    directory,
                    f"directory could not be traversed: {error}",
                )
            )
            continue

        child_directories: list[Path] = []
        for entry in ordered_entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    child_directories.append(path)
                elif entry.is_file(follow_symlinks=True):
                    files.append(path)
            except OSError as error:
                diagnostics.append(
                    _diagnostic(
                        SourceDiagnosticCode.UNREADABLE_PATH,
                        path,
                        f"path could not be inspected during traversal: {error}",
                    )
                )
        pending.extend(child_directories)

    return tuple(sorted(files)), tuple(diagnostics)


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
    files, traversal_diagnostics = _walk_regular_files(resolved_root)
    if provider is Provider.CLAUDE:
        candidates = tuple(path for path in files if path.suffix == ".jsonl")
    else:
        candidates = tuple(
            path
            for path in files
            if path.name.startswith("rollout-")
            and path.suffix == ".jsonl"
            and _is_codex_rollout(path, resolved_root)
        )
    archives = tuple(path for path in files if path.suffix in {".md", ".json"})
    return _discover(
        provider,
        resolved_root,
        candidates,
        archives,
        traversal_diagnostics,
    )


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
