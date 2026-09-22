"""Human freshness reporting and elapsed time across local clock changes."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cc_search_chats import cli


def _envelope(*, files=74, issues=None):
    return {
        "index_state": {
            "made_at": "2026-09-22T03:38:00+10:00",
            "now": "2026-09-22T18:38:00+10:00",
            "age_ms": 54_000_000,
            "unindexed": {"files": files, "directories": 5, "bytes": 100},
            "unindexed_reason": None,
        },
        "coverage": {
            "source_issues": issues
            or {
                "retry_after_parser_update": 0,
                "retryable_failures": 0,
                "needs_attention": 0,
            }
        },
    }


def test_search_reports_snapshot_age_without_global_missing_chat_alarm(capsys):
    cli._print_index_state_header(_envelope())
    assert capsys.readouterr().out.splitlines() == [
        "Using index from 2026-09-22 03:38:00 +10:00 (15h 0m ago).",
        "Source updates are awaiting refresh.",
    ]


def test_status_reports_source_files_as_changes_not_missing_sessions(capsys):
    cli._print_index_state_header(_envelope(), details=True)
    assert capsys.readouterr().out.splitlines() == [
        "Using index from 2026-09-22 03:38:00 +10:00 (15h 0m ago).",
        "74 source files have content outside this snapshot (5 directories).",
        "These include new files and updates to previously indexed files; counts are corpus-wide.",
        "Run `cc-search-chats index` to refresh.",
    ]


@pytest.mark.parametrize(
    ("issues", "expected"),
    [
        (
            {
                "retry_after_parser_update": 59,
                "retryable_failures": 0,
                "needs_attention": 0,
            },
            "Parser updates will retry previously blocked sources on the next index run.",
        ),
        (
            {
                "retry_after_parser_update": 0,
                "retryable_failures": 0,
                "needs_attention": 1,
            },
            "Some sources could not be processed; see `cc-search-chats index --status`.",
        ),
        (
            {
                "retry_after_parser_update": 0,
                "retryable_failures": 1,
                "needs_attention": 0,
            },
            "Some sources could not be read; a later index run can retry them.",
        ),
    ],
)
def test_search_distinguishes_source_issue_actions(capsys, issues, expected):
    cli._print_index_state_header(_envelope(files=0, issues=issues))
    assert capsys.readouterr().out.splitlines() == [
        "Using index from 2026-09-22 03:38:00 +10:00 (15h 0m ago).",
        expected,
    ]


@pytest.mark.parametrize(
    ("now_text", "then_text", "expected_age"),
    [
        ("2026-10-04T03:30:00+11:00", "2026-10-04T01:30:00+10:00", 3_600_000),
        ("2026-04-05T03:30:00+10:00", "2026-04-05T01:30:00+11:00", 10_800_000),
    ],
)
def test_snapshot_preserves_historical_offset_and_actual_elapsed_time(
    monkeypatch, now_text, then_text, expected_age
):
    fixed_now = datetime.fromisoformat(now_text)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    with monkeypatch.context() as local:
        local.setenv("TZ", "Australia/Sydney")
        time.tzset()
        local.setattr(cli, "datetime", FrozenDatetime)
        try:
            then = datetime.fromisoformat(then_text).astimezone(
                ZoneInfo("Australia/Sydney")
            )
            now, made_at, age_ms = cli._corpus_times(then)
            assert now.isoformat() == now_text
            assert made_at is not None
            assert made_at.isoformat() == then_text
            assert age_ms == expected_age
        finally:
            local.undo()
            time.tzset()
