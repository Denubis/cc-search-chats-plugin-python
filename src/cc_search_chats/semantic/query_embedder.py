"""Warm same-user query embedding helper."""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import socket
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock, Thread
from time import monotonic, sleep
from typing import TYPE_CHECKING

from cc_search_chats import __version__
from cc_search_chats.queueing import _runtime_dir
from cc_search_chats.semantic.model import (
    DIMENSIONS,
    ModelUnavailable,
    embed_query,
    local_model_revision,
    model_output_scope,
    release_model,
)

SEMANTIC_WARM_SECONDS = 30.0
_TEST_GUARD_ENV = "CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN"
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_LIFETIME_LOCK_WAIT_SECONDS = 2.0
_LIFETIME_LOCK_RETRY_SECONDS = 0.01
_PROCESS_EXIT_WAIT_SECONDS = 10.0
_PROCESS_EXIT_POLL_SECONDS = 0.02
_SPAWN_THREAD_LOCK = Lock()
type EmbedCallable = Callable[..., Sequence[float]]
type ProgressCallable = Callable[[str, str], None]

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class QueryEmbeddingResult:
    """One query vector plus helper timing and reuse observations."""

    embedding: tuple[float, ...]
    model_load_ms: int
    query_embed_ms: int
    warm_reused: bool


@dataclass(frozen=True, slots=True)
class QueryEmbedderPaths:
    """Filesystem endpoints owned by the query embedder."""

    runtime_dir: Path
    socket: Path
    lock: Path
    log: Path


def query_embedder_paths(runtime_dir: Path | None = None) -> QueryEmbedderPaths:
    """Return the helper's runtime paths."""
    directory = _runtime_dir() if runtime_dir is None else runtime_dir.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return QueryEmbedderPaths(
        runtime_dir=directory,
        socket=directory / "query-embedder.sock",
        lock=directory / "query-embedder.lock",
        log=directory / "query-embedder.log",
    )


def _query_embedder_command() -> list[str]:
    return [sys.executable, "-m", "cc_search_chats.semantic.query_embedder"]


@dataclass(slots=True)
class _ServerState:
    package_version: str
    model_revision: str | None
    embedder: str
    test_token_digest: str | None
    loaded: bool = False
    warm_since: str | None = None


