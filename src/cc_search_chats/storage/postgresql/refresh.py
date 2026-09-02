"""Incrementally refresh native Claude and Codex sources into PostgreSQL."""

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeIs

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

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
)
from cc_search_chats.providers.claude import (
    ClaudeDiagnosticCode,
    ClaudeParseResult,
    ClaudeParserState,
    ClaudeSessionContext,
    parse_claude_session,
)
from cc_search_chats.providers.codex import (
    CodexDiagnosticCode,
    CodexParseResult,
    CodexParserState,
    CodexSessionContext,
    parse_codex_session,
)
from cc_search_chats.providers.source_discovery import (
    BoundedReadStopReason,
    ConfiguredSourceRoot,
    DiscoveredSource,
    RecordEnvelope,
    SourceDiagnostic,
    SourceDiagnosticCode,
    discover_claude_sources,
    discover_codex_sources,
    inspect_non_native_artifact,
    read_bounded_jsonl,
    source_root_id,
)
from cc_search_chats.storage.postgresql.guardrails import (
    INDEX_NOTIFY_CHANNEL,
    INDEX_QUEUE_LOCK,
    DatabaseHeartbeat,
)
from cc_search_chats.storage.postgresql.migrations import require_current_schema
from cc_search_chats.storage.postgresql.semantic import (
    CandidateSemanticBuild,
    Chunker,
    prepare_candidate_semantics,
)

_PARSER_STATE_VERSIONS = {
    Provider.CLAUDE: 2,
    Provider.CODEX: 2,
}
_RETAINED_REFRESH_RUNS = 100
_WAIT_HEARTBEAT_SECONDS = 5.0
_RUN_HEARTBEAT_SECONDS = 5.0
_ROOT_FAILURES = {
    SourceDiagnosticCode.MISSING_ROOT,
    SourceDiagnosticCode.UNREADABLE_ROOT,
}
_TRAVERSAL_FAILURES = {
    SourceDiagnosticCode.UNREADABLE_ROOT,
    SourceDiagnosticCode.UNREADABLE_PATH,
}
_UNSUPPORTED_CLAUDE_DIAGNOSTICS = {
    ClaudeDiagnosticCode.MISSING_MESSAGE,
    ClaudeDiagnosticCode.NON_OBJECT_MESSAGE,
    ClaudeDiagnosticCode.UNKNOWN_ROLE,
    ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
    ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD,
    ClaudeDiagnosticCode.MISSING_MESSAGE_UUID,
}
_UNSUPPORTED_CODEX_DIAGNOSTICS = {
    CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
    CodexDiagnosticCode.UNKNOWN_ROLE,
    CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
    CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
    CodexDiagnosticCode.UNKNOWN_EVENT,
    CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
    CodexDiagnosticCode.INVALID_PAYLOAD,
    CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
}
_SKIPPABLE_CLAUDE_DIAGNOSTICS = {
    ClaudeDiagnosticCode.MALFORMED_JSON,
    ClaudeDiagnosticCode.INVALID_UNICODE,
}
_SKIPPABLE_CODEX_DIAGNOSTICS = {
    CodexDiagnosticCode.MALFORMED_JSON,
    CodexDiagnosticCode.INVALID_UNICODE,
}


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Committed refresh state and bounded current-source observations."""

    corpus_generation: int
    source_count: int
    message_count: int
    changed_source_count: int = 0
    failed_source_count: int = 0
    read_source_count: int = 0
    removed_source_count: int = 0
    advanced_source_count: int = 0
    pending_bytes: int = 0
    metadata_checked_source_count: int = 0
    attempted_source_count: int = 0
    attempted_content_bytes: int = 0
    blocked_source_count: int = 0
    transient_failure_source_count: int = 0
    run_id: int | None = None


@dataclass(frozen=True, slots=True)
class CorpusIndexResult:
    """One jointly published literal and semantic corpus."""

    corpus_generation: int
    semantic_build: int
    source_count: int
    message_count: int
    embedding_count: int
    changed_source_count: int
    failed_source_count: int
    read_source_count: int
    removed_source_count: int
    advanced_source_count: int
    pending_bytes: int
    metadata_checked_source_count: int
    attempted_source_count: int
    attempted_content_bytes: int
    blocked_source_count: int
    transient_failure_source_count: int
    run_id: int | None


@dataclass(frozen=True, slots=True)
class RefreshProgress:
    """One observable refresh-owner or source-progress transition."""

    phase: str
    state: str
    completed_units: int | None = None
    total_units: int | None = None
    run_id: int | None = None
    owner_pid: int | None = None


type ProgressCallback = Callable[[RefreshProgress], None]


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    source_root_id: str
    source_file_relative: Path
    file_device: int
    file_inode: int
    observed_size: int
    observed_mtime_ns: int
    complete_byte_offset: int
    next_record_ordinal: int
    next_source_line: int
    parser_state_version: int
    parser_state: object
    source_status: str
    skipped_record_count: int


@dataclass(frozen=True, slots=True)
class _ObservedSource:
    root: ConfiguredSourceRoot
    source: DiscoveredSource
    file_device: int
    file_inode: int
    size: int
    mtime_ns: int

    @property
    def key(self) -> tuple[str, Path]:
        return self.root.source_root_id, self.source.source_file_relative


@dataclass(frozen=True, slots=True)
class _SourcePlan:
    observed: _ObservedSource
    disposition: str
    start_byte_offset: int
    next_record_ordinal: int
    next_source_line: int
    prior_state: ClaudeParserState | CodexParserState | None
    prior_skipped_record_count: int


@dataclass(frozen=True, slots=True)
class _FailedObservation:
    source_root_id: str
    source_file_relative: Path
    file_device: int
    file_inode: int
    observed_size: int
    observed_mtime_ns: int
    parser_state_version: int
    failure_record_ordinal: int | None
    failure_source_line: int | None
    failure_source_byte_offset: int | None
    failure_code: str
    failure_class: str
    attempted_content_bytes: int
    consecutive_failures: int
    retry_eligible: bool


class _SourceRefreshError(RuntimeError):
    """A source-local failure that must not publish its staged rows."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "source_refresh_failed",
        failure_class: str = "transient",
        record_ordinal: int | None = None,
        source_line: int | None = None,
        source_byte_offset: int | None = None,
        attempted_content_bytes: int = 0,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.failure_class = failure_class
        self.record_ordinal = record_ordinal
        self.source_line = source_line
        self.source_byte_offset = source_byte_offset
        self.attempted_content_bytes = attempted_content_bytes


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _required_text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise ValueError(  # noqa: TRY004  # persisted-state ValueError contract
            f"{key} must be a string"
        )
    return result


def _optional_text(value: Mapping[str, object], key: str) -> str | None:
    result = value.get(key)
    if result is not None and not isinstance(result, str):
        raise ValueError(f"{key} must be a string or null")
    return result


def _required_integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValueError(  # noqa: TRY004  # persisted-state ValueError contract
            f"{key} must be an integer"
        )
    return result


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
    if not _is_json_object(value):
        raise ValueError("Codex trailing candidate must be an object")
    key_kind = LocatorKeyKind(_required_text(value, "key_kind"))
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
        source_session_id=_required_text(value, "source_session_id"),
        key_kind=key_kind,
        key=key,
        record_digest=_optional_text(value, "record_digest"),
    )
    alias = PhysicalAlias(
        locator=locator,
        source_file_relative=Path(_required_text(value, "source_file_relative")),
        record_ordinal=_required_integer(value, "record_ordinal"),
        source_line=_required_integer(value, "source_line"),
        source_byte_offset=_required_integer(value, "source_byte_offset"),
        raw_byte_length=_required_integer(value, "raw_byte_length"),
        source_digest=_required_text(value, "source_digest"),
    )
    message = NativeMessage(
        identity=MessageIdentity(
            logical_message_id=_required_text(value, "logical_message_id"),
            canonical_locator=locator,
            physical_aliases=(alias,),
        ),
        timestamp=_required_text(value, "timestamp"),
        role=_required_text(value, "role"),
        session_kind=SessionKind(_required_text(value, "session_kind")),
        conversation_epoch=_required_integer(value, "conversation_epoch"),
        content_class=ContentClass(_required_text(value, "content_class")),
        text=_required_text(value, "text"),
        repository=_optional_text(value, "repository"),
        cwd=_optional_text(value, "cwd"),
        submitted_by=SubmittedBy(_required_text(value, "submitted_by")),
        submission_evidence=tuple(
            item for item in raw_evidence if isinstance(item, str)
        ),
        submission_match_cardinality=_required_integer(
            value, "submission_match_cardinality"
        ),
    )
    return PhysicalMessageCandidate(
        message=message,
        record_family=CodexRecordFamily(_required_text(value, "record_family")),
    )


def _serialize_parser_state(
    provider: Provider, state: ClaudeParserState | CodexParserState
) -> dict[str, object]:
    if provider is Provider.CLAUDE:
        if not isinstance(state, ClaudeParserState):
            raise TypeError("Claude source produced non-Claude parser state")
        return {
            "next_conversation_epoch": state.next_conversation_epoch,
            "seen_compaction_uuids": list(state.seen_compaction_uuids),
        }
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


def _string_tuple(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = value.get(key)
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"{key} must be a string array")
    return tuple(item for item in raw if isinstance(item, str))


def _deserialize_parser_state(
    provider: Provider, value: object
) -> ClaudeParserState | CodexParserState:
    if not _is_json_object(value):
        raise ValueError("parser state must be an object")
    if provider is Provider.CLAUDE:
        return ClaudeParserState(
            next_conversation_epoch=_required_integer(value, "next_conversation_epoch"),
            seen_compaction_uuids=_string_tuple(value, "seen_compaction_uuids"),
        )
    raw_kind = value.get("session_kind")
    if raw_kind is not None and not isinstance(raw_kind, str):
        raise ValueError("session_kind must be a string or null")
    return CodexParserState(
        next_conversation_epoch=_required_integer(value, "next_conversation_epoch"),
        session_kind=SessionKind(raw_kind) if raw_kind is not None else None,
        seen_compaction_digests=_string_tuple(value, "seen_compaction_digests"),
        trailing_candidate=(
            _deserialize_codex_candidate(value["trailing_candidate"])
            if value.get("trailing_candidate") is not None
            else None
        ),
        source_session_id=_optional_text(value, "source_session_id"),
    )


def _legacy_roots(
    claude_root: Path | None, codex_root: Path | None
) -> tuple[ConfiguredSourceRoot, ...]:
    if claude_root is None or codex_root is None:
        raise ValueError("claude_root and codex_root must be supplied together")
    values = (
        (Provider.CLAUDE, claude_root.resolve()),
        (Provider.CODEX, codex_root.resolve()),
    )
    return tuple(
        ConfiguredSourceRoot(
            provider=provider,
            path=path,
            source_root_id=source_root_id(provider, path),
        )
        for provider, path in values
    )


def _resolved_roots(
    *,
    source_roots: Sequence[ConfiguredSourceRoot] | None,
    claude_root: Path | None,
    codex_root: Path | None,
) -> tuple[ConfiguredSourceRoot, ...]:
    if source_roots is not None:
        if claude_root is not None or codex_root is not None:
            raise ValueError("source_roots cannot be combined with singular roots")
        roots = tuple(source_roots)
    else:
        roots = _legacy_roots(claude_root, codex_root)
    keys = [(root.provider, root.path.resolve()) for root in roots]
    identities = [root.source_root_id for root in roots]
    if len(set(keys)) != len(keys) or len(set(identities)) != len(identities):
        raise ValueError("configured source roots must be unique")
    if any(
        root.path != root.path.resolve()
        or root.source_root_id != source_root_id(root.provider, root.path)
        for root in roots
    ):
        raise ValueError(
            "configured source roots must use IDs derived from provider "
            "and resolved path"
        )
    return roots


