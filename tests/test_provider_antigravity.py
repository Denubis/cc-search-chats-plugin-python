"""Pure Antigravity admission and record projection."""

import hashlib
import json
from pathlib import Path

import pytest
from tests.conftest import ANTIGRAVITY_FIXTURES, ANTIGRAVITY_SESSION_IDS

from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    Provider,
    SessionKind,
    SubmittedBy,
    format_locator,
)
from cc_search_chats.providers.antigravity import (
    SCOPE_START,
    AdmissionOutcome,
    AntigravityDiagnosticCode,
    AntigravityParserState,
    AntigravitySessionContext,
    admit_antigravity_session,
    parse_antigravity_session,
)
from cc_search_chats.providers.source_discovery import (
    RecordEnvelope,
    read_bounded_jsonl,
)

SESSION_ID = ANTIGRAVITY_SESSION_IDS["october_human"]
RELATIVE = Path(SESSION_ID) / ".system_generated" / "logs" / "transcript_full.jsonl"
CONTEXT = AntigravitySessionContext(source_session_id=SESSION_ID)


def record(source: str, type_: str, created_at: str, **extra: object) -> dict:
    return {
        "source": source,
        "type": type_,
        "created_at": created_at,
        "status": "DONE",
        "step_index": 0,
        **extra,
    }


def user_input(text: str, created_at: str = "2026-10-03T09:15:00Z") -> dict:
    return record(
        "USER_EXPLICIT",
        "USER_INPUT",
        created_at,
        content=f"<USER_REQUEST>{text}</USER_REQUEST>",
    )


def raw_envelope(raw: bytes, *, ordinal: int, offset: int) -> RecordEnvelope:
    return RecordEnvelope(
        source_file_relative=RELATIVE,
        record_ordinal=ordinal,
        source_line=ordinal + 1,
        source_byte_offset=offset,
        raw_bytes=raw,
        raw_byte_length=len(raw),
        source_digest=hashlib.sha256(raw).hexdigest(),
    )


def envelopes(*payloads: object) -> tuple[RecordEnvelope, ...]:
    values: list[RecordEnvelope] = []
    offset = 0
    for ordinal, payload in enumerate(payloads):
        raw = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode()
        )
        values.append(raw_envelope(raw, ordinal=ordinal, offset=offset))
        offset += len(raw) + 1
    return tuple(values)


def fixture_envelopes(name: str) -> tuple[RecordEnvelope, ...]:
    path = (
        ANTIGRAVITY_FIXTURES
        / "sessions"
        / name
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    result = read_bounded_jsonl(
        path,
        source_file_relative=RELATIVE,
        target_size=path.stat().st_size,
    )
    return result.envelopes


def texts(parsed, content_class: ContentClass = ContentClass.PROSE) -> list[str]:
    return [
        message.text
        for message in parsed.messages
        if message.content_class is content_class
    ]


class TestAdmission:
    def test_october_human_session_is_admitted(self) -> None:
        decision = admit_antigravity_session(json.dumps(user_input("hello")).encode())
        assert decision.outcome is AdmissionOutcome.ADMITTED
        assert decision.code is None

    @pytest.mark.parametrize(
        ("created_at", "outcome"),
        [
            ("2026-09-30T23:59:59Z", AdmissionOutcome.EXCLUDED),
            ("2026-10-01T00:00:00Z", AdmissionOutcome.ADMITTED),
        ],
    )
    def test_scope_boundary_is_inclusive_at_the_constant(
        self, created_at: str, outcome: AdmissionOutcome
    ) -> None:
        assert SCOPE_START == "2026-10-01T00:00:00Z"
        decision = admit_antigravity_session(
            json.dumps(user_input("x", created_at)).encode()
        )
        assert decision.outcome is outcome

    def test_pre_october_session_is_excluded_with_its_code(self) -> None:
        decision = admit_antigravity_session(
            json.dumps(user_input("x", "2026-09-30T23:59:59Z")).encode()
        )
        assert (decision.outcome, decision.code) == (
            AdmissionOutcome.EXCLUDED,
            AntigravityDiagnosticCode.BEFORE_SCOPE_START,
        )

    def test_system_first_session_is_excluded_as_not_human_initiated(self) -> None:
        decision = admit_antigravity_session(
            json.dumps(
                record("SYSTEM", "SYSTEM_MESSAGE", "2026-10-05T10:00:00Z", content="x")
            ).encode()
        )
        assert (decision.outcome, decision.code) == (
            AdmissionOutcome.EXCLUDED,
            AntigravityDiagnosticCode.NOT_HUMAN_INITIATED,
        )

    @pytest.mark.parametrize(
        "raw",
        [
            b'{"hello": 1}',
            b"[]",
            b"not json",
            json.dumps(
                record("MODEL", "PLANNER_RESPONSE", "2026-10-05T10:00:00Z")
            ).encode(),
            json.dumps(
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": 1}
            ).encode(),
            json.dumps(
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": "2026-10-05 10:00:00",
                }
            ).encode(),
        ],
    )
    def test_unrecognised_first_records_block(self, raw: bytes) -> None:
        decision = admit_antigravity_session(raw)
        assert (decision.outcome, decision.code) == (
            AdmissionOutcome.BLOCKED,
            AntigravityDiagnosticCode.UNRECOGNISED_FIRST_RECORD,
        )