def _send(connection: socket.socket, message: Mapping[str, object]) -> None:
    connection.sendall(
        json.dumps(message, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    )


def _read_request(connection: socket.socket) -> dict[str, object] | None:
    content = bytearray()
    while len(content) <= _MAX_REQUEST_BYTES:
        chunk = connection.recv(min(65_536, _MAX_REQUEST_BYTES + 1 - len(content)))
        if not chunk:
            return None
        content.extend(chunk)
        newline = content.find(b"\n")
        if newline >= 0:
            if content[newline + 1 :]:
                raise ValueError("query embedder accepts one request per connection")
            decoded = json.loads(content[:newline])
            if not isinstance(decoded, dict):
                raise ValueError("query embedder request must be a JSON object")
            return decoded
    raise ValueError("query embedder request is too large")


def _peer_uid(connection: socket.socket) -> int:
    credentials = connection.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _, uid, _ = struct.unpack("3i", credentials)
    return uid


def _hello(state: _ServerState) -> dict[str, object]:
    return {
        "kind": "hello",
        "package_version": state.package_version,
        "model_revision": state.model_revision,
        "loaded": state.loaded,
        "warm_since": state.warm_since,
        "embedder": state.embedder,
        "test_token_digest": state.test_token_digest,
    }


def _model_unavailable(error: ModelUnavailable) -> dict[str, object]:
    return {
        "kind": "model_unavailable",
        "message": str(error),
        "code": error.code,
        "phase": error.phase,
        "available_vram_bytes": error.available_vram_bytes,
        "required_vram_bytes": error.required_vram_bytes,
        "total_vram_bytes": error.total_vram_bytes,
    }


def _validated_embedding(values: Sequence[float]) -> tuple[float, ...]:
    embedding = tuple(float(value) for value in values)
    if len(embedding) != DIMENSIONS:
        raise ValueError(
            f"query embedder returned {len(embedding)} dimensions; "
            f"expected {DIMENSIONS}"
        )
    return embedding


def _serve_embed(
    connection: socket.socket,
    request: Mapping[str, object],
    state: _ServerState,
    embed: EmbedCallable,
) -> None:
    query = request.get("query")
    quiet = request.get("quiet")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("embed request query must be non-blank text")
    if not isinstance(quiet, bool):
        raise TypeError("embed request quiet must be boolean")
    reused = state.loaded
    started = monotonic()
    model_started: float | None = None
    model_load_ms = 0
    _send(connection, {"kind": "progress", "phase": "query_embed", "state": "running"})

    def progress(phase: str, progress_state: str) -> None:
        nonlocal model_load_ms, model_started
        if reused and phase == "model_load":
            return
        observed = monotonic()
        if phase == "model_load" and progress_state == "running":
            model_started = observed
        elif (
            phase == "model_load"
            and progress_state == "complete"
            and model_started is not None
            and not reused
        ):
            model_load_ms = round((observed - model_started) * 1000)
            state.loaded = True
            state.warm_since = datetime.now(UTC).isoformat()
        _send(
            connection,
            {"kind": "progress", "phase": phase, "state": progress_state},
        )

    try:
        with model_output_scope(quiet=quiet):
            embedding = _validated_embedding(embed(query, progress=progress))
    except ModelUnavailable as error:
        _send(
            connection,
            {"kind": "progress", "phase": "query_embed", "state": "degraded"},
        )
        _send(connection, _model_unavailable(error))
        return
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _send(
            connection,
            {
                "kind": "error",
                "detail": f"{type(error).__name__}: {error}",
            },
        )
        return
    finished = monotonic()
    if not state.loaded:
        state.loaded = True
        state.warm_since = datetime.now(UTC).isoformat()
    _send(
        connection,
        {"kind": "progress", "phase": "query_embed", "state": "complete"},
    )
    total_ms = round((finished - started) * 1000)
    _send(
        connection,
        {
            "kind": "result",
            "embedding": embedding,
            "model_load_ms": 0 if reused else model_load_ms,
            "query_embed_ms": max(0, total_ms - model_load_ms),
            "warm_reused": reused,
        },
    )


def _dispatch_request(
    connection: socket.socket,
    request: Mapping[str, object],
    state: _ServerState,
    embed: EmbedCallable,
) -> tuple[bool, bool]:
    kind = request.get("kind")
    if kind == "hello":
        _send(connection, _hello(state))
        return False, False
    if kind == "shutdown":
        _send(connection, {"kind": "shutdown", "state": "complete"})
        return True, False
    if kind == "embed":
        _serve_embed(connection, request, state, embed)
        return False, True
    raise ValueError(f"unsupported query embedder request kind: {kind!r}")


def _serve_request(
    connection: socket.socket,
    state: _ServerState,
    embed: EmbedCallable,
) -> tuple[bool, bool]:
    try:
        request = _read_request(connection)
        if request is None:
            return False, False
        outcome = _dispatch_request(connection, request, state, embed)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError, ValueError) as error:
        _send(
            connection,
            {"kind": "error", "detail": f"{type(error).__name__}: {error}"},
        )
        return False, False
    else:
        return outcome


def _same_file(path: Path, file_descriptor: int) -> bool:
    try:
        descriptor = os.fstat(file_descriptor)
        current = path.stat()
    except OSError, ValueError:
        return False
    return (descriptor.st_dev, descriptor.st_ino) == (current.st_dev, current.st_ino)


def _path_identity(path: Path) -> tuple[int, int]:
    current = path.stat()
    return current.st_dev, current.st_ino


def _path_matches(path: Path, identity: tuple[int, int]) -> bool:
    try:
        return _path_identity(path) == identity
    except FileNotFoundError:
        return False


def _unlink_owned_socket(path: Path, identity: tuple[int, int]) -> None:
    if _path_matches(path, identity):
        path.unlink(missing_ok=True)


def _acquire_lifetime_lock(file_descriptor: int) -> bool:
    deadline = monotonic() + _LIFETIME_LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            sleep(min(_LIFETIME_LOCK_RETRY_SECONDS, remaining))
        else:
            return True


