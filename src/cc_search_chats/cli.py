"""CLI entry point for cc-search-chats.

Imperative Shell — parses arguments and orchestrates storage/output layers.
"""

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Never, TypedDict, cast

import psycopg

from cc_search_chats import __version__
from cc_search_chats.core.discovery import (
    decode_project_path,
    encode_project_path,
    get_claude_projects_dir,
    list_session_files,
    rank_sessions,
)
from cc_search_chats.core.identity import ResolutionStatus
from cc_search_chats.output import (
    event_export_payload,
    format_context,
    format_extract,
    format_search_results,
    format_session_list,
    json_context,
    json_extract,
    json_index_all_result,
    json_index_result,
    json_search_results,
    json_session_list,
)
from cc_search_chats.providers.source_discovery import configured_source_roots
from cc_search_chats.queueing import client_admission
from cc_search_chats.semantic import (
    ModelUnavailable,
    chunk_passages,
    embed_passages,
    local_model_revision,
    model_output_scope,
)
from cc_search_chats.semantic.query_embedder import (
    QueryEmbeddingResult,
    request_query_embedding,
    semantic_warm_seconds,
    shutdown_query_embedder,
)
from cc_search_chats.storage.index import (
    ProjectRebuildError,
    close_db,
    discard_damaged_database,
    ensure_fts5,
    extract_context,
    extract_session,
    format_exception_detail,
    format_index_error,
    get_db_path,
    index_all_projects,
    is_database_damage,
    jit_reindex,
    list_sessions,
    open_db,
    reindex_project,
    search,
)
from cc_search_chats.storage.postgresql import (
    CorpusIndexResult,
    ExactResolution,
    HybridHit,
    MessageResolution,
    RefreshProgress,
    RefreshResult,
    SearchHit,
    StoredAlias,
    StoredMessage,
    exhaustive_search_page,
    export_human_message_events,
    index_corpus,
    migrate,
    resolve_exact_messages,
    search_messages,
    unindexed_sources,
)
from cc_search_chats.storage.postgresql import (
    context_messages as pg_context_messages,
)
from cc_search_chats.storage.postgresql import (
    extract_session as pg_extract_session,
)
from cc_search_chats.storage.postgresql import (
    list_sessions as pg_list_sessions,
)
from cc_search_chats.storage.postgresql import (
    resolve_messages as pg_resolve_messages,
)
from cc_search_chats.storage.postgresql.guardrails import (
    ReadDeadlineExceeded,
    acquire_index_session,
    read_deadline,
)
from cc_search_chats.storage.postgresql.migrations import (
    MaintenanceRequired,
    require_current_schema,
)
from cc_search_chats.storage.postgresql.semantic import (
    fuse_hybrid,
    semantic_search,
    verify_model_revision,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from cc_search_chats.providers.source_discovery import ConfiguredSourceRoot

_DEFAULT_POSTGRES_DSN = "service=cc_search_chats"
_POSTGRES_SCHEMA_VERSION = 5
_SEARCH_DEADLINE_SECONDS = 5.0
_SEARCH_RENDER_RESERVE_SECONDS = 0.1
_INDEX_STATE_SCAN_BUDGET_SECONDS = (
    _SEARCH_DEADLINE_SECONDS - _SEARCH_RENDER_RESERVE_SECONDS
)
_CONTAINMENT_PREFLIGHT_TIMEOUT_SECONDS = 10.0
_CONTAINMENT_REMEDY = (
    "You are probably blocked by your sandbox: ask the user for permission to run "
    "`cc-search-chats index` on the host through the configured approval route. "
    "Indexing never runs uncontained; `cc-search-chats index --status` needs no scope."
)
_LITERAL_MODE_HELP = "exact PostgreSQL full-text search; no model, no GPU"
_SEMANTIC_MODE_HELP = (
    "model-ranked search: hybrid fusion of full-text and embedding candidates by "
    "reciprocal rank; no deadline, and first use takes about 10 s"
)


class SearchDeadlineExceeded(TimeoutError):
    """The ranked search request no longer has a safe answer budget."""


class SystemdScopeUnavailable(RuntimeError):
    """The index process could not enter its required systemd user scope."""

    def __init__(self, systemd_detail: str) -> None:
        self.code = "systemd_scope_unavailable"
        self.phase = "containment"
        self.systemd_detail = systemd_detail
        super().__init__(
            "indexing could not create its systemd user scope from this process: "
            f"{systemd_detail}"
        )


class _ArgumentParser(argparse.ArgumentParser):
    """Keep the required search-mode error as descriptive as the help surface."""

    def error(self, message: str) -> Never:
        if "required" in message and "--literal" in message and "--semantic" in message:
            message = (
                f"search requires --literal ({_LITERAL_MODE_HELP}) or "
                f"--semantic ({_SEMANTIC_MODE_HELP})"
            )
        super().error(message)


def _remaining_search_seconds(args: argparse.Namespace) -> float:
    answer_deadline = args.answer_deadline
    if answer_deadline is None:
        raise RuntimeError("this search mode has no answer deadline")
    return answer_deadline - monotonic()


def _bounded_query_embedding(
    query: str,
    *,
    progress: Callable[[str, str], None],
    quiet: bool = False,
) -> QueryEmbeddingResult:
    """Return one embedding from the same-user warm helper."""
    return request_query_embedding(query, progress=progress, quiet=quiet)


class _ProgressStream:
    """Render one ordered progress stream without contaminating stdout."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._started = getattr(args, "request_started", monotonic())
        self._stderr = sys.stderr
        self._sequence = 0
        self._terminal = False
        self._emit_lock = Lock()
        self._human_active_phase: str | None = None
        self._human_phase_samples: dict[str, tuple[float, int]] = {}
        selected = getattr(args, "progress", "auto")
        self._ndjson = (
            selected == "ndjson"
            or args.json
            or (selected == "auto" and not sys.stderr.isatty())
        )

    def emit(
        self,
        phase: str,
        state: str,
        *,
        event: str = "progress",
        run_id: int | None = None,
        completed_units: int | None = None,
        total_units: int | None = None,
        owner: int | None = None,
        corpus_generation: int | None = None,
        semantic_build: int | None = None,
        deadline_ms: int | None = None,
        retrieval_mode: str | None = None,
        mode: str | None = None,
        indexed_at: str | None = None,
        corpus_age_ms: int | None = None,
        index_state: object = None,
        stale_reasons: object = None,
        warning: object = None,
        error: object = None,
        coverage: object = None,
        refresh: object = None,
        semantic: object = None,
    ) -> None:
        with self._emit_lock:
            emitted_at = monotonic()
            self._sequence += 1
            value = {
                "schema_version": _POSTGRES_SCHEMA_VERSION,
                "sequence": self._sequence,
                "event": event,
                "run_id": run_id,
                "phase": phase,
                "state": state,
                "elapsed_ms": round((emitted_at - self._started) * 1000),
                "completed_units": completed_units,
                "total_units": total_units,
                "owner": owner,
                "corpus_generation": corpus_generation,
                "semantic_build": semantic_build,
                "deadline_ms": deadline_ms,
                "retrieval_mode": retrieval_mode,
                "mode": mode,
                "indexed_at": indexed_at,
                "corpus_age_ms": corpus_age_ms,
                "index_state": index_state,
                "stale_reasons": stale_reasons,
                "warning": warning,
                "error": error,
                "coverage": coverage,
                "refresh": refresh,
                "semantic": semantic,
            }
            if self._ndjson:
                self._emit_ndjson(value)
            else:
                self._emit_human(
                    phase,
                    state,
                    emitted_at=emitted_at,
                    completed_units=completed_units,
                    total_units=total_units,
                )

    def _emit_ndjson(self, value: dict[str, object]) -> None:
        print(
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            file=self._stderr,
        )

    def _emit_human(
        self,
        phase: str,
        state: str,
        *,
        emitted_at: float,
        completed_units: int | None,
        total_units: int | None,
    ) -> None:
        if completed_units is not None and total_units is not None:
            self._emit_human_progress(
                phase,
                state,
                emitted_at=emitted_at,
                completed_units=completed_units,
                total_units=total_units,
            )
            return
        self._close_human_phase()
        print(f"{phase}: {state}", file=self._stderr)

    def _emit_human_progress(
        self,
        phase: str,
        state: str,
        *,
        emitted_at: float,
        completed_units: int,
        total_units: int,
    ) -> None:
        if self._human_active_phase is not None and self._human_active_phase != phase:
            self._close_human_phase()
        started_at, started_units = self._human_phase_samples.setdefault(
            phase,
            (emitted_at, completed_units),
        )
        elapsed = emitted_at - started_at
        advanced = completed_units - started_units
        rate = advanced / elapsed if elapsed > 0 and advanced > 0 else 0.0
        percent = completed_units / total_units * 100 if total_units > 0 else 100.0
        rate_text = f" {rate:.1f} units/s" if rate > 0 else ""
        eta_text = self._human_eta(completed_units, total_units, rate)
        end = "" if state == "running" else "\n"
        print(
            f"\x1b[2K\r{phase}: {state} {completed_units}/{total_units} "
            f"({percent:.1f}%){rate_text}{eta_text}",
            end=end,
            file=self._stderr,
            flush=True,
        )
        self._human_active_phase = phase if state == "running" else None
        if self._human_active_phase is None:
            self._human_phase_samples.pop(phase, None)

    @staticmethod
    def _human_eta(completed_units: int, total_units: int, rate: float) -> str:
        if rate <= 0 or completed_units >= total_units:
            return ""
        remaining_seconds = round((total_units - completed_units) / rate)
        hours, remainder = divmod(remaining_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f" ETA {hours:d}:{minutes:02d}:{seconds:02d}"

    def _close_human_phase(self) -> None:
        if self._human_active_phase is None:
            return
        print(file=self._stderr)
        self._human_phase_samples.pop(self._human_active_phase, None)
        self._human_active_phase = None

    @contextmanager
    def heartbeat(
        self,
        phase: str,
        *,
        interval_seconds: float = 5.0,
    ) -> Iterator[Callable[[str, int | None, int | None, int | None], None]]:
        """Emit periodic heartbeats while allowing the owner to change phase."""
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        stop = Event()
        state_lock = Lock()
        active_phase = phase
        active_run_id: int | None = None
        active_completed: int | None = None
        active_total: int | None = None

        def update(
            next_phase: str,
            next_run_id: int | None = None,
            next_completed: int | None = None,
            next_total: int | None = None,
        ) -> None:
            nonlocal active_completed, active_phase, active_run_id, active_total
            with state_lock:
                active_phase = next_phase
                active_run_id = next_run_id
                active_completed = next_completed
                active_total = next_total

        def pulse() -> None:
            while not stop.wait(interval_seconds):
                with state_lock:
                    snapshot = (
                        active_phase,
                        active_run_id,
                        active_completed,
                        active_total,
                    )
                self.emit(
                    snapshot[0],
                    "running",
                    event="heartbeat",
                    run_id=snapshot[1],
                    completed_units=snapshot[2],
                    total_units=snapshot[3],
                )

        thread = Thread(target=pulse, name="cc-search-progress", daemon=True)
        thread.start()
        try:
            yield update
        finally:
            stop.set()
            thread.join(timeout=min(1.0, interval_seconds + 0.1))

    def terminal(self, envelope: dict[str, object]) -> None:
        if self._terminal:
            raise RuntimeError("progress stream already has a terminal event")
        self._terminal = True
        refresh = cast("dict[str, object]", envelope["refresh"])
        semantic = cast("dict[str, object]", envelope["semantic"])
        coverage = cast("dict[str, object]", envelope["coverage"])
        self.emit(
            "done",
            str(envelope["status"]),
            event="terminal",
            run_id=cast("int | None", refresh.get("run_id")),
            corpus_generation=cast("int | None", refresh.get("corpus_generation")),
            semantic_build=cast(
                "int | None",
                semantic.get("semantic_build"),
            ),
            deadline_ms=cast("int | None", envelope.get("deadline_ms")),
            retrieval_mode=cast("str | None", envelope.get("retrieval_mode")),
            mode=cast("str | None", envelope.get("mode")),
            indexed_at=cast("str | None", envelope.get("indexed_at")),
            corpus_age_ms=cast("int | None", envelope.get("corpus_age_ms")),
            index_state=envelope.get("index_state"),
            stale_reasons=envelope.get("stale_reasons"),
            warning=envelope.get("warnings"),
            error=envelope.get("error"),
            coverage=coverage,
            refresh=refresh,
            semantic=semantic,
        )

    @property
    def ndjson(self) -> bool:
        return self._ndjson

    def failure(self, status: str, error: object) -> None:
        if self._terminal:
            return
        self._terminal = True
        self.emit(
            "done",
            status,
            event="terminal",
            error=error,
        )


def _error_envelope(
    command: str,
    status: str,
    error: Mapping[str, object],
    *,
    mode: str | None = None,
    index_state_reason: str | None = None,
) -> dict[str, object]:
    envelope: dict[str, object] = {
        "schema_version": _POSTGRES_SCHEMA_VERSION,
        "command": command,
        "status": status,
        "coverage": {
            "configured_root_count": 0,
            "resolved_root_count": 0,
            "roots": [],
            "repository_count": 0,
            "discovered_files": 0,
            "metadata_checked_files": 0,
            "unchanged_files": 0,
            "content_read_files": 0,
            "content_read_bytes": 0,
            "read_files": 0,
            "removed_files": 0,
            "blocked_files": 0,
            "transient_failure_files": 0,
            "indexed_files": 0,
            "skipped_files": 0,
            "skipped_records": 0,
            "repaired_records": 0,
            "excluded_files": 0,
            "unreadable_files": 0,
            "unknown_sessions": 0,
            "unrecognized_conversation_records": 0,
            "completeness": "unknown",
        },
        "refresh": {
            "corpus_generation": None,
            "run_id": None,
            "state": "unavailable",
            "failed_sources": 0,
            "attempted_sources": 0,
            "attempted_content_bytes": 0,
            "blocked_sources": 0,
            "transient_failure_sources": 0,
            "advanced_sources": 0,
            "pending_bytes": 0,
        },
        "semantic": {
            "semantic_build": None,
            "corpus_generation": None,
            "state": "unavailable",
            "profile_id": None,
            "completed_units": 0,
            "total_units": 0,
            "fresh": False,
            "model_load_ms": None,
            "query_embed_ms": None,
            "warm_reused": None,
        },
        "indexed_at": None,
        "corpus_age_ms": None,
        "warnings": [],
        "error": error,
    }
    if mode is not None:
        envelope["mode"] = mode
    if index_state_reason is not None:
        now = datetime.now().astimezone()
        envelope["index_state"] = {
            "made_at": None,
            "now": now.isoformat(),
            "age_ms": None,
            "corpus_generation": None,
            "semantic_build": None,
            "unindexed": None,
            "unindexed_reason": index_state_reason,
        }
    return envelope


def _requested_search_mode(args: argparse.Namespace) -> str:
    return "literal" if args.literal else "semantic"


def _command_error_envelope(
    args: argparse.Namespace,
    status: str,
    error: Mapping[str, object],
) -> dict[str, object]:
    search = args.command == "search"
    status_request = args.command == "index" and getattr(args, "status", False)
    return _error_envelope(
        args.command,
        status,
        error,
        mode=_requested_search_mode(args) if search else None,
        index_state_reason="unavailable" if search or status_request else None,
    )


def _alias_json(alias: StoredAlias) -> dict[str, object]:
    return {
        "locator": alias.locator,
        "source_file_relative": alias.source_file_relative,
        "record_ordinal": alias.record_ordinal,
        "source_line": alias.source_line,
        "source_byte_offset": alias.source_byte_offset,
        "raw_byte_length": alias.raw_byte_length,
        "source_digest": alias.source_digest,
    }


def _identity_json(message: StoredMessage) -> dict[str, object]:
    return {
        "provider": message.provider,
        "source_session_id": message.source_session_id,
        "logical_message_id": message.logical_message_id,
        "canonical_locator": message.canonical_locator,
        "physical_aliases": [_alias_json(alias) for alias in message.physical_aliases],
    }


def _message_json(
    message: StoredMessage, *, reference_only: bool = False
) -> dict[str, object]:
    value: dict[str, object] = {
        "identity": _identity_json(message),
        "timestamp": message.timestamp,
        "role": message.role,
        "session_kind": message.session_kind,
        "conversation_epoch": message.conversation_epoch,
        "content_class": message.content_class,
    }
    if not reference_only:
        value["text"] = message.text
    return value


def _corpus_times(
    indexed_at: datetime | None,
) -> tuple[datetime, datetime | None, int | None]:
    now = datetime.now().astimezone()
    if indexed_at is None:
        return now, None, None
    made_at = indexed_at.astimezone(now.tzinfo)
    age_ms = max(0, int((now - made_at).total_seconds() * 1000))
    return now, made_at, age_ms


def _index_state_payload(
    connection: psycopg.Connection,
    *,
    roots: tuple[ConfiguredSourceRoot, ...] | None,
    deadline: float | None,
    now: datetime,
    made_at: datetime | None,
    age_ms: int | None,
    corpus_generation: int | None,
    semantic_build: int | None,
) -> dict[str, object]:
    if roots is None:
        return {}
    if made_at is None:
        unindexed, reason = None, "no_selected_corpus"
    elif deadline is None:
        raise RuntimeError("index-state scan requires a deadline")
    else:
        unindexed, reason = unindexed_sources(
            connection,
            roots,
            deadline_monotonic=deadline,
        )
    return {
        "index_state": {
            "made_at": made_at.isoformat() if made_at is not None else None,
            "now": now.isoformat(),
            "age_ms": age_ms,
            "corpus_generation": corpus_generation,
            "semantic_build": semantic_build,
            "unindexed": (
                {
                    "files": unindexed.files,
                    "directories": unindexed.directories,
                    "bytes": unindexed.bytes,
                }
                if unindexed is not None
                else None
            ),
            "unindexed_reason": reason,
        }
    }


@dataclass(frozen=True)
class _RefreshMetrics:
    corpus_generation: int | None
    run_id: int | None
    state: str
    discovered_files: int
    changed_files: int
    failed_files: int
    read_files: int
    removed_files: int
    advanced_files: int
    metadata_checked_files: int
    attempted_files: int
    attempted_content_bytes: int
    blocked_files: int
    transient_failure_files: int
    pending_bytes: int
    skipped_records: int


@dataclass(frozen=True)
class _CorpusTiming:
    now: datetime
    made_at: datetime | None
    age_ms: int | None


class _RootCoverage(TypedDict):
    provider: str
    resolved_path: str
    discovered_files: int
    indexed_files: int
    excluded_files: int
    pending_files: int


def _postgres_roots(connection: psycopg.Connection) -> list[_RootCoverage]:
    return [
        {
            "provider": provider,
            "resolved_path": resolved_path,
            "discovered_files": discovered,
            "indexed_files": indexed,
            "excluded_files": excluded,
            "pending_files": pending,
        }
        for (
            provider,
            resolved_path,
            discovered,
            indexed,
            excluded,
            pending,
        ) in connection.execute(
            """
            SELECT root.provider, root.resolved_path,
                   count(source.source_file_relative),
                   count(*) FILTER (WHERE source.source_status = 'indexed'),
                   count(*) FILTER (WHERE source.source_status = 'excluded'),
                   count(*) FILTER (WHERE source.pending_bytes > 0)
            FROM cc_search_chats.source_root_current AS root
            LEFT JOIN cc_search_chats.source_file_current AS source
              USING (source_root_id)
            GROUP BY root.source_root_id, root.provider, root.resolved_path,
                     root.configured_order
            ORDER BY root.configured_order
            """
        )
    ]


def _postgres_refresh_row(connection: psycopg.Connection):
    return next(
        connection.execute(
            """
            SELECT state.current_corpus_generation, run.run_id, run.status,
                   run.source_count, run.changed_source_count,
                   run.failed_source_count, run.read_source_count,
                   run.removed_source_count, run.advanced_source_count,
                   run.metadata_checked_source_count,
                   run.attempted_source_count,
                   run.attempted_content_bytes,
                   run.blocked_source_count,
                   run.transient_failure_source_count,
                   run.diagnostics,
                   COALESCE((
                       SELECT sum(source.pending_bytes)
                       FROM cc_search_chats.source_file_current AS source
                   ), 0)::bigint,
                   COALESCE((
                       SELECT sum(source.skipped_record_count)
                       FROM cc_search_chats.source_file_current AS source
                   ), 0)::bigint
            FROM cc_search_chats.corpus_state AS state
            LEFT JOIN LATERAL (
                SELECT *
                FROM cc_search_chats.refresh_run
                ORDER BY run_id DESC
                LIMIT 1
            ) AS run ON true
            WHERE state.singleton
            """
        )
    )


def _refresh_metrics(
    refresh_row,
    roots: Sequence[_RootCoverage],
    refresh_result: RefreshResult | CorpusIndexResult | None,
) -> _RefreshMetrics:
    current_discovered = sum(int(root["discovered_files"]) for root in roots)
    values = _RefreshMetrics(
        corpus_generation=refresh_row[0],
        run_id=refresh_row[1],
        state=refresh_row[2] or "unchanged",
        discovered_files=(
            int(refresh_row[3]) if refresh_row[3] is not None else current_discovered
        ),
        changed_files=int(refresh_row[4] or 0),
        failed_files=int(refresh_row[5] or 0),
        read_files=int(refresh_row[6] or 0),
        removed_files=int(refresh_row[7] or 0),
        advanced_files=int(refresh_row[8] or 0),
        metadata_checked_files=int(refresh_row[9] or 0),
        attempted_files=int(refresh_row[10] or 0),
        attempted_content_bytes=int(refresh_row[11] or 0),
        blocked_files=int(refresh_row[12] or 0),
        transient_failure_files=int(refresh_row[13] or 0),
        pending_bytes=int(refresh_row[15] or 0),
        skipped_records=int(refresh_row[16] or 0),
    )
    if refresh_result is None:
        return values
    return _RefreshMetrics(
        corpus_generation=refresh_result.corpus_generation,
        run_id=refresh_result.run_id,
        state=(
            refresh_row[2]
            if refresh_result.run_id is not None
            and refresh_result.run_id == refresh_row[1]
            else "unchanged"
        ),
        discovered_files=refresh_result.source_count,
        changed_files=refresh_result.changed_source_count,
        failed_files=refresh_result.failed_source_count,
        read_files=refresh_result.read_source_count,
        removed_files=refresh_result.removed_source_count,
        advanced_files=refresh_result.advanced_source_count,
        metadata_checked_files=refresh_result.metadata_checked_source_count,
        attempted_files=refresh_result.attempted_source_count,
        attempted_content_bytes=refresh_result.attempted_content_bytes,
        blocked_files=refresh_result.blocked_source_count,
        transient_failure_files=refresh_result.transient_failure_source_count,
        pending_bytes=values.pending_bytes,
        skipped_records=values.skipped_records,
    )


def _postgres_repository_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            SELECT count(DISTINCT COALESCE(repository, cwd))
            FROM cc_search_chats.message_current
            WHERE COALESCE(repository, cwd) IS NOT NULL
            """
        )
    )[0]


