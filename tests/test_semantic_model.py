"""Semantic runtime failure remains explicit and offline."""

from pathlib import Path

import pytest

from cc_search_chats.semantic import ModelUnavailable, embed_query
from cc_search_chats.semantic import model as semantic_model


def test_query_embedding_requires_local_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CC_SEARCH_MODEL_PATH", raising=False)
    monkeypatch.setenv("HF_HOME", "/missing")
    with pytest.raises(ModelUnavailable, match="model path"):
        embed_query("find the receipt discussion")


def test_cuda_failure_is_scoped_to_the_calling_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class TorchWithoutCuda:
        cuda = UnavailableCuda()

    monkeypatch.setattr(semantic_model, "_model_path", lambda: Path("/model"))
    monkeypatch.setattr(
        semantic_model,
        "import_module",
        lambda name: TorchWithoutCuda() if name == "torch" else object(),
    )
    semantic_model._runtime.cache_clear()

    with pytest.raises(
        ModelUnavailable,
        match=(
            "CUDA is unavailable to this process.*agent sandbox.*host approval route"
        ),
    ):
        semantic_model._runtime()

    semantic_model._runtime.cache_clear()