class TestProjection:
    def test_october_fixture_projects_only_visible_prose_and_tool_rows(self) -> None:
        parsed = parse_antigravity_session(
            fixture_envelopes("october_human"), context=CONTEXT
        )
        assert texts(parsed) == [
            "Please summarise the orchard ledger",
            "I will read the orchard ledger first.",
            "Now tally the apricot rows",
            "Tally complete: forty apricot rows <truncated 12 bytes>",
        ]
        assert texts(parsed, ContentClass.TOOL_NAME) == ["run_command view_file"]
        assert texts(parsed, ContentClass.TOOL_INPUT) == [
            (
                '{"CommandLine":"ls orchard","Cwd":"/synthetic/orchard"}\n'
                '{"AbsolutePath":"/synthetic/orchard/ledger.md"}'
            )
        ]
        everything = "\n".join(message.text for message in parsed.messages)
        for decoy in (
            "thinking-decoy-quince",
            "generic-decoy-damson",
            "metadata-decoy-pear",
            "settings-decoy-plum",
            "system-decoy-medlar",
            "checkpoint-decoy-sloe",
        ):
            assert decoy not in everything
        assert all(
            message.session_kind is SessionKind.PRIMARY
            and message.submitted_by is SubmittedBy.UNKNOWN
            and message.repository is None
            for message in parsed.messages
        )

    def test_roles_timestamps_and_epochs_follow_the_record_policy(self) -> None:
        parsed = parse_antigravity_session(
            fixture_envelopes("october_human"), context=CONTEXT
        )
        prose = [m for m in parsed.messages if m.content_class is ContentClass.PROSE]
        assert [(m.role, m.timestamp, m.conversation_epoch) for m in prose] == [
            ("user", "2026-10-03T09:15:00Z", 0),
            ("assistant", "2026-10-03T09:15:04Z", 0),
            ("user", "2026-10-03T09:21:00Z", 1),
            ("assistant", "2026-10-03T09:21:09Z", 1),
        ]
        assert parsed.next_state == AntigravityParserState(
            next_conversation_epoch=1, cwd="/synthetic/orchard"
        )

    def test_tool_rows_share_the_prose_row_identity(self) -> None:
        parsed = parse_antigravity_session(
            fixture_envelopes("october_human"), context=CONTEXT
        )
        by_class = {
            message.content_class: message
            for message in parsed.messages
            if message.identity.physical_aliases[0].record_ordinal == 1
        }
        assert set(by_class) == {
            ContentClass.PROSE,
            ContentClass.TOOL_NAME,
            ContentClass.TOOL_INPUT,
        }
        locators = {
            format_locator(message.identity.canonical_locator)
            for message in by_class.values()
        }
        logical_ids = {
            message.identity.logical_message_id for message in by_class.values()
        }
        assert len(locators) == 1
        assert len(logical_ids) == 1
        locator = by_class[ContentClass.PROSE].identity.canonical_locator
        assert locator.provider is Provider.ANTIGRAVITY
        assert locator.key_kind is LocatorKeyKind.ORDINAL
        assert locator.key == 1
        assert next(iter(logical_ids)) == f"record-1-{locator.record_digest}"

    def test_truncation_marker_is_kept_and_reported(self) -> None:
        parsed = parse_antigravity_session(
            fixture_envelopes("october_human"), context=CONTEXT
        )
        truncated = [
            d
            for d in parsed.diagnostics
            if d.code is AntigravityDiagnosticCode.TRUNCATED_PROSE
        ]
        assert [d.record_ordinal for d in truncated] == [6]
        assert "<truncated 12 bytes>" in texts(parsed)[-1]

    def test_malformed_line_is_skipped_and_consumes_its_ordinal(self) -> None:
        parsed = parse_antigravity_session(
            envelopes(
                user_input("first"),
                b"{not json",
                record(
                    "MODEL",
                    "PLANNER_RESPONSE",
                    "2026-10-03T09:15:04Z",
                    content="third",
                ),
            ),
            context=CONTEXT,
        )
        assert texts(parsed) == ["first", "third"]
        ordinals = [
            m.identity.physical_aliases[0].record_ordinal for m in parsed.messages
        ]
        assert ordinals == [0, 2]
        assert [d.code for d in parsed.diagnostics if d.record_ordinal == 1] == [
            AntigravityDiagnosticCode.MALFORMED_JSON
        ]

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (
                record("MODEL", "SOMETHING_NEW", "2026-10-03T09:15:04Z", content="x"),
                AntigravityDiagnosticCode.UNKNOWN_RECORD_PAIR,
            ),
            (
                record(
                    "USER_EXPLICIT",
                    "USER_INPUT",
                    "2026-10-03T09:15:04Z",
                    content="plain text without the wrapper",
                ),
                AntigravityDiagnosticCode.MISSING_USER_REQUEST,
            ),
            (
                record("USER_EXPLICIT", "USER_INPUT", "2026-10-03T09:15:04Z"),
                AntigravityDiagnosticCode.MISSING_REQUIRED_KEY,
            ),
            (
                record(
                    "MODEL",
                    "PLANNER_RESPONSE",
                    "2026-10-03T09:15:04Z",
                    tool_calls="not a list",
                ),
                AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
            ),
            (
                record(
                    "MODEL",
                    "PLANNER_RESPONSE",
                    "2026-10-03T09:15:04Z",
                    tool_calls=[{"args": {}}],
                ),
                AntigravityDiagnosticCode.INVALID_FIELD_TYPE,
            ),
            (
                record("MODEL", "PLANNER_RESPONSE", "2026-10-03 09:15:04", content="x"),
                AntigravityDiagnosticCode.INVALID_CREATED_AT,
            ),
            (
                {"source": "MODEL", "type": "GENERIC"},
                AntigravityDiagnosticCode.MISSING_REQUIRED_KEY,
            ),
            ([1, 2, 3], AntigravityDiagnosticCode.UNKNOWN_RECORD_PAIR),
        ],
    )
    def test_blocking_shapes_are_named(
        self, payload: object, code: AntigravityDiagnosticCode
    ) -> None:
        parsed = parse_antigravity_session(
            envelopes(user_input("ok"), payload), context=CONTEXT
        )
        assert texts(parsed) == ["ok"]
        assert [d.code for d in parsed.diagnostics if d.record_ordinal == 1] == [code]

    def test_extra_keys_and_missing_status_never_block(self) -> None:
        payload = {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-03T09:15:04Z",
            "content": "fine",
            "novel_key": {"anything": True},
        }
        parsed = parse_antigravity_session(
            envelopes(user_input("ok"), payload), context=CONTEXT
        )
        assert texts(parsed) == ["ok", "fine"]

    def test_blank_content_yields_no_row(self) -> None:
        parsed = parse_antigravity_session(
            envelopes(
                user_input("   "),
                record("MODEL", "PLANNER_RESPONSE", "2026-10-03T09:15:04Z", content=""),
                record("MODEL", "PLANNER_RESPONSE", "2026-10-03T09:15:05Z"),
            ),
            context=CONTEXT,
        )
        assert parsed.messages == ()
        assert {d.code for d in parsed.diagnostics} == {
            AntigravityDiagnosticCode.EMPTY_CONTENT
        }

    def test_two_batch_parse_equals_one_batch_parse(self) -> None:
        whole = fixture_envelopes("october_human")
        one = parse_antigravity_session(whole, context=CONTEXT)
        first = parse_antigravity_session(whole[:3], context=CONTEXT)
        second = parse_antigravity_session(
            whole[3:], context=CONTEXT, prior_state=first.next_state
        )
        assert first.messages + second.messages == one.messages
        assert second.next_state == one.next_state