def _serve_loop(
    listener: socket.socket,
    paths: QueryEmbedderPaths,
    lock_descriptor: int,
    socket_identity: tuple[int, int],
    warm_seconds: float,
    state: _ServerState,
    embed: EmbedCallable,
) -> None:
    last_completed = monotonic()
    while _same_file(paths.lock, lock_descriptor) and _path_matches(
        paths.socket, socket_identity
    ):
        remaining = warm_seconds - (monotonic() - last_completed)
        if remaining <= 0:
            return
        listener.settimeout(min(0.1, remaining))
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            continue
        with connection:
            connection.settimeout(max(0.1, remaining))
            if _peer_uid(connection) != os.getuid():
                _read_request(connection)
                _send(
                    connection,
                    {
                        "kind": "error",
                        "code": "peer_uid_mismatch",
                        "detail": "query embedder accepts only same-user clients",
                    },
                )
                continue
            shutdown, completed = _serve_request(connection, state, embed)
        if shutdown:
            return
        if completed:
            last_completed = monotonic()


def serve_query_embedder(
    *,
    runtime_dir: Path | None = None,
    warm_seconds: float | None = None,
    embed: EmbedCallable,
    package_version: str,
    model_revision: str | None,
    embedder: str = "real",
    test_token_digest: str | None = None,
) -> bool:
    """Serve one warm helper lifetime, or return false if another owns it."""
    selected_warm_seconds = (
        semantic_warm_seconds() if warm_seconds is None else warm_seconds
    )
    if selected_warm_seconds <= 0:
        raise ValueError("semantic warm window must be positive")
    paths = query_embedder_paths(runtime_dir)
    with paths.lock.open("a+", encoding="utf-8") as lock:
        paths.lock.chmod(0o600)
        if not _acquire_lifetime_lock(lock.fileno()):
            return False
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        paths.socket.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_identity: tuple[int, int] | None = None
        try:
            listener.bind(str(paths.socket))
            paths.socket.chmod(0o600)
            listener.listen()
            socket_identity = _path_identity(paths.socket)
            state = _ServerState(
                package_version,
                model_revision,
                embedder,
                test_token_digest,
            )
            _serve_loop(
                listener,
                paths,
                lock.fileno(),
                socket_identity,
                selected_warm_seconds,
                state,
                embed,
            )
        finally:
            if socket_identity is not None:
                _unlink_owned_socket(paths.socket, socket_identity)
            listener.close()
            try:
                release_model()
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return True


def semantic_warm_seconds() -> float:
    """Read the operator-tunable warm window, defaulting to thirty seconds."""
    configured = os.environ.get("CC_SEARCH_SEMANTIC_WARM_SECONDS")
    if configured is None:
        return SEMANTIC_WARM_SECONDS
    try:
        value = float(configured)
    except ValueError as error:
        raise ValueError("CC_SEARCH_SEMANTIC_WARM_SECONDS must be numeric") from error
    if value <= 0:
        raise ValueError("CC_SEARCH_SEMANTIC_WARM_SECONDS must be positive")
    return value


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _deterministic_test_embedder(
    text: str,
    *,
    progress: ProgressCallable | None = None,
) -> list[float]:
    if progress is not None:
        progress("model_preflight", "running")
        progress("model_preflight", "complete")
        progress("model_load", "running")
        progress("model_load", "complete")
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [0.0] * DIMENSIONS
    vector[int.from_bytes(digest[:2]) % DIMENSIONS] = 1.0
    return vector


