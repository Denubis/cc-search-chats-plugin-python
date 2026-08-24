"""CLI entry point for cc-search-chats.

Imperative Shell — parses arguments and orchestrates storage/output layers.
"""

import argparse
import json
import os
import shlex
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import cast

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
    embed_query,
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
    RefreshProgress,
    StoredAlias,
    StoredMessage,
    exhaustive_search_page,
    index_embeddings,
    refresh_native_sources,
    resolve_exact_messages,
    search_messages,
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
from cc_search_chats.storage.postgresql.guardrails import acquire_index_session
from cc_search_chats.storage.postgresql.semantic import hybrid_search

_DEFAULT_POSTGRES_DSN = "service=cc_search_chats"


class _ProgressStream:
    """Render one ordered progress stream without contaminating stdout."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._started = monotonic()
        self._sequence = 0
        self._terminal = False
        self._emit_lock = Lock()
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
        fts_revision: int | None = None,
        semantic_revision: int | None = None,
        source_watermark: object = None,
        warning: object = None,
        error: object = None,
        coverage: object = None,
        refresh: object = None,
        semantic: object = None,
    ) -> None:
        with self._emit_lock:
            self._sequence += 1
            value = {
                "schema_version": 2,
                "sequence": self._sequence,
                "event": event,
                "run_id": run_id,
                "phase": phase,
                "state": state,
                "elapsed_ms": round((monotonic() - self._started) * 1000),
                "completed_units": completed_units,
                "total_units": total_units,
                "owner": owner,
                "fts_revision": fts_revision,
                "semantic_revision": semantic_revision,
                "source_watermark": source_watermark,
                "warning": warning,
                "error": error,
                "coverage": coverage,
                "refresh": refresh,
                "semantic": semantic,
            }
            if self._ndjson:
                print(
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    file=sys.stderr,
                )
            else:
                units = (
                    f" {completed_units}/{total_units}"
                    if completed_units is not None and total_units is not None
                    else ""
                )
                print(f"{phase}: {state}{units}", file=sys.stderr)

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
        refresh = cast(dict[str, object], envelope["refresh"])
        semantic = cast(dict[str, object], envelope["semantic"])
        coverage = cast(dict[str, object], envelope["coverage"])
        self.emit(
            "done",
            str(envelope["status"]),
            event="terminal",
            run_id=cast(int | None, refresh.get("run_id")),
            fts_revision=cast(int | None, refresh.get("fts_revision")),
            semantic_revision=cast(
                int | None,
                semantic.get("semantic_revision"),
            ),
            source_watermark=coverage.get("source_watermarks"),
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
    error: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "command": command,
        "status": status,
        "coverage": {
            "configured_root_count": 0,
            "resolved_root_count": 0,
            "roots": [],
            "repositories": [],
            "discovered_files": 0,
            "read_files": 0,
            "removed_files": 0,
            "indexed_files": 0,
            "skipped_files": 0,
            "excluded_files": 0,
            "unreadable_files": 0,
            "unknown_sessions": 0,
            "unrecognized_conversation_records": 0,
            "source_watermarks": [],
            "completeness": "unknown",
        },
        "refresh": {
            "fts_revision": None,
            "run_id": None,
            "state": "unavailable",
            "failed_sources": 0,
            "advanced_sources": 0,
            "pending_bytes": 0,
        },
        "semantic": {
            "semantic_revision": None,
            "fts_revision": None,
            "state": "unavailable",
            "profile_id": None,
            "completed_units": 0,
            "total_units": 0,
            "fresh": False,
        },
        "warnings": [],
        "error": error,
    }


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


def _postgres_envelope(
    connection: psycopg.Connection,
    command: str,
    *,
    status: str = "complete",
    **payload: object,
) -> dict[str, object]:
    roots = [
        {
            "provider": provider,
            "resolved_path": resolved_path,
            "discovered_files": discovered,
            "indexed_files": indexed,
            "excluded_files": excluded,
            "pending_files": pending,
        }
        for provider, resolved_path, discovered, indexed, excluded, pending in connection.execute(
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
    refresh_row = next(
        connection.execute(
            """
            SELECT state.current_revision_id, run.run_id, run.status,
                   run.source_count, run.changed_source_count,
                   run.failed_source_count, run.read_source_count,
                   run.removed_source_count, run.advanced_source_count,
                   run.diagnostics,
                   COALESCE((
                       SELECT sum(source.pending_bytes)
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
    repositories = [
        value
        for (value,) in connection.execute(
            """
            SELECT DISTINCT COALESCE(repository, cwd) AS project
            FROM cc_search_chats.message_current
            WHERE COALESCE(repository, cwd) IS NOT NULL
            ORDER BY project
            """
        )
    ]
    unknown_sessions = next(
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
    source_watermarks = [
        {
            "provider": provider,
            "resolved_root": resolved_root,
            "source_file_relative": source_file_relative,
            "observed_size": observed_size,
            "complete_byte_offset": complete_byte_offset,
            "next_record_ordinal": next_record_ordinal,
            "pending_bytes": pending_bytes,
        }
        for (
            provider,
            resolved_root,
            source_file_relative,
            observed_size,
            complete_byte_offset,
            next_record_ordinal,
            pending_bytes,
        ) in connection.execute(
            """
            SELECT root.provider, root.resolved_path,
                   source.source_file_relative, source.observed_size,
                   source.complete_byte_offset, source.next_record_ordinal,
                   source.pending_bytes
            FROM cc_search_chats.source_file_current AS source
            JOIN cc_search_chats.source_root_current AS root
              USING (source_root_id)
            ORDER BY root.configured_order, source.source_file_relative
            """
        )
    ]
    semantic_row = next(
        connection.execute(
            """
            SELECT semantic.semantic_revision_id,
                   semantic.corpus_revision_id,
                   semantic.status,
                   semantic.profile_id,
                   semantic.completed_units,
                   semantic.total_units,
                   semantic.corpus_revision_id = corpus.current_revision_id
                       AND semantic.status = 'complete' AS fresh
            FROM cc_search_chats.corpus_state AS corpus
            LEFT JOIN cc_search_chats.semantic_state AS state
              ON state.singleton
            LEFT JOIN cc_search_chats.semantic_revision AS semantic
              ON semantic.semantic_revision_id = state.current_semantic_revision_id
            WHERE corpus.singleton
            """
        )
    )
    diagnostics = refresh_row[9] or []
    current_discovered = sum(int(root["discovered_files"]) for root in roots)
    discovered_files = (
        int(refresh_row[3]) if refresh_row[3] is not None else current_discovered
    )
    changed_files = int(refresh_row[4] or 0)
    failed_files = int(refresh_row[5] or 0)
    read_files = int(refresh_row[6] or 0)
    removed_files = int(refresh_row[7] or 0)
    advanced_files = int(refresh_row[8] or 0)
    pending_bytes = int(refresh_row[10] or 0)
    unrecognized_records = sum(
        1
        for diagnostic in diagnostics
        if isinstance(diagnostic, dict)
        and "unsupported " in str(diagnostic.get("detail", "")).lower()
    )
    coverage = {
        "configured_root_count": len(roots),
        "resolved_root_count": len(roots),
        "roots": roots,
        "repositories": repositories,
        "discovered_files": discovered_files,
        "read_files": read_files,
        "removed_files": removed_files,
        "indexed_files": sum(int(root["indexed_files"]) for root in roots),
        "skipped_files": max(0, discovered_files - changed_files),
        "excluded_files": sum(int(root["excluded_files"]) for root in roots),
        "unreadable_files": failed_files,
        "unknown_sessions": unknown_sessions,
        "unrecognized_conversation_records": unrecognized_records,
        "source_watermarks": source_watermarks,
        "completeness": (
            "partial"
            if refresh_row[2] in {"partial", "failed"} or failed_files or pending_bytes
            else "complete"
        ),
    }
    refresh = {
        "fts_revision": refresh_row[0],
        "run_id": refresh_row[1],
        "state": refresh_row[2] or "unchanged",
        "failed_sources": failed_files,
        "advanced_sources": advanced_files,
        "pending_bytes": pending_bytes,
    }
    semantic = {
        "semantic_revision": semantic_row[0],
        "fts_revision": semantic_row[1],
        "state": semantic_row[2] or "unavailable",
        "profile_id": semantic_row[3],
        "completed_units": semantic_row[4] or 0,
        "total_units": semantic_row[5] or 0,
        "fresh": semantic_row[6] is True,
    }
    return {
        "schema_version": 2,
        "command": command,
        "status": status,
        "coverage": coverage,
        "refresh": refresh,
        "semantic": semantic,
        "warnings": diagnostics,
        **payload,
    }


def _eta(seconds: float) -> str:
    minutes, seconds = divmod(max(0, round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


def _contain_semantic_index(args: argparse.Namespace) -> None:
    """Re-enter indexing in a host-safe, low-priority systemd scope."""
    if (
        args.command != "index"
        or args.status
        or os.environ.get("CC_SEARCH_CONTAINED") == "1"
        or "/run-r" in Path("/proc/self/cgroup").read_text(encoding="utf-8")
    ):
        return
    command = [
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
        "ionice",
        "--class=idle",
        *sys.argv,
    ]
    try:
        os.execvp(command[0], command)
    except OSError as error:
        raise RuntimeError(
            "indexing requires a working systemd user session for resource containment"
        ) from error


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


def _handle_postgres(
    args: argparse.Namespace,
    dsn: str,
    progress_stream: _ProgressStream,
) -> int:
    """Run the migrated index/search surface against PostgreSQL."""
    with psycopg.connect(dsn, autocommit=True) as connection:

        def finish(
            command: str,
            *,
            status: str = "complete",
            **payload: object,
        ) -> dict[str, object]:
            envelope = _postgres_envelope(
                connection,
                command,
                status=status,
                **payload,
            )
            progress_stream.terminal(envelope)
            return envelope

        if args.command == "index":
            if args.status:
                status = next(
                    connection.execute(
                        """
                        SELECT cs.current_revision_id, sr.semantic_revision_id,
                               COALESCE(sr.completed, 0),
                               COALESCE(sr.total, 0),
                               COALESCE(sr.selected, false)
                        FROM cc_search_chats.corpus_state AS cs
                        LEFT JOIN LATERAL (
                            SELECT r.semantic_revision_id,
                                   r.completed_units AS completed,
                                   r.total_units AS total,
                                   ss.current_semantic_revision_id =
                                       r.semantic_revision_id AS selected
                            FROM cc_search_chats.semantic_revision AS r
                            LEFT JOIN cc_search_chats.semantic_state AS ss
                              ON ss.singleton
                            WHERE r.corpus_revision_id = cs.current_revision_id
                            ORDER BY r.semantic_revision_id DESC
                            LIMIT 1
                        ) AS sr ON true
                        WHERE cs.singleton
                        """
                    ),
                    None,
                )
                revision, semantic_revision, completed, total, selected = status or (
                    None,
                    None,
                    0,
                    0,
                    False,
                )
                envelope = finish(
                    "index",
                    revision_id=revision,
                    semantic_revision_id=semantic_revision,
                    completed=completed,
                    total=total,
                    selected=bool(selected),
                )
                if args.json:
                    print(json.dumps(envelope, sort_keys=True))
                else:
                    print(f"Semantic index: {completed}/{total} passages")
                return 0

            acquire_index_session(connection)

            scan_complete = False
            parse_seen = False
            refresh_heartbeat: (
                Callable[[str, int | None, int | None, int | None], None] | None
            ) = None

            def progress(event: RefreshProgress) -> None:
                nonlocal parse_seen, scan_complete
                if refresh_heartbeat is not None:
                    refresh_heartbeat(
                        event.phase,
                        event.run_id,
                        event.completed_units,
                        event.total_units,
                    )
                if event.phase == "parse" and not scan_complete:
                    progress_stream.emit("scan", "complete")
                    scan_complete = True
                parse_seen = parse_seen or event.phase == "parse"
                progress_stream.emit(
                    event.phase,
                    event.state,
                    run_id=event.run_id,
                    completed_units=event.completed_units,
                    total_units=event.total_units,
                    owner=event.owner_pid,
                )

            if args.semantic_only:
                revision_id, source_count, message_count = next(
                    connection.execute(
                        """
                        SELECT s.current_revision_id, 0, count(*)
                        FROM cc_search_chats.corpus_state AS s
                        CROSS JOIN cc_search_chats.message_current AS m
                        WHERE s.singleton
                        GROUP BY s.current_revision_id
                        """
                    )
                )
            else:
                progress_stream.emit("scan", "running")
                with progress_stream.heartbeat("scan") as heartbeat_update:
                    refresh_heartbeat = heartbeat_update
                    result = refresh_native_sources(
                        connection,
                        source_roots=configured_source_roots(),
                        progress=progress,
                    )
                revision_id = result.revision_id
                source_count = result.source_count
                message_count = result.message_count
                if not scan_complete:
                    progress_stream.emit("scan", "complete")
                    scan_complete = True
                if not parse_seen:
                    progress_stream.emit(
                        "parse",
                        "complete",
                        completed_units=result.read_source_count,
                        total_units=result.changed_source_count,
                    )
                progress_stream.emit(
                    "fts_commit",
                    "complete",
                    completed_units=result.changed_source_count
                    - result.failed_source_count,
                    total_units=result.changed_source_count,
                    fts_revision=result.revision_id,
                )
            vector_count = 0
            if not args.literal_only:
                embedding_heartbeat: (
                    Callable[[str, int | None, int | None, int | None], None] | None
                ) = None

                def model_progress(phase: str, state: str) -> None:
                    if embedding_heartbeat is not None:
                        embedding_heartbeat(phase, None, None, None)
                    progress_stream.emit(phase, state)
                    if (
                        embedding_heartbeat is not None
                        and phase == "model_load"
                        and state == "complete"
                    ):
                        embedding_heartbeat("semantic_embed", None, None, None)

                def passage_embed(texts):
                    return embed_passages(texts, progress=model_progress)

                def embedding_progress(completed: int, total: int) -> None:
                    progress_stream.emit(
                        "semantic_embed",
                        "running" if completed < total else "complete",
                        completed_units=completed,
                        total_units=total,
                    )

                progress_stream.emit("semantic_embed", "running")
                with progress_stream.heartbeat("semantic_embed") as heartbeat_update:
                    embedding_heartbeat = heartbeat_update
                    vector_count = index_embeddings(
                        connection,
                        passage_embed,
                        chunker=chunk_passages,
                        progress=embedding_progress,
                    )
                progress_stream.emit(
                    "semantic_commit",
                    "complete",
                    completed_units=vector_count,
                    total_units=vector_count,
                )
            envelope = finish(
                "index",
                revision_id=revision_id,
                sources=source_count,
                messages=message_count,
                embeddings=vector_count,
            )
            if args.json:
                print(json.dumps(envelope, sort_keys=True))
            else:
                print(
                    f"Indexed {message_count} messages from "
                    f"{source_count} sources into revision {revision_id}",
                    file=sys.stderr,
                )
            return 0

        if args.command == "list":
            sessions = pg_list_sessions(
                connection,
                provider=args.provider,
                project=args.project,
                since=_since_days(args.days),
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
            envelope = finish("list", sessions=values)
            if args.json:
                print(json.dumps(envelope))
            else:
                for value in values:
                    print(
                        f"{value['provider']}:{value['session_id']} "
                        f"({value['session_kind']}, {value['message_count']} messages)"
                    )
            return 0

        if args.command == "extract":
            session_id = args.session_id
            provider = args.provider
            if session_id is None:
                sessions = pg_list_sessions(
                    connection, provider=args.provider, project=args.project
                )
                if not sessions:
                    raise ValueError("no matching sessions")
                session_id = sessions[0].source_session_id
                provider = sessions[0].provider
            messages = pg_extract_session(
                connection,
                session_id,
                provider=provider,
                epoch=args.epoch,
            )
            providers = sorted({message.provider for message in messages})
            if (
                args.session_id is not None
                and args.provider is None
                and len(providers) > 1
            ):
                matches = [
                    {
                        "provider": candidate,
                        "source_session_id": session_id,
                    }
                    for candidate in providers
                ]
                envelope = finish(
                    "extract",
                    status="multiple_matches",
                    matches=matches,
                    messages=[],
                )
                if args.json:
                    print(json.dumps(envelope))
                else:
                    print(
                        "Session ID matches multiple providers; pass --provider",
                        file=sys.stderr,
                    )
                return 3
            values = [_message_json(message) for message in messages]
            envelope = finish(
                "extract",
                status="complete" if messages else "no_match",
                messages=values,
            )
            if args.json:
                print(json.dumps(envelope))
            else:
                for value in messages:
                    print(f"[{value.timestamp}] {value.role}:\n{value.text}")
            return 0 if messages else 3

        if args.command in {"context", "resolve"}:
            if args.command == "resolve" and args.stdin:
                if args.uuid is not None:
                    print(
                        "resolve accepts a locator or --stdin, not both",
                        file=sys.stderr,
                    )
                    return 2
                locators = tuple(line.strip() for line in sys.stdin if line.strip())
                if not locators:
                    print(
                        "resolve --stdin requires at least one locator", file=sys.stderr
                    )
                    return 2
                resolutions = resolve_exact_messages(
                    connection,
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
                                reference_only=args.reference_only,
                            )
                            for message in resolution.messages
                        ],
                        "detail": resolution.detail,
                    }
                    for resolution in resolutions
                ]
                statuses = {resolution.status for resolution in resolutions}
                overall_status = (
                    next(iter(statuses)).value if len(statuses) == 1 else "partial"
                )
                envelope = finish(
                    "resolve",
                    status=overall_status,
                    resolutions=values,
                )
                if args.json:
                    print(json.dumps(envelope))
                else:
                    for value in values:
                        print(f"{value['status']}\t{value['locator']}")
                if statuses == {ResolutionStatus.RESOLVED}:
                    return 0
                if ResolutionStatus.MALFORMED_LOCATOR in statuses:
                    return 2
                return 3
            exact = resolve_exact_messages(
                connection,
                (args.uuid,),
                source_roots=configured_source_roots(),
            )[0]
            messages = (
                pg_context_messages(connection, args.uuid, depth=args.depth)
                if args.command == "context"
                and exact.status is ResolutionStatus.RESOLVED
                else exact.messages
                if args.command == "resolve"
                else ()
            )
            values = [
                _message_json(
                    message,
                    reference_only=(args.command == "resolve" and args.reference_only),
                )
                for message in messages
            ]
            envelope = finish(
                args.command,
                status=exact.status.value,
                detail=exact.detail,
                messages=values,
            )
            if args.json:
                print(json.dumps(envelope))
            else:
                for value in messages:
                    print(f"[{value.timestamp}] {value.role}:\n{value.text}")
            if exact.status is ResolutionStatus.RESOLVED:
                return 0
            if exact.status is ResolutionStatus.MALFORMED_LOCATOR:
                return 2
            return 3

        acquire_index_session(connection)
        search_scan_complete = False
        search_parse_seen = False
        search_refresh_heartbeat: (
            Callable[[str, int | None, int | None, int | None], None] | None
        ) = None

        def search_refresh_progress(event: RefreshProgress) -> None:
            nonlocal search_parse_seen, search_scan_complete
            if search_refresh_heartbeat is not None:
                search_refresh_heartbeat(
                    event.phase,
                    event.run_id,
                    event.completed_units,
                    event.total_units,
                )
            if event.phase == "parse" and not search_scan_complete:
                progress_stream.emit("scan", "complete")
                search_scan_complete = True
            search_parse_seen = search_parse_seen or event.phase == "parse"
            progress_stream.emit(
                event.phase,
                event.state,
                run_id=event.run_id,
                completed_units=event.completed_units,
                total_units=event.total_units,
                owner=event.owner_pid,
            )

        progress_stream.emit("scan", "running")
        with progress_stream.heartbeat("scan") as heartbeat_update:
            search_refresh_heartbeat = heartbeat_update
            refresh_result = refresh_native_sources(
                connection,
                source_roots=configured_source_roots(),
                progress=search_refresh_progress,
            )
        if not search_scan_complete:
            progress_stream.emit("scan", "complete")
        if not search_parse_seen:
            progress_stream.emit(
                "parse",
                "complete",
                completed_units=refresh_result.read_source_count,
                total_units=refresh_result.changed_source_count,
            )
        progress_stream.emit(
            "fts_commit",
            "complete",
            completed_units=refresh_result.changed_source_count
            - refresh_result.failed_source_count,
            total_units=refresh_result.changed_source_count,
            fts_revision=refresh_result.revision_id,
        )
        project = args.project
        hybrid_rankings = {}
        if args.exhaustive:
            progress_stream.emit("retrieve", "running")
            exhaustive_hits = []
            cursor = None
            while True:
                page = exhaustive_search_page(
                    connection,
                    args.query,
                    page_size=500,
                    after=cursor,
                    provider=args.provider,
                    role=args.role,
                    project=project,
                    since=_since_days(args.days),
                    epoch=args.epoch,
                    include_agents=args.agents,
                    include_tools=args.tools,
                )
                exhaustive_hits.extend(page.hits)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
            hits = tuple(exhaustive_hits)
        elif args.literal:
            progress_stream.emit("retrieve", "running")
            hits = search_messages(
                connection,
                args.query,
                limit=args.limit,
                provider=args.provider,
                role=args.role,
                project=project,
                since=_since_days(args.days),
                epoch=args.epoch,
                include_agents=args.agents,
                include_tools=args.tools,
            )
        else:
            active_search_heartbeat: (
                Callable[[str, int | None, int | None, int | None], None] | None
            ) = None
            resume_search_phase = "semantic_embed"

            def search_model_progress(phase: str, state: str) -> None:
                if active_search_heartbeat is not None:
                    active_search_heartbeat(phase, None, None, None)
                progress_stream.emit(phase, state)
                if (
                    active_search_heartbeat is not None
                    and phase == "model_load"
                    and state == "complete"
                ):
                    active_search_heartbeat(resume_search_phase, None, None, None)

            def search_passage_embed(texts):
                return embed_passages(texts, progress=search_model_progress)

            def search_embedding_progress(completed: int, total: int) -> None:
                progress_stream.emit(
                    "semantic_embed",
                    "running" if completed < total else "complete",
                    completed_units=completed,
                    total_units=total,
                )

            progress_stream.emit("semantic_embed", "running")
            with progress_stream.heartbeat("semantic_embed") as heartbeat_update:
                active_search_heartbeat = heartbeat_update
                embedded_count = index_embeddings(
                    connection,
                    search_passage_embed,
                    chunker=chunk_passages,
                    progress=search_embedding_progress,
                )
            progress_stream.emit(
                "semantic_commit",
                "complete",
                completed_units=embedded_count,
                total_units=embedded_count,
            )
            progress_stream.emit("query_embed", "running")
            resume_search_phase = "query_embed"
            with progress_stream.heartbeat("query_embed") as heartbeat_update:
                active_search_heartbeat = heartbeat_update
                query_embedding = embed_query(
                    args.query,
                    progress=search_model_progress,
                )
            progress_stream.emit("query_embed", "complete")
            progress_stream.emit("retrieve", "running")
            hybrid_hits = hybrid_search(
                connection,
                args.query,
                query_embedding,
                limit=args.limit,
                provider=args.provider,
                role=args.role,
                project=project,
                since=_since_days(args.days),
                epoch=args.epoch,
                include_agents=args.agents,
            )
            hits = tuple(value.message for value in hybrid_hits)
            hybrid_rankings = {
                value.message.canonical_locator: value for value in hybrid_hits
            }
        progress_stream.emit(
            "retrieve",
            "complete",
            completed_units=len(hits),
            total_units=len(hits),
        )
        resolutions = pg_resolve_messages(
            connection,
            tuple(hit.canonical_locator for hit in hits),
        )
        results = []
        for hit, resolution in zip(hits, resolutions, strict=True):
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
            hybrid_ranking = hybrid_rankings.get(hit.canonical_locator)
            ranking = (
                {
                    "method": "rrf",
                    "score": {
                        "numerator": hybrid_ranking.score.numerator,
                        "denominator": hybrid_ranking.score.denominator,
                    },
                    "rank_constant": hybrid_ranking.rank_constant,
                    "component_depth": hybrid_ranking.component_depth,
                    "literal_rank": hybrid_ranking.literal_rank,
                    "semantic_rank": hybrid_ranking.semantic_rank,
                    "literal_score": hybrid_ranking.literal_score,
                    "semantic_score": hybrid_ranking.semantic_score,
                    "semantic_chunk_ordinal": (hybrid_ranking.semantic_chunk_ordinal),
                    "tie_breaker": "canonical_locator",
                }
                if hybrid_ranking is not None
                else {
                    "method": "fts",
                    "score": hit.rank,
                    "tie_breaker": (
                        "canonical_locator,content_class,record_ordinal,digest"
                        if args.exhaustive
                        else "rank,provider,session,logical_message"
                    ),
                }
            )
            results.append(
                {
                    "identity": _identity_json(identity_message),
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
                    "score": (
                        float(hybrid_ranking.score)
                        if hybrid_ranking is not None
                        else hit.rank
                    ),
                    "ranking": ranking,
                }
            )
        envelope = finish(
            "search",
            exhaustive=args.exhaustive,
            result_limit=None if args.exhaustive else args.limit,
            results=results,
        )
        if args.json:
            print(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
        else:
            for result in results:
                print(
                    f"[{result['timestamp']}] {result['provider']}:"
                    f"{result['session_id']} ({result['role']})\n"
                    f"  {result['text']}\n  {result['locator']}"
                )
        return 0


def _since_days(days: int | None) -> str | None:
    if days is None:
        return None
    if days < 0:
        raise ValueError("days must be nonnegative")
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _resolve_project(args: argparse.Namespace) -> str:
    """Resolve the project path from args or cwd."""
    if hasattr(args, "project") and args.project is not None:
        return args.project
    return os.getcwd()


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
        project_path = os.getcwd()
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
    parser = argparse.ArgumentParser(
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
    common_parser = argparse.ArgumentParser(add_help=False)
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
            '  cc-search-chats search "database migration"\n'
            '  cc-search-chats search "auth" --literal --provider codex\n'
            '  cc-search-chats search "tool output" --literal --tools\n'
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
        help="include tool name, input, and output rows in literal search",
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
    search_parser.add_argument(
        "--literal", action="store_true", help="use PostgreSQL FTS without the model"
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

    # index
    index_parser = subparsers.add_parser(
        "index",
        help="build or rebuild the search index",
        parents=[common_parser],
        formatter_class=_fmt,
        epilog=(
            "Examples:\n"
            "  cc-search-chats index\n"
            "  cc-search-chats index --literal-only\n"
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
    index_mode = index_parser.add_mutually_exclusive_group()
    index_mode.add_argument(
        "--literal-only",
        action="store_true",
        help="skip local semantic embedding generation",
    )
    index_mode.add_argument(
        "--semantic-only",
        action="store_true",
        help="embed the selected corpus without refreshing native sources",
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


def main() -> None:
    """Entry point for cc-search-chats CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "search":
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

    postgres_commands = {"index", "search", "list", "extract", "context", "resolve"}
    postgres = (
        args.command in postgres_commands and "CC_SEARCH_DB_PATH" not in os.environ
    )
    standard_connection = any(
        key in os.environ
        for key in ("PGSERVICE", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER")
    )
    if postgres:
        progress_stream = _ProgressStream(args)
        try:
            _contain_semantic_index(args)
            admission_name = (
                None
                if args.command == "index" and args.status
                else "index"
                if args.command in {"index", "search"}
                else "read"
            )
            if admission_name is None:
                exit_code = _handle_postgres(
                    args,
                    "" if standard_connection else _DEFAULT_POSTGRES_DSN,
                    progress_stream,
                )
            else:
                with client_admission(admission_name):
                    exit_code = _handle_postgres(
                        args,
                        "" if standard_connection else _DEFAULT_POSTGRES_DSN,
                        progress_stream,
                    )
            sys.exit(exit_code)
        except ModelUnavailable as exc:
            error = {
                "code": exc.code,
                "phase": exc.phase,
                "message": str(exc),
                "available_vram_bytes": exc.available_vram_bytes,
                "required_vram_bytes": exc.required_vram_bytes,
                "total_vram_bytes": exc.total_vram_bytes,
                "literal_requirement": (
                    "Literal search is required for complete current results"
                ),
                "literal_command": _literal_fallback_command(args),
            }
            envelope = _error_envelope(
                args.command,
                "semantic_unavailable",
                error,
            )
            progress_stream.terminal(envelope)
            if args.json:
                print(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
            elif not progress_stream.ndjson:
                print(
                    f"Semantic unavailable [{exc.code}] during {exc.phase}: {exc}\n"
                    "Semantic freshness: unavailable.\n"
                    "Literal search is required for complete current results.\n"
                    f"Run: {_literal_fallback_command(args)}",
                    file=sys.stderr,
                )
            sys.exit(8)
        except (OSError, psycopg.Error, RuntimeError, ValueError) as exc:
            detail = (
                exc.diag.message_primary
                if isinstance(exc, psycopg.Error) and exc.diag.message_primary
                else str(exc)
            )
            error = {
                "code": "postgresql_operation_failed",
                "phase": "done",
                "message": detail,
            }
            envelope = _error_envelope(args.command, "internal_failure", error)
            progress_stream.terminal(envelope)
            if args.json:
                print(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
            elif not progress_stream.ndjson:
                print(
                    f"PostgreSQL operation failed: {detail}",
                    file=sys.stderr,
                )
            sys.exit(1)
    if args.command == "resolve":
        print("PostgreSQL connection is required for resolve", file=sys.stderr)
        sys.exit(1)

    # Check FTS5 availability before opening the database.
    try:
        ensure_fts5()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    conn: sqlite3.Connection | None = None
    db_path = None
    try:
        db_path = get_db_path()
        conn = open_db(db_path)
        exit_code = args.func(args, conn)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    except ProjectRebuildError as exc:
        if db_path is None:
            print(str(exc), file=sys.stderr)
        elif conn is not None and is_database_damage(exc.cause):
            damaged_conn = conn
            conn = None
            print(
                discard_damaged_database(
                    damaged_conn,
                    db_path,
                    format_exception_detail(exc.cause),
                    "SQLite stopped the operation with an explicit damage result.",
                ),
                file=sys.stderr,
            )
        else:
            diagnostic = format_index_error(db_path, exc.cause, conn)
            print(
                f"Project index rebuild failed for {exc.project_path}. "
                f"{diagnostic} The transaction was rolled back; "
                "prior index contents remain intact.",
                file=sys.stderr,
            )
        exit_code = 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    except sqlite3.ProgrammingError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        if db_path is None:
            print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        elif conn is not None and is_database_damage(exc):
            damaged_conn = conn
            conn = None
            print(
                discard_damaged_database(
                    damaged_conn,
                    db_path,
                    format_exception_detail(exc),
                    "SQLite stopped the operation with an explicit damage result.",
                ),
                file=sys.stderr,
            )
        else:
            print(format_index_error(db_path, exc, conn), file=sys.stderr)
        exit_code = 1
    finally:
        if conn is not None:
            close_db(conn)

    sys.exit(exit_code)
