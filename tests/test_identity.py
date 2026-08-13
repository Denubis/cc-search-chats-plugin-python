"""Provider-qualified native-message identity contract tests."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    MessageIdentity,
    NativeLocator,
    NativeMessage,
    PhysicalAlias,
    Provider,
    ResolutionStatus,
    SessionKind,
    SubmittedBy,
    format_locator,
    parse_locator,
)

DIGEST = "a" * 64


def claude_locator(
    session_id: str = "session-1", message_id: str = "message-1"
) -> NativeLocator:
    """Construct a representative Claude locator."""
    return NativeLocator(
        provider=Provider.CLAUDE,
        source_session_id=session_id,
        key_kind=LocatorKeyKind.UUID,
        key=message_id,
    )


def alias(locator: NativeLocator | None = None) -> PhysicalAlias:
    """Construct a representative root-independent physical alias."""
    return PhysicalAlias(
        locator=locator or claude_locator(),
        source_file_relative=Path("project/session-1.jsonl"),
        record_ordinal=0,
        source_line=1,
        source_byte_offset=0,
        raw_byte_length=12,
        source_digest=DIGEST,
    )


class TestClosedContracts:
    def test_provider_values(self) -> None:
        assert tuple(Provider) == (Provider.CLAUDE, Provider.CODEX)
        assert [value.value for value in Provider] == ["claude", "codex"]

    def test_session_kind_values(self) -> None:
        assert [value.value for value in SessionKind] == [
            "primary",
            "agent",
            "unknown",
        ]

    def test_content_class_values(self) -> None:
        assert [value.value for value in ContentClass] == [
            "prose",
            "tool_name",
            "tool_input",
            "tool_output",
        ]

    def test_submitted_by_values(self) -> None:
        assert [value.value for value in SubmittedBy] == [
            "human",
            "identified_harness",
            "unknown",
        ]

    def test_locator_key_kind_values(self) -> None:
        assert [value.value for value in LocatorKeyKind] == ["uuid", "id", "ordinal"]

    def test_resolution_status_values(self) -> None:
        assert [value.value for value in ResolutionStatus] == [
            "resolved",
            "no_match",
            "multiple_matches",
            "source_unavailable",
            "stale_source",
            "stale_index",
            "malformed_locator",
            "unsupported_provider_schema",
        ]


class TestCanonicalLocator:
    @pytest.mark.parametrize(
        ("locator", "expected"),
        [
            (
                claude_locator("session-id", "message-uuid"),
                "ccchat:v1:claude:session-id:uuid:message-uuid",
            ),
            (
                NativeLocator(
                    provider=Provider.CODEX,
                    source_session_id="session-id",
                    key_kind=LocatorKeyKind.ID,
                    key="item-id",
                ),
                "ccchat:v1:codex:session-id:id:item-id",
            ),
            (
                NativeLocator(
                    provider=Provider.CODEX,
                    source_session_id="session-id",
                    key_kind=LocatorKeyKind.ORDINAL,
                    key=7,
                    record_digest=DIGEST,
                ),
                f"ccchat:v1:codex:session-id:ordinal:7:sha256:{DIGEST}",
            ),
        ],
    )
    def test_formats_and_parses_exact_canonical_strings(
        self, locator: NativeLocator, expected: str
    ) -> None:
        assert format_locator(locator) == expected
        assert parse_locator(expected) == locator

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "http:v1:claude:session:uuid:message",
            "ccchat:v2:claude:session:uuid:message",
            "ccchat:v1:other:session:uuid:message",
            "ccchat:v1:claude:session:id:message",
            "ccchat:v1:codex:session:uuid:message",
            "ccchat:v1:claude::uuid:message",
            "ccchat:v1:claude:session:uuid:",
            "ccchat:v1:claude:session:uuid:message:extra",
            "ccchat:v1:codex:session:ordinal:-1:sha256:" + DIGEST,
            "ccchat:v1:codex:session:ordinal:1.0:sha256:" + DIGEST,
            "ccchat:v1:codex:session:ordinal:١:sha256:" + DIGEST,
            "ccchat:v1:codex:session:ordinal:01:sha256:" + DIGEST,
            "ccchat:v1:codex:session:ordinal:1:sha256:ABCDEF" + "0" * 58,
            "ccchat:v1:codex:session:ordinal:1:sha256:" + "a" * 63,
            "ccchat:v1:codex:session:ordinal:1:md5:" + DIGEST,
        ],
    )
    def test_rejects_noncanonical_locator_syntax(self, value: str) -> None:
        assert parse_locator(value) is ResolutionStatus.MALFORMED_LOCATOR


identifier = st.text(
    alphabet=st.characters(blacklist_characters=":\r\n", blacklist_categories=("Cs",)),
    min_size=1,
    max_size=40,
).filter(lambda value: bool(value.strip()))


native_locators = st.one_of(
    st.builds(
        NativeLocator,
        provider=st.just(Provider.CLAUDE),
        source_session_id=identifier,
        key_kind=st.just(LocatorKeyKind.UUID),
        key=identifier,
    ),
    st.builds(
        NativeLocator,
        provider=st.just(Provider.CODEX),
        source_session_id=identifier,
        key_kind=st.just(LocatorKeyKind.ID),
        key=identifier,
    ),
    st.builds(
        NativeLocator,
        provider=st.just(Provider.CODEX),
        source_session_id=identifier,
        key_kind=st.just(LocatorKeyKind.ORDINAL),
        key=st.integers(min_value=0, max_value=2**31),
        record_digest=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    ),
)


@given(native_locators)
def test_locator_roundtrip(locator: NativeLocator) -> None:
    assert parse_locator(format_locator(locator)) == locator


@given(native_locators)
def test_locator_reformatting_is_canonical(locator: NativeLocator) -> None:
    rendered = format_locator(locator)
    parsed = parse_locator(rendered)
    assert isinstance(parsed, NativeLocator)
    assert format_locator(parsed) == rendered


@given(native_locators)
def test_controlled_locator_mutations_never_produce_a_locator(
    locator: NativeLocator,
) -> None:
    rendered = format_locator(locator)
    parts = rendered.split(":")
    provider_mismatch = list(parts)
    provider_mismatch[2] = (
        Provider.CODEX.value
        if locator.provider is Provider.CLAUDE
        else Provider.CLAUDE.value
    )
    empty_session = list(parts)
    empty_session[3] = ""
    empty_key = list(parts)
    empty_key[5] = ""
    malformed = {
        rendered.replace("ccchat:", "not-ccchat:", 1),
        rendered.replace(":v1:", ":v2:", 1),
        ":".join(provider_mismatch),
        ":".join(empty_session),
        ":".join(empty_key),
        ":".join(parts[:-1]),
        f"{rendered}:extra",
    }
    if locator.key_kind is LocatorKeyKind.ORDINAL:
        noncanonical_ordinal = list(parts)
        noncanonical_ordinal[5] = f"0{parts[5]}"
        invalid_digest = list(parts)
        invalid_digest[7] = f"g{parts[7][1:]}"
        malformed.update({":".join(noncanonical_ordinal), ":".join(invalid_digest)})

    assert malformed
    assert all(
        parse_locator(value) is ResolutionStatus.MALFORMED_LOCATOR
        for value in malformed
    )


class TestIdentityModels:
    def test_submitted_by_defaults_to_unknown_for_user_role(self) -> None:
        locator = claude_locator()
        message = NativeMessage(
            identity=MessageIdentity(
                logical_message_id="logical-1",
                canonical_locator=locator,
                physical_aliases=(alias(locator),),
            ),
            timestamp="2026-08-11T00:00:00Z",
            role="user",
            session_kind=SessionKind.PRIMARY,
            conversation_epoch=0,
            content_class=ContentClass.PROSE,
            text="visible text",
        )

        assert message.submitted_by is SubmittedBy.UNKNOWN
        assert message.submission_evidence == ()
        assert message.submission_match_cardinality == 0

    @pytest.mark.parametrize("conversation_epoch", [-1, -100])
    def test_messages_reject_negative_conversation_epochs(
        self, conversation_epoch: int
    ) -> None:
        locator = claude_locator()
        with pytest.raises(ValueError, match="conversation_epoch"):
            NativeMessage(
                identity=MessageIdentity(
                    logical_message_id="logical-1",
                    canonical_locator=locator,
                    physical_aliases=(alias(locator),),
                ),
                timestamp="",
                role="assistant",
                session_kind=SessionKind.UNKNOWN,
                conversation_epoch=conversation_epoch,
                content_class=ContentClass.PROSE,
                text="visible",
            )

    def test_physical_alias_rejects_absolute_source_path(self) -> None:
        with pytest.raises(ValueError, match="source_file_relative"):
            PhysicalAlias(
                locator=claude_locator(),
                source_file_relative=Path("/provider-root/session.jsonl"),
                record_ordinal=0,
                source_line=1,
                source_byte_offset=0,
                raw_byte_length=1,
                source_digest=DIGEST,
            )

    @pytest.mark.parametrize(
        "source_file_relative",
        [Path("../outside.jsonl"), Path("project/../../outside.jsonl")],
    )
    def test_physical_alias_rejects_parent_traversal(
        self, source_file_relative: Path
    ) -> None:
        with pytest.raises(ValueError, match="source_file_relative"):
            PhysicalAlias(
                locator=claude_locator(),
                source_file_relative=source_file_relative,
                record_ordinal=0,
                source_line=1,
                source_byte_offset=0,
                raw_byte_length=1,
                source_digest=DIGEST,
            )

    def test_physical_alias_accepts_lexically_normalized_dot_path(self) -> None:
        physical_alias = PhysicalAlias(
            locator=claude_locator(),
            source_file_relative=Path("project/./session.jsonl"),
            record_ordinal=0,
            source_line=1,
            source_byte_offset=0,
            raw_byte_length=1,
            source_digest=DIGEST,
        )

        assert physical_alias.source_file_relative == Path("project/session.jsonl")

    @pytest.mark.parametrize("cardinality", [0, 2, 7])
    def test_identified_harness_requires_exactly_one_match(
        self, cardinality: int
    ) -> None:
        locator = claude_locator()
        with pytest.raises(ValueError, match="exactly one"):
            NativeMessage(
                identity=MessageIdentity(
                    logical_message_id="logical-1",
                    canonical_locator=locator,
                    physical_aliases=(alias(locator),),
                ),
                timestamp="2026-08-11T00:00:00Z",
                role="user",
                session_kind=SessionKind.AGENT,
                conversation_epoch=0,
                content_class=ContentClass.PROSE,
                text="visible text",
                submitted_by=SubmittedBy.IDENTIFIED_HARNESS,
                submission_evidence=("native-positive",),
                submission_match_cardinality=cardinality,
            )

    @pytest.mark.parametrize(
        ("evidence", "cardinality"),
        [((), 1), (("ambiguous",), 0), (("ambiguous",), 2)],
    )
    def test_unknown_authorship_cannot_carry_positive_match_state(
        self, evidence: tuple[str, ...], cardinality: int
    ) -> None:
        locator = claude_locator()
        with pytest.raises(ValueError, match="unknown submissions"):
            NativeMessage(
                identity=MessageIdentity(
                    logical_message_id="logical-1",
                    canonical_locator=locator,
                    physical_aliases=(alias(locator),),
                ),
                timestamp="2026-08-11T00:00:00Z",
                role="user",
                session_kind=SessionKind.UNKNOWN,
                conversation_epoch=0,
                content_class=ContentClass.PROSE,
                text="visible text",
                submitted_by=SubmittedBy.UNKNOWN,
                submission_evidence=evidence,
                submission_match_cardinality=cardinality,
            )

    @pytest.mark.parametrize(
        (
            "field_name",
            "record_ordinal",
            "source_line",
            "source_byte_offset",
            "raw_byte_length",
        ),
        [
            ("record_ordinal", -1, 1, 0, 1),
            ("source_line", 0, 0, 0, 1),
            ("source_byte_offset", 0, 1, -1, 1),
            ("raw_byte_length", 0, 1, 0, -1),
        ],
    )
    def test_physical_coordinates_validate_their_bases(
        self,
        field_name: str,
        record_ordinal: int,
        source_line: int,
        source_byte_offset: int,
        raw_byte_length: int,
    ) -> None:
        with pytest.raises(ValueError, match=field_name):
            PhysicalAlias(
                locator=claude_locator(),
                source_file_relative=Path("session.jsonl"),
                record_ordinal=record_ordinal,
                source_line=source_line,
                source_byte_offset=source_byte_offset,
                raw_byte_length=raw_byte_length,
                source_digest=DIGEST,
            )


root_component = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=20
)


@given(st.lists(root_component, min_size=2, max_size=2, unique=True))
def test_provider_root_changes_leave_identity_unchanged(
    root_names: list[str],
) -> None:
    relative = Path("project/session.jsonl")
    first_alias = PhysicalAlias(
        locator=claude_locator(),
        source_file_relative=relative,
        record_ordinal=0,
        source_line=1,
        source_byte_offset=0,
        raw_byte_length=1,
        source_digest=DIGEST,
    )
    second_alias = PhysicalAlias(
        locator=claude_locator(),
        source_file_relative=Path("project") / "session.jsonl",
        record_ordinal=0,
        source_line=1,
        source_byte_offset=0,
        raw_byte_length=1,
        source_digest=DIGEST,
    )
    first_identity = MessageIdentity(
        logical_message_id="logical-1",
        canonical_locator=claude_locator(),
        physical_aliases=(first_alias,),
    )
    second_identity = MessageIdentity(
        logical_message_id="logical-1",
        canonical_locator=claude_locator(),
        physical_aliases=(second_alias,),
    )

    first_root = Path("/") / root_names[0]
    second_root = Path("/") / root_names[1]
    assert first_root / relative != second_root / relative
    assert first_identity == second_identity
    assert first_identity.canonical_locator == second_identity.canonical_locator
    assert first_identity.logical_message_id == second_identity.logical_message_id
    assert first_identity.physical_aliases == second_identity.physical_aliases