def _server_configuration(
    test_embedder_token: str | None,
) -> tuple[EmbedCallable, str, str | None]:
    if test_embedder_token is None:
        return embed_query, "real", None
    environment_token = os.environ.get(_TEST_GUARD_ENV)
    if environment_token is None or not hmac.compare_digest(
        environment_token,
        test_embedder_token,
    ):
        raise ValueError(
            "test query embedder requires a matching "
            "CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN"
        )
    return _deterministic_test_embedder, "test", _token_digest(environment_token)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cc-search-chats query embedder")
    parser.add_argument("--test-embedder-token", default=None, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    """Run the detached helper process."""
    args = _build_parser().parse_args()
    try:
        embed, embedder_name, token_digest = _server_configuration(
            args.test_embedder_token
        )
        serve_query_embedder(
            embed=embed,
            package_version=__version__,
            model_revision=local_model_revision(),
            embedder=embedder_name,
            test_token_digest=token_digest,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"query embedder failed: {error}", file=sys.stderr)
        return 1
    os._exit(0)


def request_query_embedding(
    query: str,
    *,
    progress: ProgressCallable,
    quiet: bool = False,
) -> QueryEmbeddingResult:
    """Return an embedding from the compatible same-user helper."""
    log = "the configured runtime directory"
    try:
        paths = query_embedder_paths()
        log = str(paths.log)
        _ensure_compatible_helper(paths)
        request = {"kind": "embed", "query": query, "quiet": quiet}
        try:
            response = _exchange(paths.socket, request, progress=progress)
        except OSError:
            _ensure_compatible_helper(paths)
            response = _exchange(paths.socket, request, progress=progress)
    except OSError as error:
        raise RuntimeError(
            f"query embedder communication failed; inspect {log}: {error}"
        ) from error
    return _embedding_result(response)


def shutdown_query_embedder() -> None:
    """Ask a live helper to exit and wait until it releases its resources."""
    paths = query_embedder_paths()
    helper_pid = _recorded_helper_pid(paths.lock)
    try:
        response = _exchange(paths.socket, {"kind": "shutdown"})
    except OSError:
        if _lifetime_lock_free(paths):
            paths.socket.unlink(missing_ok=True)
            return
        helper_pid = _recorded_helper_pid(paths.lock)
        response = None
    if response is not None and response.get("kind") != "shutdown":
        raise RuntimeError("query embedder rejected shutdown")
    while not _lifetime_lock_free(paths):
        sleep(0.02)
    paths.socket.unlink(missing_ok=True)
    _wait_for_process_exit(helper_pid, paths.log)


def _decode_response(line: str) -> dict[str, object]:
    response = json.loads(line)
    if not isinstance(response, dict):
        raise TypeError("query embedder response must be a JSON object")
    return response


def _exchange(
    path: Path,
    request: Mapping[str, object],
    *,
    progress: ProgressCallable | None = None,
) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        _send(client, request)
        with client.makefile("r", encoding="utf-8") as stream:
            for line in stream:
                response = _decode_response(line)
                if response.get("kind") == "progress":
                    phase = response.get("phase")
                    state = response.get("state")
                    if not isinstance(phase, str) or not isinstance(state, str):
                        raise RuntimeError("query embedder returned malformed progress")
                    if progress is not None:
                        progress(phase, state)
                    continue
                return response
    raise RuntimeError("query embedder closed without a terminal response")


def _try_hello(paths: QueryEmbedderPaths) -> dict[str, object] | None:
    try:
        response = _exchange(paths.socket, {"kind": "hello"})
    except OSError:
        return None
    return response if response.get("kind") == "hello" else None


def _compatible_hello(hello: Mapping[str, object]) -> bool:
    if hello.get("package_version") != __version__:
        return False
    if hello.get("model_revision") != local_model_revision():
        return False
    embedder_name = hello.get("embedder")
    if embedder_name == "real":
        return hello.get("test_token_digest") is None
    if embedder_name != "test":
        return False
    token = os.environ.get(_TEST_GUARD_ENV)
    digest = hello.get("test_token_digest")
    return (
        token is not None
        and isinstance(digest, str)
        and hmac.compare_digest(_token_digest(token), digest)
    )


def _lifetime_lock_free(paths: QueryEmbedderPaths) -> bool:
    with paths.lock.open("a+", encoding="utf-8") as lock:
        return _descriptor_lifetime_lock_free(lock.fileno())


def _descriptor_lifetime_lock_free(file_descriptor: int) -> bool:
    try:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    fcntl.flock(file_descriptor, fcntl.LOCK_UN)
    return True


def _recorded_helper_pid(path: Path) -> int | None:
    try:
        content = path.read_text(encoding="utf-8").strip()
        pid = int(content)
    except OSError, ValueError:
        return None
    return pid if pid > 0 else None


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_exit(pid: int | None, log: Path) -> None:
    if pid is None or pid == os.getpid():
        return
    deadline = monotonic() + _PROCESS_EXIT_WAIT_SECONDS
    while _process_exists(pid):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"query embedder process {pid} did not exit within "
                f"{_PROCESS_EXIT_WAIT_SECONDS:g} seconds after releasing its "
                f"lifetime lock; inspect {log}"
            )
        sleep(min(_PROCESS_EXIT_POLL_SECONDS, remaining))


def _stop_incompatible_helper(
    paths: QueryEmbedderPaths,
    *,
    lock_descriptor: int,
) -> None:
    helper_pid = _recorded_helper_pid(paths.lock)
    try:
        response = _exchange(paths.socket, {"kind": "shutdown"})
    except OSError:
        response = None
    if response is not None and response.get("kind") != "shutdown":
        raise RuntimeError("incompatible query embedder rejected shutdown")
    while not _descriptor_lifetime_lock_free(lock_descriptor):
        sleep(0.02)
    paths.socket.unlink(missing_ok=True)
    _wait_for_process_exit(helper_pid, paths.log)


