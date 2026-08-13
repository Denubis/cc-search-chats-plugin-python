"""Semantic runtime failure remains explicit and offline."""

import pytest

from cc_search_chats.semantic import ModelUnavailable, embed_query


def test_query_embedding_requires_local_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CC_SEARCH_MODEL_PATH", raising=False)
    monkeypatch.setenv("HF_HOME", "/missing")
    with pytest.raises(ModelUnavailable, match="model path"):
        embed_query("find the receipt discussion")
