"""Record-shape fingerprints carry key names only and canonicalise stably."""

import hashlib
import json

import pytest

from cc_search_chats.core.record_shape import (
    RecordShape,
    path_segment,
    record_shape,
    record_shape_from_json,
)

SENTINEL = "value-decoy-" + "x" * 200


def test_fingerprint_takes_envelope_keys_then_prefixed_payload_keys() -> None:
    shape = record_shape(
        "codex",
        "response_item/mystery",
        {"type": "response_item", "timestamp": SENTINEL, "payload": {}},
        {"type": "mystery", "id": SENTINEL, "nested": {"deep": SENTINEL}},
    )

    assert shape == RecordShape(
        provider="codex",
        record_path="response_item/mystery",
        keys=(
            "payload",
            "timestamp",
            "type",
            "payload.id",
            "payload.nested",
            "payload.type",
        ),
    )
    assert SENTINEL not in shape.canonical
    assert "deep" not in shape.canonical


def test_canonical_form_is_sorted_compact_json_and_digest_is_its_sha256() -> None:
    shape = RecordShape("claude", "future", ("b", "c"))

    assert shape.canonical == json.dumps(
        {"keys": ["b", "c"], "provider": "claude", "record_path": "future"},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert shape.digest == hashlib.sha256(shape.canonical.encode()).hexdigest()
    assert record_shape_from_json(shape.as_json()) == shape


def test_payload_absent_and_present_are_distinct_shapes() -> None:
    envelope = {"type": "turn_context", "timestamp": "t", "payload": {}}

    without = record_shape("codex", "turn_context", envelope)
    with_payload = record_shape("codex", "turn_context", envelope, {"cwd": "/x"})

    assert without.keys == ("payload", "timestamp", "type")
    assert with_payload.keys == ("payload", "timestamp", "type", "payload.cwd")
    assert without.digest != with_payload.digest


@pytest.mark.parametrize(
    "keys",
    [
        ("b", "a"),
        ("a", "a"),
        ("payload.a", "b"),
        ("a", "payload.b", "payload.a"),
    ],
)
def test_keys_must_be_two_sorted_unique_segments(keys: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="keys"):
        RecordShape("codex", "x", keys)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("additional_tools", "additional_tools"),
        ("", "untyped"),
        (None, "untyped"),
        (7, "untyped"),
        ("with/slash", "with_slash"),
    ],
)
def test_path_segment_names_only_string_types(value: object, expected: str) -> None:
    assert path_segment(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"provider": "codex", "record_path": "x"},
        {"provider": "codex", "record_path": "x", "keys": "a"},
        {"provider": "codex", "record_path": "x", "keys": [1]},
        {"provider": 1, "record_path": "x", "keys": []},
    ],
)
def test_json_round_trip_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError, match="shape"):
        record_shape_from_json(value)
