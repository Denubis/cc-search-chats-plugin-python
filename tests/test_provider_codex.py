"""Fail-closed Codex provider adapter tests."""

import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from cc_search_chats.core.canonicalization import CanonicalizationDiagnosticCode
from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    SessionKind,
    SubmittedBy,
)
from cc_search_chats.providers.codex import (
    CodexDiagnosticCode,
    CodexSessionContext,
    parse_codex_session,
)
from cc_search_chats.providers.source_discovery import (
    RecordEnvelope,
    read_bounded_jsonl,
)

FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def envelope(
    payload: object,
    *,
    ordinal: int = 0,
    source_byte_offset: int = 0,
) -> RecordEnvelope:
    """Build one Task 2 envelope from a synthetic JSON value."""
    raw_bytes = json.dumps(payload, ensure_ascii=False).encode()
    return RecordEnvelope(
        source_file_relative=Path("2026/08/11/rollout-synthetic.jsonl"),
        record_ordinal=ordinal,
        source_line=ordinal + 1,
        source_byte_offset=source_byte_offset,
        raw_bytes=raw_bytes,
        raw_byte_length=len(raw_bytes),
        source_digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def raw_envelope(raw_bytes: bytes, *, ordinal: int = 0) -> RecordEnvelope:
    """Build one envelope without normalizing escaped JSON test bytes."""
    return RecordEnvelope(
        source_file_relative=Path("2026/08/11/rollout-synthetic.jsonl"),
        record_ordinal=ordinal,
        source_line=ordinal + 1,
        source_byte_offset=ordinal * 1000,
        raw_bytes=raw_bytes,
        raw_byte_length=len(raw_bytes),
        source_digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def sequential_envelopes(*payloads: object) -> tuple[RecordEnvelope, ...]:
    """Build deterministic consecutive envelopes from synthetic JSON values."""
    values: list[RecordEnvelope] = []
    offset = 0
    for ordinal, payload in enumerate(payloads):
        value = envelope(payload, ordinal=ordinal, source_byte_offset=offset)
        values.append(value)
        offset += value.raw_byte_length + 1
    return tuple(values)


def parse_fixture(name: str, *, session_id: str, target_size: int | None = None):
    """Read a synthetic fixture through Task 2 before adapting it."""
    path = FIXTURES / name
    read = read_bounded_jsonl(
        path,
        source_file_relative=Path(f"2026/08/11/{name}"),
        target_size=path.stat().st_size if target_size is None else target_size,
    )
    return parse_codex_session(
        read.envelopes,
        context=CodexSessionContext(source_session_id=session_id),
        source_diagnostics=read.diagnostics,
    )


class TestCodexSchemaFamilies:
    def test_legacy_primary_canonicalizes_duplicates_and_separates_tools(self) -> None:
        result = parse_fixture(
            "codex_legacy_primary_046.jsonl", session_id="codex-legacy-primary"
        )

        assert result.session_kind is SessionKind.PRIMARY
        assert [
            (message.content_class, message.text) for message in result.messages
        ] == [
            (ContentClass.PROSE, "legacy visible user"),
            (ContentClass.PROSE, "legacy visible assistant"),
            (ContentClass.TOOL_NAME, "shell_command"),
            (ContentClass.TOOL_INPUT, '{"cmd":"synthetic"}'),
            (ContentClass.TOOL_OUTPUT, "synthetic command output"),
            (ContentClass.PROSE, "legacy after compact"),
        ]
        assert [message.conversation_epoch for message in result.messages] == [
            0,
            0,
            0,
            0,
            0,
            1,
        ]
        assistant = result.messages[1]
        assert assistant.identity.canonical_locator.key_kind is LocatorKeyKind.ID
        assert assistant.identity.canonical_locator.key == "legacy-assistant-id"
        assert len(assistant.identity.physical_aliases) == 2
        assert all(
            message.submitted_by is not SubmittedBy.HUMAN for message in result.messages
        )

    def test_modern_primary_recognizes_one_unique_boundary_and_keeps_ordinals(
        self,
    ) -> None:
        result = parse_fixture(
            "codex_modern_primary_145.jsonl", session_id="codex-modern-primary"
        )

        assert result.session_kind is SessionKind.PRIMARY
        assert [message.text for message in result.messages] == [
            "modern visible user",
            "modern visible assistant",
            "modern after compact",
            "modern after unknown compact",
        ]
        assert [message.conversation_epoch for message in result.messages] == [
            0,
            0,
            1,
            1,
        ]
        assert len(result.boundaries) == 1
        assert result.boundaries[0].conversation_epoch == 1
        assert result.boundaries[0].physical_alias.record_ordinal == 8
        assert {diagnostic.code for diagnostic in result.diagnostics} >= {
            CodexDiagnosticCode.DUPLICATE_COMPACTION,
            CodexDiagnosticCode.UNKNOWN_COMPACTION,
        }

    @pytest.mark.parametrize(
        ("fixture_name", "session_id"),
        [
            ("codex_modern_child_145.jsonl", "codex-modern-child"),
            ("codex_legacy_child_140.jsonl", "codex-legacy-child"),
        ],
    )
    def test_modern_and_legacy_child_sources_are_agents(
        self, fixture_name: str, session_id: str
    ) -> None:
        result = parse_fixture(fixture_name, session_id=session_id)

        assert result.session_kind is SessionKind.AGENT
        assert all(
            message.session_kind is SessionKind.AGENT for message in result.messages
        )
        assert all(
            message.submitted_by is not SubmittedBy.HUMAN for message in result.messages
        )

    def test_modern_child_user_has_positive_native_harness_evidence(self) -> None:
        result = parse_fixture(
            "codex_modern_child_145.jsonl", session_id="codex-modern-child"
        )

        user = next(message for message in result.messages if message.role == "user")
        assert user.submitted_by is SubmittedBy.IDENTIFIED_HARNESS
        assert user.submission_evidence == ("codex:session_meta.source.subagent",)
        assert user.submission_match_cardinality == 1

    @pytest.mark.parametrize("source", ["cli", "exec", "mcp", "vscode"])
    def test_allowlisted_string_sources_are_primary(self, source: str) -> None:
        records = (
            envelope(
                {
                    "type": "session_meta",
                    "payload": {"id": "source-session", "source": source},
                }
            ),
        )

        result = parse_codex_session(
            records,
            context=CodexSessionContext(source_session_id="source-session"),
        )

        assert result.session_kind is SessionKind.PRIMARY

    def test_origin_ambiguous_unpaired_user_remains_unknown(self) -> None:
        records = (
            envelope(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "unknown-origin-session",
                        "source": {"future": "origin"},
                    },
                }
            ),
            envelope(
                {
                    "timestamp": "2026-08-11T08:30:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "visible unknown"}],
                    },
                }
            ),
        )
        records = (
            records[0],
            RecordEnvelope(
                source_file_relative=records[1].source_file_relative,
                record_ordinal=1,
                source_line=2,
                source_byte_offset=records[0].raw_byte_length + 1,
                raw_bytes=records[1].raw_bytes,
                raw_byte_length=records[1].raw_byte_length,
                source_digest=records[1].source_digest,
            ),
        )

        result = parse_codex_session(
            records,
            context=CodexSessionContext(source_session_id="unknown-origin-session"),
        )

        assert result.session_kind is SessionKind.UNKNOWN
        assert result.messages[0].submitted_by is SubmittedBy.UNKNOWN
        assert result.canonicalization_diagnostics[0].code is (
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER
        )

    def test_unallowlisted_legacy_subagent_cannot_mint_agent_provenance(
        self,
    ) -> None:
        records = sequential_envelopes(
            {
                "type": "session_meta",
                "payload": {
                    "id": "legacy-future-session",
                    "source": {"subagent": "future-unreviewed-role"},
                },
            },
            {
                "timestamp": "2026-08-11T08:30:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "visible unknown"}],
                },
            },
        )

        result = parse_codex_session(
            records,
            context=CodexSessionContext(source_session_id="legacy-future-session"),
        )

        assert result.session_kind is SessionKind.UNKNOWN
        assert result.messages[0].submitted_by is SubmittedBy.UNKNOWN

    def test_conflicting_native_session_ids_fail_closed(self) -> None:
        result = parse_codex_session(
            (
                envelope(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "other-session",
                            "session_id": "context-session",
                            "source": "cli",
                        },
                    }
                ),
            ),
            context=CodexSessionContext(source_session_id="context-session"),
        )

        assert result.session_kind is SessionKind.UNKNOWN
        assert CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE in {
            diagnostic.code for diagnostic in result.diagnostics
        }


