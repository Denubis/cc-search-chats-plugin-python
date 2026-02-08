"""CLI entry point for cc-search-chats."""

import argparse
import os
import sqlite3
import sys

from cc_search_chats.core.discovery import (
    encode_project_path,
    get_claude_projects_dir,
    list_session_files,
    rank_sessions,
)
from cc_search_chats.output import (
    format_context,
    format_extract,
    format_search_results,
    format_session_list,
    json_context,
    json_extract,
    json_search_results,
    json_session_list,
)
from cc_search_chats.storage.index import (
    close_db,
    ensure_fts5,
    extract_context,
    extract_session,
    jit_reindex,
    list_sessions,
    open_db,
    reindex_project,
    search,
    update_all_keywords,
)


def _resolve_project(args: argparse.Namespace) -> str:
    """Resolve the project path from args or cwd."""
    if hasattr(args, "project") and args.project is not None:
        return args.project
    return os.getcwd()


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


def _handle_search(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Search chat history for a query."""
    project_path = _resolve_project(args)
    _validate_project_dir(project_path)

    jit_reindex(conn, project_path)

    results = search(
        conn,
        args.query,
        epoch=args.epoch,
        project=project_path,
        days=args.days,
    )

    if args.json:
        print(json_search_results(results))
    else:
        output = format_search_results(results)
        if output:
            print(output)
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
        output = format_extract(messages, compact_events)
        if output:
            print(output)
    return 0


def _handle_list(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """List sessions."""
    project_path = _resolve_project(args)
    _validate_project_dir(project_path)

    jit_reindex(conn, project_path)

    results = list_sessions(conn, project=project_path, days=args.days)

    if args.json:
        print(json_session_list(results))
    else:
        output = format_session_list(results)
        if output:
            print(output)
    return 0


def _handle_index(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Build or rebuild the search index."""
    project_path = _resolve_project(args)
    _validate_project_dir(project_path)

    count = reindex_project(conn, project_path)
    update_all_keywords(conn)

    print(f"Indexed {count} sessions for {project_path}", file=sys.stderr)
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
        output = format_context(target, before, after)
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
            '  cc-search-chats search "deploy" --days 7 --json'
        ),
    )
    search_parser.add_argument("query", type=str, help="search query string")
    search_parser.add_argument(
        "--project", type=str, default=None, help="project path to search"
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
            "  cc-search-chats index --project /path/to/project"
        ),
    )
    index_parser.add_argument(
        "--project", type=str, default=None, help="project path to index"
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

    conn = open_db()
    try:
        exit_code = args.func(args, conn)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    finally:
        close_db(conn)

    sys.exit(exit_code)