def _postgres_unknown_sessions(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            SELECT count(*)
            FROM (
                SELECT provider, source_session_id
                FROM cc_search_chats.message_current
                WHERE session_kind = 'unknown'
                GROUP BY provider, source_session_id
            ) AS unknown_session
            """
        )
    )[0]


def _coverage_completeness(metrics: _RefreshMetrics) -> str:
    incomplete = (
        metrics.state in {"partial", "failed"}
        or metrics.failed_files
        or metrics.blocked_files
        or metrics.transient_failure_files
        or metrics.pending_bytes
    )
    return "partial" if incomplete else "complete"


def _postgres_coverage(
    connection: psycopg.Connection,
    roots: list[_RootCoverage],
    diagnostics: Sequence[object],
    metrics: _RefreshMetrics,
) -> dict[str, object]:
    unrecognized_records = sum(
        1
        for diagnostic in diagnostics
        if isinstance(diagnostic, dict)
        and "unsupported " in str(diagnostic.get("detail", "")).lower()
    )
    repaired_records = sum(
        1
        for diagnostic in diagnostics
        if isinstance(diagnostic, dict) and diagnostic.get("code") == "record_repaired"
    )
    return {
        "configured_root_count": len(roots),
        "resolved_root_count": len(roots),
        "roots": roots,
        "repository_count": _postgres_repository_count(connection),
        "discovered_files": metrics.discovered_files,
        "metadata_checked_files": metrics.metadata_checked_files,
        "unchanged_files": max(
            0,
            metrics.metadata_checked_files
            - metrics.attempted_files
            - metrics.blocked_files
            - metrics.transient_failure_files,
        ),
        "content_read_files": metrics.attempted_files,
        "content_read_bytes": metrics.attempted_content_bytes,
        "read_files": metrics.read_files,
        "removed_files": metrics.removed_files,
        "blocked_files": metrics.blocked_files,
        "transient_failure_files": metrics.transient_failure_files,
        "indexed_files": sum(int(root["indexed_files"]) for root in roots),
        "skipped_files": max(0, metrics.discovered_files - metrics.changed_files),
        "skipped_records": metrics.skipped_records,
        "repaired_records": repaired_records,
        "excluded_files": sum(int(root["excluded_files"]) for root in roots),
        "unreadable_files": metrics.transient_failure_files,
        "unknown_sessions": _postgres_unknown_sessions(connection),
        "unrecognized_conversation_records": unrecognized_records,
        "completeness": _coverage_completeness(metrics),
    }


def _postgres_refresh(metrics: _RefreshMetrics) -> dict[str, object]:
    return {
        "corpus_generation": metrics.corpus_generation,
        "run_id": metrics.run_id,
        "state": metrics.state,
        "failed_sources": metrics.failed_files,
        "attempted_sources": metrics.attempted_files,
        "attempted_content_bytes": metrics.attempted_content_bytes,
        "blocked_sources": metrics.blocked_files,
        "transient_failure_sources": metrics.transient_failure_files,
        "advanced_sources": metrics.advanced_files,
        "pending_bytes": metrics.pending_bytes,
    }


def _postgres_semantic(connection: psycopg.Connection) -> dict[str, object]:
    row = next(
        connection.execute(
            """
            SELECT build.semantic_build,
                   build.corpus_generation,
                   build.status,
                   build.profile_id,
                   build.completed_units,
                   build.total_units,
                   build.corpus_generation = generation.corpus_generation
                       AND build.status = 'complete'
                       AND build.completed_at IS NOT NULL AS fresh
            FROM cc_search_chats.corpus_state AS state
            LEFT JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            LEFT JOIN cc_search_chats.semantic_build AS build
              ON (build.semantic_build, build.corpus_generation) =
                 (generation.semantic_build, generation.corpus_generation)
            WHERE state.singleton
            """
        )
    )
    return {
        "semantic_build": row[0],
        "corpus_generation": row[1],
        "state": row[2] or "unavailable",
        "profile_id": row[3],
        "completed_units": row[4] or 0,
        "total_units": row[5] or 0,
        "fresh": row[6] is True,
        "model_load_ms": None,
        "query_embed_ms": None,
        "warm_reused": None,
    }


def _postgres_timing(connection: psycopg.Connection) -> _CorpusTiming:
    indexed_at = next(
        connection.execute(
            """
            SELECT generation.completed_at
            FROM cc_search_chats.corpus_state AS state
            LEFT JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            WHERE state.singleton
            """
        )
    )[0]
    return _CorpusTiming(*_corpus_times(indexed_at))


def _postgres_envelope(
    connection: psycopg.Connection,
    command: str,
    *,
    status: str = "complete",
    additional_warnings: Sequence[object] = (),
    include_skipped_warnings: bool | None = None,
    refresh_result: RefreshResult | CorpusIndexResult | None = None,
    index_state_roots: tuple[ConfiguredSourceRoot, ...] | None = None,
    index_state_deadline: float | None = None,
    **payload: object,
) -> dict[str, object]:
    roots = _postgres_roots(connection)
    refresh_row = _postgres_refresh_row(connection)
    metrics = _refresh_metrics(refresh_row, roots, refresh_result)
    diagnostics = refresh_row[14] or []
    coverage = _postgres_coverage(connection, roots, diagnostics, metrics)
    refresh = _postgres_refresh(metrics)
    semantic = _postgres_semantic(connection)
    timing = _postgres_timing(connection)
    if include_skipped_warnings is None:
        include_skipped_warnings = command == "index"
    refresh_warnings = [
        diagnostic
        for diagnostic in diagnostics
        if not (
            isinstance(diagnostic, dict)
            and (
                diagnostic.get("code") == "record_repaired"
                or (
                    diagnostic.get("code") == "record_skipped"
                    and not include_skipped_warnings
                )
            )
        )
    ]
    return {
        "schema_version": _POSTGRES_SCHEMA_VERSION,
        "command": command,
        "status": status,
        "coverage": coverage,
        "refresh": refresh,
        "semantic": semantic,
        "indexed_at": (
            timing.made_at.isoformat() if timing.made_at is not None else None
        ),
        "corpus_age_ms": timing.age_ms,
        "warnings": [*refresh_warnings, *additional_warnings],
        **payload,
        **_index_state_payload(
            connection,
            roots=index_state_roots,
            deadline=index_state_deadline,
            now=timing.now,
            made_at=timing.made_at,
            age_ms=timing.age_ms,
            corpus_generation=metrics.corpus_generation,
            semantic_build=cast("int | None", semantic["semantic_build"]),
        ),
    }


def _applied_schema_version(connection: psycopg.Connection) -> int:
    version = next(
        connection.execute("SELECT max(version) FROM cc_search_chats.schema_migration")
    )[0]
    if version is None:
        raise RuntimeError("schema migration ledger is empty after migration")
    return int(version)


def _index_timestamp(value: object) -> str:
    parsed = datetime.fromisoformat(str(value))
    offset = parsed.strftime("%z")
    return f"{parsed:%Y-%m-%d %H:%M:%S} {offset[:3]}:{offset[3:]}"


def _index_age(age_ms: int) -> str:
    total_minutes = max(0, int(age_ms)) // 60_000
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if hours else f"{minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {minutes}m"


def _print_index_state_header(envelope: Mapping[str, object]) -> None:
    state = cast("Mapping[str, object]", envelope["index_state"])
    made_at = state["made_at"]
    age_ms = state["age_ms"]
    now = _index_timestamp(state["now"])
    if made_at is None or age_ms is None:
        print(f"index made unknown; now {now}; age unknown")
    elif not isinstance(age_ms, int):
        raise RuntimeError("index-state age_ms must be an integer")
    else:
        print(
            f"index made {_index_timestamp(made_at)}; now {now}; "
            f"age {_index_age(age_ms)}"
        )
    unindexed = cast("Mapping[str, object] | None", state["unindexed"])
    if unindexed is None:
        print(f"unindexed chats: unknown ({state['unindexed_reason']})")
    else:
        files = unindexed["files"]
        if not isinstance(files, int):
            raise RuntimeError("index-state unindexed files must be an integer")
        if files == 0:
            print("missing 0 chats")
            return
        print(
            f"missing {files} chats in {unindexed['directories']} "
            "directories since that index; run `cc-search-chats index` to include "
            "them"
        )


def _print_human_search(
    args: argparse.Namespace,
    envelope: Mapping[str, object],
    warnings: Sequence[Mapping[str, str]],
    results: Sequence[Mapping[str, object]],
) -> None:
    print(
        f"literal search (exact full-text, no model): {args.query}"
        if args.literal
        else f"semantic search (hybrid model ranking): {args.query}"
    )
    _print_index_state_header(envelope)
    semantic = cast("Mapping[str, object]", envelope["semantic"])
    if not args.literal and isinstance(semantic["warm_reused"], bool):
        if semantic["warm_reused"]:
            print("semantic: warm model reused")
        else:
            warm_seconds = f"{semantic_warm_seconds():g}"
            print(
                "semantic: loading model (first use takes about 10 s; stays warm "
                f"{warm_seconds} s after each query)"
            )
    degraded = next(
        (
            warning
            for warning in warnings
            if warning["code"] == "semantic_search_degraded"
        ),
        None,
    )
    if degraded is not None:
        print(
            "WARNING: semantic ranking unavailable "
            f"({degraded['detail']}); these are literal results"
        )
    for result in results:
        print(
            f"[{result['timestamp']}] {result['provider']}:"
            f"{result['session_id']} ({result['role']})\n"
            f"  {result['text']}\n  {result['locator']}"
        )


def _search_identity(
    hit: SearchHit,
    identity_message: StoredMessage | None,
) -> dict[str, object]:
    if identity_message is not None:
        return _identity_json(identity_message)
    return {
        "provider": hit.provider,
        "source_session_id": hit.source_session_id,
        "logical_message_id": hit.logical_message_id,
        "canonical_locator": hit.canonical_locator,
        "physical_aliases": [],
    }


def _search_ranking(
    hit: SearchHit,
    hybrid: HybridHit | None,
    *,
    exhaustive: bool,
) -> dict[str, object]:
    if hybrid is None:
        return {
            "method": "fts",
            "score": hit.rank,
            "tie_breaker": (
                "canonical_locator,content_class,record_ordinal,digest"
                if exhaustive
                else "rank,provider,session,logical_message"
            ),
        }
    return {
        "method": "rrf",
        "score": {
            "numerator": hybrid.score.numerator,
            "denominator": hybrid.score.denominator,
        },
        "rank_constant": hybrid.rank_constant,
        "component_depth": hybrid.component_depth,
        "literal_rank": hybrid.literal_rank,
        "semantic_rank": hybrid.semantic_rank,
        "literal_score": hybrid.literal_score,
        "semantic_score": hybrid.semantic_score,
        "semantic_chunk_ordinal": hybrid.semantic_chunk_ordinal,
        "tie_breaker": "canonical_locator",
    }


def _search_result(
    hit: SearchHit,
    identity_message: StoredMessage | None,
    hybrid: HybridHit | None,
    *,
    exhaustive: bool,
) -> dict[str, object]:
    return {
        "identity": _search_identity(hit, identity_message),
        "provider": hit.provider,
        "session_id": hit.source_session_id,
        "logical_message_id": hit.logical_message_id,
        "locator": hit.canonical_locator,
        "timestamp": hit.timestamp,
        "role": hit.role,
        "session_kind": hit.session_kind,
        "conversation_epoch": hit.conversation_epoch,
        "content_class": hit.content_class,
        "text": hit.text,
        "repository": hit.repository,
        "cwd": hit.cwd,
        "score": float(hybrid.score) if hybrid is not None else hit.rank,
        "ranking": _search_ranking(hit, hybrid, exhaustive=exhaustive),
    }


def _resolved_search_results(
    hits: Sequence[SearchHit],
    resolutions: Sequence[MessageResolution],
    hybrid_rankings: Mapping[str, HybridHit],
    *,
    exhaustive: bool,
    answer_deadline: float | None,
) -> list[dict[str, object]]:
    results = []
    for hit, resolution in zip(hits, resolutions, strict=True):
        if answer_deadline is not None and monotonic() >= answer_deadline:
            raise SearchDeadlineExceeded(
                "search deadline expired during result rendering"
            )
        identity_message = next(
            (
                message
                for message in resolution.messages
                if message.logical_message_id == hit.logical_message_id
            ),
            None,
        )
        if identity_message is None:
            raise RuntimeError(
                f"search result identity became stale: {hit.canonical_locator}"
            )
        results.append(
            _search_result(
                hit,
                identity_message,
                hybrid_rankings.get(hit.canonical_locator),
                exhaustive=exhaustive,
            )
        )
    return results


def _unresolved_search_results(
    hits: Sequence[SearchHit],
    hybrid_rankings: Mapping[str, HybridHit],
    *,
    exhaustive: bool,
) -> list[dict[str, object]]:
    return [
        _search_result(
            hit,
            None,
            hybrid_rankings.get(hit.canonical_locator),
            exhaustive=exhaustive,
        )
        for hit in hits
    ]


def _append_envelope_warning(
    envelope: Mapping[str, object],
    warning: Mapping[str, str],
) -> None:
    warnings = envelope["warnings"]
    if not isinstance(warnings, list):
        raise TypeError("search envelope warnings must be a list")
    warnings.append(warning)


def _containment_command(payload: Sequence[str]) -> list[str]:
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--setenv=CC_SEARCH_CONTAINED=1",
        "--nice=10",
        "--property=MemoryHigh=24G",
        "--property=MemoryMax=32G",
        "--property=MemorySwapMax=4G",
        "--property=TasksMax=256",
        "--property=CPUWeight=25",
        "--property=IOWeight=25",
        "--",
        *payload,
    ]


def _preflight_failure_detail(stderr: str, returncode: int) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if lines:
        return lines[-1]
    if returncode < 0:
        return f"systemd-run was terminated by signal {-returncode}"
    return f"systemd-run exited with status {returncode} without diagnostics"


def _preflight_containment(command: Sequence[str]) -> None:
    separator = command.index("--")
    preflight = [*command[: separator + 1], "true"]
    try:
        result = subprocess.run(
            preflight,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_CONTAINMENT_PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        raise SystemdScopeUnavailable("systemd-run executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise SystemdScopeUnavailable(
            "systemd-run preflight timed out after "
            f"{_CONTAINMENT_PREFLIGHT_TIMEOUT_SECONDS:g} seconds"
        ) from error
    except OSError as error:
        raise SystemdScopeUnavailable(
            f"systemd-run could not be executed: {error}"
        ) from error
    if result.returncode != 0:
        raise SystemdScopeUnavailable(
            _preflight_failure_detail(result.stderr, result.returncode)
        )


def _contain_semantic_index(args: argparse.Namespace) -> None:
    """Re-enter indexing in a host-safe, low-priority systemd scope."""
    if (
        args.command != "index"
        or args.status
        or os.environ.get("CC_SEARCH_CONTAINED") == "1"
        or "/run-r" in Path("/proc/self/cgroup").read_text(encoding="utf-8")
    ):
        return
    command = _containment_command(
        [
            "ionice",
            "--class=idle",
            *sys.argv,
        ]
    )
    _preflight_containment(command)
    try:
        os.execvp(command[0], command)
    except OSError as error:
        detail = (
            "systemd-run executable was not found"
            if isinstance(error, FileNotFoundError)
            else f"systemd-run could not be executed: {error}"
        )
        raise SystemdScopeUnavailable(detail) from error


def _literal_fallback_command(args: argparse.Namespace) -> str:
    query = args.query if args.command == "search" else "QUERY"
    command = ["cc-search-chats", "search", query, "--literal"]
    if args.command == "search":
        for enabled, flag in (
            (args.all, "--all"),
            (args.json, "--json"),
        ):
            if enabled:
                command.append(flag)
        for value, flag in (
            (args.project, "--project"),
            (args.provider, "--provider"),
            (args.role, "--role"),
            (args.epoch, "--epoch"),
            (args.days, "--days"),
            (args.limit, "--limit"),
        ):
            if value is not None:
                command.extend((flag, str(value)))
    return shlex.join(command)


@dataclass(frozen=True)
class _PostgresContext:
    args: argparse.Namespace
    dsn: str
    connection: psycopg.Connection
    progress_stream: _ProgressStream

    def finish(
        self,
        command: str,
        *,
        status: str = "complete",
        additional_warnings: Sequence[object] = (),
        emit_skipped_warning_events: bool = False,
        include_skipped_warnings: bool | None = None,
        refresh_result: RefreshResult | CorpusIndexResult | None = None,
        index_state_roots: tuple[ConfiguredSourceRoot, ...] | None = None,
        index_state_deadline: float | None = None,
        **payload: object,
    ) -> dict[str, object]:
        envelope = _postgres_envelope(
            self.connection,
            command,
            status=status,
            additional_warnings=additional_warnings,
            include_skipped_warnings=include_skipped_warnings,
            refresh_result=refresh_result,
            index_state_roots=index_state_roots,
            index_state_deadline=index_state_deadline,
            **payload,
        )
        if emit_skipped_warning_events and self.progress_stream.ndjson:
            refresh = cast("dict[str, object]", envelope["refresh"])
            for warning in cast("list[object]", envelope["warnings"]):
                if (
                    isinstance(warning, dict)
                    and warning.get("code") == "record_skipped"
                ):
                    self.progress_stream.emit(
                        "parse",
                        "warning",
                        event="warning",
                        run_id=cast("int | None", refresh["run_id"]),
                        warning=warning,
                    )
        self.progress_stream.terminal(envelope)
        return envelope


class _IndexProgress:
    def __init__(self, stream: _ProgressStream) -> None:
        self.stream = stream
        self.scan_complete = False
        self.parse_seen = False
        self.report_model_progress = True
        self.heartbeat_update: (
            Callable[[str, int | None, int | None, int | None], None] | None
        ) = None

    def refresh(self, event: RefreshProgress) -> None:
        if self.heartbeat_update is not None:
            self.heartbeat_update(
                event.phase,
                event.run_id,
                event.completed_units,
                event.total_units,
            )
        if event.phase == "parse" and not self.scan_complete:
            self.stream.emit("scan", "complete")
            self.scan_complete = True
        self.parse_seen = self.parse_seen or event.phase == "parse"
        self.stream.emit(
            event.phase,
            event.state,
            run_id=event.run_id,
            completed_units=event.completed_units,
            total_units=event.total_units,
            owner=event.owner_pid,
        )

    def model(self, phase: str, state: str) -> None:
        if self.heartbeat_update is not None:
            self.heartbeat_update(phase, None, None, None)
        self.stream.emit(phase, state)
        if (
            phase == "model_load"
            and state == "complete"
            and self.heartbeat_update is not None
        ):
            self.heartbeat_update("semantic_embed", None, None, None)

    def passage_embed(self, texts):
        model_callback = self.model if self.report_model_progress else None
        self.report_model_progress = False
        with model_output_scope(quiet=self.stream.ndjson):
            return embed_passages(texts, progress=model_callback)

    def passage_chunks(self, texts):
        with model_output_scope(quiet=self.stream.ndjson):
            return chunk_passages(texts)

    def embedding(self, completed: int, total: int) -> None:
        if self.heartbeat_update is not None:
            self.heartbeat_update("semantic_embed", None, completed, total)
        self.stream.emit(
            "semantic_embed",
            "running" if completed < total else "complete",
            completed_units=completed,
            total_units=total,
        )

    def complete(self, result: CorpusIndexResult) -> None:
        if not self.scan_complete:
            self.stream.emit("scan", "complete")
            self.scan_complete = True
        if not self.parse_seen:
            self.stream.emit(
                "parse",
                "complete",
                completed_units=result.read_source_count,
                total_units=result.changed_source_count,
            )
        self.stream.emit(
            "fts_commit",
            "complete",
            completed_units=result.changed_source_count - result.failed_source_count,
            total_units=result.changed_source_count,
            corpus_generation=result.corpus_generation,
        )
        self.stream.emit(
            "semantic_commit",
            "complete",
            completed_units=result.embedding_count,
            total_units=result.embedding_count,
            semantic_build=result.semantic_build,
        )


def _postgres_index_migrate(context: _PostgresContext) -> int:
    migrate(context.connection)
    applied_schema_version = _applied_schema_version(context.connection)
    envelope = context.finish(
        "index",
        include_skipped_warnings=False,
        applied_schema_version=applied_schema_version,
    )
    if context.args.json:
        print(json.dumps(envelope, sort_keys=True))
    else:
        print(f"Applied PostgreSQL schema migration {applied_schema_version}")
    return 0


def _postgres_index_status(context: _PostgresContext) -> int:
    status = next(
        context.connection.execute(
            """
            SELECT state.current_corpus_generation,
                   build.semantic_build,
                   COALESCE(build.completed_units, 0),
                   COALESCE(build.total_units, 0),
                   build.semantic_build IS NOT NULL
            FROM cc_search_chats.corpus_state AS state
            LEFT JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            LEFT JOIN cc_search_chats.semantic_build AS build
              ON (build.semantic_build, build.corpus_generation) =
                 (generation.semantic_build, generation.corpus_generation)
            WHERE state.singleton
            """
        ),
        None,
    )
    selected_corpus, selected_build, completed, total, selected = status or (
        None,
        None,
        0,
        0,
        False,
    )
    envelope = context.finish(
        "index",
        include_skipped_warnings=False,
        index_state_roots=configured_source_roots(),
        index_state_deadline=(
            context.args.request_started
            + _SEARCH_DEADLINE_SECONDS
            - _SEARCH_RENDER_RESERVE_SECONDS
        ),
        corpus_generation=selected_corpus,
        semantic_build=selected_build,
        completed=completed,
        total=total,
        selected=bool(selected),
    )
    if context.args.json:
        print(json.dumps(envelope, sort_keys=True))
    else:
        _print_index_state_header(envelope)
        print(f"Semantic index: {completed}/{total} passages")
    return 0


def _postgres_index_run(context: _PostgresContext) -> int:
    shutdown_query_embedder()
    acquire_index_session(context.connection)
    verify_model_revision(
        context.connection,
        local_model_revision(),
        adopt_unknown=True,
    )
    progress = _IndexProgress(context.progress_stream)
    context.progress_stream.emit("scan", "running")
    with context.progress_stream.heartbeat("scan") as heartbeat_update:
        progress.heartbeat_update = heartbeat_update
        result = index_corpus(
            context.connection,
            progress.passage_embed,
            chunker=progress.passage_chunks,
            source_roots=configured_source_roots(),
            progress=progress.refresh,
            embedding_progress=progress.embedding,
            force_retry=context.args.force_retry,
        )
    progress.complete(result)
    envelope = context.finish(
        "index",
        emit_skipped_warning_events=True,
        refresh_result=result,
        corpus_generation=result.corpus_generation,
        semantic_build=result.semantic_build,
        sources=result.source_count,
        messages=result.message_count,
        embeddings=result.embedding_count,
    )
    if context.args.json:
        print(json.dumps(envelope, sort_keys=True))
    else:
        for warning in cast("list[dict[str, object]]", envelope["warnings"]):
            if warning.get("code") == "record_skipped":
                print(
                    "WARNING: skipped "
                    f"{warning.get('provider')} record "
                    f"{warning.get('source_file_relative')}:"
                    f"{warning.get('source_line')} "
                    f"({warning.get('reason')}): {warning.get('detail')}",
                    file=sys.stderr,
                )
        print(
            f"Indexed {result.message_count} messages from "
            f"{result.source_count} sources into corpus {result.corpus_generation}",
            file=sys.stderr,
        )
    return 0


def _postgres_events(context: _PostgresContext) -> int:
    export = export_human_message_events(
        context.connection,
        from_utc=_parse_utc_bound(context.args.from_utc),
        until_utc=_parse_utc_bound(context.args.until_utc),
    )
    payload = event_export_payload(export)
    envelope = context.finish(
        "events",
        window=payload["window"],
        source_corpus_generation=payload["source_corpus_generation"],
        population=payload["population"],
        events=payload["events"],
    )
    if context.args.json:
        print(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
    else:
        population = export.population
        print(
            f"Retained {population.retained} human events from "
            f"{population.scanned_logical_messages} canonical messages"
        )
    return 0


def _postgres_list(context: _PostgresContext) -> int:
    sessions = pg_list_sessions(
        context.connection,
        provider=context.args.provider,
        project=context.args.project,
        since=_since_days(context.args.days),
    )
    values = [
        {
            "identity": {
                "provider": value.provider,
                "source_session_id": value.source_session_id,
            },
            "provider": value.provider,
            "session_id": value.source_session_id,
            "session_kind": value.session_kind,
            "latest_timestamp": value.latest_timestamp,
            "message_count": value.message_count,
            "repository": value.repository,
            "cwd": value.cwd,
        }
        for value in sessions
    ]
    envelope = context.finish("list", sessions=values)
    if context.args.json:
        print(json.dumps(envelope))
    else:
        for value in values:
            print(
                f"{value['provider']}:{value['session_id']} "
                f"({value['session_kind']}, {value['message_count']} messages)"
            )
    return 0


def _postgres_extract(context: _PostgresContext) -> int:
    session_id = context.args.session_id
    provider = context.args.provider
    if session_id is None:
        sessions = pg_list_sessions(
            context.connection,
            provider=context.args.provider,
            project=context.args.project,
        )
        if not sessions:
            raise ValueError("no matching sessions")
        session_id = sessions[0].source_session_id
        provider = sessions[0].provider
    messages = pg_extract_session(
        context.connection,
        session_id,
        provider=provider,
        epoch=context.args.epoch,
    )
    providers = sorted({message.provider for message in messages})
    if (
        context.args.session_id is not None
        and context.args.provider is None
        and len(providers) > 1
    ):
        return _postgres_extract_multiple(context, session_id, providers)
    values = [_message_json(message) for message in messages]
    envelope = context.finish(
        "extract",
        status="complete" if messages else "no_match",
        messages=values,
    )
    if context.args.json:
        print(json.dumps(envelope))
    else:
        for value in messages:
            print(f"[{value.timestamp}] {value.role}:\n{value.text}")
    return 0 if messages else 3


def _postgres_extract_multiple(
    context: _PostgresContext,
    session_id: str,
    providers: Sequence[str],
) -> int:
    matches = [
        {"provider": candidate, "source_session_id": session_id}
        for candidate in providers
    ]
    envelope = context.finish(
        "extract",
        status="multiple_matches",
        matches=matches,
        messages=[],
    )
    if context.args.json:
        print(json.dumps(envelope))
    else:
        print(
            "Session ID matches multiple providers; pass --provider",
            file=sys.stderr,
        )
    return 3


def _postgres_context_resolve(context: _PostgresContext) -> int:
    if context.args.command == "resolve" and context.args.stdin:
        return _postgres_stdin_resolve(context)
    return _postgres_single_resolve(context)


def _postgres_stdin_resolve(context: _PostgresContext) -> int:
    if context.args.uuid is not None:
        print("resolve accepts a locator or --stdin, not both", file=sys.stderr)
        return 2
    locators = tuple(line.strip() for line in sys.stdin if line.strip())
    if not locators:
        print("resolve --stdin requires at least one locator", file=sys.stderr)
        return 2
    resolutions = resolve_exact_messages(
        context.connection,
        locators,
        source_roots=configured_source_roots(),
    )
    values = [
        {
            "locator": resolution.locator,
            "status": resolution.status.value,
            "message_count": len(resolution.messages),
            "messages": [
                _message_json(
                    message,
                    reference_only=context.args.reference_only,
                )
                for message in resolution.messages
            ],
            "detail": resolution.detail,
        }
        for resolution in resolutions
    ]
    statuses = {resolution.status for resolution in resolutions}
    overall_status = next(iter(statuses)).value if len(statuses) == 1 else "partial"
    envelope = context.finish(
        "resolve",
        status=overall_status,
        resolutions=values,
    )
    if context.args.json:
        print(json.dumps(envelope))
    else:
        for value in values:
            print(f"{value['status']}\t{value['locator']}")
    return _resolution_exit_code(statuses)


def _resolution_exit_code(statuses: set[ResolutionStatus]) -> int:
    if statuses == {ResolutionStatus.RESOLVED}:
        return 0
    if ResolutionStatus.MALFORMED_LOCATOR in statuses:
        return 2
    return 3


def _postgres_single_resolve(context: _PostgresContext) -> int:
    exact = resolve_exact_messages(
        context.connection,
        (context.args.uuid,),
        source_roots=configured_source_roots(),
    )[0]
    messages = _postgres_resolved_messages(context, exact)
    values = [
        _message_json(
            message,
            reference_only=(
                context.args.command == "resolve" and context.args.reference_only
            ),
        )
        for message in messages
    ]
    envelope = context.finish(
        context.args.command,
        status=exact.status.value,
        detail=exact.detail,
        messages=values,
    )
    if context.args.json:
        print(json.dumps(envelope))
    else:
        for value in messages:
            print(f"[{value.timestamp}] {value.role}:\n{value.text}")
    return _resolution_exit_code({exact.status})


def _postgres_resolved_messages(
    context: _PostgresContext,
    exact: ExactResolution,
) -> Sequence[StoredMessage]:
    if context.args.command == "context":
        if exact.status is ResolutionStatus.RESOLVED:
            return pg_context_messages(
                context.connection,
                context.args.uuid,
                depth=context.args.depth,
            )
        return ()
    return exact.messages


@dataclass
class _SearchState:
    hits: tuple[SearchHit, ...]
    literal_hits: tuple[SearchHit, ...]
    component_depth: int
    retrieval_mode: str
    hybrid_rankings: dict[str, HybridHit]
    warnings: list[dict[str, str]]
    model_load_ms: int | None = None
    query_embed_ms: int | None = None
    warm_reused: bool | None = None


def _begin_search_snapshot(connection: psycopg.Connection) -> None:
    connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    next(
        connection.execute(
            """
            SELECT current_corpus_generation
            FROM cc_search_chats.corpus_state
            WHERE singleton
            """
        )
    )


def _exhaustive_search_hits(context: _PostgresContext) -> tuple[SearchHit, ...]:
    hits: list[SearchHit] = []
    cursor = None
    while True:
        page = exhaustive_search_page(
            context.connection,
            context.args.query,
            page_size=500,
            after=cursor,
            provider=context.args.provider,
            role=context.args.role,
            project=context.args.project,
            since=_since_days(context.args.days),
            epoch=context.args.epoch,
            include_agents=context.args.agents,
            include_tools=context.args.tools,
        )
        hits.extend(page.hits)
        if page.next_cursor is None:
            return tuple(hits)
        cursor = page.next_cursor


def _literal_search_state(context: _PostgresContext) -> _SearchState:
    if context.args.exhaustive:
        hits = _exhaustive_search_hits(context)
        return _SearchState(hits, (), 0, "exhaustive_literal", {}, [])
    component_depth = (
        context.args.limit
        if context.args.literal
        else min(1000, max(100, 5 * context.args.limit))
    )
    literal_hits = tuple(
        search_messages(
            context.connection,
            context.args.query,
            limit=component_depth,
            provider=context.args.provider,
            role=context.args.role,
            project=context.args.project,
            since=_since_days(context.args.days),
            epoch=context.args.epoch,
            include_agents=context.args.agents,
            include_tools=context.args.tools,
        )
    )
    return _SearchState(
        literal_hits[: context.args.limit],
        literal_hits,
        component_depth,
        "literal",
        {},
        [],
    )


def _append_revision_warning(
    connection: psycopg.Connection,
    warnings: list[dict[str, str]],
) -> None:
    revision_warning = verify_model_revision(
        connection,
        local_model_revision(),
        adopt_unknown=False,
    )
    if revision_warning is not None:
        warnings.append(
            {
                "code": "model_revision_unverified",
                "detail": revision_warning,
            }
        )


def _semantic_search_state(
    context: _PostgresContext,
    state: _SearchState,
) -> None:
    if context.args.literal or context.args.exhaustive:
        return
    _append_revision_warning(context.connection, state.warnings)
    try:
        with context.progress_stream.heartbeat("model_load") as heartbeat_update:

            def report_progress(phase: str, progress_state: str) -> None:
                heartbeat_update(phase, None, None, None)
                context.progress_stream.emit(phase, progress_state)

            query_embedding = _bounded_query_embedding(
                context.args.query,
                progress=report_progress,
                quiet=context.progress_stream.ndjson,
            )
    except (ModelUnavailable, RuntimeError, TypeError, ValueError) as error:
        state.retrieval_mode = "literal_fallback"
        state.warnings.append(
            {
                "code": "semantic_search_degraded",
                "detail": f"{type(error).__name__}: {error}",
            }
        )
        context.progress_stream.emit(
            "query_embed",
            "degraded",
            warning=state.warnings[-1],
        )
        return
    state.model_load_ms = query_embedding.model_load_ms
    state.query_embed_ms = query_embedding.query_embed_ms
    state.warm_reused = query_embedding.warm_reused
    semantic_hits = semantic_search(
        context.connection,
        query_embedding.embedding,
        limit=state.component_depth,
        provider=context.args.provider,
        role=context.args.role,
        project=context.args.project,
        since=_since_days(context.args.days),
        epoch=context.args.epoch,
        include_agents=context.args.agents,
        allow_partial=True,
    )
    hybrid_hits = fuse_hybrid(
        state.literal_hits,
        semantic_hits,
        limit=context.args.limit,
        rank_constant=60,
        component_depth=state.component_depth,
    )
    state.hits = tuple(value.message for value in hybrid_hits)
    state.hybrid_rankings = {
        value.message.canonical_locator: value for value in hybrid_hits
    }
    state.retrieval_mode = "hybrid"


def _search_envelope(
    context: _PostgresContext,
    state: _SearchState,
) -> dict[str, object]:
    envelope = _postgres_envelope(
        context.connection,
        "search",
        index_state_roots=configured_source_roots(),
        index_state_deadline=(
            monotonic() + _INDEX_STATE_SCAN_BUDGET_SECONDS
            if context.args.answer_deadline is None
            else context.args.answer_deadline
        ),
        exhaustive=context.args.exhaustive,
        result_limit=None if context.args.exhaustive else context.args.limit,
        deadline_ms=(
            None
            if context.args.answer_deadline is None
            else round(_SEARCH_DEADLINE_SECONDS * 1000)
        ),
        elapsed_ms=round((monotonic() - context.args.request_started) * 1000),
        retrieval_mode=state.retrieval_mode,
        mode=_requested_search_mode(context.args),
        stale_reasons=[],
        additional_warnings=state.warnings,
    )
    semantic = cast("dict[str, object]", envelope["semantic"])
    semantic.update(
        model_load_ms=state.model_load_ms,
        query_embed_ms=state.query_embed_ms,
        warm_reused=state.warm_reused,
    )
    return envelope


def _apply_search_staleness(
    envelope: dict[str, object],
    retrieval_mode: str,
) -> None:
    semantic_state = cast("dict[str, object]", envelope["semantic"])
    stale_reasons = cast("list[str]", envelope["stale_reasons"])
    if semantic_state["fresh"] is not True:
        stale_reasons.append("semantic_build_unavailable")
        if retrieval_mode == "hybrid":
            envelope["retrieval_mode"] = "literal_fallback"


def _resolve_or_degrade_search(
    context: _PostgresContext,
    state: _SearchState,
    envelope: dict[str, object],
    answer_deadline: float | None,
) -> Sequence[Mapping[str, object]]:
    try:
        resolutions = pg_resolve_messages(
            context.connection,
            tuple(hit.canonical_locator for hit in state.hits),
        )
        results = _resolved_search_results(
            state.hits,
            resolutions,
            state.hybrid_rankings,
            exhaustive=context.args.exhaustive,
            answer_deadline=answer_deadline,
        )
    except (
        ReadDeadlineExceeded,
        SearchDeadlineExceeded,
        psycopg.errors.LockNotAvailable,
        psycopg.errors.QueryCanceled,
    ) as error:
        if answer_deadline is None:
            raise
        deadline_warning = {
            "code": "deadline_degraded",
            "detail": f"{type(error).__name__}: {error}",
        }
        state.warnings.append(deadline_warning)
        _append_envelope_warning(envelope, deadline_warning)
        envelope["status"] = "partial"
        results = _unresolved_search_results(
            state.hits,
            state.hybrid_rankings,
            exhaustive=context.args.exhaustive,
        )
        context.connection.execute("ROLLBACK")
    else:
        context.connection.execute("COMMIT")
    return results


def _output_search(
    context: _PostgresContext,
    state: _SearchState,
    envelope: dict[str, object],
    results: Sequence[Mapping[str, object]],
) -> None:
    envelope["elapsed_ms"] = round((monotonic() - context.args.request_started) * 1000)
    envelope["results"] = results
    context.progress_stream.terminal(envelope)
    if context.args.json:
        print(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
    else:
        _print_human_search(context.args, envelope, state.warnings, results)


def _postgres_search(context: _PostgresContext) -> int:
    answer_deadline = None if context.args.exhaustive else context.args.answer_deadline
    _begin_search_snapshot(context.connection)
    context.progress_stream.emit("retrieve", "running")
    state = _literal_search_state(context)
    _semantic_search_state(context, state)
    context.progress_stream.emit(
        "retrieve",
        "complete",
        completed_units=len(state.hits),
        total_units=len(state.hits),
    )
    envelope = _search_envelope(context, state)
    _apply_search_staleness(envelope, state.retrieval_mode)
    results = _resolve_or_degrade_search(
        context,
        state,
        envelope,
        answer_deadline,
    )
    _output_search(context, state, envelope, results)
    return 0


@contextmanager
def _postgres_connection(
    args: argparse.Namespace,
    dsn: str,
) -> Iterator[psycopg.Connection]:
    if args.command == "search" and args.answer_deadline is not None:
        remaining = _remaining_search_seconds(args)
        if remaining <= 0:
            raise SearchDeadlineExceeded("search deadline expired before connection")
        connection_context = psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=max(1, ceil(remaining)),
        )
    else:
        connection_context = psycopg.connect(dsn, autocommit=True)
    with connection_context as connection:
        yield connection


def _configure_postgres_connection(
    args: argparse.Namespace,
    connection: psycopg.Connection,
) -> None:
    if args.command != "search" or args.answer_deadline is None:
        return
    remaining = _remaining_search_seconds(args)
    if remaining <= 0:
        raise SearchDeadlineExceeded(
            "search deadline expired while connecting to PostgreSQL"
        )
    timeout_ms = max(1, int(remaining * 1000))
    connection.execute(
        """
        SELECT set_config('lock_timeout', %s, false),
               set_config('statement_timeout', %s, false)
        """,
        (f"{timeout_ms}ms", f"{timeout_ms}ms"),
    )


def _handle_postgres(
    args: argparse.Namespace,
    dsn: str,
    progress_stream: _ProgressStream,
) -> int:
    """Run the migrated index/search surface against PostgreSQL."""
    with _postgres_connection(args, dsn) as connection:
        _configure_postgres_connection(args, connection)
        context = _PostgresContext(args, dsn, connection, progress_stream)
        if args.command == "index" and args.migrate:
            return _postgres_index_migrate(context)

        require_current_schema(connection)
        if args.command == "index":
            if args.status:
                return _postgres_index_status(context)
            return _postgres_index_run(context)
        handlers: dict[str, Callable[[_PostgresContext], int]] = {
            "events": _postgres_events,
            "list": _postgres_list,
            "extract": _postgres_extract,
        }
        handler = handlers.get(args.command)
        if handler is not None:
            return handler(context)
        if args.command in {"context", "resolve"}:
            return _postgres_context_resolve(context)
        return _postgres_search(context)


def _since_days(days: int | None) -> str | None:
    if days is None:
        return None
    if days < 0:
        raise ValueError("days must be nonnegative")
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _parse_utc_bound(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid event timestamp bound: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event timestamp bounds must include a timezone")
    return parsed.astimezone(UTC)


def _resolve_project(args: argparse.Namespace) -> str:
    """Resolve the project path from args or cwd."""
    if hasattr(args, "project") and args.project is not None:
        return args.project
    return os.getcwd()  # noqa: PTH109  # patchable legacy seam


def _db_project_path(project_path: str) -> str:
    """Convert a real project path to the form stored in the database.

    The database stores project_path as decode_project_path(encode_project_path(path)),
    which is lossy for paths containing hyphens. Query filters must use this
    same form to match.
    """
    return decode_project_path(encode_project_path(project_path))


def _indexed_project_count(conn: sqlite3.Connection) -> int:
    """Number of distinct projects currently present in the index."""
    row = conn.execute("SELECT COUNT(DISTINCT project_path) FROM session").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _validate_project_dir(project_path: str) -> None:
    """Validate that the Claude Code projects directory exists for this project.

    Raises SystemExit if the encoded directory doesn't exist.
    """
    encoded = encode_project_path(project_path)
    projects_dir = get_claude_projects_dir()
    project_dir = projects_dir / encoded
    if not project_dir.is_dir():
        print(
            f"No Claude Code session data found for project: {project_path}\n"
            f"Expected directory: {project_dir}",
            file=sys.stderr,
        )
        sys.exit(1)


def _emit_search(
    args: argparse.Namespace,
    results: list[sqlite3.Row],
    *,
    scope: str,
    searched_project: str | None,
    project_count: int,
    empty_hint: str | None = None,
) -> None:
    """Render search results as JSON or human text (shared by both paths)."""
    if args.json:
        print(
            json_search_results(
                results,
                scope=scope,
                searched_project=searched_project,
                project_count=project_count,
            )
        )
    else:
        output = format_search_results(
            results,
            scope=scope,
            searched_project=searched_project,
            project_count=project_count,
        )
        if output:
            print(output)
        elif empty_hint:
            print(empty_hint, file=sys.stderr)


def _handle_search(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Search chat history, local-first with broaden-on-miss.

    Default: search the current project; if it has no hits, widen to every
    indexed project. ``--all`` searches everything up front; ``--project``
    narrows to one project and never broadens; running from a directory that
    is not a Claude project also searches everything.
    """
    explicit_project = getattr(args, "project", None) is not None
    search_all = getattr(args, "all", False)

    def _run(project: str | None) -> list[sqlite3.Row]:
        return search(
            conn, args.query, epoch=args.epoch, project=project, days=args.days
        )

    if search_all:
        results = _run(None)
        scope, searched_project = "all", None
        project_count = _indexed_project_count(conn)
    elif explicit_project:
        project_path = args.project
        _validate_project_dir(project_path)
        jit_reindex(conn, project_path)
        results = _run(_db_project_path(project_path))
        scope, searched_project, project_count = "local", project_path, 1
    else:
        project_path = os.getcwd()  # noqa: PTH109  # patchable legacy seam
        encoded = encode_project_path(project_path)
        if not (get_claude_projects_dir() / encoded).is_dir():
            # cwd is not a Claude project -> search everything
            results = _run(None)
            scope, searched_project = "all", None
            project_count = _indexed_project_count(conn)
        else:
            jit_reindex(conn, project_path)
            results = _run(_db_project_path(project_path))
            if results:
                scope, searched_project, project_count = "local", project_path, 1
            else:
                # broaden on miss
                results = _run(None)
                scope, searched_project = "widened", project_path
                project_count = _indexed_project_count(conn)

    empty_hint = None
    if scope in ("widened", "all"):
        empty_hint = (
            "No matches across any indexed project. "
            "Run `cc-search-chats index --all` to index all projects."
        )
    _emit_search(
        args,
        results,
        scope=scope,
        searched_project=searched_project,
        project_count=project_count,
        empty_hint=empty_hint,
    )
    return 0


def _handle_extract(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Extract a conversation by session ID."""
    project_path = _resolve_project(args)
    _validate_project_dir(project_path)

    jit_reindex(conn, project_path)

    session_id = args.session_id
    if session_id is None:
        # Auto-discover the most relevant session
        encoded = encode_project_path(project_path)
        projects_dir = get_claude_projects_dir()
        sessions = list_session_files(projects_dir, encoded)
        ranked = rank_sessions(sessions)

        if not ranked:
            print("No sessions found for this project.", file=sys.stderr)
            return 1

        session_id = ranked[0].session_id
        print(f"Auto-discovered session: {session_id}", file=sys.stderr)

    messages = extract_session(conn, session_id, epoch=args.epoch)

    # Get compact events for epoch markers
    compact_events = conn.execute(
        "SELECT uuid, session_id, epoch, timestamp, trigger, pre_tokens, summary_text "
        "FROM compact_event WHERE session_id = ? ORDER BY epoch",
        (session_id,),
    ).fetchall()

    if args.json:
        print(json_extract(messages, compact_events, session_id))
    else:
        output = format_extract(messages, compact_events, verbose=args.verbose)
        if output:
            print(output)
    return 0


def _handle_list(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """List sessions."""
    project_path = _resolve_project(args)
    _validate_project_dir(project_path)

    jit_reindex(conn, project_path)

    results = list_sessions(
        conn, project=_db_project_path(project_path), days=args.days
    )

    if args.json:
        print(json_session_list(results))
    else:
        output = format_session_list(results)
        if output:
            print(output)
    return 0


def _handle_index(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Build or rebuild the search index."""
    if args.all:
        counts = index_all_projects(conn)
        if args.json:
            print(json_index_all_result(counts))
        else:
            print(
                f"Indexed {counts['indexed']} sessions "
                f"({counts['skipped']} skipped) "
                f"across {counts['projects']} projects",
                file=sys.stderr,
            )
        return 0

    project_path = _resolve_project(args)
    _validate_project_dir(project_path)

    counts = reindex_project(conn, project_path)

    if args.json:
        print(json_index_result(counts, project_path))
    else:
        print(
            f"Indexed {counts['indexed']} sessions "
            f"({counts['skipped']} skipped) for {project_path}",
            file=sys.stderr,
        )
    return 0


def _handle_context(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Show context around a message UUID."""
    context_rows = extract_context(conn, args.uuid, depth=args.depth)

    # Split into target, before, and after
    target = None
    before = []
    after = []
    found_target = False

    for row in context_rows:
        if row["uuid"] == args.uuid:
            target = row
            found_target = True
        elif not found_target:
            before.append(row)
        else:
            after.append(row)

    if target is None:
        # Should not happen (extract_context raises ValueError for invalid UUID)
        print(f"Message not found in context: {args.uuid}", file=sys.stderr)
        return 1

    if args.json:
        print(json_context(target, before, after))
    else:
        output = format_context(target, before, after, verbose=args.verbose)
        if output:
            print(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands."""
    parser = _ArgumentParser(
        prog="cc-search-chats",
        description="Search and recover Claude Code and Codex chat history",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="output results as JSON",
    )

    # Create a parent parser for shared flags across all subcommands.
    common_parser = _ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="output results as JSON",
    )
    common_parser.add_argument(
        "--progress",
        choices=("auto", "ndjson", "human"),
        default="auto",
        help="stderr progress format (default: auto)",
    )

    _fmt = argparse.RawDescriptionHelpFormatter
    subparsers = parser.add_subparsers(dest="command", help="available commands")

    # search
    search_parser = subparsers.add_parser(
        "search",
        help="search chat history for a query",
        parents=[common_parser],
        formatter_class=_fmt,
        epilog=(
            "Examples:\n"
            '  cc-search-chats search "database migration" --semantic\n'
            '  cc-search-chats search "auth" --literal --provider codex\n'
            '  cc-search-chats search "file_path" --literal --tools\n'
            '  cc-search-chats search "sentinel" --literal --exhaustive --json'
        ),
    )
    search_parser.add_argument("query", type=str, help="search query string")
    search_parser.add_argument(
        "--project", type=str, default=None, help="project path to search"
    )
    search_parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="search every indexed project (skip local-first / broaden-on-miss)",
    )
    search_parser.add_argument(
        "--agents",
        action="store_true",
        default=False,
        help="include recognized agent sessions",
    )
    search_parser.add_argument(
        "--tools",
        action="store_true",
        default=False,
        help="include persisted tool name and input rows in literal search",
    )
    search_parser.add_argument(
        "--exhaustive",
        action="store_true",
        default=False,
        help="return every deterministic literal occurrence",
    )
    search_parser.add_argument(
        "--everything",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    search_parser.add_argument(
        "--epoch", type=int, default=None, help="epoch number to search within"
    )
    search_parser.add_argument(
        "--days", type=int, default=None, help="limit search to last N days"
    )
    search_parser.add_argument(
        "--provider", choices=("claude", "codex"), help="limit by provider"
    )
    search_parser.add_argument("--role", help="limit by conversational role")
    search_parser.add_argument(
        "--limit", type=int, default=20, help="maximum ranked results (default: 20)"
    )
    search_mode = search_parser.add_mutually_exclusive_group(required=True)
    search_mode.add_argument("--literal", action="store_true", help=_LITERAL_MODE_HELP)
    search_mode.add_argument(
        "--semantic", action="store_true", help=_SEMANTIC_MODE_HELP
    )
    search_parser.set_defaults(func=_handle_search)

    # extract
    extract_parser = subparsers.add_parser(
        "extract",
        help="extract a conversation by session ID",
        parents=[common_parser],
        formatter_class=_fmt,
        epilog=(
            "Examples:\n"
            "  cc-search-chats extract\n"
            "  cc-search-chats extract abc12345-6789-...\n"
            "  cc-search-chats extract --epoch 0 --json"
        ),
    )
    extract_parser.add_argument(
        "session_id",
        type=str,
        nargs="?",
        default=None,
        help="session ID to extract",
    )
    extract_parser.add_argument(
        "--epoch", type=int, default=None, help="epoch number to extract"
    )
    extract_parser.add_argument(
        "--project", type=str, default=None, help="project path"
    )
    extract_parser.add_argument(
        "--provider", choices=("claude", "codex"), help="limit by provider"
    )
    extract_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="show all messages including tool calls and empty content",
    )
    extract_parser.set_defaults(func=_handle_extract)

    # list
    list_parser = subparsers.add_parser(
        "list",
        help="list sessions",
        parents=[common_parser],
        formatter_class=_fmt,
        epilog=(
            "Examples:\n"
            "  cc-search-chats list\n"
            "  cc-search-chats list --days 30 --json\n"
            "  cc-search-chats list --project /path/to/project"
        ),
    )
    list_parser.add_argument(
        "--project", type=str, default=None, help="project path to list sessions for"
    )
    list_parser.add_argument(
        "--days", type=int, default=None, help="limit to last N days"
    )
    list_parser.add_argument(
        "--provider", choices=("claude", "codex"), help="limit by provider"
    )
    list_parser.set_defaults(func=_handle_list)

    events_parser = subparsers.add_parser(
        "events",
        help="export bounded canonical human-message events without content",
        parents=[common_parser],
    )
    events_parser.add_argument(
        "--from",
        dest="from_utc",
        required=True,
        help="inclusive ISO 8601 timestamp with timezone",
    )
    events_parser.add_argument(
        "--until",
        dest="until_utc",
        required=True,
        help="exclusive ISO 8601 timestamp with timezone",
    )
    events_parser.set_defaults(func=None)

    # index
    index_parser = subparsers.add_parser(
        "index",
        help="build or rebuild the search index",
        parents=[common_parser],
        formatter_class=_fmt,
        epilog=(
            "Examples:\n"
            "  cc-search-chats index\n"
            "  cc-search-chats index --status --json"
        ),
    )
    index_parser.add_argument(
        "--project", type=str, default=None, help="project path to index"
    )
    index_parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="index every project under ~/.claude/projects (incremental)",
    )
    index_parser.add_argument(
        "--force-retry",
        action="store_true",
        help="retry unchanged failed source observations during explicit maintenance",
    )
    index_mode = index_parser.add_mutually_exclusive_group()
    index_mode.add_argument(
        "--migrate",
        action="store_true",
        help="explicitly apply pending PostgreSQL schema migrations",
    )
    index_mode.add_argument(
        "--status",
        action="store_true",
        help="show semantic indexing checkpoint without doing work",
    )
    index_parser.set_defaults(func=_handle_index)

    # context
    context_parser = subparsers.add_parser(
        "context",
        help="show context around a message UUID",
        parents=[common_parser],
        formatter_class=_fmt,
        epilog=(
            "Examples:\n"
            "  cc-search-chats context abc12345-6789-... --depth 10\n"
            "  cc-search-chats context abc12345-6789-... --json"
        ),
    )
    context_parser.add_argument("uuid", type=str, help="message UUID or ccchat locator")
    context_parser.add_argument(
        "--depth",
        type=int,
        default=5,
        help="number of surrounding messages (default: 5)",
    )
    context_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="show all messages including tool calls and empty content",
    )
    context_parser.set_defaults(func=_handle_context)

    resolve_parser = subparsers.add_parser(
        "resolve",
        help="resolve an exact ccchat locator",
        parents=[common_parser],
    )
    resolve_parser.add_argument("uuid", nargs="?", help="exact ccchat locator")
    resolve_parser.add_argument(
        "--stdin",
        action="store_true",
        help="resolve newline-delimited locators in one database operation",
    )
    resolve_parser.add_argument(
        "--reference-only",
        action="store_true",
        help="return verified identity and source coordinates without message text",
    )
    resolve_parser.set_defaults(func=None)

    return parser


