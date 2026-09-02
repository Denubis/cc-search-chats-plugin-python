#!/usr/bin/env python3
"""Install the cc-search-chats host-routing rule into one Codex home."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

RULE_NAME = "cc-search-chats.rules"
SOURCE_RULE = Path(__file__).resolve().parents[1] / "rules" / RULE_NAME


class InstallError(RuntimeError):
    """The requested rule installation is unsafe."""


def _ensure_directory(path: Path, description: str) -> None:
    if path.is_symlink():
        raise InstallError(f"{description} must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise InstallError(f"{description} must be a directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def install_rule(codex_home: Path) -> tuple[Path, bool]:
    """Install the bundled rule atomically, returning its path and change state."""
    _ensure_directory(codex_home, "Codex home")
    rules_dir = codex_home / "rules"
    _ensure_directory(rules_dir, "Codex rules directory")

    target = rules_dir / RULE_NAME
    if target.is_symlink():
        raise InstallError(f"Codex rule target must not be a symlink: {target}")
    if target.exists() and not target.is_file():
        raise InstallError(f"Codex rule target must be a regular file: {target}")

    content = SOURCE_RULE.read_bytes()
    if target.exists() and target.read_bytes() == content:
        return target, False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{RULE_NAME}.", dir=rules_dir
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the cc-search-chats Codex host-routing rule."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(),
        help="Codex state directory (default: CODEX_HOME or ~/.codex)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        target, changed = install_rule(args.codex_home.expanduser())
    except InstallError as error:
        print(f"install-codex-rule: {error}", file=sys.stderr)
        return 1
    print(f"{'installed' if changed else 'unchanged'} {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
