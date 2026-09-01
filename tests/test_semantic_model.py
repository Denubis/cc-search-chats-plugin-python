"""Semantic runtime failure remains explicit and offline."""

import subprocess
from pathlib import Path

import pytest

from cc_search_chats.semantic import ModelUnavailable, embed_passages, embed_query
from cc_search_chats.semantic import model as semantic_model


def test_pooled_embeddings_are_normalized_in_float32() -> None:
    class Float32Values:
        pass

    class BFloat16Values:
        def float(self) -> Float32Values:
            return Float32Values()

    class PooledValues:
        def __getitem__(self, key: object) -> BFloat16Values:
            assert key == (slice(None), slice(None, semantic_model.DIMENSIONS))
            return BFloat16Values()

    class Functional:
        @staticmethod
        def normalize(values: object, *, dim: int) -> str:
            assert isinstance(values, Float32Values)
            assert dim == 1
            return "normalized"

    class NeuralNetwork:
        functional = Functional()

    class FakeTorch:
        nn = NeuralNetwork()

    assert (
        semantic_model._normalize_pooled_embeddings(FakeTorch(), PooledValues())
        == "normalized"
    )


def test_passage_embedding_does_not_start_a_synthetic_gpu_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOutOfMemoryError(RuntimeError):
        pass

    class FakeCuda:
        OutOfMemoryError = FakeOutOfMemoryError

    class FakeTorch:
        cuda = FakeCuda()

    monkeypatch.setattr(semantic_model, "_prepare_runtime", lambda progress: None)
    monkeypatch.setattr(
        semantic_model,
        "_runtime",
        lambda: (FakeTorch(), object(), object()),
    )
    monkeypatch.setattr(
        semantic_model,
        "_embed_batch",
        lambda texts, prefix: [[1.0] * semantic_model.DIMENSIONS for _ in texts],
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail(
            "passage embedding started a synthetic GPU probe"
        ),
    )

    vectors = embed_passages(("one passage",))

    assert len(vectors) == 1
    assert len(vectors[0]) == semantic_model.DIMENSIONS


def test_query_embedding_does_not_run_index_gpu_performance_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOutOfMemoryError(RuntimeError):
        pass

    class FakeCuda:
        OutOfMemoryError = FakeOutOfMemoryError

    class FakeTorch:
        cuda = FakeCuda()

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail(
            "query embedding started indexing telemetry"
        ),
    )
    monkeypatch.setattr(semantic_model, "_prepare_runtime", lambda progress: None)
    monkeypatch.setattr(
        semantic_model,
        "_runtime",
        lambda: (FakeTorch(), object(), object()),
    )
    monkeypatch.setattr(
        semantic_model,
        "_embed_batch",
        lambda texts, prefix: [[1.0] * semantic_model.DIMENSIONS for _ in texts],
    )

    vector = embed_query("needle")

    assert len(vector) == semantic_model.DIMENSIONS


def test_query_embedding_requires_local_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CC_SEARCH_MODEL_PATH", raising=False)
    monkeypatch.setenv("HF_HOME", "/missing")
    with pytest.raises(ModelUnavailable, match="model path") as raised:
        embed_query("find the receipt discussion")
    assert raised.value.code == "model_snapshot_unavailable"
    assert raised.value.phase == "model_preflight"


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
    ) as raised:
        semantic_model._runtime()
    assert raised.value.code == "cuda_unavailable"
    assert raised.value.phase == "model_preflight"

    semantic_model._runtime.cache_clear()


def test_terminal_vram_failure_is_named_and_reports_measurable_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOutOfMemoryError(RuntimeError):
        pass

    class FakeCuda:
        OutOfMemoryError = FakeOutOfMemoryError

        @staticmethod
        def empty_cache() -> None:
            return None

        @staticmethod
        def mem_get_info() -> tuple[int, int]:
            return 2_000, 8_000

    class FakeTorch:
        cuda = FakeCuda()

    monkeypatch.setattr(
        semantic_model,
        "_runtime",
        lambda: (FakeTorch(), object(), object()),
    )
    monkeypatch.setattr(
        semantic_model,
        "_embed_batch",
        lambda texts, prefix: (_ for _ in ()).throw(FakeOutOfMemoryError("oom")),
    )

    with pytest.raises(ModelUnavailable, match="VRAM") as raised:
        embed_passages(("one passage",))

    assert raised.value.code == "vram_unavailable"
    assert raised.value.phase == "semantic_embed"
    assert raised.value.available_vram_bytes == 2_000
    assert raised.value.total_vram_bytes == 8_000


def test_query_embedding_runtime_failure_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOutOfMemoryError(RuntimeError):
        pass

    class FakeCuda:
        OutOfMemoryError = FakeOutOfMemoryError

    class FakeTorch:
        cuda = FakeCuda()

    monkeypatch.setattr(
        semantic_model,
        "_runtime",
        lambda: (FakeTorch(), object(), object()),
    )
    monkeypatch.setattr(
        semantic_model,
        "_embed_batch",
        lambda texts, prefix: (_ for _ in ()).throw(ValueError("fixture tokenizer")),
    )

    with pytest.raises(ModelUnavailable, match="query embedding") as raised:
        embed_query("needle")

    assert raised.value.code == "query_embedding_failed"
    assert raised.value.phase == "query_embed"
