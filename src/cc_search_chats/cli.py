"""CLI entry point for cc-search-chats.

Imperative Shell — parses arguments and orchestrates storage/output layers.
"""

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

import psycopg

from cc_search_chats import __version__
from cc_search_chats.core.discovery import (
    decode_project_path,
    encode_project_path,
    get_claude_projects_dir,
    list_session_files,
    rank_sessions,
)
from cc_search_chats.core.identity import NativeLocator, parse_locator
from cc_search_chats.core.models import SessionMeta
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
from cc_search_chats.queueing import client_admission
from cc_search_chats.semantic import ModelUnavailable, embed_passages, embed_query
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
    search_full_content,
)
from cc_search_chats.storage.postgresql import (
    context_messages as pg_context_messages,
)
from cc_search_chats.storage.postgresql import (
    extract_session as pg_extract_session,
)
from cc_search_chats.storage.postgresql import (
    index_embeddings,
    refresh_native_sources,
    search_messages,
)
from cc_search_chats.storage.postgresql import (
    list_sessions as pg_list_sessions,
)
from cc_search_chats.storage.postgresql import (
    resolve_message as pg_resolve_message,
)
from cc_search_chats.storage.postgresql import (
    resolve_messages as pg_resolve_messages,
)
from cc_search_chats.storage.postgresql.guardrails import acquire_index_session
from cc_search_chats.storage.postgresql.semantic import hybrid_search

_DEFAULT_POSTGRES_DSN = "service=cc_search_chats"


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
        "--nice=10",
        "--property=MemoryHigh=24G",
        "--property=MemoryMax=32G",
        "--property=MemorySwapMax=4G",
        "--property=TasksMax=256",
        "--property=CPUWeight=25",
        "--property=IOWeight=25",
        "--property=IOSchedulingClass=idle",
        "--",
        *sys.argv,
    ]
    try:
        os.execvp(command[0], command)
    except OSError as error:
        raise RuntimeError(
            "indexing requires a working systemd user session for resource containment"
        ) from error