class TestCodexFailClosedDiagnostics:
    def test_unsupported_fixture_emits_no_searchable_content(self) -> None:
        result = parse_fixture(
            "codex_unsupported_shapes.jsonl", session_id="codex-unsupported"
        )

        assert result.messages == ()
        assert {diagnostic.code for diagnostic in result.diagnostics} == {
            CodexDiagnosticCode.MALFORMED_JSON,
            CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
            CodexDiagnosticCode.EXCLUDED_DEVELOPER,
            CodexDiagnosticCode.UNKNOWN_ROLE,
            CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
            CodexDiagnosticCode.EXCLUDED_REASONING,
            CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
            CodexDiagnosticCode.UNKNOWN_EVENT,
            CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
            CodexDiagnosticCode.UNKNOWN_COMPACTION,
            CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE,
        }

    def test_partial_tail_from_bounded_reader_is_named_and_not_parsed(self) -> None:
        path = FIXTURES / "codex_partial_tail.jsonl"
        result = parse_fixture(
            "codex_partial_tail.jsonl",
            session_id="codex-partial",
            target_size=path.stat().st_size - 1,
        )

        assert result.messages == ()
        assert CodexDiagnosticCode.PARTIAL_TAIL in {
            diagnostic.code for diagnostic in result.diagnostics
        }

    @pytest.mark.parametrize(
        ("outer_type", "payload", "expected"),
        [
            (
                "turn_context",
                {
                    "approval_policy": "never",
                    "cwd": "/synthetic",
                    "effort": "high",
                    "model": "synthetic-model",
                    "sandbox_policy": {},
                    "summary": "synthetic summary",
                },
                None,
            ),
            ("world_state", {"full": True, "state": {}}, None),
            (
                "inter_agent_communication_metadata",
                {"trigger_turn": False},
                CodexDiagnosticCode.EXCLUDED_INTER_AGENT_METADATA,
            ),
        ],
    )
    def test_audited_metadata_shapes_are_explicitly_recognized(
        self,
        outer_type: str,
        payload: dict[str, object],
        expected: CodexDiagnosticCode | None,
    ) -> None:
        result = parse_codex_session(
            (
                envelope(
                    {
                        "timestamp": "2026-08-11T09:00:00Z",
                        "type": outer_type,
                        "payload": payload,
                    }
                ),
            ),
            context=CodexSessionContext(source_session_id="metadata-session"),
        )

        codes = {diagnostic.code for diagnostic in result.diagnostics}
        if expected is None:
            assert codes == set()
        else:
            assert codes == {expected}

    @pytest.mark.parametrize(
        "outer_type",
        [
            "turn_context",
            "world_state",
            "inter_agent_communication_metadata",
        ],
    )
    def test_conversation_shaped_metadata_payloads_are_unknown(
        self, outer_type: str
    ) -> None:
        result = parse_codex_session(
            (
                envelope(
                    {
                        "timestamp": "2026-08-11T09:00:00Z",
                        "type": outer_type,
                        "payload": {
                            "message": {
                                "role": "user",
                                "content": "must not disappear",
                            }
                        },
                    }
                ),
            ),
            context=CodexSessionContext(source_session_id="metadata-session"),
        )

        assert result.messages == ()
        assert {diagnostic.code for diagnostic in result.diagnostics} == {
            CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE
        }

    @pytest.mark.parametrize("escaped_surrogate", [b"\\ud800", b"\\udfff"])
    def test_escaped_lone_surrogate_prose_is_diagnostic(
        self, escaped_surrogate: bytes
    ) -> None:
        response = (
            b'{"timestamp":"2026-08-11T09:00:00Z","type":"response_item",'
            b'"payload":{"type":"message","role":"user","content":'
            b'[{"type":"input_text","text":"' + escaped_surrogate + b'"}]}}'
        )
        event = (
            b'{"timestamp":"2026-08-11T09:00:01Z","type":"event_msg",'
            b'"payload":{"type":"user_message","message":"' + escaped_surrogate + b'"}}'
        )

        result = parse_codex_session(
            (raw_envelope(response), raw_envelope(event, ordinal=1)),
            context=CodexSessionContext(source_session_id="surrogate-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CodexDiagnosticCode.INVALID_UNICODE,
            CodexDiagnosticCode.INVALID_UNICODE,
        ]

    def test_escaped_lone_surrogate_in_serialized_tool_value_is_diagnostic(
        self,
    ) -> None:
        raw = (
            b'{"timestamp":"2026-08-11T09:00:00Z","type":"response_item",'
            b'"payload":{"type":"function_call","name":"tool",'
            b'"arguments":{"invalid":"\\ud800"}}}'
        )

        result = parse_codex_session(
            (raw_envelope(raw),),
            context=CodexSessionContext(source_session_id="surrogate-tool-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CodexDiagnosticCode.INVALID_UNICODE
        ]

    @pytest.mark.parametrize(
        ("role", "block_type"),
        [("user", "input_text"), ("assistant", "output_text")],
    )
    def test_empty_typed_prose_is_diagnostic(self, role: str, block_type: str) -> None:
        result = parse_codex_session(
            (
                envelope(
                    {
                        "timestamp": "2026-08-11T09:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": role,
                            "content": [{"type": block_type, "text": ""}],
                        },
                    }
                ),
            ),
            context=CodexSessionContext(source_session_id="empty-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CodexDiagnosticCode.EMPTY_CONTENT
        ]

    @pytest.mark.parametrize(
        ("role", "block_type"),
        [("user", "input_text"), ("assistant", "output_text")],
    )
    @pytest.mark.parametrize(
        "content_kind",
        ["empty_list", "two_empty_blocks"],
    )
    def test_empty_typed_prose_collection_is_diagnostic(
        self, role: str, block_type: str, content_kind: str
    ) -> None:
        content = (
            []
            if content_kind == "empty_list"
            else [
                {"type": block_type, "text": ""},
                {"type": block_type, "text": ""},
            ]
        )
        result = parse_codex_session(
            (
                envelope(
                    {
                        "timestamp": "2026-08-11T09:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": role,
                            "content": content,
                        },
                    }
                ),
            ),
            context=CodexSessionContext(source_session_id="empty-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CodexDiagnosticCode.EMPTY_CONTENT
        ]

    @pytest.mark.parametrize("event_type", ["user_message", "agent_message"])
    def test_empty_event_message_is_diagnostic(self, event_type: str) -> None:
        result = parse_codex_session(
            (
                envelope(
                    {
                        "timestamp": "2026-08-11T09:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": event_type, "message": ""},
                    }
                ),
            ),
            context=CodexSessionContext(source_session_id="empty-session"),
        )

        assert result.messages == ()
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CodexDiagnosticCode.EMPTY_CONTENT
        ]


