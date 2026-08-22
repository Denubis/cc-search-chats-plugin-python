"""Offline-only adapter for the pinned local Nemotron embedding model."""

import os
from collections.abc import Sequence
from functools import lru_cache
from importlib import import_module
from pathlib import Path

MODEL_ID = "nvidia/Nemotron-3-Embed-8B-BF16"
MODEL_REVISION = "c44c20ab3f6b430336706847a6372de4b2eb3dbd"
DIMENSIONS = 1024


class ModelUnavailable(RuntimeError):
    """The exact configured local semantic runtime cannot be used."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        phase: str,
        available_vram_bytes: int | None = None,
        required_vram_bytes: int | None = None,
        total_vram_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.available_vram_bytes = available_vram_bytes
        self.required_vram_bytes = required_vram_bytes
        self.total_vram_bytes = total_vram_bytes


def _model_path() -> Path:
    configured = os.environ.get("CC_SEARCH_MODEL_PATH")
    cache = Path(
        os.environ.get(
            "HF_HOME",
            Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
            / "huggingface",
        )
    )
    path = (
        Path(
            configured
            or cache
            / "hub"
            / "models--nvidia--Nemotron-3-Embed-8B-BF16"
            / "snapshots"
            / MODEL_REVISION
        )
        .expanduser()
        .resolve()
    )
    if not path.is_dir() or path.name != MODEL_REVISION:
        raise ModelUnavailable(
            f"model path must be the local {MODEL_ID} snapshot {MODEL_REVISION}",
            code="model_snapshot_unavailable",
            phase="model_preflight",
        )
    return path


@lru_cache(maxsize=1)
def _runtime():
    path = _model_path()
    try:
        torch = import_module("torch")
        transformers = import_module("transformers")
    except ImportError as error:
        raise ModelUnavailable(
            "semantic dependencies are unavailable; use search --literal",
            code="semantic_dependencies_unavailable",
            phase="model_load",
        ) from error
    if not torch.cuda.is_available():
        raise ModelUnavailable(
            "CUDA is unavailable to this process; if this command is running in an "
            "agent sandbox, rerun it through the configured host approval route; "
            "otherwise use search --literal",
            code="cuda_unavailable",
            phase="model_preflight",
        )

    try:
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
        model = (
            transformers.AutoModel.from_pretrained(
                path,
                local_files_only=True,
                trust_remote_code=False,
                attn_implementation="sdpa",
                dtype=torch.bfloat16,
            )
            .to("cuda")
            .eval()
        )
    except torch.cuda.OutOfMemoryError as error:
        raise _vram_unavailable(torch, phase="model_load", error=error) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise ModelUnavailable(
            f"the pinned local semantic model could not be loaded: {error}",
            code="model_load_failed",
            phase="model_load",
        ) from error
    model.config.use_cache = False
    return torch, tokenizer, model


def _embed_batch(texts: Sequence[str], prefix: str) -> list[list[float]]:
    torch, tokenizer, model = _runtime()
    inputs = tokenizer(
        [f"{prefix} {text}" for text in texts],
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=4096,
    ).to("cuda")
    with torch.inference_mode():
        hidden = model(**inputs, use_cache=False).last_hidden_state
        mask = inputs["attention_mask"].unsqueeze(-1)
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1)
        vector = torch.nn.functional.normalize(pooled[:, :DIMENSIONS], dim=1)
    return vector.float().cpu().tolist()


def _vram_unavailable(torch, *, phase: str, error: BaseException) -> ModelUnavailable:
    try:
        available, total = torch.cuda.mem_get_info()
    except AttributeError, RuntimeError, TypeError, ValueError:
        available = None
        total = None
    capacity = (
        f"; available VRAM {available} bytes of {total} bytes"
        if available is not None and total is not None
        else ""
    )
    return ModelUnavailable(
        f"VRAM is unavailable for the pinned semantic model{capacity}: {error}",
        code="vram_unavailable",
        phase=phase,
        available_vram_bytes=available,
        total_vram_bytes=total,
    )


def _embed(texts: Sequence[str], prefix: str) -> list[list[float]]:
    if not texts or any(not text.strip() for text in texts):
        raise ValueError(f"semantic {prefix.rstrip(':')} text must not be blank")
    torch, _, _ = _runtime()
    try:
        return _embed_batch(texts, prefix)
    except torch.cuda.OutOfMemoryError as error:
        if len(texts) == 1:
            phase = "query_embed" if prefix == "query:" else "semantic_embed"
            raise _vram_unavailable(torch, phase=phase, error=error) from error
        torch.cuda.empty_cache()
    except ModelUnavailable:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        query = prefix == "query:"
        raise ModelUnavailable(
            f"semantic {'query' if query else 'passage'} embedding failed: {error}",
            code=("query_embedding_failed" if query else "semantic_embedding_failed"),
            phase=("query_embed" if query else "semantic_embed"),
        ) from error
    middle = len(texts) // 2
    return _embed(texts[:middle], prefix) + _embed(texts[middle:], prefix)


def embed_query(text: str) -> list[float]:
    """Embed one retrieval query with the model's required prompt."""
    return _embed([text], "query:")[0]


def embed_passages(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of retrieval passages with the required prompt."""
    return _embed(texts, "passage:")
