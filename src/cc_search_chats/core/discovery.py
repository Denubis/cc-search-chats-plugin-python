"""Session file discovery and path encoding.

Split into pure functions (Functional Core) and I/O functions (Imperative Shell).
Pure: encode_project_path, decode_project_path, rank_sessions.
I/O: get_claude_projects_dir, list_session_files.
"""

import re
from pathlib import Path

from cc_search_chats.core.models import SessionMeta

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$"
)


def encode_project_path(project_path: str) -> str:
    """Convert a project path to Claude Code's encoded directory name.

    Replaces every ``/`` (including the leading one) with ``-``.

    >>> encode_project_path("/home/brian/project")
    '-home-brian-project'
    >>> encode_project_path("/")
    '-'
    """
    return project_path.replace("/", "-")


def decode_project_path(encoded: str) -> str:
    """Inverse of encode_project_path.

    Replace leading ``-`` with ``/``, then remaining ``-`` with ``/``.
    This is lossy if the original path contained ``-`` characters --
    suitable for display purposes only.

    >>> decode_project_path("-home-brian-project")
    '/home/brian/project'
    """
    if not encoded:
        return ""
    # Replace all dashes with slashes
    return encoded.replace("-", "/")


def get_claude_projects_dir() -> Path:
    """Return the Claude Code projects directory (~/.claude/projects)."""
    return Path.home() / ".claude" / "projects"


def list_session_files(
    projects_dir: Path,
    encoded_path: str,
    include_subagents: bool = False,
) -> list[SessionMeta]:
    """List JSONL session files in a project directory.

    Scans ``projects_dir / encoded_path`` for UUID-named ``.jsonl`` files.
    By default excludes files under ``subagents/`` subdirectories.

    This is an I/O function (reads filesystem).

    Args:
        projects_dir: Path to the Claude projects directory.
        encoded_path: Encoded project path (e.g. ``-home-brian-project``).
        include_subagents: If True, also include files under subagents/.

    Returns:
        List of SessionMeta objects with file stats.
    """
    project_dir = projects_dir / encoded_path
    if not project_dir.is_dir():
        return []

    project_path = decode_project_path(encoded_path)
    results: list[SessionMeta] = []

    # Top-level UUID-named JSONL files
    for f in project_dir.iterdir():
        if f.is_file() and _UUID_RE.match(f.name):
            stat = f.stat()
            results.append(
                SessionMeta(
                    session_id=f.stem,
                    file_path=str(f),
                    project_path=project_path,
                    file_size=stat.st_size,
                    modified_at=stat.st_mtime,
                )
            )

    # Subagent files (under any subagents/ subdirectory)
    if include_subagents:
        for subagent_dir in project_dir.rglob("subagents"):
            if not subagent_dir.is_dir():
                continue
            for f in subagent_dir.iterdir():
                if f.is_file() and f.suffix == ".jsonl":
                    stat = f.stat()
                    results.append(
                        SessionMeta(
                            session_id=f.stem,
                            file_path=str(f),
                            project_path=project_path,
                            file_size=stat.st_size,
                            modified_at=stat.st_mtime,
                        )
                    )

    return results


def rank_sessions(sessions: list[SessionMeta]) -> list[SessionMeta]:
    """Rank sessions for crash recovery heuristic.

    Pure function. Algorithm:
    1. Sort by modified_at descending (newest first)
    2. Take the top 5 by mtime
    3. Among those 5, sort by file_size descending
    4. Append the remaining sessions (positions 5+) in original mtime order

    The caller takes the first result as the "most recent substantial session".
    """
    if not sessions:
        return []

    # Sort all by mtime descending
    by_mtime = sorted(sessions, key=lambda s: s.modified_at, reverse=True)

    # Split into top 5 and remainder
    top = by_mtime[:5]
    rest = by_mtime[5:]

    # Among top 5, sort by file_size descending
    top_ranked = sorted(top, key=lambda s: s.file_size, reverse=True)

    return top_ranked + rest
