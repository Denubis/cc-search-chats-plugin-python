"""Offline-only adapter for the pinned local Nemotron embedding model."""

import csv
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from io import StringIO
from pathlib import Path
from typing import Protocol

MODEL_ID = "nvidia/Nemotron-3-Embed-8B-BF16"
MODEL_REVISION = "c44c20ab3f6b430336706847a6372de4b2eb3dbd"
DIMENSIONS = 1024
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "
CHUNK_TARGET_TOKENS = 768
MAX_MODEL_TOKENS = 1024
CHUNK_OVERLAP_TOKENS = 96
CHUNKER_ID = "nemotron-token-chunks-768-1024-96:v1"
type ModelProgress = Callable[[str, str], None]
_COMMIT_HASH = re.compile(r"[0-9a-f]{40}").fullmatch


@dataclass(frozen=True, slots=True)
class SemanticChunk:
    """One tokenizer-bounded passage within a single logical message."""

    ordinal: int
    token_start: int
    token_end: int
    char_start: int
    char_end: int
    text: str


class _ChunkTokenizer(Protocol):
    def num_special_tokens_to_add(self, *, pair: bool) -> int: ...

    def __call__(
        self,
        text: str,
        *,
        add_special_tokens: bool,
        return_offsets_mapping: bool = False,
    ) -> Mapping[str, Sequence[int] | Sequence[tuple[int, int]]]: ...


@dataclass(frozen=True, slots=True)
class GpuProcess:
    """One active NVIDIA compute process observed at an allocation failure."""

    gpu_uuid: str
    pid: int
    process_name: str
    used_memory_mib: int | None
    current_process: bool


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
        gpu_processes: Sequence[GpuProcess] | None = None,
        gpu_processes_unavailable_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.available_vram_bytes = available_vram_bytes
        self.required_vram_bytes = required_vram_bytes
        self.total_vram_bytes = total_vram_bytes
        self.gpu_processes = None if gpu_processes is None else tuple(gpu_processes)
        self.gpu_processes_unavailable_reason = gpu_processes_unavailable_reason


def _configured_model_path() -> Path:
    configured = os.environ.get("CC_SEARCH_MODEL_PATH")
    cache = Path(
        os.environ.get(
            "HF_HOME",
            Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
            / "huggingface",
        )
    )
    return (
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


def local_snapshot_revision(path: Path) -> str | None:
    """Return a commit revision only for an existing commit-named directory."""
    return path.name if path.is_dir() and _COMMIT_HASH(path.name) is not None else None


def local_model_revision() -> str | None:
    """Read the configured local snapshot revision without loading the model."""
    return local_snapshot_revision(_configured_model_path())


def _silence_model_libraries() -> None:
    try:
        transformers_logging = import_module("transformers.utils.logging")
    except ImportError:
        transformers_logging = None
    if transformers_logging is not None:
        transformers_logging.disable_progress_bar()
        transformers_logging.set_verbosity_error()

    try:
        huggingface_hub = import_module("huggingface_hub")
    except ImportError:
        huggingface_hub = None
    if huggingface_hub is not None:
        huggingface_hub.utils.disable_progress_bars()
        huggingface_hub.logging.set_verbosity_error()


@contextmanager
def model_output_scope(*, quiet: bool):
    """Keep third-party model stderr out of a machine-readable progress stream."""
    if not quiet:
        yield
        return
    with redirect_stderr(StringIO()):
        _silence_model_libraries()
        yield


def _model_path() -> Path:
    path = _configured_model_path()
    if not path.is_dir() or path.name != MODEL_REVISION:
        raise ModelUnavailable(
            f"model path must be the local {MODEL_ID} snapshot {MODEL_REVISION}",
            code="model_snapshot_unavailable",
            phase="model_preflight",
        )
    return path


@lru_cache(maxsize=1)
def _tokenizer():
    path = _model_path()
    try:
        transformers = import_module("transformers")
    except ImportError as error:
        raise ModelUnavailable(
            "semantic dependencies are unavailable; use search --literal",
            code="semantic_dependencies_unavailable",
            phase="model_load",
        ) from error
    try:
        return transformers.AutoTokenizer.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise ModelUnavailable(
            f"the pinned local semantic tokenizer could not be loaded: {error}",
            code="model_load_failed",
            phase="model_load",
        ) from error


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
        tokenizer = _tokenizer()
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


def release_model() -> None:
    """Drop cached model state and release Torch's unused CUDA allocations."""
    _runtime.cache_clear()
    _tokenizer.cache_clear()
    torch = sys.modules.get("torch")
    if torch is not None:
        torch.cuda.empty_cache()


def _normalize_pooled_embeddings(torch, pooled):
    return torch.nn.functional.normalize(
        pooled[:, :DIMENSIONS].float(),
        dim=1,
    )


def _embed_batch(texts: Sequence[str], prefix: str) -> list[list[float]]:
    torch, tokenizer, model = _runtime()
    inputs = tokenizer(
        [f"{prefix}{text}" for text in texts],
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=MAX_MODEL_TOKENS,
    ).to("cuda")
    with torch.inference_mode():
        hidden = model(**inputs, use_cache=False).last_hidden_state
        mask = inputs["attention_mask"].unsqueeze(-1)
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1)
        vector = _normalize_pooled_embeddings(torch, pooled)
    return vector.float().cpu().tolist()


