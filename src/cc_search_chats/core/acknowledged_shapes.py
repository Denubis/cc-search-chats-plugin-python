"""Closed parsing of the operator's acknowledged-shapes file.

The file is operator-owned and read only by ``index``. It names unknown record
shapes the operator has looked at and does not care about, by exact
``(provider, record_path, keys)``. Anything that is not exactly the documented
format fails closed, naming the entry and field, so a typo never silently
un-acknowledges or over-acknowledges a shape.
"""

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from cc_search_chats.core.identity import Provider
from cc_search_chats.core.record_shape import RecordShape

if TYPE_CHECKING:
    from collections.abc import Mapping

ACKNOWLEDGED_SHAPES_FILENAME = "acknowledged-shapes.toml"
_APPLICATION_DIRECTORY = "cc-search-chats"
_REQUIRED_FIELDS = ("provider", "record_path", "keys", "note", "acknowledged")


class AcknowledgedShapesError(ValueError):
    """The acknowledgement file cannot be trusted; the index run must not start."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AcknowledgedShape:
    """One operator acknowledgement: the exact shape, a note and a date."""

    shape: RecordShape
    note: str
    acknowledged: date


def acknowledged_shapes_path(environ: Mapping[str, str], *, home: Path) -> Path:
    """Resolve ``$XDG_CONFIG_HOME/cc-search-chats/acknowledged-shapes.toml``."""
    configured = environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured else home / ".config"
    return base / _APPLICATION_DIRECTORY / ACKNOWLEDGED_SHAPES_FILENAME


def _entry_field(
    path: Path, index: int, entry: Mapping[str, object], field: str
) -> object:
    if field not in entry:
        raise AcknowledgedShapesError(path, f"shape[{index}]: missing field {field}")
    return entry[field]


def _entry_text(path: Path, index: int, entry: Mapping[str, object], field: str) -> str:
    value = _entry_field(path, index, entry, field)
    if not isinstance(value, str) or not value:
        raise AcknowledgedShapesError(
            path, f"shape[{index}].{field} must be a nonempty string"
        )
    return value


def _entry_keys(path: Path, index: int, entry: Mapping[str, object]) -> tuple[str, ...]:
    value = _entry_field(path, index, entry, "keys")
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(key, str) or not key for key in value)
    ):
        raise AcknowledgedShapesError(
            path, f"shape[{index}].keys must be a nonempty list of strings"
        )
    return tuple(value)


def _parse_entry(path: Path, index: int, raw: object) -> AcknowledgedShape:
    if not isinstance(raw, dict):
        raise AcknowledgedShapesError(path, f"shape[{index}] must be a table")
    for field in raw:
        if field not in _REQUIRED_FIELDS:
            raise AcknowledgedShapesError(
                path, f"shape[{index}]: unknown field {field}"
            )
    provider = _entry_text(path, index, raw, "provider")
    if provider not in {member.value for member in Provider}:
        raise AcknowledgedShapesError(
            path, f"shape[{index}].provider {provider!r} is not a known provider"
        )
    record_path = _entry_text(path, index, raw, "record_path")
    keys = _entry_keys(path, index, raw)
    try:
        shape = RecordShape(provider=provider, record_path=record_path, keys=keys)
    except ValueError as error:
        raise AcknowledgedShapesError(path, f"shape[{index}].keys {error}") from error
    note = _entry_text(path, index, raw, "note")
    acknowledged = _entry_field(path, index, raw, "acknowledged")
    if not isinstance(acknowledged, date) or type(acknowledged) is not date:
        raise AcknowledgedShapesError(
            path, f"shape[{index}].acknowledged must be a TOML local date"
        )
    return AcknowledgedShape(shape=shape, note=note, acknowledged=acknowledged)


def parse_acknowledged_shapes(
    text: str, *, path: Path
) -> tuple[AcknowledgedShape, ...]:
    """Parse the document; raise ``AcknowledgedShapesError`` for any deviation."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AcknowledgedShapesError(path, f"not valid TOML: {error}") from error
    for key in document:
        if key != "shape":
            raise AcknowledgedShapesError(
                path, f"top-level key other than shape: {key}"
            )
    raw_entries = document.get("shape", [])
    if not isinstance(raw_entries, list):
        raise AcknowledgedShapesError(path, "shape must be an array of tables")
    entries: list[AcknowledgedShape] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(raw_entries):
        entry = _parse_entry(path, index, raw)
        digest = entry.shape.digest
        if digest in seen:
            raise AcknowledgedShapesError(
                path, f"shape[{index}] duplicates shape[{seen[digest]}]"
            )
        seen[digest] = index
        entries.append(entry)
    return tuple(entries)


def load_acknowledged_shapes(path: Path) -> tuple[AcknowledgedShape, ...]:
    """Read and parse the file; absent acknowledges nothing, unreadable fails."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError) as error:
        raise AcknowledgedShapesError(path, f"could not be read: {error}") from error
    return parse_acknowledged_shapes(text, path=path)
