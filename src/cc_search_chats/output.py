"""Output formatters for human-readable and JSON output.

Imperative Shell — writes formatted strings. The formatting logic itself
is pure (returns strings, no I/O). T-strings (PEP 750) are used for
human-readable output to sanitise user-originated content.

Both human and JSON formatters accept lists of dicts (or sqlite3.Row
objects, which support dict-like access) and produce output strings.
"""

import json
from string.templatelib import Interpolation, Template


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


# ============================================================
# Human-readable formatters
# ============================================================


def format_search_results(rows: list, *, verbose: bool = False) -> str:
    """Format search results with session_id, epoch, timestamp, role, snippet.

    One result per block separated by blank lines.
    """
    if not rows:
        return ""

    blocks: list[str] = []
    for row in rows:
        session_id = row["session_id"]
        epoch = row["epoch"]
        timestamp = row["timestamp"]
        role = row["role"]
        snippet = row["snippet"]
        score = row["score"]

        header = render_safe(t"[{timestamp}] {session_id} epoch {epoch} ({role})")
        snippet_line = render_safe(t"  {snippet}")

        if verbose:
            score_line = f"  score: {score}"
            blocks.append(f"{header}\n{snippet_line}\n{score_line}")
        else:
            blocks.append(f"{header}\n{snippet_line}")

    return "\n\n".join(blocks)


def format_extract(rows: list, compact_events: list) -> str:
    """Format a full session extract with epoch markers at compression boundaries.

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

        # Insert epoch marker when epoch changes (and it's not the first epoch)
        if epoch != current_epoch and current_epoch is not None:
            event = compact_by_epoch.get(epoch)
            if event is not None:
                marker = _format_epoch_marker(event)
                output_parts.append(marker)

        current_epoch = epoch

        # Format the role label with capitalised first letter
        role_label = role.capitalize() if role else "Unknown"
        header = render_safe(t"[{timestamp}] {role_label}:")
        body = render_safe(t"{text_content}")
        output_parts.append(f"{header}\n{body}")

    return "\n\n".join(output_parts)


def _format_epoch_marker(event: dict) -> str:
    """Format an epoch marker line from a compact_event row."""
    epoch = event["epoch"]
    timestamp = event["timestamp"]
    trigger = event["trigger"]
    pre_tokens = event["pre_tokens"]
    summary_text = event.get("summary_text")

    marker = f"--- Epoch {epoch} (compression at {timestamp}, trigger: {trigger}, ~{pre_tokens} tokens) ---"
    if summary_text:
        marker += f"\nSummary: {summary_text}"
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
        details = (
            f"  project:  {project_path}\n"
            f"  size:     {file_size} bytes\n"
            f"  modified: {modified_at}\n"
            f"  epochs:   {epoch_count}  messages: {total_messages}"
        )
        if summary:
            summary_line = render_safe(t"  summary:  {summary}")
            blocks.append(f"{header}\n{details}\n{summary_line}")
        else:
            blocks.append(f"{header}\n{details}")

    return "\n\n".join(blocks)


def format_context(target: dict, before: list, after: list) -> str:
    """Format context around a message with a TARGET marker.

    Shows before messages, then >>> TARGET <<< marker, then after messages.
    """
    parts: list[str] = []

    for row in before:
        timestamp = row["timestamp"]
        role = row["role"]
        text_content = row["text_content"]
        role_label = role.capitalize() if role else "Unknown"
        header = render_safe(t"[{timestamp}] {role_label}:")
        body = render_safe(t"{text_content}")
        parts.append(f"{header}\n{body}")

    # Target message with marker
    timestamp = target["timestamp"]
    role = target["role"]
    text_content = target["text_content"]
    role_label = role.capitalize() if role else "Unknown"
    header = render_safe(t">>> TARGET >>> [{timestamp}] {role_label}:")
    body = render_safe(t"{text_content}")
    parts.append(f"{header}\n{body}")

    for row in after:
        timestamp = row["timestamp"]
        role = row["role"]
        text_content = row["text_content"]
        role_label = role.capitalize() if role else "Unknown"
        header = render_safe(t"[{timestamp}] {role_label}:")
        body = render_safe(t"{text_content}")
        parts.append(f"{header}\n{body}")

    return "\n\n".join(parts)


# ============================================================
# JSON formatters
# ============================================================


def _row_to_dict(row: dict) -> dict:
    """Convert a sqlite3.Row or dict to a plain dict.

    sqlite3.Row supports dict(row) but also subscript access.
    If already a dict, return as-is.
    """
    if isinstance(row, dict):
        return row
    return dict(row)


def json_search_results(rows: list) -> str:
    """Format search results as JSON.

    Returns a JSON array of result objects.
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
            }
        )
    return json.dumps(results, indent=2, ensure_ascii=False)


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
                "summary_text": event.get("summary_text"),
            }

        epochs.append(epoch_obj)

    return json.dumps(
        {"session_id": session_id, "epochs": epochs},
        indent=2,
        ensure_ascii=False,
    )


def json_session_list(rows: list) -> str:
    """Format session list as JSON.

    Returns a JSON array of session objects.
    """
    results = []
    for row in rows:
        results.append(
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
    return json.dumps(results, indent=2, ensure_ascii=False)


def json_context(target: dict, before: list, after: list) -> str:
    """Format context around a message as JSON.

    Returns a JSON object with target, before, and after arrays.
    """

    def _msg_dict(row: dict) -> dict:
        return {
            "uuid": row["uuid"],
            "role": row["role"],
            "text": row["text_content"],
            "timestamp": row["timestamp"],
        }

    return json.dumps(
        {
            "target": _msg_dict(target),
            "before": [_msg_dict(r) for r in before],
            "after": [_msg_dict(r) for r in after],
        },
        indent=2,
        ensure_ascii=False,
    )