def _used_memory_mib(value: str) -> int | None:
    normalized = value.strip()
    if normalized in {"", "N/A", "[N/A]"}:
        return None
    return int(normalized)


def _gpu_process_snapshot() -> tuple[tuple[GpuProcess, ...], str | None]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return (), "nvidia-smi is unavailable to this process"
    command = [
        executable,
        "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=2.0,
        )
    except subprocess.TimeoutExpired:
        return (), "nvidia-smi compute-process query timed out after 2 seconds"
    except OSError as error:
        return (), f"nvidia-smi could not run in this process: {error}"
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        return (), f"nvidia-smi compute-process query failed: {detail}"
    try:
        processes = tuple(
            GpuProcess(
                gpu_uuid=row[0].strip(),
                pid=int(row[1].strip()),
                process_name=row[2].strip(),
                used_memory_mib=_used_memory_mib(row[3]),
                current_process=int(row[1].strip()) == os.getpid(),
            )
            for row in csv.reader(StringIO(result.stdout))
            if row
        )
    except (IndexError, ValueError) as error:
        return (), (
            "nvidia-smi returned malformed compute-process data: "
            f"{type(error).__name__}"
        )
    return (
        tuple(
            sorted(
                processes,
                key=lambda process: (
                    process.current_process,
                    -(
                        process.used_memory_mib
                        if process.used_memory_mib is not None
                        else -1
                    ),
                    process.pid,
                ),
            )
        ),
        None,
    )


def _gpu_process_summary(
    processes: Sequence[GpuProcess], unavailable_reason: str | None
) -> str:
    if unavailable_reason is not None:
        return f"active GPU compute processes could not be listed: {unavailable_reason}"
    if not processes:
        return "nvidia-smi reported no active GPU compute processes at failure"
    rendered = []
    for process in processes:
        memory = (
            f"{process.used_memory_mib} MiB"
            if process.used_memory_mib is not None
            else "memory unavailable"
        )
        current = " (current process)" if process.current_process else ""
        rendered.append(f"PID {process.pid} {process.process_name}: {memory}{current}")
    return f"active GPU compute processes at failure: {'; '.join(rendered)}"


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
    gpu_processes, unavailable_reason = _gpu_process_snapshot()
    return ModelUnavailable(
        "this process could not allocate VRAM for the pinned semantic model"
        f"{capacity}: {error}; "
        f"{_gpu_process_summary(gpu_processes, unavailable_reason)}",
        code="vram_unavailable",
        phase=phase,
        available_vram_bytes=available,
        total_vram_bytes=total,
        gpu_processes=gpu_processes,
        gpu_processes_unavailable_reason=unavailable_reason,
    )


