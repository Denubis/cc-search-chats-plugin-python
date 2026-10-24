"""Provider-neutral key-name fingerprints for unrecognised native records.

A shape names the record family that failed recognition and the key names it
carried. It never holds a value: no prose, no identifiers, no tool definitions.
Two records with the same provider, record path and key names are one shape.

``keys`` has two sorted segments. The first holds the top-level key names of
the unit that failed recognition (a whole native record, or a content block).
The second holds the top-level key names of that unit's nested payload, each
prefixed ``payload.``, when the unit wraps one (Codex ``payload``, the Claude
``message`` or ``compactMetadata`` object). A content block has no nested
payload, so its fingerprint is the block's own key names.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

PAYLOAD_KEY_PREFIX = "payload."
_UNTYPED_SEGMENT = "untyped"


def path_segment(value: object) -> str:
    """Return one record-path segment for an untrusted ``type``-like value."""
    if not isinstance(value, str) or not value:
        return _UNTYPED_SEGMENT
    return value.replace("/", "_")


@dataclass(frozen=True, slots=True)
class RecordShape:
    """One unrecognised record family identified by key names only."""

    provider: str
    record_path: str
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject fingerprints that are not two sorted, duplicate-free segments."""
        if not isinstance(self.keys, tuple) or any(
            not isinstance(key, str) for key in self.keys
        ):
            raise ValueError("shape keys must be a tuple of strings")
        plain = [key for key in self.keys if not key.startswith(PAYLOAD_KEY_PREFIX)]
        nested = [key for key in self.keys if key.startswith(PAYLOAD_KEY_PREFIX)]
        if self.keys != (*sorted(set(plain)), *sorted(set(nested))):
            raise ValueError(
                "shape keys must be sorted unique envelope keys followed by "
                "sorted unique payload-prefixed keys"
            )

    def as_json(self) -> dict[str, object]:
        """Return the JSON object form persisted and reported for this shape."""
        return {
            "provider": self.provider,
            "record_path": self.record_path,
            "keys": list(self.keys),
        }

    @property
    def canonical(self) -> str:
        """Return the canonical compact JSON string that identifies this shape."""
        return json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        """Return the SHA-256 hex digest of the canonical string."""
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()


def record_shape(
    provider: str,
    record_path: str,
    envelope: Mapping[str, object] | None,
    payload: Mapping[str, object] | None = None,
) -> RecordShape:
    """Fingerprint one unit from its key names; values are never read."""
    plain = sorted(set(envelope)) if envelope is not None else []
    nested = (
        sorted({f"{PAYLOAD_KEY_PREFIX}{key}" for key in payload})
        if payload is not None
        else []
    )
    return RecordShape(
        provider=provider, record_path=record_path, keys=(*plain, *nested)
    )


def record_shape_from_json(value: object) -> RecordShape:
    """Rebuild a persisted shape, rejecting anything that is not one."""
    if not isinstance(value, Mapping):
        raise ValueError(  # noqa: TRY004  # persisted-state ValueError contract
            "persisted shape must be an object"
        )
    provider = value.get("provider")
    record_path = value.get("record_path")
    keys = value.get("keys")
    if not isinstance(provider, str) or not isinstance(record_path, str):
        raise ValueError(  # noqa: TRY004  # persisted-state ValueError contract
            "persisted shape provider and record_path must be strings"
        )
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise ValueError("persisted shape keys must be a string array")
    return RecordShape(provider=provider, record_path=record_path, keys=tuple(keys))