def _spawn_detached_helper(paths: QueryEmbedderPaths) -> subprocess.Popen[bytes]:
    descriptor = os.open(
        paths.log,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen(
            _query_embedder_command(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    Thread(
        target=process.wait,
        name="cc-search-query-embedder-reaper",
        daemon=True,
    ).start()
    return process


def _wait_for_hello(
    paths: QueryEmbedderPaths,
    process: subprocess.Popen[bytes] | None,
    *,
    lock_descriptor: int,
) -> dict[str, object] | None:
    while True:
        hello = _try_hello(paths)
        if hello is not None:
            return hello
        if _descriptor_lifetime_lock_free(lock_descriptor):
            if process is None:
                return None
            if process.poll() is not None:
                raise RuntimeError(
                    f"query embedder exited before binding; inspect {paths.log}"
                )
        sleep(0.02)


def _spawn_compatible_helper(
    paths: QueryEmbedderPaths,
    *,
    lock_descriptor: int,
) -> dict[str, object]:
    while True:
        hello = _try_hello(paths)
        if hello is not None:
            if _compatible_hello(hello):
                return hello
            _stop_incompatible_helper(paths, lock_descriptor=lock_descriptor)
            continue
        if not _descriptor_lifetime_lock_free(lock_descriptor):
            hello = _wait_for_hello(
                paths,
                None,
                lock_descriptor=lock_descriptor,
            )
            if hello is not None:
                continue
        paths.socket.unlink(missing_ok=True)
        process = _spawn_detached_helper(paths)
        hello = _wait_for_hello(
            paths,
            process,
            lock_descriptor=lock_descriptor,
        )
        if hello is None:
            continue
        if _compatible_hello(hello):
            return hello
        _stop_incompatible_helper(paths, lock_descriptor=lock_descriptor)


def _ensure_compatible_helper(paths: QueryEmbedderPaths) -> dict[str, object]:
    hello = _try_hello(paths)
    if hello is not None and _compatible_hello(hello):
        return hello
    with _SPAWN_THREAD_LOCK, paths.lock.open("a+", encoding="utf-8") as spawn_lock:
        fcntl.lockf(spawn_lock, fcntl.LOCK_EX)
        try:
            return _spawn_compatible_helper(
                paths,
                lock_descriptor=spawn_lock.fileno(),
            )
        finally:
            fcntl.lockf(spawn_lock, fcntl.LOCK_UN)


def _optional_int(value: object, field: str) -> int:
    if not isinstance(value, int):
        raise TypeError(f"query embedder returned malformed {field}")
    return value


def _optional_vram_bytes(value: object, field: str) -> int | None:
    if value is None or isinstance(value, int):
        return value
    raise TypeError(f"query embedder returned malformed {field}")


def _embedding_result(response: Mapping[str, object]) -> QueryEmbeddingResult:
    kind = response.get("kind")
    if kind == "model_unavailable":
        raise ModelUnavailable(
            str(response.get("message", "semantic model unavailable")),
            code=str(response.get("code", "model_unavailable")),
            phase=str(response.get("phase", "query_embed")),
            available_vram_bytes=_optional_vram_bytes(
                response.get("available_vram_bytes"), "available_vram_bytes"
            ),
            required_vram_bytes=_optional_vram_bytes(
                response.get("required_vram_bytes"), "required_vram_bytes"
            ),
            total_vram_bytes=_optional_vram_bytes(
                response.get("total_vram_bytes"), "total_vram_bytes"
            ),
        )
    if kind == "error":
        raise RuntimeError(str(response.get("detail", "query embedder failed")))
    if kind != "result":
        raise RuntimeError("query embedder returned an unexpected terminal response")
    raw_embedding = response.get("embedding")
    if not isinstance(raw_embedding, list):
        raise TypeError("query embedder returned a malformed embedding")
    warm_reused = response.get("warm_reused")
    if not isinstance(warm_reused, bool):
        raise TypeError("query embedder returned malformed warm_reused")
    return QueryEmbeddingResult(
        _validated_embedding(raw_embedding),
        _optional_int(response.get("model_load_ms"), "model_load_ms"),
        _optional_int(response.get("query_embed_ms"), "query_embed_ms"),
        warm_reused,
    )


if __name__ == "__main__":
    raise SystemExit(main())
