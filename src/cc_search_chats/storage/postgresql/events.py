"""Bounded, content-free export of canonical human-message events."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

import psycopg

from cc_search_chats.storage.postgresql.guardrails import queued_read_operation

_EVENT_PAGE_SIZE = 1_000


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
    """One corpus-pinned, half-open human-message event export."""

    from_utc: datetime
    until_utc: datetime
    source_corpus_generation: int
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


def _message_rows(
    connection: psycopg.Connection,
    *,
    lower: datetime,
    upper: datetime,
):
    """Yield bounded primary-key pages without a corpus-sized SQL sort."""
    after = ("", "", "", "")
    while True:
        rows = connection.execute(
            """
            SELECT message.provider, message.source_session_id,
                   message.logical_message_id, message.content_class,
                   message.canonical_locator,
                   message.timestamp_text::timestamptz, message.role,
                   message.session_kind, message.cwd, message.repository,
                   message.submitted_by,
                   COALESCE(
                       (
                           SELECT jsonb_agg(jsonb_build_array(
                               alias.source_root_id,
                               alias.source_file_relative,
                               alias.record_ordinal
                           ))
                           FROM cc_search_chats.physical_alias_current AS alias
                           WHERE (alias.provider, alias.source_session_id,
                                  alias.logical_message_id,
                                  alias.content_class) =
                                 (message.provider, message.source_session_id,
                                  message.logical_message_id,
                                  message.content_class)
                       ),
                       '[]'::jsonb
                   ) AS physical_aliases
            FROM cc_search_chats.message_current AS message
            WHERE message.timestamp_text::timestamptz >= %s
              AND message.timestamp_text::timestamptz < %s
              AND (message.provider, message.source_session_id,
                   message.logical_message_id, message.content_class) >
                  (%s, %s, %s, %s)
            ORDER BY message.provider, message.source_session_id,
                     message.logical_message_id, message.content_class
            LIMIT %s
            """,
            (lower, upper, *after, _EVENT_PAGE_SIZE),
        ).fetchall()
        if not rows:
            return
        yield from rows
        last = rows[-1]
        after = (last[0], last[1], last[2], last[3])


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
    generation_row = connection.execute(
        "SELECT current_corpus_generation "
        "FROM cc_search_chats.corpus_state WHERE singleton"
    ).fetchone()
    if generation_row is None or generation_row[0] is None:
        raise ValueError("event export requires a selected corpus generation")
    source_corpus_generation = int(generation_row[0])

    content_counts: Counter[str] = Counter()
    events: list[tuple[datetime, str, str, str, HumanMessageEvent]] = []
    retention_counts: Counter[str] = Counter()
    excluded_reasons: Counter[str] = Counter()
    unresolved_reasons: Counter[str] = Counter()
    scanned_logical_messages = 0
    current_identity: tuple[str, str, str] | None = None
    current_metadata: (
        tuple[str, datetime, str, str, str | None, str | None, str] | None
    ) = None
    current_has_prose = False
    current_aliases: set[tuple[str, str, int]] = set()

    def finish_message() -> None:
        nonlocal scanned_logical_messages
        if current_identity is None or current_metadata is None:
            return
        scanned_logical_messages += 1
        (
            canonical_locator,
            occurred_at,
            role,
            session_kind,
            cwd,
            repository,
            submitted_by,
        ) = current_metadata
        retention_status, reason = _retention(
            submitted_by,
            role,
            session_kind,
            current_has_prose,
        )
        retention_counts[retention_status] += 1
        if retention_status == "excluded":
            excluded_reasons[reason] += 1
        elif retention_status == "unresolved":
            unresolved_reasons[reason] += 1
        else:
            provider, source_session_id, logical_message_id = current_identity
            events.append(
                (
                    occurred_at,
                    provider,
                    source_session_id,
                    logical_message_id,
                    HumanMessageEvent(
                        event_id=sha256(canonical_locator.encode()).hexdigest(),
                        occurred_at_utc=occurred_at.astimezone(UTC),
                        canonical_locator=canonical_locator,
                        provider=provider,
                        source_session_id=source_session_id,
                        session_kind=session_kind,
                        cwd=cwd,
                        repository=repository,
                        submitted_by="human",
                        retention_status=retention_status,
                        physical_alias_count=len(current_aliases),
                    ),
                )
            )

    for row in _message_rows(connection, lower=lower, upper=upper):
        identity = (row[0], row[1], row[2])
        metadata = (row[4], row[5], row[6], row[7], row[8], row[9], row[10])
        if identity != current_identity:
            finish_message()
            current_identity = identity
            current_metadata = metadata
            current_has_prose = False
            current_aliases = set()
        elif current_metadata is None:
            raise RuntimeError("event metadata state is missing for an active identity")
        elif metadata != current_metadata:
            if (
                metadata[:4] + metadata[5:]
                != current_metadata[:4] + current_metadata[5:]
            ):
                raise ValueError(
                    "event export found conflicting metadata for one logical message"
                )
            current_metadata = (
                current_metadata[0],
                current_metadata[1],
                current_metadata[2],
                current_metadata[3],
                None,
                current_metadata[5],
                current_metadata[6],
            )
        content_counts[row[3]] += 1
        current_has_prose = current_has_prose or row[3] == "prose"
        for source_root_id, source_file_relative, record_ordinal in row[11]:
            current_aliases.add((source_root_id, source_file_relative, record_ordinal))
    finish_message()
    events.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    return EventExport(
        from_utc=lower,
        until_utc=upper,
        source_corpus_generation=source_corpus_generation,
        population=EventPopulation(
            scanned_content_rows=sum(content_counts.values()),
            scanned_logical_messages=scanned_logical_messages,
            retained=retention_counts["retained"],
            excluded=retention_counts["excluded"],
            unresolved=retention_counts["unresolved"],
            excluded_by_reason=dict(sorted(excluded_reasons.items())),
            unresolved_by_reason=dict(sorted(unresolved_reasons.items())),
            content_rows_by_class=dict(sorted(content_counts.items())),
        ),
        events=tuple(item[4] for item in events),
    )