def _handle_postgres(args: argparse.Namespace, dsn: str) -> int:
    """Run the migrated index/search surface against PostgreSQL."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        if args.command == "index":
            if args.status:
                status = next(
                    connection.execute(
                        """
                        SELECT cs.current_revision_id, sr.semantic_revision_id,
                               COALESCE(sr.completed, 0),
                               (SELECT count(*) FROM cc_search_chats.message AS m
                                WHERE m.revision_id = cs.current_revision_id
                                  AND m.content_class = 'prose'
                                  AND m.prose_content ~ '[^[:space:]]'),
                               COALESCE(sr.selected, false)
                        FROM cc_search_chats.corpus_state AS cs
                        LEFT JOIN LATERAL (
                            SELECT r.semantic_revision_id,
                                   count(e.semantic_revision_id) AS completed,
                                   ss.current_semantic_revision_id =
                                       r.semantic_revision_id AS selected
                            FROM cc_search_chats.semantic_revision AS r
                            LEFT JOIN cc_search_chats.message_embedding AS e
                              ON e.semantic_revision_id = r.semantic_revision_id
                            LEFT JOIN cc_search_chats.semantic_state AS ss
                              ON ss.singleton
                            WHERE r.corpus_revision_id = cs.current_revision_id
                            GROUP BY r.semantic_revision_id,
                                     ss.current_semantic_revision_id
                            ORDER BY completed DESC
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
                payload = {
                    "schema_version": 1,
                    "revision_id": revision,
                    "semantic_revision_id": semantic_revision,
                    "completed": completed,
                    "total": total,
                    "selected": bool(selected),
                }
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    print(f"Semantic index: {completed}/{total} passages")
                return 0

            acquire_index_session(connection)

            def progress(provider: str, completed: int, total: int) -> None:
                if completed == total or completed % 100 == 0:
                    print(
                        f"Indexing {provider}: {completed}/{total} sources",
                        file=sys.stderr,
                    )

            if args.semantic_only:
                revision_id, source_count, message_count = next(
                    connection.execute(
                        """
                        SELECT s.current_revision_id, 0, count(*)
                        FROM cc_search_chats.corpus_state AS s
                        JOIN cc_search_chats.message AS m
                          ON m.revision_id = s.current_revision_id
                        WHERE s.singleton
                        GROUP BY s.current_revision_id
                        """
                    )
                )
            else:
                result = refresh_native_sources(
                    connection,
                    claude_root=Path(
                        os.environ.get("CC_SEARCH_CLAUDE_ROOT", "~/.claude/projects")
                    )
                    .expanduser()
                    .resolve(),
                    codex_root=Path(
                        os.environ.get("CC_SEARCH_CODEX_ROOT", "~/.codex/sessions")
                    )
                    .expanduser()
                    .resolve(),
                    progress=progress,
                )
                revision_id = result.revision_id
                source_count = result.source_count
                message_count = result.message_count
            vector_count = 0
            if not args.literal_only:
                last_embedding_report: int | None = None
                embedding_started = monotonic()
                embedding_baseline = 0

                def embedding_progress(completed: int, total: int) -> None:
                    nonlocal embedding_baseline, last_embedding_report
                    if last_embedding_report is None:
                        embedding_baseline = completed
                        print(
                            f"Semantic refresh: {completed} reused, "
                            f"{total - completed} new passages",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_embedding_report = completed
                        return
                    if completed == total or completed - last_embedding_report >= 100:
                        embedded = completed - embedding_baseline
                        elapsed = monotonic() - embedding_started
                        rate = embedded / elapsed if elapsed > 0 else 0
                        remaining = total - completed
                        eta = _eta(remaining / rate) if rate else "calculating"
                        print(
                            f"Semantic refresh: {embedding_baseline} reused, "
                            f"{embedded} embedded, {remaining} remaining, "
                            f"{rate:.1f}/s, ETA {eta}",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_embedding_report = completed

                vector_count = index_embeddings(
                    connection, embed_passages, progress=embedding_progress
                )
            payload = {
                "schema_version": 1,
                "revision_id": revision_id,
                "sources": source_count,
                "messages": message_count,
                "embeddings": vector_count,
                "status": "complete",
            }
            if args.json:
                print(json.dumps(payload, sort_keys=True))
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
            if args.json:
                print(json.dumps({"schema_version": 1, "sessions": values}))
            else:
                for value in values:
                    print(
                        f"{value['provider']}:{value['session_id']} "
                        f"({value['session_kind']}, {value['message_count']} messages)"
                    )
            return 0

        if args.command == "extract":
            session_id = args.session_id
            if session_id is None:
                sessions = pg_list_sessions(
                    connection, provider=args.provider, project=args.project
                )
                if not sessions:
                    raise ValueError("no matching sessions")
                session_id = sessions[0].source_session_id
            messages = pg_extract_session(
                connection,
                session_id,
                provider=args.provider,
                epoch=args.epoch,
            )
            values = [asdict(message) for message in messages]
            if args.json:
                print(json.dumps({"schema_version": 1, "messages": values}))
            else:
                for value in messages:
                    print(f"[{value.timestamp}] {value.role}:\n{value.text}")
            return 0

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
                if any(
                    not isinstance(parse_locator(locator), NativeLocator)
                    for locator in locators
                ):
                    print("Malformed ccchat locator", file=sys.stderr)
                    return 2
                resolutions = pg_resolve_messages(connection, locators)
                values = [
                    {
                        "locator": resolution.locator,
                        "message_count": len(resolution.messages),
                        "messages": [
                            asdict(message) for message in resolution.messages
                        ],
                    }
                    for resolution in resolutions
                ]
                if args.json:
                    print(json.dumps({"schema_version": 1, "resolutions": values}))
                else:
                    for value in values:
                        print(f"{value['message_count']}\t{value['locator']}")
                return 0 if all(value["message_count"] == 1 for value in values) else 3
            if args.command == "resolve" and not isinstance(
                parse_locator(args.uuid), NativeLocator
            ):
                print("Malformed ccchat locator", file=sys.stderr)
                return 2
            messages = (
                pg_context_messages(connection, args.uuid, depth=args.depth)
                if args.command == "context"
                else pg_resolve_message(connection, args.uuid)
            )
            values = [asdict(message) for message in messages]
            if args.json:
                print(json.dumps({"schema_version": 1, "messages": values}))
            else:
                for value in messages:
                    print(f"[{value.timestamp}] {value.role}:\n{value.text}")
            return 0 if messages else 3

        project = args.project
        if args.literal or args.everything:
            hits = search_messages(
                connection,
                args.query,
                limit=args.limit,
                provider=args.provider,
                role=args.role,
                project=project,
                since=_since_days(args.days),
                epoch=args.epoch,
            )
        else:
            hits = tuple(
                value.message
                for value in hybrid_search(
                    connection,
                    args.query,
                    embed_query(args.query),
                    limit=args.limit,
                    provider=args.provider,
                    role=args.role,
                    project=project,
                    since=_since_days(args.days),
                    epoch=args.epoch,
                )
            )
        results = [
            {
                "provider": hit.provider,
                "session_id": hit.source_session_id,
                "logical_message_id": hit.logical_message_id,
                "locator": hit.canonical_locator,
                "timestamp": hit.timestamp,
                "role": hit.role,
                "session_kind": hit.session_kind,
                "text": hit.text,
                "repository": hit.repository,
                "cwd": hit.cwd,
                "score": hit.rank,
            }
            for hit in hits
        ]
        if args.json:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "complete",
                        "result_limit": args.limit,
                        "results": results,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
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


def _collect_everything_sessions(
    args: argparse.Namespace,
) -> tuple[list[SessionMeta], str, str | None]:
    """Resolve which sessions the --everything live scan should cover."""
    projects_dir = get_claude_projects_dir()
    if getattr(args, "all", False):
        sessions: list[SessionMeta] = []
        if projects_dir.is_dir():
            for child in sorted(projects_dir.iterdir()):
                if child.is_dir():
                    sessions.extend(list_session_files(projects_dir, child.name))
        return sessions, "all", None
    project_path = args.project if args.project is not None else os.getcwd()
    encoded = encode_project_path(project_path)
    return list_session_files(projects_dir, encoded), "local", project_path


def _handle_search(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Search chat history, local-first with broaden-on-miss.

    Default: search the current project; if it has no hits, widen to every
    indexed project. ``--all`` searches everything up front; ``--project``
    narrows to one project and never broadens; running from a directory that
    is not a Claude project also searches everything. ``--everything`` instead
    runs a live full-content scan (text + thinking + tool I/O) over the
    in-scope sessions via a throwaway in-memory index.
    """
    if getattr(args, "everything", False):
        sessions, scope, searched_project = _collect_everything_sessions(args)
        project_count = len({s.project_path for s in sessions})
        results = search_full_content(
            sessions, args.query, epoch=args.epoch, days=args.days
        )
        if not sessions and scope == "local":
            hint = (
                "No sessions for the current project. "
                "Use --everything --all to search every project."
            )
        else:
            hint = "No matches in full content (thinking + tool calls)."
        _emit_search(
            args,
            results,
            scope=scope,
            searched_project=searched_project,
            project_count=project_count,
            empty_hint=hint,
        )
        return 0

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
        description="Search and extract Claude Code chat history",
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
            '  cc-search-chats search "auth" --epoch 0\n'
            '  cc-search-chats search "deploy" --days 7 --json\n'
            '  cc-search-chats search "decision rule" --all'
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
        "--everything",
        action="store_true",
        default=False,
        help="include indexed thinking and tool calls using literal search",
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
            "  cc-search-chats index --all\n"
            "  cc-search-chats index --project /path/to/project"
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
    resolve_parser.set_defaults(func=None)

    return parser


def main() -> None:
    """Entry point for cc-search-chats CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    postgres_commands = {"index", "search", "list", "extract", "context", "resolve"}
    postgres = (
        args.command in postgres_commands and "CC_SEARCH_DB_PATH" not in os.environ
    )
    standard_connection = any(
        key in os.environ
        for key in ("PGSERVICE", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER")
    )
    if postgres:
        try:
            _contain_semantic_index(args)
            admission_name = (
                None
                if args.command == "index" and args.status
                else "index"
                if args.command == "index"
                else "read"
            )
            if admission_name is None:
                exit_code = _handle_postgres(
                    args, "" if standard_connection else _DEFAULT_POSTGRES_DSN
                )
            else:
                with client_admission(admission_name):
                    exit_code = _handle_postgres(
                        args, "" if standard_connection else _DEFAULT_POSTGRES_DSN
                    )
            sys.exit(exit_code)
        except ModelUnavailable as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(8)
        except (OSError, psycopg.Error, RuntimeError, ValueError) as exc:
            detail = (
                exc.diag.message_primary
                if isinstance(exc, psycopg.Error) and exc.diag.message_primary
                else str(exc)
            )
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