class TestDerivedWorkingDirectory:
    def test_first_run_command_cwd_stamps_every_row(self) -> None:
        parsed = parse_antigravity_session(
            fixture_envelopes("october_human"), context=CONTEXT
        )
        assert {message.cwd for message in parsed.messages} == {"/synthetic/orchard"}
        assert parsed.next_state.cwd == "/synthetic/orchard"
        assert parsed.cwd_established is True

    def test_session_without_run_command_keeps_null(self) -> None:
        parsed = parse_antigravity_session(
            fixture_envelopes("boundary_admitted"), context=CONTEXT
        )
        assert {message.cwd for message in parsed.messages} == {None}
        assert parsed.next_state.cwd is None
        assert parsed.cwd_established is False

    def test_later_run_commands_do_not_override_the_first(self) -> None:
        parsed = parse_antigravity_session(
            envelopes(
                user_input("one"),
                record(
                    "MODEL",
                    "PLANNER_RESPONSE",
                    "2026-10-03T09:15:04Z",
                    tool_calls=[{"name": "run_command", "args": {"Cwd": "/first"}}],
                ),
                record(
                    "MODEL",
                    "PLANNER_RESPONSE",
                    "2026-10-03T09:15:05Z",
                    content="again",
                    tool_calls=[{"name": "run_command", "args": {"Cwd": "/second"}}],
                ),
            ),
            context=CONTEXT,
        )
        assert {message.cwd for message in parsed.messages} == {"/first"}

    def test_run_command_without_a_string_cwd_leaves_null(self) -> None:
        parsed = parse_antigravity_session(
            envelopes(
                user_input("one"),
                record(
                    "MODEL",
                    "PLANNER_RESPONSE",
                    "2026-10-03T09:15:04Z",
                    content="x",
                    tool_calls=[{"name": "run_command", "args": {"Cwd": 7}}],
                ),
            ),
            context=CONTEXT,
        )
        assert {message.cwd for message in parsed.messages} == {None}

    def test_split_after_the_run_command_carries_the_value_forward(self) -> None:
        whole = fixture_envelopes("october_human")
        first = parse_antigravity_session(whole[:2], context=CONTEXT)
        second = parse_antigravity_session(
            whole[2:], context=CONTEXT, prior_state=first.next_state
        )
        assert first.cwd_established is True
        assert second.cwd_established is False
        assert {m.cwd for m in first.messages + second.messages} == {
            "/synthetic/orchard"
        }
        assert second.next_state == first.next_state or (
            second.next_state.cwd == first.next_state.cwd
        )
