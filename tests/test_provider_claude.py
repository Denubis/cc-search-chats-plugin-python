"""Fail-closed Claude provider adapter tests."""

import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    Provider,
    SessionKind,
    SubmittedBy,
)
from cc_search_chats.providers.claude import (
    ClaudeDiagnosticCode,
    ClaudeSessionContext,
    parse_claude_session,
)
from cc_search_chats.providers.source_discovery import (
    RecordEnvelope,
    read_bounded_jsonl,
)

FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def envelope(payload: object, *, path: Path = Path("project/session.jsonl")) -> RecordEnvelope:
    """Build one Task 2 envelope from a synthetic JSON value."""
    raw_bytes = json.dumps(payload, ensure_ascii=False).encode()
    return RecordEnvelope(
        source_file_relative=path,
        record_ordinal=0,
        source_line=1,
        source_byte_offset=0,
        raw_bytes=raw_bytes,
        raw_byte_length=len(raw_bytes),
        source_digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def parse_fixture(
    name: str,
    *,
    session_id: str,
    source_file_relative: Path,
):
    """Read a synthetic fixture through Task 2 before adapting it."""
    path = FIXTURES / name
    read = read_bounded_jsonl(
        path,
        source_file_relative=source_file_relative,
        target_size=path.stat().st_size,
    )
    return parse_claude_session(
        read.envelopes,
        context=ClaudeSessionContext(source_session_id=session_id),
    )


class TestClaudeRecognizedShapes:
    def test_primary_fixture_emits_visible_prose_and_distinct_tool_rows(self) -> None:
        result = parse_fixture(
            "claude_primary.jsonl",
            session_id="claude-session-primary",
            source_file_relative=Path("project/claude-session-primary.jsonl"),
        )

        assert result.session_kind is SessionKind.PRIMARY
        assert [(message.content_class, message.text) for message in result.messages] == [
            (ContentClass.PROSE, "visible primary user"),
            (ContentClass.PROSE, "visible assistant"),
            (ContentClass.TOOL_NAME, "Read"),
            (ContentClass.TOOL_INPUT, '{"file_path":"synthetic.txt"}'),
            (ContentClass.TOOL_OUTPUT, "synthetic tool output"),
            (ContentClass.PROSE, "visible after boundary"),
        ]
        assert all("[tool:" not in message.text for message in result.messages)
        assert {message.identity.canonical_locator.provider for message in result.messages} == {
            Provider.CLAUDE
        }
        assert all(
            message.identity.canonical_locator.key_kind is LocatorKeyKind.UUID
            for message in result.messages
        )
        assert [message.conversation_epoch for message in result.messages] == [
            0,
            0,
            0,
            0,
            0,
            1,
        ]

    def test_compact_boundary_is_retained_but_never_searchable(self) -> None:
        result = parse_fixture(
            "claude_primary.jsonl",
            session_id="claude-session-primary",
            source_file_relative=Path("project/claude-session-primary.jsonl"),
        )

        assert len(result.boundaries) == 1
        boundary = result.boundaries[0]
        assert boundary.conversation_epoch == 1
        assert boundary.trigger == "auto"
        assert boundary.token_count == 50
        assert all(message.text != "synthetic injected compact summary" for message in result.messages)
        assert ClaudeDiagnosticCode.EXCLUDED_INJECTED in {
            diagnostic.code for diagnostic in result.diagnostics
        }

    def test_agent_origin_and_positive_native_harness_evidence_are_separate(self) -> None:
        result = parse_fixture(
            "claude_agent.jsonl",
            session_id="claude-agent-session",
            source_file_relative=Path(
                "project/parent-session/subagents/claude-agent-session.jsonl"
            ),
        )

        assert result.session_kind is SessionKind.AGENT
        user, assistant = result.messages
        assert user.role == "user"
        assert user.submitted_by is SubmittedBy.IDENTIFIED_HARNESS
        assert user.submission_evidence == ("claude:isSidechain+agentId",)
        assert user.submission_match_cardinality == 1
        assert assistant.submitted_by is SubmittedBy.UNKNOWN
        assert all(message.submitted_by is not SubmittedBy.HUMAN for message in result.messages)

    @pytest.mark.parametrize(
        ("path", "is_sidechain"),
        [
            (Path("unexpected/nesting/session.jsonl"), False),
            (Path("project/session.jsonl"), True),
            (Path("project/parent/subagents/agent.jsonl"), False),
        ],
    )
    def test_unrecognized_or_contradictory_origin_is_unknown(
        self, path: Path, is_sidechain: bool
    ) -> None:
        record = {
            "type": "user",
            "uuid": "origin-message",
            "isSidechain": is_sidechain,
            "message": {"role": "user", "content": "visible but not primary"},
        }

        result = parse_claude_session(
            (envelope(record, path=path),),
            context=ClaudeSessionContext(source_session_id="origin-session"),
        )

        assert result.session_kind is SessionKind.UNKNOWN
        assert result.messages[0].session_kind is SessionKind.UNKNOWN
        assert result.messages[0].submitted_by is SubmittedBy.UNKNOWN


class TestClaudeFailClosedDiagnostics:
    def test_unsupported_fixture_distinguishes_named_exclusions(self) -> None:
        result = parse_fixture(
            "claude_unsupported_shapes.jsonl",
            session_id="claude-unsupported-session",
            source_file_relative=Path("project/claude-unsupported-session.jsonl"),
        )

        assert result.messages == ()
        assert {diagnostic.code for diagnostic in result.diagnostics} == {
            ClaudeDiagnosticCode.MALFORMED_JSON,
            ClaudeDiagnosticCode.MISSING_MESSAGE,
            ClaudeDiagnosticCode.NON_OBJECT_MESSAGE,
            ClaudeDiagnosticCode.UNKNOWN_ROLE,
            ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
            ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD,
            ClaudeDiagnosticCode.EXCLUDED_SYSTEM,
            ClaudeDiagnosticCode.EXCLUDED_INJECTED,
            ClaudeDiagnosticCode.MISSING_MESSAGE_UUID,
            ClaudeDiagnosticCode.INVALID_COMPACT_BOUNDARY,
            ClaudeDiagnosticCode.EXCLUDED_REASONING,
        }
        assert result.boundaries == ()

    def test_missing_uuid_never_falls_back_to_ordinal_digest(self) -> None:
        result = parse_claude_session(
            (
                envelope(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "not searchable"},
                    }
                ),
            ),
            context=ClaudeSessionContext(source_session_id="session-without-uuid"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.MISSING_MESSAGE_UUID
        ]


