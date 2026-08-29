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
    ClaudeParserState,
    ClaudeSessionContext,
    parse_claude_session,
)
from cc_search_chats.providers.source_discovery import (
    RecordEnvelope,
    read_bounded_jsonl,
)

FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def envelope(
    payload: object, *, path: Path = Path("project/session.jsonl")
) -> RecordEnvelope:
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


def raw_envelope(raw_bytes: bytes) -> RecordEnvelope:
    """Build one envelope without normalizing escaped JSON test bytes."""
    return RecordEnvelope(
        source_file_relative=Path("project/session.jsonl"),
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
    @pytest.mark.parametrize(
        "payload",
        [
            {
                "type": "progress",
                "cwd": "/synthetic",
                "data": {},
                "gitBranch": "main",
                "isSidechain": False,
                "parentToolUseID": None,
                "parentUuid": None,
                "sessionId": "session",
                "timestamp": "2026-08-29T00:00:00Z",
                "toolUseID": "tool",
                "userType": "external",
                "uuid": "progress",
                "version": "1",
            },
            {
                "type": "attachment",
                "agentId": "agent",
                "attachment": {},
                "cwd": "/synthetic",
                "entrypoint": "cli",
                "gitBranch": "main",
                "isSidechain": True,
                "parentUuid": None,
                "sessionId": "session",
                "timestamp": "2026-08-29T00:00:00Z",
                "userType": "external",
                "uuid": "attachment",
                "version": "1",
            },
            {"type": "last-prompt", "leafUuid": "leaf", "sessionId": "session"},
            {
                "type": "file-history-snapshot",
                "isSnapshotUpdate": False,
                "messageId": "message",
                "snapshot": {},
            },
            {"type": "agent-setting", "agentSetting": "agent", "sessionId": "session"},
            {
                "type": "permission-mode",
                "permissionMode": "default",
                "sessionId": "session",
            },
            {"type": "mode", "mode": "default", "sessionId": "session"},
            {"type": "custom-title", "customTitle": "title", "sessionId": "session"},
            {
                "type": "fork-context-ref",
                "agentId": "agent",
                "contextLength": 1,
                "parentLastUuid": "parent-message",
                "parentSessionId": "parent-session",
            },
            {
                "type": "queue-operation",
                "content": "queued",
                "operation": "enqueue",
                "sessionId": "session",
                "timestamp": "2026-08-29T00:00:00Z",
            },
            {"type": "started", "agentId": "agent", "key": "key"},
            {"type": "ai-title", "aiTitle": "title", "sessionId": "session"},
        ],
    )
    def test_observed_ui_metadata_families_are_explicitly_excluded(
        self, payload: dict[str, object]
    ) -> None:
        result = parse_claude_session(
            (envelope(payload),),
            context=ClaudeSessionContext(source_session_id="session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.EXCLUDED_METADATA
        ]

    def test_non_text_tool_result_is_explicitly_excluded(self) -> None:
        result = parse_claude_session(
            (
                envelope(
                    {
                        "type": "user",
                        "uuid": "image-result",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool",
                                    "content": [
                                        {
                                            "type": "image",
                                            "source": {
                                                "type": "base64",
                                                "media_type": "image/png",
                                                "data": "synthetic",
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ),
            ),
            context=ClaudeSessionContext(source_session_id="session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.EXCLUDED_NON_TEXT_TOOL_RESULT
        ]

    def test_resumes_conversation_epoch_from_explicit_state(self) -> None:
        boundary_payload = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary-uuid",
            "timestamp": "2026-08-11T09:00:00Z",
            "compactMetadata": {"trigger": "auto", "preTokens": 50},
        }
        prefix = parse_claude_session(
            (envelope(boundary_payload),),
            context=ClaudeSessionContext(source_session_id="session"),
        )
        suffix_payload = {
            "type": "assistant",
            "uuid": "after-boundary",
            "timestamp": "2026-08-11T09:00:01Z",
            "message": {"role": "assistant", "content": "after boundary"},
        }

        suffix = parse_claude_session(
            (envelope(suffix_payload),),
            context=ClaudeSessionContext(source_session_id="session"),
            prior_state=prefix.next_state,
        )

        assert prefix.next_state == ClaudeParserState(
            next_conversation_epoch=1,
            seen_compaction_uuids=("boundary-uuid",),
        )
        assert [message.conversation_epoch for message in suffix.messages] == [1]
        assert suffix.next_state == prefix.next_state

    def test_duplicate_compaction_in_later_suffix_does_not_advance_twice(
        self,
    ) -> None:
        boundary_payload = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "duplicate-boundary",
            "timestamp": "2026-08-11T09:00:00Z",
            "compactMetadata": {"trigger": "auto", "preTokens": 50},
        }
        prefix = parse_claude_session(
            (envelope(boundary_payload),),
            context=ClaudeSessionContext(source_session_id="session"),
        )

        suffix = parse_claude_session(
            (envelope(boundary_payload),),
            context=ClaudeSessionContext(source_session_id="session"),
            prior_state=prefix.next_state,
        )

        assert suffix.boundaries == ()
        assert [diagnostic.code for diagnostic in suffix.diagnostics] == [
            ClaudeDiagnosticCode.DUPLICATE_COMPACT_BOUNDARY
        ]
        assert suffix.next_state == prefix.next_state

    def test_rejects_malformed_claude_continuation(self) -> None:
        with pytest.raises(ValueError, match="next_conversation_epoch"):
            ClaudeParserState(next_conversation_epoch=-1)
        with pytest.raises(ValueError, match="seen_compaction_uuids"):
            ClaudeParserState(seen_compaction_uuids=("bad:uuid",))

    def test_primary_fixture_emits_visible_prose_and_distinct_tool_rows(self) -> None:
        result = parse_fixture(
            "claude_primary.jsonl",
            session_id="claude-session-primary",
            source_file_relative=Path("project/claude-session-primary.jsonl"),
        )

        assert result.session_kind is SessionKind.PRIMARY
        assert [
            (message.content_class, message.text) for message in result.messages
        ] == [
            (ContentClass.PROSE, "visible primary user"),
            (ContentClass.PROSE, "visible assistant"),
            (ContentClass.TOOL_NAME, "Read"),
            (ContentClass.TOOL_INPUT, '{"file_path":"synthetic.txt"}'),
            (ContentClass.TOOL_OUTPUT, "synthetic tool output"),
            (ContentClass.PROSE, "visible after boundary"),
        ]
        assert all("[tool:" not in message.text for message in result.messages)
        assert {
            message.identity.canonical_locator.provider for message in result.messages
        } == {Provider.CLAUDE}
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

    def test_multiple_tools_share_one_row_per_content_class(self) -> None:
        result = parse_fixture(
            "claude_multiple_tools.jsonl",
            session_id="claude-multiple-tools",
            source_file_relative=Path("project/claude-multiple-tools.jsonl"),
        )

        assert [
            (message.content_class, message.text) for message in result.messages
        ] == [
            (ContentClass.TOOL_NAME, "Read\nGrep"),
            (
                ContentClass.TOOL_INPUT,
                '{"file_path":"one.txt"}\n{"pattern":"needle"}',
            ),
        ]
        assert any(
            diagnostic.code is ClaudeDiagnosticCode.INVALID_UNICODE
            for diagnostic in result.diagnostics
        )

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
        assert all(
            message.text != "synthetic injected compact summary"
            for message in result.messages
        )
        assert ClaudeDiagnosticCode.EXCLUDED_INJECTED in {
            diagnostic.code for diagnostic in result.diagnostics
        }

    def test_agent_origin_and_positive_native_harness_evidence_are_separate(
        self,
    ) -> None:
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
        assert all(
            message.submitted_by is not SubmittedBy.HUMAN for message in result.messages
        )

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

    @pytest.mark.parametrize("agent_id", [None, True, 1, [], {}, ""])
    def test_malformed_present_agent_id_fails_closed(self, agent_id: object) -> None:
        result = parse_claude_session(
            (
                envelope(
                    {
                        "type": "user",
                        "uuid": "malformed-agent-id",
                        "agentId": agent_id,
                        "message": {"role": "user", "content": "visible unknown"},
                    }
                ),
            ),
            context=ClaudeSessionContext(source_session_id="origin-session"),
        )

        assert result.session_kind is SessionKind.UNKNOWN
        assert result.messages[0].submitted_by is SubmittedBy.UNKNOWN


class TestClaudeFailClosedDiagnostics:
    def test_known_metadata_type_with_unobserved_shape_stays_unknown(self) -> None:
        result = parse_claude_session(
            (envelope({"type": "custom-title", "unexpected": "value"}),),
            context=ClaudeSessionContext(source_session_id="session"),
        )

        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.UNKNOWN_CONVERSATION_RECORD
        ]

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

    @pytest.mark.parametrize("escaped_surrogate", [b"\\ud800", b"\\udfff"])
    def test_escaped_lone_surrogate_prose_is_diagnostic(
        self, escaped_surrogate: bytes
    ) -> None:
        raw = (
            b'{"type":"user","uuid":"surrogate-message","message":'
            b'{"role":"user","content":"' + escaped_surrogate + b'"}}'
        )

        result = parse_claude_session(
            (raw_envelope(raw),),
            context=ClaudeSessionContext(source_session_id="surrogate-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.INVALID_UNICODE
        ]

    def test_escaped_lone_surrogate_in_serialized_tool_value_is_diagnostic(
        self,
    ) -> None:
        raw = (
            b'{"type":"assistant","uuid":"surrogate-tool","message":'
            b'{"role":"assistant","content":[{"type":"tool_use",'
            b'"name":"tool","input":{"invalid":"\\ud800"}}]}}'
        )

        result = parse_claude_session(
            (raw_envelope(raw),),
            context=ClaudeSessionContext(source_session_id="surrogate-tool-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.INVALID_UNICODE
        ]

    def test_empty_typed_prose_is_diagnostic(self) -> None:
        result = parse_claude_session(
            (
                envelope(
                    {
                        "type": "assistant",
                        "uuid": "empty-typed-message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": ""}],
                        },
                    }
                ),
            ),
            context=ClaudeSessionContext(source_session_id="empty-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.EMPTY_CONTENT
        ]

    @pytest.mark.parametrize(
        "content",
        [[], [{"type": "text", "text": ""}, {"type": "text", "text": ""}]],
    )
    def test_empty_typed_prose_collection_is_diagnostic(
        self, content: list[object]
    ) -> None:
        result = parse_claude_session(
            (
                envelope(
                    {
                        "type": "assistant",
                        "uuid": "empty-typed-prose-collection",
                        "message": {"role": "assistant", "content": content},
                    }
                ),
            ),
            context=ClaudeSessionContext(source_session_id="empty-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            ClaudeDiagnosticCode.EMPTY_CONTENT
        ]


visible_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=200,
)
non_boolean_json = st.recursive(
    st.none()
    | st.integers()
    | st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20),
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=8),
            children,
            max_size=3,
        )
    ),
    max_leaves=8,
)
unknown_block_type = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=30
).filter(
    lambda value: (
        value
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


@given(non_boolean_json)
@example("not-a-boolean")
def test_non_boolean_sidechain_metadata_never_classifies_primary(
    is_sidechain: object,
) -> None:
    result = parse_claude_session(
        (
            envelope(
                {
                    "type": "user",
                    "uuid": "malformed-sidechain",
                    "isSidechain": is_sidechain,
                    "message": {"role": "user", "content": "visible unknown"},
                }
            ),
        ),
        context=ClaudeSessionContext(source_session_id="origin-session"),
    )

    assert result.session_kind is SessionKind.UNKNOWN
    assert result.messages[0].session_kind is SessionKind.UNKNOWN


@given(
    unknown_block_type,
    st.recursive(
        st.none() | st.booleans() | st.integers(),
        lambda children: (
            st.lists(children, max_size=3)
            | st.dictionaries(st.text(max_size=8), children, max_size=3)
        ),
        max_leaves=8,
    ),
)
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
