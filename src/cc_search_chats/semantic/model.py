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
            f"model path must be the local {MODEL_ID} snapshot {MODEL_REVISION}"
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
            "semantic dependencies are unavailable; use search --literal"
        ) from error
    if not torch.cuda.is_available():
        raise ModelUnavailable(
            "CUDA is unavailable to this process; if this command is running in an "
            "agent sandbox, rerun it through the configured host approval route; "
            "otherwise use search --literal"
        )

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


def _embed(texts: Sequence[str], prefix: str) -> list[list[float]]:
    if not texts or any(not text.strip() for text in texts):
        raise ValueError(f"semantic {prefix.rstrip(':')} text must not be blank")
    torch, _, _ = _runtime()
    try:
        return _embed_batch(texts, prefix)
    except torch.cuda.OutOfMemoryError:
        if len(texts) == 1:
            raise
        torch.cuda.empty_cache()
    middle = len(texts) // 2
    return _embed(texts[:middle], prefix) + _embed(texts[middle:], prefix)


def embed_query(text: str) -> list[float]:
    """Embed one retrieval query with the model's required prompt."""
    return _embed([text], "query:")[0]


def embed_passages(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of retrieval passages with the required prompt."""
    return _embed(texts, "passage:")
