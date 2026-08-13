"""Codex physical-alias canonicalization tests."""

import hashlib
import sys
from pathlib import Path
from types import FrameType

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import cc_search_chats.core.canonicalization as canonicalization_module
from cc_search_chats.core.canonicalization import (
    CanonicalizationDiagnosticCode,
    CodexRecordFamily,
    PhysicalMessageCandidate,
    canonicalize_codex_candidates,
)
from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    MessageIdentity,
    NativeLocator,
    NativeMessage,
    PhysicalAlias,
    Provider,
    SessionKind,
)


def candidate(
    *,
    ordinal: int,
    family: CodexRecordFamily,
    text: str,
    timestamp: str = "2026-08-11T08:00:00Z",
    role: str = "user",
    session_id: str = "codex-session",
    epoch: int = 0,
    message_id: str | None = None,
    source_file: Path = Path("2026/08/11/rollout-synthetic.jsonl"),
) -> PhysicalMessageCandidate:
    """Construct one deterministic physical message candidate."""
    source_digest = hashlib.sha256(f"record-{ordinal}".encode()).hexdigest()
    if message_id is None:
        locator = NativeLocator(
            provider=Provider.CODEX,
            source_session_id=session_id,
            key_kind=LocatorKeyKind.ORDINAL,
            key=ordinal,
            record_digest=source_digest,
        )
        logical_id = f"record-{ordinal}-{source_digest}"
    else:
        locator = NativeLocator(
            provider=Provider.CODEX,
            source_session_id=session_id,
            key_kind=LocatorKeyKind.ID,
            key=message_id,
        )
        logical_id = message_id
    alias = PhysicalAlias(
        locator=locator,
        source_file_relative=source_file,
        record_ordinal=ordinal,
        source_line=ordinal + 1,
        source_byte_offset=ordinal * 100,
        raw_byte_length=99,
        source_digest=source_digest,
    )
    return PhysicalMessageCandidate(
        message=NativeMessage(
            identity=MessageIdentity(
                logical_message_id=logical_id,
                canonical_locator=locator,
                physical_aliases=(alias,),
            ),
            timestamp=timestamp,
            role=role,
            session_kind=SessionKind.PRIMARY,
            conversation_epoch=epoch,
            content_class=ContentClass.PROSE,
            text=text,
        ),
        record_family=family,
    )


def aliases(candidates: tuple[PhysicalMessageCandidate, ...]) -> set[PhysicalAlias]:
    """Collect all physical aliases from test candidates."""
    return {
        alias
        for value in candidates
        for alias in value.message.identity.physical_aliases
    }


def output_aliases(messages: tuple[NativeMessage, ...]) -> set[PhysicalAlias]:
    """Collect all physical aliases retained by logical messages."""
    return {
        alias for message in messages for alias in message.identity.physical_aliases
    }


