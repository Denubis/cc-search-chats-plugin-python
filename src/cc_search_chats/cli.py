"""CLI entry point for cc-search-chats.

Imperative Shell — parses arguments and orchestrates storage/output layers.
"""

import argparse
import os
import sqlite3
import sys

from cc_search_chats.core.discovery import (
    decode_project_path,
    encode_project_path,
    get_claude_projects_dir,
    list_session_files,
    rank_sessions,
)
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
        help="live full-content scan incl. thinking and tool calls (not persisted)",
    )
    search_parser.add_argument(
        "--epoch", type=int, default=None, help="epoch number to search within"
    )
    search_parser.add_argument(
        "--days", type=int, default=None, help="limit search to last N days"
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
    context_parser.add_argument("uuid", type=str, help="message UUID")
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

    return parser


def main() -> None:
    """Entry point for cc-search-chats CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

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
