"""Corpus-wide unknown record shapes derived from current rows.

The summary is a bounded aggregate over ``source_file_current.unknown_shapes``
and ``source_failure_current.failure_shape``, grouped by shape in SQL. It
never fetches per-file maps into Python, and nothing maintains a separate shape
table, so removal, replacement or reparse of a file cannot leave a stale count.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cc_search_chats.core.record_shape import RecordShape, record_shape_from_json

if TYPE_CHECKING:
    import psycopg

UNKNOWN_SHAPE_WARNING_CODE = "unknown_record_shape"
ACKNOWLEDGED_DISPOSITION = "acknowledged"


@dataclass(frozen=True, slots=True)
class UnknownShapeReport:
    """One distinct unknown shape with its corpus-wide counts and first example."""

    shape: RecordShape
    record_count: int
    file_count: int
    first_source_root_id: str
    first_source_file_relative: str
    first_source_line: int
    dispositions: tuple[str, ...]
    parser_state_version: int
    note: str | None = None
    acknowledged: str | None = None

    @property
    def pending(self) -> bool:
        """Return whether any current occurrence is unacknowledged."""
        return any(
            disposition != ACKNOWLEDGED_DISPOSITION for disposition in self.dispositions
        )

    def as_json(self) -> dict[str, object]:
        """Return the fields shared by the warning and the status list entry."""
        return {
            "provider": self.shape.provider,
            "record_path": self.shape.record_path,
            "keys": list(self.shape.keys),
            "record_count": self.record_count,
            "file_count": self.file_count,
            "first_source_root_id": self.first_source_root_id,
            "first_source_file_relative": self.first_source_file_relative,
            "first_source_line": self.first_source_line,
            "dispositions": list(self.dispositions),
            "parser_state_version": self.parser_state_version,
            **(
                {"note": self.note, "acknowledged": self.acknowledged}
                if not self.pending
                else {}
            ),
        }

    def as_warning(self) -> dict[str, object]:
        """Return the schema-v5 warning entry for a pending shape."""
        return {"code": UNKNOWN_SHAPE_WARNING_CODE, **self.as_json()}


_SUMMARY_SQL = """
WITH per_file AS (
    SELECT entry.value -> 'shape' AS shape,
           (entry.value ->> 'count')::bigint AS record_count,
           source.source_root_id,
           source.source_file_relative,
           (entry.value ->> 'first_line')::bigint AS first_line,
           entry.value ->> 'disposition' AS disposition,
           source.parser_state_version,
           entry.value ->> 'note' AS note,
           entry.value ->> 'acknowledged' AS acknowledged
    FROM cc_search_chats.source_file_current AS source
    CROSS JOIN LATERAL jsonb_each(source.unknown_shapes) AS entry
    UNION ALL
    SELECT failure.failure_shape,
           1::bigint,
           failure.source_root_id,
           failure.source_file_relative,
           failure.failure_source_line,
           'blocked',
           failure.parser_state_version,
           NULL,
           NULL
    FROM cc_search_chats.source_failure_current AS failure
    WHERE failure.failure_shape IS NOT NULL
)
SELECT shape,
       sum(record_count)::bigint,
       count(DISTINCT (source_root_id, source_file_relative))::bigint,
       (array_agg(source_root_id
                  ORDER BY source_root_id, source_file_relative, first_line))[1],
       (array_agg(source_file_relative
                  ORDER BY source_root_id, source_file_relative, first_line))[1],
       (array_agg(first_line
                  ORDER BY source_root_id, source_file_relative, first_line))[1],
       array_agg(DISTINCT disposition ORDER BY disposition),
       max(parser_state_version),
       max(note),
       max(acknowledged)
FROM per_file
GROUP BY shape
ORDER BY shape ->> 'provider', shape ->> 'record_path', shape::text
"""


def unknown_shape_summary(
    connection: psycopg.Connection,
) -> tuple[UnknownShapeReport, ...]:
    """Aggregate every unknown shape in current rows, one report per shape."""
    return tuple(
        UnknownShapeReport(
            shape=record_shape_from_json(shape),
            record_count=int(record_count),
            file_count=int(file_count),
            first_source_root_id=first_root,
            first_source_file_relative=first_file,
            first_source_line=int(first_line),
            dispositions=tuple(dispositions),
            parser_state_version=int(parser_state_version),
            note=note,
            acknowledged=acknowledged,
        )
        for (
            shape,
            record_count,
            file_count,
            first_root,
            first_file,
            first_line,
            dispositions,
            parser_state_version,
            note,
            acknowledged,
        ) in connection.execute(_SUMMARY_SQL)
    )