@contextmanager
def _refresh_owner(
    connection: psycopg.Connection,
    progress: ProgressCallback | None,
) -> Iterator[None]:
    connection.execute(f"LISTEN {INDEX_NOTIFY_CHANNEL}")
    while not next(
        connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            (INDEX_QUEUE_LOCK,),
        )
    )[0]:
        if progress is not None:
            active = next(
                connection.execute(
                    """
                    SELECT run.run_id, run.owner_pid
                    FROM cc_search_chats.refresh_run AS run
                    WHERE run.status = 'building'
                      AND EXISTS (
                          SELECT 1
                          FROM pg_locks AS lock
                          WHERE lock.pid = run.owner_pid
                            AND lock.locktype = 'advisory'
                            AND lock.granted
                      )
                    ORDER BY run.run_id DESC
                    LIMIT 1
                    """
                ),
                None,
            )
            progress(
                RefreshProgress(
                    phase="scan",
                    state="waiting_for_index",
                    run_id=active[0] if active is not None else None,
                    owner_pid=active[1] if active is not None else None,
                )
            )
        for _notification in connection.notifies(
            timeout=_WAIT_HEARTBEAT_SECONDS,
            stop_after=1,
        ):
            pass
    connection.execute(f"UNLISTEN {INDEX_NOTIFY_CHANNEL}")
    try:
        yield
    finally:
        connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (INDEX_QUEUE_LOCK,),
        )
        connection.execute(
            "SELECT pg_notify(%s, %s)",
            (INDEX_NOTIFY_CHANNEL, "released"),
        )


def _sync_roots(
    connection: psycopg.Connection, roots: tuple[ConfiguredSourceRoot, ...]
) -> None:
    for configured_order, root in enumerate(roots):
        connection.execute(
            """
            INSERT INTO cc_search_chats.source_root_current (
                source_root_id, provider, resolved_path, configured_order
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_root_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                resolved_path = EXCLUDED.resolved_path,
                configured_order = EXCLUDED.configured_order
            WHERE ROW(
                source_root_current.provider,
                source_root_current.resolved_path,
                source_root_current.configured_order
            ) IS DISTINCT FROM ROW(
                EXCLUDED.provider,
                EXCLUDED.resolved_path,
                EXCLUDED.configured_order
            )
            """,
            (
                root.source_root_id,
                root.provider.value,
                str(root.path),
                configured_order,
            ),
        )
    connection.execute(
        """
        DELETE FROM cc_search_chats.source_root_current
        WHERE NOT (source_root_id = ANY(%s::text[]))
          AND NOT EXISTS (
              SELECT 1
              FROM cc_search_chats.source_file_current AS source
              WHERE source.source_root_id =
                    source_root_current.source_root_id
          )
        """,
        ([root.source_root_id for root in roots],),
    )


def _load_checkpoints(
    connection: psycopg.Connection,
) -> dict[tuple[str, Path], _Checkpoint]:
    return {
        (source_root, Path(relative)): _Checkpoint(
            source_root_id=source_root,
            source_file_relative=Path(relative),
            file_device=device,
            file_inode=inode,
            observed_size=size,
            observed_mtime_ns=mtime_ns,
            complete_byte_offset=offset,
            next_record_ordinal=ordinal,
            next_source_line=line,
            parser_state_version=state_version,
            parser_state=state,
            source_status=status,
            skipped_record_count=skipped_record_count,
        )
        for (
            source_root,
            relative,
            device,
            inode,
            size,
            mtime_ns,
            offset,
            ordinal,
            line,
            state_version,
            state,
            status,
            skipped_record_count,
        ) in connection.execute(
            """
            SELECT source_root_id, source_file_relative, file_device, file_inode,
                   observed_size, observed_mtime_ns, complete_byte_offset,
                   next_record_ordinal, next_source_line, parser_state_version,
                   parser_state, source_status, skipped_record_count
            FROM cc_search_chats.source_file_current
            """
        )
    }


def _load_failed_observations(
    connection: psycopg.Connection,
) -> dict[tuple[str, Path], _FailedObservation]:
    return {
        (source_root, Path(relative)): _FailedObservation(
            source_root_id=source_root,
            source_file_relative=Path(relative),
            file_device=device,
            file_inode=inode,
            observed_size=size,
            observed_mtime_ns=mtime_ns,
            parser_state_version=parser_version,
            failure_record_ordinal=record_ordinal,
            failure_source_line=source_line,
            failure_source_byte_offset=source_byte_offset,
            failure_code=failure_code,
            failure_class=failure_class,
            attempted_content_bytes=attempted_bytes,
            consecutive_failures=consecutive_failures,
            retry_eligible=retry_eligible,
        )
        for (
            source_root,
            relative,
            device,
            inode,
            size,
            mtime_ns,
            parser_version,
            record_ordinal,
            source_line,
            source_byte_offset,
            failure_code,
            failure_class,
            attempted_bytes,
            consecutive_failures,
            retry_eligible,
        ) in connection.execute(
            """
            SELECT source_root_id, source_file_relative, file_device,
                   file_inode, observed_size, observed_mtime_ns,
                   parser_state_version, failure_record_ordinal,
                   failure_source_line, failure_source_byte_offset,
                   failure_code, failure_class, attempted_content_bytes,
                   consecutive_failures,
                   retry_after IS NULL OR retry_after <= now()
            FROM cc_search_chats.source_failure_current
            """
        )
    }


