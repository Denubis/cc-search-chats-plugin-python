"""Bounded comparison of native source metadata with indexed checkpoints."""

from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

import psycopg  # noqa: TC002  # keep public annotations runtime-resolvable

from cc_search_chats.core.identity import Provider
from cc_search_chats.providers.source_discovery import (
    SourceDiagnosticCode,
    discover_claude_sources,
    discover_codex_sources,
)

if TYPE_CHECKING:
    from os import stat_result

    from cc_search_chats.providers.source_discovery import (
        ConfiguredSourceRoot,
        DiscoveredSource,
    )

_INCOMPLETE_DISCOVERY_CODES = {
    SourceDiagnosticCode.MISSING_ROOT,
    SourceDiagnosticCode.UNREADABLE_ROOT,
    SourceDiagnosticCode.UNREADABLE_PATH,
}


@dataclass(frozen=True, slots=True)
class UnindexedSourceCounts:
    """Native files and bytes not represented by their indexed checkpoints."""

    files: int
    directories: int
    bytes: int


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    file_device: int
    file_inode: int
    observed_size: int
    observed_mtime_ns: int
    complete_byte_offset: int


def _load_checkpoints(
    connection: psycopg.Connection,
    roots: tuple[ConfiguredSourceRoot, ...],
) -> dict[tuple[str, str], _Checkpoint]:
    return {
        (source_root, source_file): _Checkpoint(
            file_device=file_device,
            file_inode=file_inode,
            observed_size=observed_size,
            observed_mtime_ns=observed_mtime_ns,
            complete_byte_offset=complete_byte_offset,
        )
        for (
            source_root,
            source_file,
            file_device,
            file_inode,
            observed_size,
            observed_mtime_ns,
            complete_byte_offset,
        ) in connection.execute(
            """
            SELECT source_root_id, source_file_relative, file_device,
                   file_inode, observed_size, observed_mtime_ns,
                   complete_byte_offset
            FROM cc_search_chats.source_file_current
            WHERE source_root_id = ANY(%s::text[])
            """,
            ([root.source_root_id for root in roots],),
        )
    }


def _discover_root(
    root: ConfiguredSourceRoot,
    deadline_monotonic: float,
) -> tuple[tuple[DiscoveredSource, ...] | None, str | None]:
    if monotonic() >= deadline_monotonic:
        return None, "scan_budget_exhausted"
    discovery = (
        discover_claude_sources(root.path, inspect_content=False)
        if root.provider is Provider.CLAUDE
        else discover_codex_sources(root.path, inspect_content=False)
    )
    if monotonic() >= deadline_monotonic:
        return None, "scan_budget_exhausted"
    incomplete = next(
        (
            diagnostic
            for diagnostic in discovery.diagnostics
            if diagnostic.code in _INCOMPLETE_DISCOVERY_CODES
        ),
        None,
    )
    if incomplete is not None:
        return None, incomplete.code.value
    return discovery.sources, None


def _unindexed_bytes(stat: stat_result, checkpoint: _Checkpoint | None) -> int | None:
    if checkpoint is None or (stat.st_dev, stat.st_ino) != (
        checkpoint.file_device,
        checkpoint.file_inode,
    ):
        return stat.st_size
    unchanged = (
        stat.st_size == checkpoint.observed_size
        and stat.st_mtime_ns == checkpoint.observed_mtime_ns
        and checkpoint.observed_size <= checkpoint.complete_byte_offset
    )
    if unchanged:
        return None
    return max(0, stat.st_size - checkpoint.complete_byte_offset)


def _scan_root(
    root: ConfiguredSourceRoot,
    checkpoints: dict[tuple[str, str], _Checkpoint],
    deadline_monotonic: float,
) -> tuple[UnindexedSourceCounts | None, str | None]:
    sources, reason = _discover_root(root, deadline_monotonic)
    if sources is None:
        return None, reason
    files = 0
    unindexed_bytes = 0
    directories: set[str] = set()
    for source in sources:
        if monotonic() >= deadline_monotonic:
            return None, "scan_budget_exhausted"
        try:
            stat = source.path.stat()
        except OSError:
            return None, "source_stat_failed"
        relative = source.source_file_relative.as_posix()
        pending = _unindexed_bytes(
            stat,
            checkpoints.get((root.source_root_id, relative)),
        )
        if pending is None:
            continue
        files += 1
        unindexed_bytes += pending
        directories.add(source.source_file_relative.parent.as_posix())
    return UnindexedSourceCounts(files, len(directories), unindexed_bytes), None


def unindexed_sources(
    connection: psycopg.Connection,
    roots: tuple[ConfiguredSourceRoot, ...],
    *,
    deadline_monotonic: float,
) -> tuple[UnindexedSourceCounts | None, str | None]:
    """Return bounded native-source counts and a closed incomplete reason."""
    if monotonic() >= deadline_monotonic:
        return None, "scan_budget_exhausted"
    if not roots:
        return UnindexedSourceCounts(files=0, directories=0, bytes=0), None
    checkpoints = _load_checkpoints(connection, roots)
    if monotonic() >= deadline_monotonic:
        return None, "scan_budget_exhausted"

    files = 0
    unindexed_bytes = 0
    directories = 0
    for root in roots:
        counts, reason = _scan_root(root, checkpoints, deadline_monotonic)
        if counts is None:
            return None, reason
        files += counts.files
        directories += counts.directories
        unindexed_bytes += counts.bytes
    return (
        UnindexedSourceCounts(
            files=files,
            directories=directories,
            bytes=unindexed_bytes,
        ),
        None,
    )