class TestCanonicalPairing:
    def test_pairs_adjacent_projections_with_ordered_unequal_timestamps(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="same message",
                timestamp="2026-08-11T08:00:00Z",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="same message",
                timestamp="2026-08-11T08:00:20.609Z",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 1
        assert set(result.messages[0].identity.physical_aliases) == aliases(values)

    @pytest.mark.parametrize("timestamp", ["", "not-a-timestamp"])
    def test_missing_or_invalid_timestamp_cannot_pair(self, timestamp: str) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="same message",
                timestamp=timestamp,
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="same message",
                timestamp=timestamp,
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 2
        assert output_aliases(result.messages) == aliases(values)
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
        ]

    def test_timestamp_order_must_match_physical_record_order(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="same message",
                timestamp="2026-08-11T08:00:01Z",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="same message",
                timestamp="2026-08-11T08:00:00Z",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 2
        assert output_aliases(result.messages) == aliases(values)
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
        ]

    def test_fallback_identity_is_stable_when_second_alias_arrives(self) -> None:
        response = candidate(
            ordinal=1,
            family=CodexRecordFamily.RESPONSE_MESSAGE,
            text="same message",
        )
        event = candidate(
            ordinal=2,
            family=CodexRecordFamily.EVENT_MESSAGE,
            text="same message",
        )

        prefix = canonicalize_codex_candidates((response,)).messages[0]
        paired = canonicalize_codex_candidates((response, event)).messages[0]

        assert paired.identity.logical_message_id == prefix.identity.logical_message_id
        assert paired.identity.canonical_locator == prefix.identity.canonical_locator
        assert len(prefix.identity.physical_aliases) == 1
        assert len(paired.identity.physical_aliases) == 2

    @pytest.mark.parametrize(
        ("first_family", "first_message_id", "second_family", "second_message_id"),
        [
            (
                CodexRecordFamily.RESPONSE_MESSAGE,
                "native-response-id",
                CodexRecordFamily.EVENT_MESSAGE,
                None,
            ),
            (
                CodexRecordFamily.EVENT_MESSAGE,
                None,
                CodexRecordFamily.RESPONSE_MESSAGE,
                "native-response-id",
            ),
        ],
    )
    def test_earliest_physical_alias_owns_identity_as_aliases_grow(
        self,
        first_family: CodexRecordFamily,
        first_message_id: str | None,
        second_family: CodexRecordFamily,
        second_message_id: str | None,
    ) -> None:
        first = candidate(
            ordinal=1,
            family=first_family,
            text="same message",
            message_id=first_message_id,
        )
        second = candidate(
            ordinal=2,
            family=second_family,
            text="same message",
            message_id=second_message_id,
        )

        prefix = canonicalize_codex_candidates((first,)).messages[0]
        one_shot = canonicalize_codex_candidates((first, second))
        permuted = canonicalize_codex_candidates((second, first))

        assert one_shot == permuted
        assert len(one_shot.messages) == 1
        identity = one_shot.messages[0].identity
        assert identity.logical_message_id == prefix.identity.logical_message_id
        assert identity.canonical_locator == prefix.identity.canonical_locator
        assert identity.physical_aliases == tuple(
            sorted(aliases((first, second)), key=lambda value: value.record_ordinal)
        )

    def test_pairs_only_mutually_unique_response_and_event_aliases(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="same message",
                message_id="native-response-id",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="same message",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 1
        message = result.messages[0]
        assert message.identity.canonical_locator.key_kind is LocatorKeyKind.ID
        assert message.identity.canonical_locator.key == "native-response-id"
        assert set(message.identity.physical_aliases) == aliases(values)
        assert result.diagnostics == ()

    def test_zero_compatible_partners_remain_distinct_and_diagnostic(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="response text",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="different event text",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert [message.text for message in result.messages] == [
            "response text",
            "different event text",
        ]
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
        ]

    def test_multiple_compatible_partners_are_never_guessed(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="ambiguous",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="ambiguous",
            ),
            candidate(
                ordinal=3,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="ambiguous",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 3
        assert output_aliases(result.messages) == aliases(values)
        assert CanonicalizationDiagnosticCode.MULTIPLE_COMPATIBLE_PARTNERS in {
            diagnostic.code for diagnostic in result.diagnostics
        }

    def test_repeated_identical_text_preserves_every_ambiguous_candidate(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="repeated",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="repeated",
            ),
            candidate(
                ordinal=3,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="repeated",
            ),
            candidate(
                ordinal=4,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="repeated",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 4
        assert output_aliases(result.messages) == aliases(values)
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CanonicalizationDiagnosticCode.MULTIPLE_COMPATIBLE_PARTNERS,
            CanonicalizationDiagnosticCode.MULTIPLE_COMPATIBLE_PARTNERS,
            CanonicalizationDiagnosticCode.MULTIPLE_COMPATIBLE_PARTNERS,
            CanonicalizationDiagnosticCode.MULTIPLE_COMPATIBLE_PARTNERS,
        ]

    def test_intervening_visible_message_prevents_text_pairing(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="repeated",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="intervening",
                role="assistant",
            ),
            candidate(
                ordinal=3,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="repeated",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 3
        assert output_aliases(result.messages) == aliases(values)
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
            CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER,
        ]

    def test_compatible_shapes_across_boundary_epochs_remain_two_messages(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="same text",
                epoch=0,
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="same text",
                epoch=1,
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 2
        assert [message.conversation_epoch for message in result.messages] == [0, 1]
        assert output_aliases(result.messages) == aliases(values)

    def test_same_text_across_sessions_never_collapses(self) -> None:
        values = (
            candidate(
                ordinal=1,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text="same text",
                session_id="session-a",
            ),
            candidate(
                ordinal=2,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text="same text",
                session_id="session-b",
            ),
        )

        result = canonicalize_codex_candidates(values)

        assert len(result.messages) == 2
        assert output_aliases(result.messages) == aliases(values)


def _compatibility_comparison_count(
    values: tuple[PhysicalMessageCandidate, ...],
) -> int:
    """Count calls at the exact compatibility-inspection boundary."""
    calls = 0
    compatibility_code = canonicalization_module._base_compatible.__code__
    previous_profile = sys.getprofile()

    def count_call(frame: FrameType, event: str, arg: object) -> None:
        del arg
        nonlocal calls
        if event == "call" and frame.f_code is compatibility_code:
            calls += 1

    try:
        sys.setprofile(count_call)
        canonicalize_codex_candidates(values)
    finally:
        sys.setprofile(previous_profile)
    return calls


def test_compatibility_work_is_bounded_by_exact_fact_buckets() -> None:
    pair_count = 128
    values = tuple(
        item
        for index in range(pair_count)
        for item in (
            candidate(
                ordinal=index * 2,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text=f"unique-{index}",
            ),
            candidate(
                ordinal=index * 2 + 1,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text=f"unique-{index}",
            ),
        )
    )
    larger_values = tuple(
        item
        for index in range(pair_count * 2)
        for item in (
            candidate(
                ordinal=index * 2,
                family=CodexRecordFamily.RESPONSE_MESSAGE,
                text=f"unique-{index}",
            ),
            candidate(
                ordinal=index * 2 + 1,
                family=CodexRecordFamily.EVENT_MESSAGE,
                text=f"unique-{index}",
            ),
        )
    )

    assert _compatibility_comparison_count(values) == pair_count
    assert _compatibility_comparison_count(larger_values) == pair_count * 2


generated_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=100,
)


@given(generated_text)
@example("same\ntext")
def test_pairing_is_idempotent_and_preserves_every_occurrence(text: str) -> None:
    values = (
        candidate(
            ordinal=5,
            family=CodexRecordFamily.RESPONSE_MESSAGE,
            text=text,
            message_id="generated-native-id",
        ),
        candidate(
            ordinal=6,
            family=CodexRecordFamily.EVENT_MESSAGE,
            text=text,
        ),
    )

    first = canonicalize_codex_candidates(values)
    second = canonicalize_codex_candidates(first.messages)

    assert second.messages == first.messages
    assert second.diagnostics == ()
    assert output_aliases(first.messages) == aliases(values)


@given(generated_text)
def test_output_order_is_deterministic_under_input_permutation(text: str) -> None:
    values = (
        candidate(
            ordinal=8,
            family=CodexRecordFamily.EVENT_MESSAGE,
            text=text,
        ),
        candidate(
            ordinal=7,
            family=CodexRecordFamily.RESPONSE_MESSAGE,
            text=text,
            message_id="stable-id",
        ),
    )

    forward = canonicalize_codex_candidates(values)
    reverse = canonicalize_codex_candidates(tuple(reversed(values)))

    assert forward == reverse
    assert forward.messages[0].identity.canonical_locator.key == "stable-id"
