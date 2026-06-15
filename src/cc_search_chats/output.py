"""Output formatters for human-readable and JSON output.

Functional Core — pure functions that format and return strings, no I/O.
T-strings (PEP 750) are used for human-readable output to sanitise
user-originated content. The Imperative Shell (cli.py) prints results.

Both human and JSON formatters accept lists of dicts (or sqlite3.Row
objects, which support dict-like access) and produce output strings.
"""

import json
import re
import sqlite3
from string.templatelib import Interpolation, Template

# Version of the --json output contract. Every JSON payload carries this as
# `schema_version`. Evolve the JSON additively (add fields; never remove,
# rename, or reshape) so old consumers keep working. Bump ONLY on an
# unavoidable breaking change. The search-chat skill asserts this value, so a
# CLI/plugin version mismatch fails loudly instead of silently mis-parsing.
SCHEMA_VERSION = 1

# Type alias for row-like objects (dict or sqlite3.Row — both support [] access)
type _Row = dict | sqlite3.Row

# Pattern matching messages that are only tool call markers (no real text)
_TOOL_ONLY_RE = re.compile(r"^(\[tool: [^\]]+\]\n?)+$")


def render_safe(template: Template) -> str:
    """Process a t-string template, sanitising interpolated values.

    Literal parts (written by the developer) are trusted and passed
    through unchanged. Interpolated values (user-originated content)
    have null bytes stripped to prevent terminal and C-string issues.
    """
    parts: list[str] = []
    for part in template:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, Interpolation):
            value = str(part.value)
            value = value.replace("\x00", "")
            parts.append(value)
    return "".join(parts)


def _is_noise(text_content: str) -> bool:
    """Return True if a message is noise that should be filtered in default mode.

    Noise = empty content or content that is only tool call markers.
    """
    stripped = text_content.strip()
    if not stripped:
        return True
    if _TOOL_ONLY_RE.match(stripped):
        return True
    return False


def _clean_text(text_content: str) -> str:
    """Strip leading newlines from message text for cleaner output."""
    return text_content.lstrip("\n")


# ============================================================
# Human-readable formatters
# ============================================================


def format_search_results(
    rows: list,
    *,
    verbose: bool = False,
    scope: str = "local",
    searched_project: str | None = None,
    project_count: int = 1,
) -> str:
    """Format search results with session_id, epoch, timestamp, role, snippet.

    One result per block separated by blank lines. When ``scope`` is
    ``widened`` a leading note explains that the search broadened after a
    local miss. Results carry a project label whenever the scope spans more
    than the current project (``widened`` or ``all``).
    """
    if not rows:
        return ""

    show_project = scope in ("widened", "all")

    blocks: list[str] = []
    if scope == "widened" and searched_project is not None:
        blocks.append(
            render_safe(
                t"No matches in {searched_project}; widened to all "
                t"{project_count} indexed projects."
            )
        )

    for row in rows:
        session_id = row["session_id"]
        epoch = row["epoch"]
        timestamp = row["timestamp"]
        role = row["role"]
        snippet = row["snippet"]
        score = row["score"]

        if show_project:
            project = row["project_path"]
            header = render_safe(
                t"[{timestamp}] {session_id} epoch {epoch} ({role}) — {project}"
            )
        else:
            header = render_safe(
                t"[{timestamp}] {session_id} epoch {epoch} ({role})"
            )
        snippet_line = render_safe(t"  {snippet}")

        if verbose:
            score_line = f"  score: {score}"
            blocks.append(f"{header}\n{snippet_line}\n{score_line}")
        else:
            blocks.append(f"{header}\n{snippet_line}")

    return "\n\n".join(blocks)


