"""Deterministic bounds and arithmetic for PostgreSQL hybrid ranking."""

from fractions import Fraction

import pytest

from cc_search_chats.storage.postgresql.index import SearchHit
from cc_search_chats.storage.postgresql.semantic import (
    _fuse_hybrid,
    _ranked_component_depth,
)


def _hit(locator: str) -> SearchHit:
    return SearchHit(
        provider="claude",
        source_session_id="session",
        logical_message_id=locator,
        canonical_locator=locator,
        timestamp="2026-08-11T00:00:00Z",
        role="assistant",
        session_kind="primary",
        conversation_epoch=0,
        content_class="prose",
        text=locator,
        repository=None,
        cwd=None,
        rank=1.0,
    )


@pytest.mark.parametrize("limit", [False, 0, 201])
def test_hybrid_search_rejects_unbounded_ranked_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 200"):
        _ranked_component_depth(limit)


@pytest.mark.parametrize(
    ("limit", "expected_depth"),
    [(1, 100), (20, 100), (21, 105), (200, 1000)],
)
def test_hybrid_search_uses_bounded_component_depth(
    limit: int,
    expected_depth: int,
) -> None:
    assert _ranked_component_depth(limit) == expected_depth


def test_hybrid_search_uses_exact_rrf_and_locator_tie_breaking() -> None:
    first = _hit("ccchat:v1:claude:session:uuid:first")
    second = _hit("ccchat:v1:claude:session:uuid:second")
    results = _fuse_hybrid(
        (first, second),
        (second,),
        limit=2,
        rank_constant=60,
        component_depth=100,
    )

    assert [result.message for result in results] == [second, first]
    assert results[0].score == Fraction(1, 61) + Fraction(1, 62)
    assert results[1].score == Fraction(1, 61)
    assert all(isinstance(result.score, Fraction) for result in results)
    assert results[0].literal_score == second.rank
    assert results[0].semantic_score == second.rank
    assert results[0].rank_constant == 60
    assert results[0].component_depth == 100
