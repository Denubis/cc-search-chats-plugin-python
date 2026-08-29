"""Minimal console bootstrap that starts the interactive request clock early."""

from time import monotonic


def main() -> None:
    """Record invocation time before importing the command and storage stacks."""
    request_started = monotonic()
    from cc_search_chats.cli import main as cli_main

    cli_main(request_started=request_started)
