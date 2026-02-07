"""CLI entry point for cc-search-chats."""

from __future__ import annotations

import argparse
import sys


def _not_implemented(subcommand: str) -> int:
    """Print 'not yet implemented' and return exit code 1."""
    print(f"cc-search-chats {subcommand}: not yet implemented", file=sys.stderr)
    return 1


def _handle_search(args: argparse.Namespace) -> int:
    return _not_implemented("search")


def _handle_extract(args: argparse.Namespace) -> int:
    return _not_implemented("extract")


def _handle_list(args: argparse.Namespace) -> int:
    return _not_implemented("list")


def _handle_index(args: argparse.Namespace) -> int:
    return _not_implemented("index")


def _handle_context(args: argparse.Namespace) -> int:
    return _not_implemented("context")


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

    subparsers = parser.add_subparsers(dest="command", help="available commands")

    # search
    search_parser = subparsers.add_parser(
        "search", help="search chat history for a query"
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
        "extract", help="extract a conversation by session ID"
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
    extract_parser.set_defaults(func=_handle_extract)

    # list
    list_parser = subparsers.add_parser("list", help="list sessions")
    list_parser.add_argument(
        "--project", type=str, default=None, help="project path to list sessions for"
    )
    list_parser.add_argument(
        "--days", type=int, default=None, help="limit to last N days"
    )
    list_parser.set_defaults(func=_handle_list)

    # index
    index_parser = subparsers.add_parser(
        "index", help="build or rebuild the search index"
    )
    index_parser.add_argument(
        "--project", type=str, default=None, help="project path to index"
    )
    index_parser.set_defaults(func=_handle_index)

    # context
    context_parser = subparsers.add_parser(
        "context", help="show context around a message UUID"
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

    exit_code = args.func(args)
    sys.exit(exit_code)