visible_text = st.text(min_size=1, max_size=200)
unknown_block_type = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=30
).filter(
    lambda value: (
        value not in {"input_text", "output_text", "reasoning", "summary_text"}
    )
)


@given(visible_text)
@example("\nvisible exactly\n")
def test_generated_visible_text_is_preserved_exactly(text: str) -> None:
    result = parse_codex_session(
        (
            envelope(
                {
                    "type": "response_item",
                    "timestamp": "2026-08-11T09:00:00Z",
                    "payload": {
                        "type": "message",
                        "id": "property-id",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    },
                }
            ),
        ),
        context=CodexSessionContext(source_session_id="property-session"),
    )

    assert [message.text for message in result.messages] == [text]


@given(unknown_block_type, st.integers())
@example("future_block", 0)
def test_arbitrary_unrecognized_message_blocks_never_become_searchable(
    block_type: str, payload: int
) -> None:
    result = parse_codex_session(
        (
            envelope(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": block_type, "payload": payload}],
                    },
                }
            ),
        ),
        context=CodexSessionContext(source_session_id="property-session"),
    )

    assert result.messages == ()
    assert CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK in {
        diagnostic.code for diagnostic in result.diagnostics
    }


@pytest.mark.parametrize("block_type", ["reasoning", "summary_text"])
def test_known_excluded_message_blocks_are_reasoning_diagnostics(
    block_type: str,
) -> None:
    result = parse_codex_session(
        (
            envelope(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": block_type, "text": "excluded"}],
                    },
                }
            ),
        ),
        context=CodexSessionContext(source_session_id="excluded-session"),
    )

    assert result.messages == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        CodexDiagnosticCode.EXCLUDED_REASONING
    ]