def _embed(texts: Sequence[str], prefix: str) -> list[list[float]]:
    if not texts or any(not text.strip() for text in texts):
        raise ValueError(f"semantic {prefix.rstrip(': ')} text must not be blank")
    torch, _, _ = _runtime()
    try:
        return _embed_batch(texts, prefix)
    except torch.cuda.OutOfMemoryError as error:
        if len(texts) == 1:
            phase = "query_embed" if prefix == QUERY_PREFIX else "semantic_embed"
            raise _vram_unavailable(torch, phase=phase, error=error) from error
        torch.cuda.empty_cache()
    except ModelUnavailable:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        query = prefix == QUERY_PREFIX
        raise ModelUnavailable(
            f"semantic {'query' if query else 'passage'} embedding failed: {error}",
            code=("query_embedding_failed" if query else "semantic_embedding_failed"),
            phase=("query_embed" if query else "semantic_embed"),
        ) from error
    middle = len(texts) // 2
    return _embed(texts[:middle], prefix) + _embed(texts[middle:], prefix)


def _prepare_runtime(progress: ModelProgress | None) -> None:
    if progress is not None:
        progress("model_preflight", "running")
    _model_path()
    if progress is not None:
        progress("model_preflight", "complete")
        progress("model_load", "running")
    _runtime()
    if progress is not None:
        progress("model_load", "complete")


def embed_query(
    text: str,
    *,
    progress: ModelProgress | None = None,
) -> list[float]:
    """Embed one retrieval query with the model's required prompt."""
    _prepare_runtime(progress)
    return _embed([text], QUERY_PREFIX)[0]


def embed_passages(
    texts: Sequence[str],
    *,
    progress: ModelProgress | None = None,
) -> list[list[float]]:
    """Embed a batch of retrieval passages with the required prompt."""
    _prepare_runtime(progress)
    return _embed(texts, PASSAGE_PREFIX)


def _required_sequence(
    encoded: Mapping[str, Sequence[int] | Sequence[tuple[int, int]]],
    key: str,
) -> Sequence[int] | Sequence[tuple[int, int]]:
    value = encoded.get(key)
    if value is None:
        raise ValueError(f"tokenizer did not return {key}")
    return value


def chunk_passages(
    texts: Sequence[str],
    *,
    tokenizer: _ChunkTokenizer | None = None,
) -> tuple[tuple[SemanticChunk, ...], ...]:
    """Split each message independently using the pinned token budget."""
    selected = _tokenizer() if tokenizer is None else tokenizer
    prefix_ids = _required_sequence(
        selected(PASSAGE_PREFIX, add_special_tokens=False),
        "input_ids",
    )
    special_tokens = selected.num_special_tokens_to_add(pair=False)
    hard_content_limit = MAX_MODEL_TOKENS - len(prefix_ids) - special_tokens
    chunk_size = min(CHUNK_TARGET_TOKENS, hard_content_limit)
    if chunk_size <= CHUNK_OVERLAP_TOKENS:
        raise ValueError("model token budget cannot accommodate configured overlap")

    results: list[tuple[SemanticChunk, ...]] = []
    for text in texts:
        if not text.strip():
            raise ValueError("semantic passage text must not be blank")
        encoded = selected(
            text,
            add_special_tokens=False,
            return_offsets_mapping=True,
        )
        input_ids = _required_sequence(encoded, "input_ids")
        raw_offsets = _required_sequence(encoded, "offset_mapping")
        if len(input_ids) != len(raw_offsets):
            raise ValueError("tokenizer offsets do not match input tokens")
        offsets: list[tuple[int, int]] = []
        for value in raw_offsets:
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(isinstance(bound, int) for bound in value)
            ):
                raise ValueError("tokenizer returned malformed character offsets")
            offsets.append(value)
        if not offsets:
            raise ValueError("tokenizer returned no content tokens")

        chunks: list[SemanticChunk] = []
        start = 0
        while start < len(offsets):
            end = min(start + chunk_size, len(offsets))
            char_start = offsets[start][0]
            char_end = offsets[end - 1][1]
            if not 0 <= char_start < char_end <= len(text):
                raise ValueError("tokenizer returned invalid character bounds")
            chunks.append(
                SemanticChunk(
                    ordinal=len(chunks),
                    token_start=start,
                    token_end=end,
                    char_start=char_start,
                    char_end=char_end,
                    text=text[char_start:char_end],
                )
            )
            if end == len(offsets):
                break
            start = end - CHUNK_OVERLAP_TOKENS
        results.append(tuple(chunks))
    return tuple(results)
