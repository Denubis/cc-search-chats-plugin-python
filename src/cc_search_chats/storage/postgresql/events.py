"""Bounded, content-free export of canonical human-message events."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

import psycopg

from cc_search_chats.storage.postgresql.guardrails import queued_read_operation


@dataclass(frozen=True, slots=True)
class HumanMessageEvent:
    """One positively identified human submission without its message body."""

    event_id: str
    occurred_at_utc: datetime
    canonical_locator: str
    provider: str
    source_session_id: str
    session_kind: str
    cwd: str | None
    repository: str | None
    submitted_by: str
    retention_status: str
    physical_alias_count: int


@dataclass(frozen=True, slots=True)
class EventPopulation:
    """Positive population accounting for one event-export window."""

    scanned_content_rows: int
    scanned_logical_messages: int
    retained: int
    excluded: int
    unresolved: int
    excluded_by_reason: dict[str, int]
    unresolved_by_reason: dict[str, int]
    content_rows_by_class: dict[str, int]


@dataclass(frozen=True, slots=True)
class EventExport:
    """One revision-pinned, half-open human-message event export."""

    from_utc: datetime
    until_utc: datetime
    source_revision: int
    population: EventPopulation
    events: tuple[HumanMessageEvent, ...]


def _validate_window(from_utc: datetime, until_utc: datetime) -> None:
    if from_utc.tzinfo is None or from_utc.utcoffset() is None:
        raise ValueError("event export lower bound must include a timezone")
    if until_utc.tzinfo is None or until_utc.utcoffset() is None:
        raise ValueError("event export upper bound must include a timezone")
    if from_utc >= until_utc:
        raise ValueError("event export window must be non-empty")


def _retention(
    submitted_by: str,
    role: str,
    session_kind: str,
    has_prose: bool,
) -> tuple[str, str]:
    if (
        role == "user"
        and has_prose
        and (
            submitted_by == "human"
            or (submitted_by == "unknown" and session_kind == "primary")
        )
    ):
        return "retained", "retained"
    if submitted_by == "identified_harness":
        return "excluded", "identified_harness"
    if role == "user" and not has_prose:
        return "excluded", "non_prose"
    if submitted_by == "unknown" and role == "user":
        return "unresolved", "unknown_authorship"
    if role != "user":
        return "excluded", "non_user_role"
    return "excluded", "non_prose"


@queued_read_operation
def export_human_message_events(
    connection: psycopg.Connection,
    *,
    from_utc: datetime,
    until_utc: datetime,
) -> EventExport:
    """Export canonical human submissions once inside a half-open UTC window."""
    _validate_window(from_utc, until_utc)
    lower = from_utc.astimezone(UTC)
    upper = until_utc.astimezone(UTC)
    revision_row = connection.execute(
        "SELECT current_revision_id FROM cc_search_chats.corpus_state WHERE singleton"
    ).fetchone()
    if revision_row is None or revision_row[0] is None:
        raise ValueError("event export requires a selected corpus revision")
    source_revision = int(revision_row[0])

    content_counts = {
        content_class: int(count)
        for content_class, count in connection.execute(
            """
            SELECT content_class, count(*)
            FROM cc_search_chats.message_current
            WHERE timestamp_text::timestamptz >= %s
              AND timestamp_text::timestamptz < %s
            GROUP BY content_class
            ORDER BY content_class
            """,
            (lower, upper),
        )
    }
    rows = connection.execute(
        """
        SELECT message.provider, message.source_session_id,
               message.logical_message_id, message.canonical_locator,
               message.timestamp_text::timestamptz, message.role,
               message.session_kind, message.cwd, message.repository,
               message.submitted_by,
               bool_or(message.content_class = 'prose') AS has_prose,
               count(DISTINCT (
                   alias.source_root_id, alias.source_file_relative,
                   alias.record_ordinal
               )) AS physical_alias_count
        FROM cc_search_chats.message_current AS message
        LEFT JOIN cc_search_chats.physical_alias_current AS alias
          ON (alias.provider, alias.source_session_id,
              alias.logical_message_id, alias.content_class) =
             (message.provider, message.source_session_id,
              message.logical_message_id, message.content_class)
        WHERE message.timestamp_text::timestamptz >= %s
          AND message.timestamp_text::timestamptz < %s
        GROUP BY message.provider, message.source_session_id,
                 message.logical_message_id, message.canonical_locator,
                 message.timestamp_text::timestamptz, message.role,
                 message.session_kind, message.cwd, message.repository,
                 message.submitted_by
        ORDER BY message.timestamp_text::timestamptz, message.provider,
                 message.source_session_id, message.logical_message_id
        """,
        (lower, upper),
    )

    events: list[HumanMessageEvent] = []
    retention_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    unresolved_reasons: Counter[str] = Counter()
    identities: set[tuple[str, str, str]] = set()
    for row in rows:
        identity = (row[0], row[1], row[2])
        if identity in identities:
            raise ValueError(
                "event export found conflicting metadata for one logical message"
            )
        identities.add(identity)
        retention_status, reason = _retention(row[9], row[5], row[6], row[10])
        retention_counts[retention_status] += 1
        if retention_status == "excluded":
            excluded_reasons[reason] += 1
        elif retention_status == "unresolved":
            unresolved_reasons[reason] += 1
        else:
            events.append(
                HumanMessageEvent(
                    event_id=sha256(row[3].encode()).hexdigest(),
                    occurred_at_utc=row[4].astimezone(UTC),
                    canonical_locator=row[3],
                    provider=row[0],
                    source_session_id=row[1],
                    session_kind=row[6],
                    cwd=row[7],
                    repository=row[8],
                    submitted_by="human",
                    retention_status=retention_status,
                    physical_alias_count=int(row[11]),
                )
            )

    return EventExport(
        from_utc=lower,
        until_utc=upper,
        source_revision=source_revision,
        population=EventPopulation(
            scanned_content_rows=sum(content_counts.values()),
            scanned_logical_messages=len(identities),
            retained=retention_counts["retained"],
            excluded=retention_counts["excluded"],
            unresolved=retention_counts["unresolved"],
            excluded_by_reason=dict(sorted(excluded_reasons.items())),
            unresolved_by_reason=dict(sorted(unresolved_reasons.items())),
            content_rows_by_class=content_counts,
        ),
        events=tuple(events),
    )
