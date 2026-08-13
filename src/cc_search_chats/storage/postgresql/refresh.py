"""Stream native Claude and Codex sources into a PostgreSQL revision."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg

from cc_search_chats.core.identity import NativeMessage
from cc_search_chats.providers.claude import (
    ClaudeParserState,
    ClaudeSessionContext,
    parse_claude_session,
)
from cc_search_chats.providers.codex import (
    CodexParserState,
    CodexSessionContext,
    parse_codex_session,
)
from cc_search_chats.providers.source_discovery import (
    BoundedReadStopReason,
    DiscoveredSource,
    SourceDiagnosticCode,
    discover_claude_sources,
    discover_codex_sources,
    read_bounded_jsonl,
)
from cc_search_chats.storage.postgresql.index import migrate, replace_messages

type ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class RefreshResult:
    revision_id: int
    source_count: int
    message_count: int


def _batches(source: DiscoveredSource):
    target_size = source.path.stat().st_size
    offset, ordinal, source_line = 0, 0, 1
    while offset < target_size:
        batch = read_bounded_jsonl(
            source.path,
            source_file_relative=source.source_file_relative,
            target_size=target_size,
            start_byte_offset=offset,
            next_record_ordinal=ordinal,
            next_source_line=source_line,
        )
        if batch.stop_reason not in {
            BoundedReadStopReason.TARGET_REACHED,
            BoundedReadStopReason.BATCH_LIMIT_REACHED,
        }:
            raise RuntimeError(
                f"native source stopped at {batch.stop_reason.value}: {source.path}"
            )
        yield batch
        if batch.stop_reason is not BoundedReadStopReason.BATCH_LIMIT_REACHED:
            break
        if batch.next_source_byte_offset <= offset:
            raise RuntimeError(f"native source batch made no progress: {source.path}")
        offset = batch.next_source_byte_offset
        ordinal = batch.next_record_ordinal
        source_line = batch.next_source_line


def iter_native_messages(
    *,
    claude_root: Path,
    codex_root: Path,
    progress: ProgressCallback | None = None,
) -> Iterator[NativeMessage]:
    """Yield retained messages from both provider roots without accumulating them."""
    claude = discover_claude_sources(claude_root)
    codex = discover_codex_sources(codex_root)
    root_failures = {
        SourceDiagnosticCode.MISSING_ROOT,
        SourceDiagnosticCode.UNREADABLE_ROOT,
    }
    if any(
        diagnostic.code in root_failures
        for discovery in (claude, codex)
        for diagnostic in discovery.diagnostics
    ):
        raise RuntimeError("one or more native provider roots are unavailable")
    claude_sources = claude.sources
    codex_sources = codex.sources
    total = len(claude_sources) + len(codex_sources)
    completed = 0
    for source in claude_sources:
        state: ClaudeParserState | None = None
        context = ClaudeSessionContext(
            source_session_id=source.source_file_relative.stem
        )
        for batch in _batches(source):
            parsed = parse_claude_session(
                batch.envelopes, context=context, prior_state=state
            )
            yield from parsed.messages
            state = parsed.next_state
        completed += 1
        if progress is not None:
            progress("claude", completed, total)

    for source in codex_sources:
        state: CodexParserState | None = None
        for batch in _batches(source):
            parsed = parse_codex_session(
                batch.envelopes,
                context=CodexSessionContext(),
                source_diagnostics=batch.diagnostics,
                prior_state=state,
            )
            yield from parsed.messages
            state = parsed.next_state
        completed += 1
        if progress is not None:
            progress("codex", completed, total)


def refresh_native_sources(
    connection: psycopg.Connection,
    *,
    claude_root: Path,
    codex_root: Path,
    progress: ProgressCallback | None = None,
) -> RefreshResult:
    """Build and atomically select one cross-vendor PostgreSQL revision."""
    migrate(connection)
    message_count = 0
    source_count = 0

    def report(provider: str, completed: int, total: int) -> None:
        nonlocal source_count
        source_count = total
        if progress is not None:
            progress(provider, completed, total)

    def messages() -> Iterator[NativeMessage]:
        nonlocal message_count
        for message in iter_native_messages(
            claude_root=claude_root,
            codex_root=codex_root,
            progress=report,
        ):
            message_count += 1
            yield message

    revision_id = replace_messages(connection, messages())
    return RefreshResult(revision_id, source_count, message_count)