def _discover_sources(
    roots: tuple[ConfiguredSourceRoot, ...],
) -> tuple[
    tuple[_ObservedSource, ...],
    frozenset[str],
    frozenset[tuple[str, Path]],
    tuple[dict[str, object], ...],
]:
    observed: list[_ObservedSource] = []
    complete_roots: set[str] = set()
    discovered_keys: set[tuple[str, Path]] = set()
    failures: list[dict[str, object]] = []
    for root in roots:
        discovery = (
            discover_claude_sources(root.path, inspect_content=False)
            if root.provider is Provider.CLAUDE
            else discover_codex_sources(root.path, inspect_content=False)
        )
        if any(
            diagnostic.code in _ROOT_FAILURES for diagnostic in discovery.diagnostics
        ):
            raise RuntimeError(
                "one or more native provider roots are unavailable: "
                f"{root.provider.value}:{root.path}"
            )
        traversal_failures = [
            diagnostic
            for diagnostic in discovery.diagnostics
            if diagnostic.code in _TRAVERSAL_FAILURES
        ]
        if not traversal_failures:
            complete_roots.add(root.source_root_id)
        failures.extend(
            {
                "code": diagnostic.code.value,
                "path": str(diagnostic.path),
                "detail": diagnostic.detail,
            }
            for diagnostic in traversal_failures
        )
        for source in discovery.sources:
            discovered_keys.add((root.source_root_id, source.source_file_relative))
            try:
                stat = source.path.stat()
            except OSError as error:
                failures.append(
                    {
                        "code": SourceDiagnosticCode.UNREADABLE_SOURCE.value,
                        "path": str(source.path),
                        "detail": str(error),
                    }
                )
                continue
            observed.append(
                _ObservedSource(
                    root=root,
                    source=source,
                    file_device=stat.st_dev,
                    file_inode=stat.st_ino,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
    return (
        tuple(
            sorted(
                observed,
                key=lambda value: (
                    value.root.provider.value,
                    value.root.source_root_id,
                    value.source.source_file_relative.as_posix(),
                ),
            )
        ),
        frozenset(complete_roots),
        frozenset(discovered_keys),
        tuple(failures),
    )


def _plan_source(
    observed: _ObservedSource, checkpoint: _Checkpoint | None
) -> _SourcePlan | None:
    version = _PARSER_STATE_VERSIONS[observed.root.provider]
    if checkpoint is None:
        return _SourcePlan(observed, "replace", 0, 0, 1, None, 0)
    same_identity = (
        observed.file_device == checkpoint.file_device
        and observed.file_inode == checkpoint.file_inode
    )
    same_metadata = (
        same_identity
        and observed.size == checkpoint.observed_size
        and observed.mtime_ns == checkpoint.observed_mtime_ns
    )
    if same_metadata and checkpoint.parser_state_version == version:
        return None
    if (
        checkpoint.source_status == "indexed"
        and checkpoint.parser_state_version == version
        and same_identity
        and observed.size > checkpoint.observed_size
    ):
        try:
            prior_state = _deserialize_parser_state(
                observed.root.provider, checkpoint.parser_state
            )
        except ValueError:
            pass
        else:
            return _SourcePlan(
                observed=observed,
                disposition="append",
                start_byte_offset=checkpoint.complete_byte_offset,
                next_record_ordinal=checkpoint.next_record_ordinal,
                next_source_line=checkpoint.next_source_line,
                prior_state=prior_state,
                prior_skipped_record_count=checkpoint.skipped_record_count,
            )
    return _SourcePlan(observed, "replace", 0, 0, 1, None, 0)


def _failed_observation_matches(
    observed: _ObservedSource,
    failure: _FailedObservation | None,
) -> bool:
    return failure is not None and (
        observed.file_device,
        observed.file_inode,
        observed.size,
        observed.mtime_ns,
        _PARSER_STATE_VERSIONS[observed.root.provider],
    ) == (
        failure.file_device,
        failure.file_inode,
        failure.observed_size,
        failure.observed_mtime_ns,
        failure.parser_state_version,
    )


def _failed_observation_blocks(
    observed: _ObservedSource,
    failure: _FailedObservation | None,
    *,
    force_retry: bool,
) -> bool:
    if force_retry or not _failed_observation_matches(observed, failure):
        return False
    if failure is None:
        return False
    return failure.failure_class == "deterministic" or not failure.retry_eligible


def _create_stage_tables(connection: psycopg.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS pg_temp.refresh_stage_message")
    connection.execute("DROP TABLE IF EXISTS pg_temp.refresh_stage_message_batch")
    connection.execute("DROP TABLE IF EXISTS pg_temp.refresh_stage_source")
    connection.execute("DROP TABLE IF EXISTS pg_temp.refresh_stage_removed")
    connection.execute(
        """
        CREATE TEMP TABLE refresh_stage_message (
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            canonical_locator text NOT NULL,
            timestamp_text text NOT NULL,
            role text NOT NULL,
            session_kind text NOT NULL,
            conversation_epoch integer NOT NULL,
            content_class text NOT NULL,
            prose_content text NOT NULL,
            repository text,
            cwd text,
            submitted_by text NOT NULL,
            embedding_input_digest text NOT NULL,
            source_root_id text NOT NULL,
            alias_locator text NOT NULL,
            source_file_relative text NOT NULL,
            record_ordinal bigint NOT NULL,
            source_line bigint NOT NULL,
            source_byte_offset bigint NOT NULL,
            raw_byte_length bigint NOT NULL,
            source_digest text NOT NULL,
            PRIMARY KEY (
                source_root_id, source_file_relative, record_ordinal, content_class
            )
        ) ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE refresh_stage_message_batch
        (LIKE refresh_stage_message EXCLUDING CONSTRAINTS)
        ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE refresh_stage_source (
            source_root_id text NOT NULL,
            source_file_relative text NOT NULL,
            disposition text NOT NULL,
            file_device bigint NOT NULL,
            file_inode bigint NOT NULL,
            observed_size bigint NOT NULL,
            observed_mtime_ns bigint NOT NULL,
            complete_byte_offset bigint NOT NULL,
            next_record_ordinal bigint NOT NULL,
            next_source_line bigint NOT NULL,
            parser_state_version integer NOT NULL,
            parser_state jsonb NOT NULL,
            source_status text NOT NULL,
            pending_bytes bigint NOT NULL,
            skipped_record_count bigint NOT NULL,
            advanced boolean NOT NULL,
            PRIMARY KEY (source_root_id, source_file_relative)
        ) ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE refresh_stage_removed (
            source_root_id text NOT NULL,
            source_file_relative text NOT NULL,
            PRIMARY KEY (source_root_id, source_file_relative)
        ) ON COMMIT PRESERVE ROWS
        """
    )


_STAGE_MESSAGE_COLUMNS = """
    provider, source_session_id, logical_message_id, canonical_locator,
    timestamp_text, role, session_kind, conversation_epoch, content_class,
    prose_content, repository, cwd, submitted_by, embedding_input_digest,
    source_root_id, alias_locator, source_file_relative, record_ordinal,
    source_line, source_byte_offset, raw_byte_length, source_digest
"""


def _stage_messages(
    connection: psycopg.Connection,
    plan: _SourcePlan,
    messages: tuple[NativeMessage, ...],
) -> None:
    if not messages:
        return
    connection.execute("TRUNCATE pg_temp.refresh_stage_message_batch")
    relative = plan.observed.source.source_file_relative
    with connection.cursor().copy(
        f"COPY pg_temp.refresh_stage_message_batch ({_STAGE_MESSAGE_COLUMNS}) "
        "FROM STDIN"
    ) as copy:
        for message in messages:
            identity = message.identity
            locator = identity.canonical_locator
            base = (
                locator.provider.value,
                locator.source_session_id,
                identity.logical_message_id,
                format_locator(locator),
                message.timestamp,
                message.role,
                message.session_kind.value,
                message.conversation_epoch,
                message.content_class.value,
                message.text,
                message.repository,
                message.cwd,
                message.submitted_by.value,
                hashlib.sha256(message.text.encode("utf-8")).hexdigest(),
            )
            for alias in identity.physical_aliases:
                if alias.source_file_relative != relative:
                    raise _SourceRefreshError(
                        "provider parser returned an alias from another source"
                    )
                copy.write_row(
                    (
                        *base,
                        plan.observed.root.source_root_id,
                        format_locator(alias.locator),
                        alias.source_file_relative.as_posix(),
                        alias.record_ordinal,
                        alias.source_line,
                        alias.source_byte_offset,
                        alias.raw_byte_length,
                        alias.source_digest,
                    )
                )
    conflict = next(
        connection.execute(
            """
            SELECT min(alias_locator)
            FROM pg_temp.refresh_stage_message_batch
            GROUP BY source_root_id, source_file_relative, record_ordinal,
                     content_class
            HAVING count(DISTINCT jsonb_build_array(
                provider, source_session_id, logical_message_id,
                canonical_locator, timestamp_text, role, session_kind,
                conversation_epoch, prose_content, repository, cwd,
                submitted_by, embedding_input_digest, alias_locator,
                source_line, source_byte_offset, raw_byte_length, source_digest
            )) > 1
            LIMIT 1
            """
        ),
        None,
    )
    if conflict is not None:
        raise _SourceRefreshError(
            f"provider emitted conflicting content for {conflict[0]}"
        )
    connection.execute(
        """
        DELETE FROM pg_temp.refresh_stage_message AS staged
        USING pg_temp.refresh_stage_message_batch AS batch
        WHERE (staged.source_root_id, staged.source_file_relative,
               staged.record_ordinal, staged.content_class) =
              (batch.source_root_id, batch.source_file_relative,
               batch.record_ordinal, batch.content_class)
        """
    )
    connection.execute(
        sql.SQL(
            """
        INSERT INTO pg_temp.refresh_stage_message ({columns})
        SELECT DISTINCT ON (
            source_root_id, source_file_relative, record_ordinal, content_class
        ) {columns}
        FROM pg_temp.refresh_stage_message_batch
        ORDER BY source_root_id, source_file_relative, record_ordinal,
                 content_class, alias_locator
        """
        ).format(columns=sql.SQL(_STAGE_MESSAGE_COLUMNS))
    )


def _parse_batch(
    plan: _SourcePlan,
    envelopes: tuple[RecordEnvelope, ...],
    diagnostics: tuple[SourceDiagnostic, ...],
    state: ClaudeParserState | CodexParserState | None,
) -> ClaudeParseResult | CodexParseResult:
    provider = plan.observed.root.provider
    if provider is Provider.CLAUDE:
        if state is not None and not isinstance(state, ClaudeParserState):
            raise _SourceRefreshError("invalid Claude continuation state")
        parsed = parse_claude_session(
            envelopes,
            context=ClaudeSessionContext(
                source_session_id=plan.observed.source.source_file_relative.stem
            ),
            prior_state=state,
        )
        unsupported = next(
            (
                diagnostic
                for diagnostic in parsed.diagnostics
                if diagnostic.code in _UNSUPPORTED_CLAUDE_DIAGNOSTICS
            ),
            None,
        )
        if unsupported is not None:
            raise _SourceRefreshError(
                "unsupported Claude record "
                f"{unsupported.code.value} at ordinal {unsupported.record_ordinal}",
                code=unsupported.code.value,
                failure_class="deterministic",
                record_ordinal=unsupported.record_ordinal,
                source_line=unsupported.source_line,
                source_byte_offset=unsupported.source_byte_offset,
            )
        return parsed
    if state is not None and not isinstance(state, CodexParserState):
        raise _SourceRefreshError("invalid Codex continuation state")
    parsed = parse_codex_session(
        envelopes,
        context=CodexSessionContext(),
        source_diagnostics=diagnostics,
        prior_state=state,
    )
    unsupported = next(
        (
            diagnostic
            for diagnostic in parsed.diagnostics
            if diagnostic.code in _UNSUPPORTED_CODEX_DIAGNOSTICS
        ),
        None,
    )
    if unsupported is not None:
        raise _SourceRefreshError(
            "unsupported Codex record "
            f"{unsupported.code.value} at ordinal {unsupported.record_ordinal}",
            code=unsupported.code.value,
            failure_class="deterministic",
            record_ordinal=unsupported.record_ordinal,
            source_line=unsupported.source_line,
            source_byte_offset=unsupported.source_byte_offset,
        )
    return parsed


def _skipped_record_diagnostics(
    plan: _SourcePlan,
    parsed: ClaudeParseResult | CodexParseResult,
    source_diagnostics: tuple[SourceDiagnostic, ...],
) -> tuple[dict[str, object], ...]:
    """Describe each intentionally omitted physical record exactly once."""
    provider = plan.observed.root.provider
    candidates: list[tuple[str, str, int | None, int | None, int | None]] = []
    if provider is Provider.CLAUDE:
        if not isinstance(parsed, ClaudeParseResult):
            raise _SourceRefreshError("Claude source produced a non-Claude result")
        candidates.extend(
            (
                diagnostic.code.value,
                diagnostic.detail,
                diagnostic.record_ordinal,
                diagnostic.source_line,
                diagnostic.source_byte_offset,
            )
            for diagnostic in parsed.diagnostics
            if diagnostic.code in _SKIPPABLE_CLAUDE_DIAGNOSTICS
        )
    else:
        if not isinstance(parsed, CodexParseResult):
            raise _SourceRefreshError("Codex source produced a non-Codex result")
        candidates.extend(
            (
                diagnostic.code.value,
                diagnostic.detail,
                diagnostic.record_ordinal,
                diagnostic.source_line,
                diagnostic.source_byte_offset,
            )
            for diagnostic in parsed.diagnostics
            if diagnostic.code in _SKIPPABLE_CODEX_DIAGNOSTICS
        )
    candidates.extend(
        (
            diagnostic.code.value,
            diagnostic.detail,
            diagnostic.record_ordinal,
            diagnostic.source_line,
            diagnostic.source_byte_offset,
        )
        for diagnostic in source_diagnostics
        if diagnostic.code is SourceDiagnosticCode.OVERSIZED_RECORD
    )

    by_ordinal: dict[int, dict[str, object]] = {}
    for reason, detail, record_ordinal, source_line, source_byte_offset in candidates:
        if record_ordinal is None or source_line is None or source_byte_offset is None:
            raise _SourceRefreshError(
                f"skipped record {reason} lacks complete source coordinates"
            )
        existing = by_ordinal.get(record_ordinal)
        if existing is not None:
            if existing["reason"] != reason:
                raise _SourceRefreshError(
                    "one skipped record has conflicting diagnostic reasons"
                )
            continue
        by_ordinal[record_ordinal] = {
            "code": "record_skipped",
            "provider": provider.value,
            "source_root_id": plan.observed.root.source_root_id,
            "source_file_relative": (
                plan.observed.source.source_file_relative.as_posix()
            ),
            "record_ordinal": record_ordinal,
            "source_line": source_line,
            "source_byte_offset": source_byte_offset,
            "reason": reason,
            "detail": detail,
        }
    return tuple(by_ordinal[ordinal] for ordinal in sorted(by_ordinal))


def _messages_without_skipped_records(
    messages: tuple[NativeMessage, ...], skipped_ordinals: frozenset[int]
) -> tuple[NativeMessage, ...]:
    """Suppress every projection whose physical record was skipped."""
    return tuple(
        message
        for message in messages
        if all(
            alias.record_ordinal not in skipped_ordinals
            for alias in message.identity.physical_aliases
        )
    )


def _stage_source_checkpoint(
    connection: psycopg.Connection,
    plan: _SourcePlan,
    *,
    complete_byte_offset: int,
    next_record_ordinal: int,
    next_source_line: int,
    parser_state: object,
    source_status: str,
    final_size: int,
    skipped_record_count: int,
) -> tuple[bool, int]:
    observed = plan.observed
    advanced = final_size > observed.size
    pending_bytes = max(0, final_size - complete_byte_offset)
    connection.execute(
        """
        INSERT INTO pg_temp.refresh_stage_source (
            source_root_id, source_file_relative, disposition,
            file_device, file_inode, observed_size, observed_mtime_ns,
            complete_byte_offset, next_record_ordinal, next_source_line,
            parser_state_version, parser_state, source_status, pending_bytes,
            skipped_record_count, advanced
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s
        )
        ON CONFLICT (source_root_id, source_file_relative) DO UPDATE SET
            disposition = EXCLUDED.disposition,
            file_device = EXCLUDED.file_device,
            file_inode = EXCLUDED.file_inode,
            observed_size = EXCLUDED.observed_size,
            observed_mtime_ns = EXCLUDED.observed_mtime_ns,
            complete_byte_offset = EXCLUDED.complete_byte_offset,
            next_record_ordinal = EXCLUDED.next_record_ordinal,
            next_source_line = EXCLUDED.next_source_line,
            parser_state_version = EXCLUDED.parser_state_version,
            parser_state = EXCLUDED.parser_state,
            source_status = EXCLUDED.source_status,
            pending_bytes = EXCLUDED.pending_bytes,
            skipped_record_count = EXCLUDED.skipped_record_count,
            advanced = EXCLUDED.advanced
        """,
        (
            observed.root.source_root_id,
            observed.source.source_file_relative.as_posix(),
            plan.disposition,
            observed.file_device,
            observed.file_inode,
            observed.size,
            observed.mtime_ns,
            complete_byte_offset,
            next_record_ordinal,
            next_source_line,
            _PARSER_STATE_VERSIONS[observed.root.provider],
            Jsonb(parser_state),
            source_status,
            pending_bytes,
            skipped_record_count,
            advanced,
        ),
    )
    return advanced, pending_bytes


def _parse_and_stage_source(
    connection: psycopg.Connection, plan: _SourcePlan
) -> tuple[bool, int, int, tuple[dict[str, object], ...]]:
    observed = plan.observed
    artifact = inspect_non_native_artifact(observed.source.path)
    if artifact is not None:
        if artifact.code not in {
            SourceDiagnosticCode.NON_NATIVE_AGY,
            SourceDiagnosticCode.NON_NATIVE_TRANSPORT_ARCHIVE,
        }:
            raise _SourceRefreshError(
                "native source inspection failed: "
                f"{artifact.code.value}: {artifact.detail}",
                code=artifact.code.value,
                failure_class=(
                    "transient"
                    if artifact.code is SourceDiagnosticCode.UNREADABLE_SOURCE
                    else "deterministic"
                ),
                record_ordinal=artifact.record_ordinal,
                source_line=artifact.source_line,
                source_byte_offset=artifact.source_byte_offset,
            )
        advanced, pending = _stage_source_checkpoint(
            connection,
            plan,
            complete_byte_offset=0,
            next_record_ordinal=0,
            next_source_line=1,
            parser_state={
                "excluded_code": artifact.code.value,
                "detail": artifact.detail,
            },
            source_status="excluded",
            final_size=observed.size,
            skipped_record_count=0,
        )
        return advanced, pending, 0, ()

    offset = plan.start_byte_offset
    ordinal = plan.next_record_ordinal
    source_line = plan.next_source_line
    state: ClaudeParserState | CodexParserState | None = plan.prior_state
    skipped_diagnostics: list[dict[str, object]] = []
    if observed.size == offset and state is None:
        state = (
            ClaudeParserState()
            if observed.root.provider is Provider.CLAUDE
            else CodexParserState()
        )
    while offset < observed.size:
        batch = read_bounded_jsonl(
            observed.source.path,
            source_file_relative=observed.source.source_file_relative,
            target_size=observed.size,
            start_byte_offset=offset,
            next_record_ordinal=ordinal,
            next_source_line=source_line,
        )
        if batch.stop_reason not in {
            BoundedReadStopReason.TARGET_REACHED,
            BoundedReadStopReason.BATCH_LIMIT_REACHED,
            BoundedReadStopReason.PARTIAL_TAIL,
        }:
            raise _SourceRefreshError(
                f"native source stopped at {batch.stop_reason.value}",
                code=batch.stop_reason.value,
                failure_class=(
                    "deterministic"
                    if batch.stop_reason is BoundedReadStopReason.OVERSIZED_RECORD
                    else "transient"
                ),
                attempted_content_bytes=max(
                    0, batch.next_source_byte_offset - plan.start_byte_offset
                ),
            )
        try:
            parsed = _parse_batch(
                plan,
                batch.envelopes,
                batch.diagnostics,
                state,
            )
        except _SourceRefreshError as error:
            error.attempted_content_bytes = max(
                error.attempted_content_bytes,
                batch.next_source_byte_offset - plan.start_byte_offset,
            )
            raise
        batch_skips = _skipped_record_diagnostics(
            plan,
            parsed,
            batch.diagnostics,
        )
        skipped_diagnostics.extend(batch_skips)
        skipped_ordinals = frozenset(
            _required_integer(diagnostic, "record_ordinal")
            for diagnostic in batch_skips
        )
        _stage_messages(
            connection,
            plan,
            _messages_without_skipped_records(parsed.messages, skipped_ordinals),
        )
        state = parsed.next_state
        next_offset = batch.next_source_byte_offset
        ordinal = batch.next_record_ordinal
        source_line = batch.next_source_line
        if batch.stop_reason is BoundedReadStopReason.BATCH_LIMIT_REACHED:
            if next_offset <= offset:
                raise _SourceRefreshError("native source batch made no progress")
            offset = next_offset
            continue
        offset = next_offset
        break
    if state is None:
        raise _SourceRefreshError("provider parser did not produce continuation state")
    try:
        final = observed.source.path.stat()
    except OSError as error:
        raise _SourceRefreshError(
            f"native source final stat failed: {error}"
        ) from error
    if (final.st_dev, final.st_ino) != (
        observed.file_device,
        observed.file_inode,
    ):
        raise _SourceRefreshError("native source was replaced during refresh")
    if final.st_size < observed.size:
        raise _SourceRefreshError("native source was truncated during refresh")
    if final.st_size == observed.size and final.st_mtime_ns != observed.mtime_ns:
        raise _SourceRefreshError("native source changed during its bounded read")
    advanced, pending = _stage_source_checkpoint(
        connection,
        plan,
        complete_byte_offset=offset,
        next_record_ordinal=ordinal,
        next_source_line=source_line,
        parser_state=_serialize_parser_state(observed.root.provider, state),
        source_status="indexed",
        final_size=final.st_size,
        skipped_record_count=(
            plan.prior_skipped_record_count + len(skipped_diagnostics)
        ),
    )
    return (
        advanced,
        pending,
        max(0, offset - plan.start_byte_offset),
        tuple(skipped_diagnostics),
    )


def _clear_staged_source(connection: psycopg.Connection, plan: _SourcePlan) -> None:
    key = (
        plan.observed.root.source_root_id,
        plan.observed.source.source_file_relative.as_posix(),
    )
    connection.execute(
        "DELETE FROM pg_temp.refresh_stage_message "
        "WHERE source_root_id = %s AND source_file_relative = %s",
        key,
    )
    connection.execute(
        "DELETE FROM pg_temp.refresh_stage_source "
        "WHERE source_root_id = %s AND source_file_relative = %s",
        key,
    )


def _record_failed_observation(
    connection: psycopg.Connection,
    *,
    plan: _SourcePlan,
    run_id: int,
    error: _SourceRefreshError,
    previous: _FailedObservation | None,
) -> None:
    observed = plan.observed
    same_failure = (
        _failed_observation_matches(observed, previous)
        and previous is not None
        and (
            previous.failure_class,
            previous.failure_code,
            previous.failure_record_ordinal,
            previous.failure_source_line,
            previous.failure_source_byte_offset,
        )
        == (
            error.failure_class,
            error.code,
            error.record_ordinal,
            error.source_line,
            error.source_byte_offset,
        )
    )
    consecutive_failures = (
        previous.consecutive_failures + 1
        if same_failure and previous is not None
        else 1
    )
    retry_seconds = min(300, 5 * (2 ** min(consecutive_failures - 1, 6)))
    connection.execute(
        """
        INSERT INTO cc_search_chats.source_failure_current (
            source_root_id, source_file_relative, provider,
            file_device, file_inode, observed_size, observed_mtime_ns,
            parser_state_version, failure_record_ordinal,
            failure_source_line, failure_source_byte_offset,
            failure_code, failure_detail, failure_class,
            attempted_content_bytes, consecutive_failures,
            retry_after, last_run_id
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s,
            CASE WHEN %s = 'transient'
                 THEN now() + make_interval(secs => %s)
                 ELSE NULL END,
            %s
        )
        ON CONFLICT (source_root_id, source_file_relative) DO UPDATE SET
            provider = EXCLUDED.provider,
            file_device = EXCLUDED.file_device,
            file_inode = EXCLUDED.file_inode,
            observed_size = EXCLUDED.observed_size,
            observed_mtime_ns = EXCLUDED.observed_mtime_ns,
            parser_state_version = EXCLUDED.parser_state_version,
            failure_record_ordinal = EXCLUDED.failure_record_ordinal,
            failure_source_line = EXCLUDED.failure_source_line,
            failure_source_byte_offset = EXCLUDED.failure_source_byte_offset,
            failure_code = EXCLUDED.failure_code,
            failure_detail = EXCLUDED.failure_detail,
            failure_class = EXCLUDED.failure_class,
            attempted_content_bytes = EXCLUDED.attempted_content_bytes,
            consecutive_failures = EXCLUDED.consecutive_failures,
            first_failed_at = CASE
                WHEN %s THEN source_failure_current.first_failed_at
                ELSE now()
            END,
            last_failed_at = now(),
            retry_after = EXCLUDED.retry_after,
            last_run_id = EXCLUDED.last_run_id
        """,
        (
            observed.root.source_root_id,
            observed.source.source_file_relative.as_posix(),
            observed.root.provider.value,
            observed.file_device,
            observed.file_inode,
            observed.size,
            observed.mtime_ns,
            _PARSER_STATE_VERSIONS[observed.root.provider],
            error.record_ordinal,
            error.source_line,
            error.source_byte_offset,
            error.code,
            str(error),
            error.failure_class,
            error.attempted_content_bytes,
            consecutive_failures,
            error.failure_class,
            retry_seconds,
            run_id,
            same_failure,
        ),
    )


def _delete_obsolete_failed_observations(
    connection: psycopg.Connection,
    failures: Mapping[tuple[str, Path], _FailedObservation],
    discovered_keys: frozenset[tuple[str, Path]],
    complete_roots: frozenset[str],
) -> None:
    obsolete = [
        (root_id, relative.as_posix())
        for root_id, relative in failures
        if root_id in complete_roots and (root_id, relative) not in discovered_keys
    ]
    if not obsolete:
        return
    connection.execute(
        """
        DELETE FROM cc_search_chats.source_failure_current AS failure
        USING unnest(%s::text[], %s::text[]) AS removed(
            source_root_id, source_file_relative
        )
        WHERE (failure.source_root_id, failure.source_file_relative) =
              (removed.source_root_id, removed.source_file_relative)
        """,
        (
            [root_id for root_id, _relative in obsolete],
            [relative for _root_id, relative in obsolete],
        ),
    )


def _start_run(
    connection: psycopg.Connection,
    *,
    source_count: int,
    changed_source_count: int,
    removed_source_count: int,
) -> int:
    with connection.transaction():
        connection.execute(
            """
            UPDATE cc_search_chats.refresh_run
            SET status = 'failed', completed_at = now(),
                phase = 'done', heartbeat_at = now(),
                completed_units = total_units,
                diagnostics = diagnostics || %s::jsonb
            WHERE status = 'building'
            """,
            (
                Jsonb(
                    [
                        {
                            "code": "abandoned_refresh",
                            "detail": "exclusive owner ended before publication",
                        }
                    ]
                ),
            ),
        )
        return next(
            connection.execute(
                """
                INSERT INTO cc_search_chats.refresh_run (
                    status, source_count, changed_source_count,
                    removed_source_count, owner_pid, phase, heartbeat_at,
                    completed_units, total_units
                ) VALUES (
                    'building', %s, %s, %s, pg_backend_pid(),
                    'parse', now(), 0, %s
                )
                RETURNING run_id
                """,
                (
                    source_count,
                    changed_source_count,
                    removed_source_count,
                    changed_source_count,
                ),
            )
        )[0]


def _update_run_progress(
    connection: psycopg.Connection,
    run_id: int,
    *,
    phase: str,
    completed_units: int,
) -> None:
    connection.execute(
        """
        UPDATE cc_search_chats.refresh_run
        SET phase = %s, heartbeat_at = now(), completed_units = %s
        WHERE run_id = %s AND status = 'building'
        """,
        (phase, completed_units, run_id),
    )


def _record_run_failure(
    connection: psycopg.Connection,
    run_id: int,
    diagnostics: tuple[dict[str, object], ...],
) -> None:
    try:
        connection.execute(
            """
            UPDATE cc_search_chats.refresh_run
            SET status = 'failed', completed_at = now(), diagnostics = %s,
                phase = 'done', heartbeat_at = now(),
                completed_units = total_units
            WHERE run_id = %s AND status = 'building'
            """,
            (Jsonb(list(diagnostics)), run_id),
        )
    except psycopg.Error:
        return


def _stage_removed_sources(
    connection: psycopg.Connection,
    checkpoints: Mapping[tuple[str, Path], _Checkpoint],
    observed_keys: set[tuple[str, Path]],
    configured_root_ids: set[str],
    complete_root_ids: frozenset[str],
) -> int:
    removed = 0
    for root_id, relative in checkpoints:
        if (root_id, relative) in observed_keys:
            continue
        if root_id in configured_root_ids and root_id not in complete_root_ids:
            continue
        connection.execute(
            """
            INSERT INTO pg_temp.refresh_stage_removed (
                source_root_id, source_file_relative
            ) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (root_id, relative.as_posix()),
        )
        removed += 1
    return removed


def _message_conflict(connection: psycopg.Connection) -> str | None:
    staged = next(
        connection.execute(
            """
            SELECT min(canonical_locator)
            FROM pg_temp.refresh_stage_message
            GROUP BY provider, source_session_id, logical_message_id,
                     content_class
            HAVING count(DISTINCT jsonb_build_array(
                canonical_locator, timestamp_text, role, session_kind,
                conversation_epoch, prose_content, repository,
                submitted_by, embedding_input_digest
            )) > 1
            LIMIT 1
            """
        ),
        None,
    )
    if staged is not None:
        return staged[0]
    current = next(
        connection.execute(
            """
            SELECT min(staged.canonical_locator)
            FROM (
                SELECT DISTINCT ON (
                    provider, source_session_id, logical_message_id, content_class
                ) *
                FROM pg_temp.refresh_stage_message
                ORDER BY provider, source_session_id, logical_message_id,
                         content_class, source_root_id, source_file_relative,
                         record_ordinal
            ) AS staged
            JOIN cc_search_chats.message_current AS current
              USING (provider, source_session_id, logical_message_id, content_class)
            WHERE ROW(
                current.canonical_locator, current.timestamp_text, current.role,
                current.session_kind, current.conversation_epoch,
                current.prose_content, current.repository,
                current.submitted_by, current.embedding_input_digest
            ) IS DISTINCT FROM ROW(
                staged.canonical_locator, staged.timestamp_text, staged.role,
                staged.session_kind, staged.conversation_epoch,
                staged.prose_content, staged.repository,
                staged.submitted_by, staged.embedding_input_digest
            )
              AND EXISTS (
                  SELECT 1
                  FROM cc_search_chats.physical_alias_current AS alias
                  WHERE (alias.provider, alias.source_session_id,
                         alias.logical_message_id, alias.content_class) =
                        (current.provider, current.source_session_id,
                         current.logical_message_id, current.content_class)
              )
            LIMIT 1
            """
        ),
        None,
    )
    return current[0] if current is not None else None


def _create_candidate_projection(connection: psycopg.Connection) -> None:
    """Materialize affected final rows while leaving unchanged rows by reference."""
    for relation in (
        "refresh_candidate_observation",
        "refresh_candidate_message",
        "refresh_candidate_alias",
        "refresh_affected_message",
    ):
        connection.execute(
            sql.SQL("DROP TABLE IF EXISTS pg_temp.{}").format(sql.Identifier(relation))
        )
    connection.execute(
        """
        CREATE TEMP TABLE refresh_affected_message (
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            content_class text NOT NULL,
            PRIMARY KEY (
                provider, source_session_id, logical_message_id, content_class
            )
        ) ON COMMIT PRESERVE ROWS
        """
    )
    _finish_candidate_projection(connection)


def _publish_coherent_candidate(
    connection: psycopg.Connection,
    *,
    corpus_generation: int,
    semantic: CandidateSemanticBuild,
    run_id: int,
    roots: tuple[ConfiguredSourceRoot, ...],
    failed_source_count: int,
    read_source_count: int,
    removed_source_count: int,
    advanced_source_count: int,
    metadata_checked_source_count: int,
    attempted_source_count: int,
    attempted_content_bytes: int,
    blocked_source_count: int,
    transient_failure_source_count: int,
    diagnostics: tuple[dict[str, object], ...],
) -> None:
    """Publish the literal delta and prepared semantic build in one transaction."""
    with connection.transaction():
        _sync_roots(connection, roots)
        connection.execute(
            """
            DELETE FROM cc_search_chats.physical_alias_current AS alias
            USING pg_temp.refresh_affected_message AS affected
            WHERE (alias.provider, alias.source_session_id,
                   alias.logical_message_id, alias.content_class) =
                  (affected.provider, affected.source_session_id,
                   affected.logical_message_id, affected.content_class)
            """
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.message_current AS message
            USING pg_temp.refresh_affected_message AS affected
            WHERE (message.provider, message.source_session_id,
                   message.logical_message_id, message.content_class) =
                  (affected.provider, affected.source_session_id,
                   affected.logical_message_id, affected.content_class)
            """
        )
        connection.execute(
            """
            INSERT INTO cc_search_chats.message_current (
                provider, source_session_id, logical_message_id,
                canonical_locator, timestamp_text, role, session_kind,
                conversation_epoch, content_class, prose_content, repository,
                cwd, submitted_by, embedding_input_digest
            )
            SELECT provider, source_session_id, logical_message_id,
                   canonical_locator, timestamp_text, role, session_kind,
                   conversation_epoch, content_class, prose_content, repository,
                   cwd, submitted_by, embedding_input_digest
            FROM pg_temp.refresh_candidate_message
            """
        )
        connection.execute(
            """
            INSERT INTO cc_search_chats.physical_alias_current (
                provider, source_session_id, logical_message_id, content_class,
                source_root_id, locator, source_file_relative, record_ordinal,
                source_line, source_byte_offset, raw_byte_length, source_digest
            )
            SELECT provider, source_session_id, logical_message_id, content_class,
                   source_root_id, locator, source_file_relative, record_ordinal,
                   source_line, source_byte_offset, raw_byte_length, source_digest
            FROM pg_temp.refresh_candidate_alias
            """
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.source_file_current AS current
            USING pg_temp.refresh_stage_removed AS removed
            WHERE (current.source_root_id, current.source_file_relative) =
                  (removed.source_root_id, removed.source_file_relative)
            """
        )
        connection.execute(
            """
            INSERT INTO cc_search_chats.source_file_current (
                source_root_id, source_file_relative, file_device, file_inode,
                observed_size, observed_mtime_ns, complete_byte_offset,
                next_record_ordinal, next_source_line, parser_state_version,
                parser_state, source_status, pending_bytes, skipped_record_count,
                updated_corpus_generation
            )
            SELECT source_root_id, source_file_relative, file_device, file_inode,
                   observed_size, observed_mtime_ns, complete_byte_offset,
                   next_record_ordinal, next_source_line, parser_state_version,
                   parser_state, source_status, pending_bytes,
                   skipped_record_count, %s
            FROM pg_temp.refresh_stage_source
            ON CONFLICT (source_root_id, source_file_relative) DO UPDATE SET
                file_device = EXCLUDED.file_device,
                file_inode = EXCLUDED.file_inode,
                observed_size = EXCLUDED.observed_size,
                observed_mtime_ns = EXCLUDED.observed_mtime_ns,
                complete_byte_offset = EXCLUDED.complete_byte_offset,
                next_record_ordinal = EXCLUDED.next_record_ordinal,
                next_source_line = EXCLUDED.next_source_line,
                parser_state_version = EXCLUDED.parser_state_version,
                parser_state = EXCLUDED.parser_state,
                source_status = EXCLUDED.source_status,
                pending_bytes = EXCLUDED.pending_bytes,
                skipped_record_count = EXCLUDED.skipped_record_count,
                updated_corpus_generation =
                    EXCLUDED.updated_corpus_generation
            """,
            (corpus_generation,),
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.source_failure_current AS failure
            USING (
                SELECT source_root_id, source_file_relative
                FROM pg_temp.refresh_stage_source
                UNION ALL
                SELECT source_root_id, source_file_relative
                FROM pg_temp.refresh_stage_removed
            ) AS resolved
            WHERE (failure.source_root_id, failure.source_file_relative) =
                  (resolved.source_root_id, resolved.source_file_relative)
            """
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.source_root_current
            WHERE NOT (source_root_id = ANY(%s::text[]))
              AND NOT EXISTS (
                  SELECT 1
                  FROM cc_search_chats.source_file_current AS source
                  WHERE source.source_root_id =
                        source_root_current.source_root_id
              )
            """,
            ([root.source_root_id for root in roots],),
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.semantic_chunk_current AS chunk
            USING pg_temp.semantic_candidate_message AS replacement
            WHERE (chunk.provider, chunk.source_session_id,
                   chunk.logical_message_id, chunk.content_class) =
                  (replacement.provider, replacement.source_session_id,
                   replacement.logical_message_id, replacement.content_class)
            """
        )
        connection.execute(
            """
            INSERT INTO cc_search_chats.semantic_chunk_current
            SELECT * FROM pg_temp.semantic_candidate_chunk
            """
        )
        message_count, alias_count, source_watermarks = next(
            connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM cc_search_chats.message_current),
                  (SELECT count(*)
                   FROM cc_search_chats.physical_alias_current),
                  COALESCE((
                    SELECT jsonb_object_agg(
                        source_root_id || ':' || source_file_relative,
                        jsonb_build_object(
                            'observed_size', observed_size,
                            'complete_byte_offset', complete_byte_offset,
                            'next_record_ordinal', next_record_ordinal,
                            'pending_bytes', pending_bytes
                        )
                        ORDER BY source_root_id, source_file_relative
                    )
                    FROM cc_search_chats.source_file_current
                  ), '{}'::jsonb)
                """
            )
        )
        eligible_messages, chunked_messages, eligible_chunks, mapped_chunks = next(
            connection.execute(
                """
                SELECT
                  (SELECT count(*)
                   FROM cc_search_chats.message_current
                   WHERE content_class = 'prose'
                     AND prose_content ~ '[^[:space:]]'),
                  (SELECT count(DISTINCT (
                       chunk.provider, chunk.source_session_id,
                       chunk.logical_message_id, chunk.content_class
                   ))
                   FROM cc_search_chats.semantic_chunk_current AS chunk
                   JOIN cc_search_chats.message_current AS message
                     USING (provider, source_session_id,
                            logical_message_id, content_class)
                   WHERE chunk.profile_id =
                         'nemotron-3-embed-8b-bf16:chunks-v1'
                     AND chunk.chunker_id =
                         'nemotron-token-chunks-768-1024-96:v1'
                     AND chunk.source_text_digest =
                         message.embedding_input_digest),
                  (SELECT count(*)
                   FROM cc_search_chats.semantic_chunk_current AS chunk
                   JOIN cc_search_chats.message_current AS message
                     USING (provider, source_session_id,
                            logical_message_id, content_class)
                   WHERE chunk.profile_id =
                         'nemotron-3-embed-8b-bf16:chunks-v1'
                     AND chunk.chunker_id =
                         'nemotron-token-chunks-768-1024-96:v1'
                     AND chunk.source_text_digest =
                         message.embedding_input_digest),
                  (SELECT count(*)
                   FROM cc_search_chats.semantic_chunk_current AS chunk
                   JOIN cc_search_chats.message_current AS message
                     USING (provider, source_session_id,
                            logical_message_id, content_class)
                   JOIN cc_search_chats.embedding_value AS value
                     ON (value.profile_id, value.input_digest) =
                        (chunk.profile_id, chunk.input_digest)
                   WHERE chunk.profile_id =
                         'nemotron-3-embed-8b-bf16:chunks-v1'
                     AND chunk.chunker_id =
                         'nemotron-token-chunks-768-1024-96:v1'
                     AND chunk.source_text_digest =
                         message.embedding_input_digest)
                """
            )
        )
        if (
            eligible_messages != chunked_messages
            or eligible_chunks != mapped_chunks
            or eligible_chunks != semantic.embedded_count
        ):
            raise ValueError(
                "joint corpus publication is incomplete: "
                f"messages={chunked_messages}/{eligible_messages}, "
                f"mapped={mapped_chunks}/{eligible_chunks}, "
                f"expected={semantic.embedded_count}"
            )
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_build
            SET status = 'complete', completed_at = now(),
                embedded_count = %s, failure = NULL, phase = 'done',
                heartbeat_at = now(), completed_units = %s, total_units = %s
            WHERE semantic_build = %s
              AND corpus_generation = %s
              AND status = 'building'
            """,
            (
                semantic.embedded_count,
                semantic.embedded_count,
                semantic.embedded_count,
                semantic.semantic_build,
                corpus_generation,
            ),
        )
        connection.execute(
            """
            UPDATE cc_search_chats.corpus_generation
            SET status = 'complete', completed_at = now(),
                message_count = %s, alias_count = %s,
                source_watermarks = %s, failure = NULL,
                semantic_build = %s
            WHERE corpus_generation = %s AND status = 'building'
            """,
            (
                message_count,
                alias_count,
                Jsonb(source_watermarks),
                semantic.semantic_build,
                corpus_generation,
            ),
        )
        connection.execute(
            """
            UPDATE cc_search_chats.corpus_state
            SET current_corpus_generation = %s
            WHERE singleton
            """,
            (corpus_generation,),
        )
        connection.execute(
            """
            UPDATE cc_search_chats.refresh_run
            SET status = %s, completed_at = now(), corpus_generation = %s,
                failed_source_count = %s,
                read_source_count = %s,
                removed_source_count = %s,
                advanced_source_count = %s,
                metadata_checked_source_count = %s,
                attempted_source_count = %s,
                attempted_content_bytes = %s,
                blocked_source_count = %s,
                transient_failure_source_count = %s,
                diagnostics = %s, phase = 'done', heartbeat_at = now(),
                completed_units = total_units
            WHERE run_id = %s
            """,
            (
                "partial" if failed_source_count else "complete",
                corpus_generation,
                failed_source_count,
                read_source_count,
                removed_source_count,
                advanced_source_count,
                metadata_checked_source_count,
                attempted_source_count,
                attempted_content_bytes,
                blocked_source_count,
                transient_failure_source_count,
                Jsonb(list(diagnostics)),
                run_id,
            ),
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.embedding_value AS value
            WHERE NOT EXISTS (
                SELECT 1
                FROM cc_search_chats.semantic_chunk_current AS chunk
                WHERE (chunk.profile_id, chunk.input_digest) =
                      (value.profile_id, value.input_digest)
            )
            """
        )


def _finish_candidate_projection(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        INSERT INTO pg_temp.refresh_affected_message
        SELECT DISTINCT alias.provider, alias.source_session_id,
               alias.logical_message_id, alias.content_class
        FROM cc_search_chats.physical_alias_current AS alias
        WHERE EXISTS (
            SELECT 1
            FROM pg_temp.refresh_stage_source AS source
            WHERE source.disposition = 'replace'
              AND (source.source_root_id, source.source_file_relative) =
                  (alias.source_root_id, alias.source_file_relative)
        ) OR EXISTS (
            SELECT 1
            FROM pg_temp.refresh_stage_removed AS removed
            WHERE (removed.source_root_id, removed.source_file_relative) =
                  (alias.source_root_id, alias.source_file_relative)
        )
        UNION
        SELECT DISTINCT staged.provider, staged.source_session_id,
               staged.logical_message_id, staged.content_class
        FROM pg_temp.refresh_stage_message AS staged
        """
    )
    _finish_candidate_projection_rows(connection)


def _record_literal_candidate(
    connection: psycopg.Connection,
    *,
    run_id: int,
    roots: tuple[ConfiguredSourceRoot, ...],
    failed_source_count: int,
    read_source_count: int,
    removed_source_count: int,
    advanced_source_count: int,
    metadata_checked_source_count: int,
    attempted_source_count: int,
    attempted_content_bytes: int,
    blocked_source_count: int,
    transient_failure_source_count: int,
    diagnostics: tuple[dict[str, object], ...],
) -> int:
    """Complete a literal diagnostic without mutating selected corpus state."""
    del roots
    current_generation = next(
        connection.execute(
            "SELECT current_corpus_generation "
            "FROM cc_search_chats.corpus_state WHERE singleton"
        )
    )[0]
    connection.execute(
        """
        UPDATE cc_search_chats.refresh_run
        SET status = %s, completed_at = now(),
            failed_source_count = %s,
            read_source_count = %s,
            removed_source_count = %s,
            advanced_source_count = %s,
            metadata_checked_source_count = %s,
            attempted_source_count = %s,
            attempted_content_bytes = %s,
            blocked_source_count = %s,
            transient_failure_source_count = %s,
            diagnostics = %s || jsonb_build_array(jsonb_build_object(
                'code', 'literal_candidate_not_published',
                'detail', 'run composed index to publish a searchable corpus'
            )),
            phase = 'done', heartbeat_at = now(),
            completed_units = total_units
        WHERE run_id = %s
        """,
        (
            "partial" if failed_source_count else "complete",
            failed_source_count,
            read_source_count,
            removed_source_count,
            advanced_source_count,
            metadata_checked_source_count,
            attempted_source_count,
            attempted_content_bytes,
            blocked_source_count,
            transient_failure_source_count,
            Jsonb(list(diagnostics)),
            run_id,
        ),
    )
    return 0 if current_generation is None else current_generation


def _finish_candidate_projection_rows(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE refresh_candidate_alias (
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            content_class text NOT NULL,
            source_root_id text NOT NULL,
            locator text NOT NULL,
            source_file_relative text NOT NULL,
            record_ordinal bigint NOT NULL,
            source_line bigint NOT NULL,
            source_byte_offset bigint NOT NULL,
            raw_byte_length bigint NOT NULL,
            source_digest text NOT NULL,
            from_stage boolean NOT NULL,
            PRIMARY KEY (
                source_root_id, source_file_relative,
                record_ordinal, content_class
            )
        ) ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        INSERT INTO pg_temp.refresh_candidate_alias
        SELECT alias.provider, alias.source_session_id,
               alias.logical_message_id, alias.content_class,
               alias.source_root_id, alias.locator,
               alias.source_file_relative, alias.record_ordinal,
               alias.source_line, alias.source_byte_offset,
               alias.raw_byte_length, alias.source_digest, false
        FROM cc_search_chats.physical_alias_current AS alias
        JOIN pg_temp.refresh_affected_message AS affected
          USING (provider, source_session_id, logical_message_id, content_class)
        WHERE NOT EXISTS (
            SELECT 1
            FROM pg_temp.refresh_stage_source AS source
            WHERE source.disposition = 'replace'
              AND (source.source_root_id, source.source_file_relative) =
                  (alias.source_root_id, alias.source_file_relative)
        )
          AND NOT EXISTS (
            SELECT 1
            FROM pg_temp.refresh_stage_removed AS removed
            WHERE (removed.source_root_id, removed.source_file_relative) =
                  (alias.source_root_id, alias.source_file_relative)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO pg_temp.refresh_candidate_alias
        SELECT staged.provider, staged.source_session_id,
               staged.logical_message_id, staged.content_class,
               staged.source_root_id, staged.alias_locator,
               staged.source_file_relative, staged.record_ordinal,
               staged.source_line, staged.source_byte_offset,
               staged.raw_byte_length, staged.source_digest, true
        FROM pg_temp.refresh_stage_message AS staged
        ON CONFLICT (
            source_root_id, source_file_relative,
            record_ordinal, content_class
        ) DO UPDATE SET
            provider = EXCLUDED.provider,
            source_session_id = EXCLUDED.source_session_id,
            logical_message_id = EXCLUDED.logical_message_id,
            locator = EXCLUDED.locator,
            source_line = EXCLUDED.source_line,
            source_byte_offset = EXCLUDED.source_byte_offset,
            raw_byte_length = EXCLUDED.raw_byte_length,
            source_digest = EXCLUDED.source_digest,
            from_stage = true
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE refresh_candidate_observation (
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            canonical_locator text NOT NULL,
            timestamp_text text NOT NULL,
            role text NOT NULL,
            session_kind text NOT NULL,
            conversation_epoch integer NOT NULL,
            content_class text NOT NULL,
            prose_content text NOT NULL,
            repository text,
            cwd text,
            submitted_by text NOT NULL,
            embedding_input_digest text NOT NULL,
            source_root_id text NOT NULL,
            source_file_relative text NOT NULL,
            record_ordinal bigint NOT NULL
        ) ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        INSERT INTO pg_temp.refresh_candidate_observation
        SELECT message.provider, message.source_session_id,
               message.logical_message_id, message.canonical_locator,
               message.timestamp_text, message.role, message.session_kind,
               message.conversation_epoch, message.content_class,
               message.prose_content, message.repository, message.cwd,
               message.submitted_by, message.embedding_input_digest,
               alias.source_root_id, alias.source_file_relative,
               alias.record_ordinal
        FROM pg_temp.refresh_candidate_alias AS alias
        JOIN cc_search_chats.message_current AS message
          USING (provider, source_session_id, logical_message_id, content_class)
        WHERE NOT alias.from_stage
        UNION ALL
        SELECT staged.provider, staged.source_session_id,
               staged.logical_message_id, staged.canonical_locator,
               staged.timestamp_text, staged.role, staged.session_kind,
               staged.conversation_epoch, staged.content_class,
               staged.prose_content, staged.repository, staged.cwd,
               staged.submitted_by, staged.embedding_input_digest,
               staged.source_root_id, staged.source_file_relative,
               staged.record_ordinal
        FROM pg_temp.refresh_candidate_alias AS alias
        JOIN pg_temp.refresh_stage_message AS staged
          ON (staged.source_root_id, staged.source_file_relative,
              staged.record_ordinal, staged.content_class) =
             (alias.source_root_id, alias.source_file_relative,
              alias.record_ordinal, alias.content_class)
        WHERE alias.from_stage
        """
    )
    conflict = next(
        connection.execute(
            """
            SELECT min(canonical_locator)
            FROM pg_temp.refresh_candidate_observation
            GROUP BY provider, source_session_id,
                     logical_message_id, content_class
            HAVING count(DISTINCT jsonb_build_array(
                canonical_locator, timestamp_text, role, session_kind,
                conversation_epoch, prose_content, repository,
                submitted_by, embedding_input_digest
            )) > 1
            LIMIT 1
            """
        ),
        None,
    )
    if conflict is not None:
        raise ValueError(f"conflicting observations for {conflict[0]}")
    connection.execute(
        """
        CREATE TEMP TABLE refresh_candidate_message
        (LIKE cc_search_chats.message_current
         EXCLUDING INDEXES EXCLUDING GENERATED EXCLUDING CONSTRAINTS)
        ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        INSERT INTO pg_temp.refresh_candidate_message (
            provider, source_session_id, logical_message_id,
            canonical_locator, timestamp_text, role, session_kind,
            conversation_epoch, content_class, prose_content, repository,
            cwd, submitted_by, embedding_input_digest
        )
        SELECT DISTINCT ON (
            provider, source_session_id, logical_message_id, content_class
        )
            provider, source_session_id, logical_message_id,
            canonical_locator, timestamp_text, role, session_kind,
            conversation_epoch, content_class, prose_content, repository,
            cwd, submitted_by, embedding_input_digest
        FROM pg_temp.refresh_candidate_observation
        ORDER BY provider, source_session_id, logical_message_id,
                 content_class, source_root_id,
                 source_file_relative, record_ordinal
        """
    )


def _publish_staged_refresh(
    connection: psycopg.Connection,
    *,
    run_id: int,
    roots: tuple[ConfiguredSourceRoot, ...],
    failed_source_count: int,
    read_source_count: int,
    removed_source_count: int,
    advanced_source_count: int,
    metadata_checked_source_count: int,
    attempted_source_count: int,
    attempted_content_bytes: int,
    blocked_source_count: int,
    transient_failure_source_count: int,
    diagnostics: tuple[dict[str, object], ...],
) -> int:
    with connection.transaction():
        revision_id = next(
            connection.execute(
                """
                INSERT INTO cc_search_chats.corpus_revision (status)
                VALUES ('building')
                RETURNING revision_id
                """
            )
        )[0]
        _sync_roots(connection, roots)
        connection.execute(
            """
            DELETE FROM cc_search_chats.physical_alias_current AS alias
            USING (
                SELECT source_root_id, source_file_relative
                FROM pg_temp.refresh_stage_source
                WHERE disposition = 'replace'
                UNION ALL
                SELECT source_root_id, source_file_relative
                FROM pg_temp.refresh_stage_removed
            ) AS changed
            WHERE (alias.source_root_id, alias.source_file_relative) =
                  (changed.source_root_id, changed.source_file_relative)
            """
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.physical_alias_current AS alias
            USING pg_temp.refresh_stage_message AS staged
            WHERE (alias.source_root_id, alias.source_file_relative,
                   alias.record_ordinal, alias.content_class) =
                  (staged.source_root_id, staged.source_file_relative,
                   staged.record_ordinal, staged.content_class)
              AND (alias.provider, alias.source_session_id,
                   alias.logical_message_id) IS DISTINCT FROM
                  (staged.provider, staged.source_session_id,
                   staged.logical_message_id)
            """
        )
        conflict = _message_conflict(connection)
        if conflict is not None:
            raise ValueError(f"conflicting observations for {conflict}")
        connection.execute(
            """
            INSERT INTO cc_search_chats.message_current (
                provider, source_session_id, logical_message_id,
                canonical_locator, timestamp_text, role, session_kind,
                conversation_epoch, content_class, prose_content, repository,
                cwd, submitted_by, embedding_input_digest
            )
            SELECT DISTINCT ON (
                provider, source_session_id, logical_message_id, content_class
            )
                provider, source_session_id, logical_message_id,
                canonical_locator, timestamp_text, role, session_kind,
                conversation_epoch, content_class, prose_content, repository,
                cwd, submitted_by, embedding_input_digest
            FROM pg_temp.refresh_stage_message
            ORDER BY provider, source_session_id, logical_message_id,
                     content_class, source_root_id, source_file_relative,
                     record_ordinal
            ON CONFLICT (
                provider, source_session_id, logical_message_id, content_class
            ) DO UPDATE SET
                canonical_locator = EXCLUDED.canonical_locator,
                timestamp_text = EXCLUDED.timestamp_text,
                role = EXCLUDED.role,
                session_kind = EXCLUDED.session_kind,
                conversation_epoch = EXCLUDED.conversation_epoch,
                prose_content = EXCLUDED.prose_content,
                repository = EXCLUDED.repository,
                cwd = EXCLUDED.cwd,
                submitted_by = EXCLUDED.submitted_by,
                embedding_input_digest = EXCLUDED.embedding_input_digest
            WHERE ROW(
                message_current.canonical_locator,
                message_current.timestamp_text,
                message_current.role,
                message_current.session_kind,
                message_current.conversation_epoch,
                message_current.prose_content,
                message_current.repository,
                message_current.cwd,
                message_current.submitted_by,
                message_current.embedding_input_digest
            ) IS DISTINCT FROM ROW(
                EXCLUDED.canonical_locator,
                EXCLUDED.timestamp_text,
                EXCLUDED.role,
                EXCLUDED.session_kind,
                EXCLUDED.conversation_epoch,
                EXCLUDED.prose_content,
                EXCLUDED.repository,
                EXCLUDED.cwd,
                EXCLUDED.submitted_by,
                EXCLUDED.embedding_input_digest
            )
            """
        )
        connection.execute(
            """
            INSERT INTO cc_search_chats.physical_alias_current (
                provider, source_session_id, logical_message_id, content_class,
                source_root_id, locator, source_file_relative, record_ordinal,
                source_line, source_byte_offset, raw_byte_length, source_digest
            )
            SELECT provider, source_session_id, logical_message_id, content_class,
                   source_root_id, alias_locator, source_file_relative,
                   record_ordinal, source_line, source_byte_offset,
                   raw_byte_length, source_digest
            FROM pg_temp.refresh_stage_message
            ON CONFLICT (
                provider, source_session_id, logical_message_id, content_class,
                source_root_id, source_file_relative, record_ordinal
            ) DO UPDATE SET
                locator = EXCLUDED.locator,
                source_line = EXCLUDED.source_line,
                source_byte_offset = EXCLUDED.source_byte_offset,
                raw_byte_length = EXCLUDED.raw_byte_length,
                source_digest = EXCLUDED.source_digest
            WHERE ROW(
                physical_alias_current.locator,
                physical_alias_current.source_line,
                physical_alias_current.source_byte_offset,
                physical_alias_current.raw_byte_length,
                physical_alias_current.source_digest
            ) IS DISTINCT FROM ROW(
                EXCLUDED.locator,
                EXCLUDED.source_line,
                EXCLUDED.source_byte_offset,
                EXCLUDED.raw_byte_length,
                EXCLUDED.source_digest
            )
            """
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.message_current AS message
            WHERE NOT EXISTS (
                SELECT 1
                FROM cc_search_chats.physical_alias_current AS alias
                WHERE (alias.provider, alias.source_session_id,
                       alias.logical_message_id, alias.content_class) =
                      (message.provider, message.source_session_id,
                       message.logical_message_id, message.content_class)
            )
            """
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.source_file_current AS current
            USING pg_temp.refresh_stage_removed AS removed
            WHERE (current.source_root_id, current.source_file_relative) =
                  (removed.source_root_id, removed.source_file_relative)
            """
        )
        connection.execute(
            """
            INSERT INTO cc_search_chats.source_file_current (
                source_root_id, source_file_relative, file_device, file_inode,
                observed_size, observed_mtime_ns, complete_byte_offset,
                next_record_ordinal, next_source_line, parser_state_version,
                parser_state, source_status, pending_bytes, skipped_record_count,
                updated_revision_id
            )
            SELECT source_root_id, source_file_relative, file_device, file_inode,
                   observed_size, observed_mtime_ns, complete_byte_offset,
                   next_record_ordinal, next_source_line, parser_state_version,
                   parser_state, source_status, pending_bytes,
                   skipped_record_count, %s
            FROM pg_temp.refresh_stage_source
            ON CONFLICT (source_root_id, source_file_relative) DO UPDATE SET
                file_device = EXCLUDED.file_device,
                file_inode = EXCLUDED.file_inode,
                observed_size = EXCLUDED.observed_size,
                observed_mtime_ns = EXCLUDED.observed_mtime_ns,
                complete_byte_offset = EXCLUDED.complete_byte_offset,
                next_record_ordinal = EXCLUDED.next_record_ordinal,
                next_source_line = EXCLUDED.next_source_line,
                parser_state_version = EXCLUDED.parser_state_version,
                parser_state = EXCLUDED.parser_state,
                source_status = EXCLUDED.source_status,
                pending_bytes = EXCLUDED.pending_bytes,
                skipped_record_count = EXCLUDED.skipped_record_count,
                updated_revision_id = EXCLUDED.updated_revision_id
            """,
            (revision_id,),
        )
        connection.execute(
            """
            DELETE FROM cc_search_chats.source_failure_current AS failure
            USING (
                SELECT source_root_id, source_file_relative
                FROM pg_temp.refresh_stage_source
                UNION ALL
                SELECT source_root_id, source_file_relative
                FROM pg_temp.refresh_stage_removed
            ) AS resolved
            WHERE (failure.source_root_id, failure.source_file_relative) =
                  (resolved.source_root_id, resolved.source_file_relative)
            """
        )
        configured_ids = [root.source_root_id for root in roots]
        connection.execute(
            """
            DELETE FROM cc_search_chats.source_root_current
            WHERE NOT (source_root_id = ANY(%s::text[]))
              AND NOT EXISTS (
                  SELECT 1
                  FROM cc_search_chats.source_file_current AS source
                  WHERE source.source_root_id =
                        source_root_current.source_root_id
              )
            """,
            (configured_ids,),
        )
        message_count, alias_count, source_watermarks = next(
            connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM cc_search_chats.message_current),
                  (SELECT count(*)
                   FROM cc_search_chats.physical_alias_current),
                  COALESCE((
                    SELECT jsonb_object_agg(
                        source_root_id || ':' || source_file_relative,
                        jsonb_build_object(
                            'observed_size', observed_size,
                            'complete_byte_offset', complete_byte_offset,
                            'next_record_ordinal', next_record_ordinal,
                            'pending_bytes', pending_bytes
                        )
                        ORDER BY source_root_id, source_file_relative
                    )
                    FROM cc_search_chats.source_file_current
                  ), '{}'::jsonb)
                """
            )
        )
        connection.execute(
            """
            UPDATE cc_search_chats.corpus_revision
            SET status = 'complete', completed_at = now(),
                message_count = %s, alias_count = %s,
                source_watermarks = %s
            WHERE revision_id = %s
            """,
            (message_count, alias_count, Jsonb(source_watermarks), revision_id),
        )
        connection.execute(
            """
            UPDATE cc_search_chats.corpus_state
            SET current_revision_id = %s
            WHERE singleton
            """,
            (revision_id,),
        )
        connection.execute(
            """
            UPDATE cc_search_chats.refresh_run
            SET status = %s, completed_at = now(), corpus_revision_id = %s,
                failed_source_count = %s,
                read_source_count = %s,
                removed_source_count = %s,
                advanced_source_count = %s,
                metadata_checked_source_count = %s,
                attempted_source_count = %s,
                attempted_content_bytes = %s,
                blocked_source_count = %s,
                transient_failure_source_count = %s,
                diagnostics = %s, phase = 'done', heartbeat_at = now(),
                completed_units = total_units
            WHERE run_id = %s
            """,
            (
                "partial" if failed_source_count else "complete",
                revision_id,
                failed_source_count,
                read_source_count,
                removed_source_count,
                advanced_source_count,
                metadata_checked_source_count,
                attempted_source_count,
                attempted_content_bytes,
                blocked_source_count,
                transient_failure_source_count,
                Jsonb(list(diagnostics)),
                run_id,
            ),
        )
    return revision_id


def _empty_initial_revision(connection: psycopg.Connection) -> int:
    with connection.transaction():
        revision_id = next(
            connection.execute(
                """
                INSERT INTO cc_search_chats.corpus_revision (
                    completed_at, status, message_count, alias_count
                ) VALUES (now(), 'complete', 0, 0)
                RETURNING revision_id
                """
            )
        )[0]
        connection.execute(
            "UPDATE cc_search_chats.corpus_state SET current_revision_id = %s "
            "WHERE singleton",
            (revision_id,),
        )
    return revision_id


def _current_result_values(
    connection: psycopg.Connection,
) -> tuple[int | None, int]:
    return next(
        connection.execute(
            """
            SELECT state.current_corpus_generation,
                   (SELECT count(*) FROM cc_search_chats.message_current)
            FROM cc_search_chats.corpus_state AS state
            WHERE singleton
            """
        )
    )


def _prune_refresh_runs(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        DELETE FROM cc_search_chats.refresh_run
        WHERE status <> 'building'
          AND run_id NOT IN (
              SELECT run_id
              FROM cc_search_chats.refresh_run
              WHERE status <> 'building'
              ORDER BY run_id DESC
              LIMIT %s
          )
        """,
        (_RETAINED_REFRESH_RUNS,),
    )


def refresh_native_sources(
    connection: psycopg.Connection,
    *,
    source_roots: Sequence[ConfiguredSourceRoot] | None = None,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    progress: ProgressCallback | None = None,
    force_retry: bool = False,
    _candidate_publisher: Callable[..., int] | None = None,
    _require_publication: bool = False,
) -> RefreshResult:
    """Refresh changed native sources and atomically publish their deltas."""
    roots = _resolved_roots(
        source_roots=source_roots,
        claude_root=claude_root,
        codex_root=codex_root,
    )
    require_current_schema(connection)
    with _refresh_owner(connection, progress):
        checkpoints = _load_checkpoints(connection)
        failed_observations = _load_failed_observations(connection)
        (
            observed,
            complete_roots,
            discovered_keys,
            preflight_failures,
        ) = _discover_sources(roots)
        blocked_observations = tuple(
            value
            for value in observed
            if _failed_observation_blocks(
                value,
                failed_observations.get(value.key),
                force_retry=force_retry,
            )
        )
        blocked_keys = {value.key for value in blocked_observations}
        plans = tuple(
            plan
            for value in observed
            if value.key not in blocked_keys
            and (plan := _plan_source(value, checkpoints.get(value.key))) is not None
        )
        configured_root_ids = {root.source_root_id for root in roots}
        removed_keys = {
            key
            for key in checkpoints
            if key not in discovered_keys
            and (key[0] not in configured_root_ids or key[0] in complete_roots)
        }
        with connection.transaction():
            _sync_roots(connection, roots)
            _delete_obsolete_failed_observations(
                connection,
                failed_observations,
                discovered_keys,
                complete_roots,
            )
        current_revision, current_messages = _current_result_values(connection)
        if (
            not plans
            and not removed_keys
            and not preflight_failures
            and not _require_publication
        ):
            if current_revision is None:
                current_revision = 0
            blocked_source_count = sum(
                failed_observations[value.key].failure_class == "deterministic"
                for value in blocked_observations
            )
            transient_failure_source_count = sum(
                failed_observations[value.key].failure_class == "transient"
                for value in blocked_observations
            )
            return RefreshResult(
                corpus_generation=current_revision,
                source_count=len(discovered_keys),
                message_count=current_messages,
                failed_source_count=len(blocked_observations),
                metadata_checked_source_count=len(discovered_keys),
                blocked_source_count=blocked_source_count,
                transient_failure_source_count=transient_failure_source_count,
            )

        changed_source_count = len(plans) + len(removed_keys)
        run_id = _start_run(
            connection,
            source_count=len(discovered_keys),
            changed_source_count=changed_source_count,
            removed_source_count=len(removed_keys),
        )
        diagnostics = list(preflight_failures)
        failed_source_count = len(preflight_failures)
        advanced_source_count = 0
        pending_bytes = 0
        attempted_content_bytes = 0
        blocked_source_count = sum(
            failed_observations[value.key].failure_class == "deterministic"
            for value in blocked_observations
        )
        transient_failure_source_count = sum(
            failed_observations[value.key].failure_class == "transient"
            for value in blocked_observations
        )
        completed = 0
        heartbeat = DatabaseHeartbeat(
            connection.info.dsn,
            """
            UPDATE cc_search_chats.refresh_run
            SET heartbeat_at = now()
            WHERE run_id = %s AND status = 'building'
            """,
            (run_id,),
            interval_seconds=_RUN_HEARTBEAT_SECONDS,
            label=f"refresh heartbeat {run_id}",
        )
        try:
            heartbeat.start()
            _create_stage_tables(connection)
            _stage_removed_sources(
                connection,
                checkpoints,
                set(discovered_keys),
                configured_root_ids,
                complete_roots,
            )
            for plan in plans:
                try:
                    (
                        advanced,
                        source_pending,
                        source_attempted_bytes,
                        source_skipped_diagnostics,
                    ) = _parse_and_stage_source(connection, plan)
                except _SourceRefreshError as error:
                    _clear_staged_source(connection, plan)
                    attempted_content_bytes += error.attempted_content_bytes
                    failed_source_count += 1
                    if error.failure_class == "deterministic":
                        blocked_source_count += 1
                    else:
                        transient_failure_source_count += 1
                    _record_failed_observation(
                        connection,
                        plan=plan,
                        run_id=run_id,
                        error=error,
                        previous=failed_observations.get(plan.observed.key),
                    )
                    diagnostics.append(
                        {
                            "code": "source_refresh_failed",
                            "failure_code": error.code,
                            "failure_class": error.failure_class,
                            "provider": plan.observed.root.provider.value,
                            "source_root_id": plan.observed.root.source_root_id,
                            "source_file_relative": (
                                plan.observed.source.source_file_relative.as_posix()
                            ),
                            "detail": str(error),
                            "record_ordinal": error.record_ordinal,
                            "source_line": error.source_line,
                            "source_byte_offset": error.source_byte_offset,
                            "attempted_content_bytes": (error.attempted_content_bytes),
                        }
                    )
                else:
                    attempted_content_bytes += source_attempted_bytes
                    advanced_source_count += int(advanced)
                    pending_bytes += source_pending
                    diagnostics.extend(source_skipped_diagnostics)
                completed += 1
                _update_run_progress(
                    connection,
                    run_id,
                    phase="parse",
                    completed_units=completed,
                )
                if progress is not None:
                    progress(
                        RefreshProgress(
                            phase="parse",
                            state="running",
                            completed_units=completed,
                            total_units=len(plans),
                            run_id=run_id,
                        )
                    )

            heartbeat.raise_if_failed()
            read_source_count, removed_source_count = next(
                connection.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM pg_temp.refresh_stage_source),
                      (SELECT count(*) FROM pg_temp.refresh_stage_removed)
                    """
                )
            )
            successful_changes = read_source_count + removed_source_count
            if successful_changes or (
                _require_publication and failed_source_count == 0
            ):
                _update_run_progress(
                    connection,
                    run_id,
                    phase="fts_commit",
                    completed_units=len(plans) + len(removed_keys),
                )
                publisher = _candidate_publisher or _record_literal_candidate
                revision_id = publisher(
                    connection,
                    run_id=run_id,
                    roots=roots,
                    failed_source_count=failed_source_count,
                    read_source_count=read_source_count,
                    removed_source_count=removed_source_count,
                    advanced_source_count=advanced_source_count,
                    metadata_checked_source_count=len(discovered_keys),
                    attempted_source_count=len(plans),
                    attempted_content_bytes=attempted_content_bytes,
                    blocked_source_count=blocked_source_count,
                    transient_failure_source_count=(transient_failure_source_count),
                    diagnostics=tuple(diagnostics),
                )
            else:
                revision_id = current_revision if current_revision is not None else 0
                connection.execute(
                    """
                    UPDATE cc_search_chats.refresh_run
                    SET status = 'failed', completed_at = now(),
                        corpus_generation = NULL, failed_source_count = %s,
                        metadata_checked_source_count = %s,
                        attempted_source_count = %s,
                        attempted_content_bytes = %s,
                        blocked_source_count = %s,
                        transient_failure_source_count = %s,
                        diagnostics = %s, phase = 'done', heartbeat_at = now(),
                        completed_units = total_units
                    WHERE run_id = %s
                    """,
                    (
                        failed_source_count,
                        len(discovered_keys),
                        len(plans),
                        attempted_content_bytes,
                        blocked_source_count,
                        transient_failure_source_count,
                        Jsonb(diagnostics),
                        run_id,
                    ),
                )
            _prune_refresh_runs(connection)
            _revision, message_count = _current_result_values(connection)
            return RefreshResult(
                corpus_generation=revision_id,
                source_count=len(discovered_keys),
                message_count=message_count,
                changed_source_count=changed_source_count,
                failed_source_count=failed_source_count,
                read_source_count=read_source_count,
                removed_source_count=removed_source_count,
                advanced_source_count=advanced_source_count,
                pending_bytes=pending_bytes,
                metadata_checked_source_count=len(discovered_keys),
                attempted_source_count=len(plans),
                attempted_content_bytes=attempted_content_bytes,
                blocked_source_count=blocked_source_count,
                transient_failure_source_count=transient_failure_source_count,
                run_id=run_id,
            )
        except Exception as error:
            _record_run_failure(
                connection,
                run_id,
                (
                    *diagnostics,
                    {
                        "code": "refresh_failed",
                        "detail": f"{type(error).__name__}: {error}",
                    },
                ),
            )
            raise
        finally:
            heartbeat.stop()
            for relation in (
                "semantic_embedding_queue",
                "semantic_candidate_chunk",
                "semantic_candidate_message",
                "refresh_candidate_observation",
                "refresh_candidate_message",
                "refresh_candidate_alias",
                "refresh_affected_message",
                "refresh_stage_message",
                "refresh_stage_message_batch",
                "refresh_stage_source",
                "refresh_stage_removed",
            ):
                connection.execute(f"DROP TABLE IF EXISTS pg_temp.{relation}")


def _selected_coherent_build(
    connection: psycopg.Connection,
) -> tuple[int, int, int] | None:
    return next(
        connection.execute(
            """
            SELECT generation.corpus_generation, build.semantic_build,
                   build.embedded_count
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            JOIN cc_search_chats.semantic_build AS build
              ON (build.semantic_build, build.corpus_generation) =
                 (generation.semantic_build, generation.corpus_generation)
            WHERE state.singleton
              AND generation.status = 'complete'
              AND generation.completed_at IS NOT NULL
              AND build.status = 'complete'
              AND build.completed_at IS NOT NULL
            """
        ),
        None,
    )


def index_corpus(
    connection: psycopg.Connection,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    chunker: Chunker,
    source_roots: Sequence[ConfiguredSourceRoot] | None = None,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    progress: ProgressCallback | None = None,
    embedding_progress: Callable[[int, int], None] | None = None,
    batch_size: int = 4,
    force_retry: bool = False,
) -> CorpusIndexResult:
    """Prepare and jointly publish one coherent literal and semantic corpus."""
    require_current_schema(connection)
    selected_before = _selected_coherent_build(connection)
    prepared: CandidateSemanticBuild | None = None

    def publish_candidate(
        candidate_connection: psycopg.Connection,
        *,
        run_id: int,
        roots: tuple[ConfiguredSourceRoot, ...],
        failed_source_count: int,
        read_source_count: int,
        removed_source_count: int,
        advanced_source_count: int,
        metadata_checked_source_count: int,
        attempted_source_count: int,
        attempted_content_bytes: int,
        blocked_source_count: int,
        transient_failure_source_count: int,
        diagnostics: tuple[dict[str, object], ...],
    ) -> int:
        nonlocal prepared
        corpus_generation = next(
            candidate_connection.execute(
                """
                INSERT INTO cc_search_chats.corpus_generation (status)
                VALUES ('building')
                RETURNING corpus_generation
                """
            )
        )[0]
        try:
            _create_candidate_projection(candidate_connection)
            prepared = prepare_candidate_semantics(
                candidate_connection,
                corpus_generation,
                embed,
                chunker=chunker,
                batch_size=batch_size,
                progress=embedding_progress,
            )
            _publish_coherent_candidate(
                candidate_connection,
                corpus_generation=corpus_generation,
                semantic=prepared,
                run_id=run_id,
                roots=roots,
                failed_source_count=failed_source_count,
                read_source_count=read_source_count,
                removed_source_count=removed_source_count,
                advanced_source_count=advanced_source_count,
                metadata_checked_source_count=metadata_checked_source_count,
                attempted_source_count=attempted_source_count,
                attempted_content_bytes=attempted_content_bytes,
                blocked_source_count=blocked_source_count,
                transient_failure_source_count=transient_failure_source_count,
                diagnostics=diagnostics,
            )
        except Exception as error:
            if prepared is not None:
                candidate_connection.execute(
                    """
                    UPDATE cc_search_chats.semantic_build
                    SET status = 'failed', completed_at = now(),
                        heartbeat_at = now(),
                        failure = jsonb_build_object(
                            'code', 'coherent_corpus_publication_failed',
                            'phase', phase,
                            'error', %s::text
                        )
                    WHERE semantic_build = %s AND status = 'building'
                    """,
                    (str(error), prepared.semantic_build),
                )
            candidate_connection.execute(
                """
                UPDATE cc_search_chats.corpus_generation
                SET status = 'failed', completed_at = now(),
                    failure = jsonb_build_object(
                        'code', 'coherent_corpus_candidate_failed',
                        'error', %s::text
                    )
                WHERE corpus_generation = %s AND status = 'building'
                """,
                (str(error), corpus_generation),
            )
            raise
        return corpus_generation

    refresh = refresh_native_sources(
        connection,
        source_roots=source_roots,
        claude_root=claude_root,
        codex_root=codex_root,
        progress=progress,
        force_retry=force_retry,
        _candidate_publisher=publish_candidate,
        _require_publication=selected_before is None,
    )
    selected_after = _selected_coherent_build(connection)
    if selected_after is None:
        raise ValueError("coherent corpus publication did not select a build")
    corpus_generation, semantic_build, embedded_count = selected_after
    if prepared is not None and (
        corpus_generation,
        semantic_build,
        embedded_count,
    ) != (
        refresh.corpus_generation,
        prepared.semantic_build,
        prepared.embedded_count,
    ):
        raise ValueError("published corpus does not match its prepared candidate")
    return CorpusIndexResult(
        corpus_generation=corpus_generation,
        semantic_build=semantic_build,
        source_count=refresh.source_count,
        message_count=refresh.message_count,
        embedding_count=embedded_count,
        changed_source_count=refresh.changed_source_count,
        failed_source_count=refresh.failed_source_count,
        read_source_count=refresh.read_source_count,
        removed_source_count=refresh.removed_source_count,
        advanced_source_count=refresh.advanced_source_count,
        pending_bytes=refresh.pending_bytes,
        metadata_checked_source_count=refresh.metadata_checked_source_count,
        attempted_source_count=refresh.attempted_source_count,
        attempted_content_bytes=refresh.attempted_content_bytes,
        blocked_source_count=refresh.blocked_source_count,
        transient_failure_source_count=refresh.transient_failure_source_count,
        run_id=refresh.run_id,
    )