def _validate_search_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    request_started: float,
) -> None:
    if args.command != "search":
        return
    args.answer_deadline = (
        request_started + _SEARCH_DEADLINE_SECONDS - _SEARCH_RENDER_RESERVE_SECONDS
        if args.literal and not args.exhaustive
        else None
    )
    if args.everything:
        parser.error(
            "--everything was removed; use --literal --tools --exhaustive "
            "for complete lexical tool/prose matches. "
            "Reasoning and instructions remain excluded"
        )
    if args.tools and not args.literal:
        parser.error("--tools requires --literal")
    if args.exhaustive and not args.literal:
        parser.error("--exhaustive requires --literal")
    if not args.exhaustive and not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200 for ranked search")


def _uses_postgres(args: argparse.Namespace) -> bool:
    postgres_commands = {
        "index",
        "search",
        "list",
        "events",
        "extract",
        "context",
        "resolve",
    }
    return args.command in postgres_commands and "CC_SEARCH_DB_PATH" not in os.environ


def _postgres_dsn() -> str:
    standard_connection = any(
        key in os.environ
        for key in ("PGSERVICE", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER")
    )
    return "" if standard_connection else _DEFAULT_POSTGRES_DSN


def _postgres_read_scope(args: argparse.Namespace):
    if args.command != "search" or args.answer_deadline is None:
        return nullcontext()
    remaining = _remaining_search_seconds(args)
    if remaining <= 0:
        raise SearchDeadlineExceeded(
            "search deadline expired before PostgreSQL admission"
        )
    return read_deadline(max(1, int(remaining * 1000)))


def _postgres_admission_name(args: argparse.Namespace) -> str | None:
    if (args.command == "index" and args.status) or args.command == "search":
        return None
    if args.command == "index":
        return "index"
    return "read"


def _dispatch_postgres(
    args: argparse.Namespace,
    progress_stream: _ProgressStream,
) -> int:
    read_scope = _postgres_read_scope(args)
    _contain_semantic_index(args)
    admission_name = _postgres_admission_name(args)
    with read_scope:
        if admission_name is None:
            return _handle_postgres(args, _postgres_dsn(), progress_stream)
        with client_admission(admission_name):
            return _handle_postgres(args, _postgres_dsn(), progress_stream)


def _exit_with_error(
    args: argparse.Namespace,
    progress_stream: _ProgressStream,
    status: str,
    error: Mapping[str, object],
    exit_code: int,
    *,
    human_message: str | None = None,
) -> Never:
    envelope = _command_error_envelope(args, status, error)
    progress_stream.terminal(envelope)
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
    elif human_message is not None and not progress_stream.ndjson:
        print(human_message, file=sys.stderr)
    sys.exit(exit_code)


def _postgres_error_detail(error: BaseException) -> str:
    if isinstance(error, psycopg.Error) and error.diag.message_primary:
        return error.diag.message_primary
    return str(error)


def _exit_postgres_operation_error(
    args: argparse.Namespace,
    progress_stream: _ProgressStream,
    error: OSError | psycopg.Error | RuntimeError | ValueError,
) -> Never:
    detail = _postgres_error_detail(error)
    if (
        args.command == "search"
        and args.answer_deadline is not None
        and isinstance(
            error,
            (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled),
        )
    ):
        _exit_with_error(
            args,
            progress_stream,
            "deadline_exceeded",
            {
                "code": "search_deadline_exceeded",
                "phase": "retrieve",
                "message": detail,
            },
            7,
        )
    _exit_with_error(
        args,
        progress_stream,
        "internal_failure",
        {
            "code": "postgresql_operation_failed",
            "phase": "done",
            "message": detail,
        },
        1,
        human_message=f"PostgreSQL operation failed: {detail}",
    )


def _run_postgres_cli(args: argparse.Namespace) -> Never:
    progress_stream = _ProgressStream(args)
    try:
        sys.exit(_dispatch_postgres(args, progress_stream))
    except SystemdScopeUnavailable as error:
        _exit_with_error(
            args,
            progress_stream,
            "containment_unavailable",
            {
                "code": error.code,
                "phase": error.phase,
                "message": str(error),
                "remedy": _CONTAINMENT_REMEDY,
                "systemd_detail": error.systemd_detail,
            },
            9,
            human_message=f"{error}\n{_CONTAINMENT_REMEDY}",
        )
    except (ReadDeadlineExceeded, SearchDeadlineExceeded) as error:
        _exit_with_error(
            args,
            progress_stream,
            "deadline_exceeded",
            {
                "code": "search_deadline_exceeded",
                "phase": "retrieve",
                "message": str(error),
            },
            7,
        )
    except MaintenanceRequired as error:
        _exit_with_error(
            args,
            progress_stream,
            "maintenance_required",
            {
                "code": "maintenance_required",
                "phase": "migration",
                "message": str(error),
                "pending_versions": [migration.version for migration in error.pending],
            },
            6,
        )
    except ModelUnavailable as error:
        _exit_with_error(
            args,
            progress_stream,
            "semantic_unavailable",
            {
                "code": error.code,
                "phase": error.phase,
                "message": str(error),
                "available_vram_bytes": error.available_vram_bytes,
                "required_vram_bytes": error.required_vram_bytes,
                "total_vram_bytes": error.total_vram_bytes,
                "literal_requirement": (
                    "Literal search is required for complete current results"
                ),
                "literal_command": _literal_fallback_command(args),
            },
            8,
            human_message=(
                f"Semantic unavailable [{error.code}] during {error.phase}: {error}\n"
                "Semantic freshness: unavailable.\n"
                "Literal search is required for complete current results.\n"
                f"Run: {_literal_fallback_command(args)}"
            ),
        )
    except (OSError, psycopg.Error, RuntimeError, ValueError) as error:
        _exit_postgres_operation_error(args, progress_stream, error)


def _ensure_legacy_available(args: argparse.Namespace) -> None:
    if args.command == "resolve":
        print("PostgreSQL connection is required for resolve", file=sys.stderr)
        sys.exit(1)
    try:
        ensure_fts5()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


def _report_project_rebuild_error(
    error: ProjectRebuildError,
    db_path: Path | None,
    connection: sqlite3.Connection | None,
) -> sqlite3.Connection | None:
    if db_path is None:
        print(str(error), file=sys.stderr)
    elif connection is not None and is_database_damage(error.cause):
        damaged_connection = connection
        connection = None
        print(
            discard_damaged_database(
                damaged_connection,
                db_path,
                format_exception_detail(error.cause),
                "SQLite stopped the operation with an explicit damage result.",
            ),
            file=sys.stderr,
        )
    else:
        diagnostic = format_index_error(db_path, error.cause, connection)
        print(
            f"Project index rebuild failed for {error.project_path}. "
            f"{diagnostic} The transaction was rolled back; "
            "prior index contents remain intact.",
            file=sys.stderr,
        )
    return connection


def _report_legacy_database_error(
    error: OSError | sqlite3.DatabaseError,
    db_path: Path | None,
    connection: sqlite3.Connection | None,
) -> sqlite3.Connection | None:
    if db_path is None:
        print(f"{error.__class__.__name__}: {error}", file=sys.stderr)
    elif connection is not None and is_database_damage(error):
        damaged_connection = connection
        connection = None
        print(
            discard_damaged_database(
                damaged_connection,
                db_path,
                format_exception_detail(error),
                "SQLite stopped the operation with an explicit damage result.",
            ),
            file=sys.stderr,
        )
    else:
        print(format_index_error(db_path, error, connection), file=sys.stderr)
    return connection


def _run_legacy_cli(args: argparse.Namespace) -> Never:
    _ensure_legacy_available(args)
    connection: sqlite3.Connection | None = None
    db_path: Path | None = None
    try:
        db_path = get_db_path()
        connection = open_db(db_path)
        exit_code = args.func(args, connection)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        exit_code = 1
    except ProjectRebuildError as error:
        connection = _report_project_rebuild_error(error, db_path, connection)
        exit_code = 1
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        exit_code = 1
    except sqlite3.ProgrammingError:
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        connection = _report_legacy_database_error(error, db_path, connection)
        exit_code = 1
    finally:
        if connection is not None:
            close_db(connection)
    sys.exit(exit_code)


def main(*, request_started: float | None = None) -> None:
    """Entry point for cc-search-chats CLI."""
    if request_started is None:
        request_started = monotonic()
    parser = build_parser()
    args = parser.parse_args()
    args.request_started = request_started
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    _validate_search_args(args, parser, request_started)
    if _uses_postgres(args):
        _run_postgres_cli(args)
    _run_legacy_cli(args)