visible_text = st.text(min_size=1, max_size=200)
unknown_block_type = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=30
).filter(
    lambda value: value
    not in {
        "text",
        "tool_use",
        "tool_result",
        "thinking",
        "reasoning",
        "system",
        "developer",
        "injected",
    }
)


@given(visible_text)
@example("\nleading and trailing\n")
def test_generated_visible_text_is_preserved_exactly(text: str) -> None:
    result = parse_claude_session(
        (
            envelope(
                {
                    "type": "user",
                    "uuid": "property-message",
                    "message": {"role": "user", "content": text},
                }
            ),
        ),
        context=ClaudeSessionContext(source_session_id="property-session"),
    )

    assert [message.text for message in result.messages] == [text]


@given(unknown_block_type, st.recursive(st.none() | st.booleans() | st.integers(), lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=8), children, max_size=3), max_leaves=8))
def test_arbitrary_unrecognized_content_never_becomes_searchable(
    block_type: str, payload: object
) -> None:
    result = parse_claude_session(
        (
            envelope(
                {
                    "type": "assistant",
                    "uuid": "unknown-property-message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": block_type, "payload": payload}],
                    },
                }
            ),
        ),
        context=ClaudeSessionContext(source_session_id="property-session"),
    )

    assert result.messages == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        ClaudeDiagnosticCode.UNKNOWN_CONTENT_BLOCK
    ]
