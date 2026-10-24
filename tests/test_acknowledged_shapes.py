"""The operator acknowledgement file parses closed and matches shapes exactly."""

from datetime import date
from pathlib import Path

import pytest

from cc_search_chats.core.acknowledged_shapes import (
    ACKNOWLEDGED_SHAPES_FILENAME,
    AcknowledgedShape,
    AcknowledgedShapesError,
    acknowledged_shapes_path,
    load_acknowledged_shapes,
    parse_acknowledged_shapes,
)
from cc_search_chats.core.record_shape import RecordShape

VALID = """
[[shape]]
provider = "codex"
record_path = "response_item/additional_tools"
keys = ["id", "role", "tools", "type"]
note = "developer-role tool definitions; no prose"
acknowledged = 2026-10-24
"""
PATH = Path("/synthetic/acknowledged-shapes.toml")


def test_valid_file_parses_into_exact_shapes() -> None:
    assert parse_acknowledged_shapes(VALID, path=PATH) == (
        AcknowledgedShape(
            shape=RecordShape(
                "codex",
                "response_item/additional_tools",
                ("id", "role", "tools", "type"),
            ),
            note="developer-role tool definitions; no prose",
            acknowledged=date(2026, 10, 24),
        ),
    )


def test_empty_and_absent_files_acknowledge_nothing(tmp_path: Path) -> None:
    assert parse_acknowledged_shapes("", path=PATH) == ()
    assert load_acknowledged_shapes(tmp_path / "missing.toml") == ()


def test_load_reads_the_named_file(tmp_path: Path) -> None:
    path = tmp_path / ACKNOWLEDGED_SHAPES_FILENAME
    path.write_text(VALID, encoding="utf-8")

    [entry] = load_acknowledged_shapes(path)

    assert entry.shape.record_path == "response_item/additional_tools"


def test_unreadable_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / ACKNOWLEDGED_SHAPES_FILENAME
    path.write_text(VALID, encoding="utf-8")
    path.chmod(0)
    try:
        with pytest.raises(AcknowledgedShapesError, match="could not be read"):
            load_acknowledged_shapes(path)
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[[shape]\nprovider = 'codex'", "not valid TOML"),
        ('[[other]]\nprovider = "codex"', "top-level key other"),
        ('shape = "codex"', "shape must be an array of tables"),
        (
            VALID.replace('note = "developer-role tool definitions; no prose"\n', ""),
            r"shape\[0\]: missing field note",
        ),
        (
            VALID.replace('provider = "codex"', 'provider = "gemini"'),
            r"shape\[0\].provider",
        ),
        (VALID.replace('["id", "role", "tools", "type"]', "[]"), r"shape\[0\].keys"),
        (
            VALID.replace('["id", "role", "tools", "type"]', '["role", "id"]'),
            r"shape\[0\].keys",
        ),
        (
            VALID.replace('["id", "role", "tools", "type"]', '["id", 1]'),
            r"shape\[0\].keys",
        ),
        (
            VALID.replace("acknowledged = 2026-10-24", 'acknowledged = "2026-10-24"'),
            r"shape\[0\].acknowledged",
        ),
        (
            VALID.replace(
                'record_path = "response_item/additional_tools"', 'record_path = ""'
            ),
            r"shape\[0\].record_path",
        ),
        (VALID + 'reason = "extra"\n', r"shape\[0\]: unknown field reason"),
        (VALID + VALID, r"shape\[1\] duplicates shape\[0\]"),
    ],
)
def test_malformed_files_fail_closed_naming_the_entry(text: str, match: str) -> None:
    with pytest.raises(AcknowledgedShapesError, match=match) as raised:
        parse_acknowledged_shapes(text, path=PATH)
    assert raised.value.path == PATH


def test_path_honours_xdg_config_home_and_defaults_to_dot_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert (
        acknowledged_shapes_path({"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home=home)
        == tmp_path / "xdg" / "cc-search-chats" / ACKNOWLEDGED_SHAPES_FILENAME
    )
    assert acknowledged_shapes_path({}, home=home) == (
        home / ".config" / "cc-search-chats" / ACKNOWLEDGED_SHAPES_FILENAME
    )
    assert acknowledged_shapes_path({"XDG_CONFIG_HOME": ""}, home=home) == (
        home / ".config" / "cc-search-chats" / ACKNOWLEDGED_SHAPES_FILENAME
    )