def format_extract(rows: list, compact_events: list, *, verbose: bool = False) -> str:
    """Format a full session extract with epoch markers at compression boundaries.

    By default, filters out noise (empty messages and tool-call-only messages).
    Use verbose=True to include everything.

    Inserts epoch markers between epochs with compression info.
    Each message formatted as:
        [timestamp] Role:
        message text
    """
    if not rows:
        return ""

    # Build a lookup of compact events by epoch
    compact_by_epoch: dict[int, dict] = {}
    for event in compact_events:
        compact_by_epoch[event["epoch"]] = event

    output_parts: list[str] = []
    current_epoch: int | None = None

    for row in rows:
        epoch = row["epoch"]
        timestamp = row["timestamp"]
        role = row["role"]
        text_content = row["text_content"]

        # Filter noise unless verbose
        if not verbose and _is_noise(text_content):
            continue

        # Insert epoch marker when epoch changes (and it's not the first epoch)
        if epoch != current_epoch and current_epoch is not None:
            event = compact_by_epoch.get(epoch)
            if event is not None:
                marker = _format_epoch_marker(event)
                output_parts.append(marker)

        current_epoch = epoch

        # Clean up text for display
        display_text = text_content if verbose else _clean_text(text_content)

        # Format the role label with capitalised first letter
        role_label = role.capitalize() if role else "Unknown"
        header = render_safe(t"[{timestamp}] {role_label}:")
        body = render_safe(t"{display_text}")
        output_parts.append(f"{header}\n{body}")

    return "\n\n".join(output_parts)


def _format_epoch_marker(event: dict) -> str:
    """Format an epoch marker line from a compact_event row."""
    epoch = event["epoch"]
    timestamp = event["timestamp"]
    trigger = event["trigger"]
    pre_tokens = event["pre_tokens"]
    summary_text = event["summary_text"]

    marker = render_safe(
        t"--- Epoch {epoch} (compression at {timestamp}, trigger: {trigger}, ~{pre_tokens} tokens) ---"
    )
    if summary_text:
        marker += "\n" + render_safe(t"Summary: {summary_text}")
    return marker


def format_session_list(rows: list) -> str:
    """Format session list with session_id, project, file size, modified date,
    summary excerpt, epoch count, message count.
    """
    if not rows:
        return ""

    blocks: list[str] = []
    for row in rows:
        session_id = row["session_id"]
        project_path = row["project_path"]
        file_size = row["file_size"]
        modified_at = row["modified_at"]
        summary = row["summary"]
        epoch_count = row["epoch_count"]
        total_messages = row["total_messages"]

        header = render_safe(t"{session_id}")
        details = render_safe(
            t"  project:  {project_path}\n  size:     {file_size} bytes\n  modified: {modified_at}\n  epochs:   {epoch_count}  messages: {total_messages}"
        )
        if summary:
            summary_line = render_safe(t"  summary:  {summary}")
            blocks.append(f"{header}\n{details}\n{summary_line}")
        else:
            blocks.append(f"{header}\n{details}")

    return "\n\n".join(blocks)


def format_context(
    target: _Row, before: list, after: list, *, verbose: bool = False
) -> str:
    """Format context around a message with a TARGET marker.

    Shows before messages, then >>> TARGET <<< marker, then after messages.
    By default, filters noise and strips leading newlines. Use verbose=True
    for raw output.
    """
    parts: list[str] = []

    for row in before:
        text_content = row["text_content"]
        if not verbose and _is_noise(text_content):
            continue
        display_text = text_content if verbose else _clean_text(text_content)
        timestamp = row["timestamp"]
        role = row["role"]
        role_label = role.capitalize() if role else "Unknown"
        header = render_safe(t"[{timestamp}] {role_label}:")
        body = render_safe(t"{display_text}")
        parts.append(f"{header}\n{body}")

    # Target message with marker (always shown, but still clean text)
    text_content = target["text_content"]
    display_text = text_content if verbose else _clean_text(text_content)
    timestamp = target["timestamp"]
    role = target["role"]
    role_label = role.capitalize() if role else "Unknown"
    header = render_safe(t">>> TARGET >>> [{timestamp}] {role_label}:")
    body = render_safe(t"{display_text}")
    parts.append(f"{header}\n{body}")

    for row in after:
        text_content = row["text_content"]
        if not verbose and _is_noise(text_content):
            continue
        display_text = text_content if verbose else _clean_text(text_content)
        timestamp = row["timestamp"]
        role = row["role"]
        role_label = role.capitalize() if role else "Unknown"
        header = render_safe(t"[{timestamp}] {role_label}:")
        body = render_safe(t"{display_text}")
        parts.append(f"{header}\n{body}")

    return "\n\n".join(parts)


# ============================================================
# JSON formatters
# ============================================================


def json_search_results(
    rows: list,
    *,
    scope: str = "local",
    searched_project: str | None = None,
    project_count: int = 1,
) -> str:
    """Format search results as JSON.

    Returns an object ``{scope, searched_project, project_count, results}``.
    ``scope`` is ``local`` (current project), ``widened`` (broadened after a
    local miss), or ``all`` (machine-wide). ``results`` is the match array,
    each entry carrying its originating ``project_path``.
    """
    results = []
    for row in rows:
        results.append(
            {
                "uuid": row["uuid"],
                "session_id": row["session_id"],
                "epoch": row["epoch"],
                "timestamp": row["timestamp"],
                "role": row["role"],
                "snippet": row["snippet"],
                "score": row["score"],
                "project_path": row["project_path"],
            }
        )
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "scope": scope,
            "searched_project": searched_project,
            "project_count": project_count,
            "results": results,
        },
        indent=2,
        ensure_ascii=False,
    )


def json_extract(rows: list, compact_events: list, session_id: str) -> str:
    """Format a session extract as JSON.

    Returns a JSON object with session_id and an epochs array.
    Each epoch contains its messages and optional compression metadata.
    """
    # Build compact event lookup by epoch
    compact_by_epoch: dict[int, dict] = {}
    for event in compact_events:
        compact_by_epoch[event["epoch"]] = event

    # Group messages by epoch
    epochs_map: dict[int, list[dict]] = {}
    for row in rows:
        epoch = row["epoch"]
        if epoch not in epochs_map:
            epochs_map[epoch] = []
        epochs_map[epoch].append(
            {
                "uuid": row["uuid"],
                "role": row["role"],
                "text": row["text_content"],
                "timestamp": row["timestamp"],
            }
        )

    # Build epochs array
    epochs = []
    for epoch_num in sorted(epochs_map.keys()):
        epoch_obj: dict = {
            "epoch": epoch_num,
            "messages": epochs_map[epoch_num],
        }

        # Add compression info if available
        event = compact_by_epoch.get(epoch_num)
        if event is not None:
            epoch_obj["compression"] = {
                "timestamp": event["timestamp"],
                "trigger": event["trigger"],
                "pre_tokens": event["pre_tokens"],
                "summary_text": event["summary_text"],
            }

        epochs.append(epoch_obj)

    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "epochs": epochs,
        },
        indent=2,
        ensure_ascii=False,
    )


def json_session_list(rows: list) -> str:
    """Format session list as JSON.

    Returns an object ``{schema_version, sessions}`` where ``sessions`` is the
    array of session objects.
    """
    sessions = []
    for row in rows:
        sessions.append(
            {
                "session_id": row["session_id"],
                "project_path": row["project_path"],
                "file_size": row["file_size"],
                "modified_at": row["modified_at"],
                "summary": row["summary"],
                "epoch_count": row["epoch_count"],
                "message_count": row["total_messages"],
            }
        )
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "sessions": sessions},
        indent=2,
        ensure_ascii=False,
    )


def json_context(target: _Row, before: list, after: list) -> str:
    """Format context around a message as JSON.

    Returns a JSON object with target, before, and after arrays.
    """

    def _msg_dict(row: _Row) -> dict:
        return {
            "uuid": row["uuid"],
            "role": row["role"],
            "text": row["text_content"],
            "timestamp": row["timestamp"],
        }

    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "target": _msg_dict(target),
            "before": [_msg_dict(r) for r in before],
            "after": [_msg_dict(r) for r in after],
        },
        indent=2,
        ensure_ascii=False,
    )


def json_index_result(sessions_indexed: int, project_path: str) -> str:
    """Format index result as JSON.

    Returns a JSON object with sessions_indexed count and project_path.
    """
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "sessions_indexed": sessions_indexed,
            "project_path": project_path,
        },
        indent=2,
        ensure_ascii=False,
    )


def json_index_all_result(counts: dict[str, int]) -> str:
    """Format an ``index --all`` result as JSON.

    Returns a JSON object with the number of projects scanned and the
    sessions indexed (new/changed) versus skipped (already current).
    """
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "projects": counts["projects"],
            "sessions_indexed": counts["indexed"],
            "sessions_skipped": counts["skipped"],
        },
        indent=2,
        ensure_ascii=False,
    )
