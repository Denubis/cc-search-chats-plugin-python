"""Unix-socket contract and lifecycle tests for the warm query embedder."""

import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
from threading import Barrier, Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING

import pytest

from cc_search_chats import __version__
from cc_search_chats.semantic import query_embedder
from cc_search_chats.semantic.model import DIMENSIONS, GpuProcess, ModelUnavailable
from cc_search_chats.semantic.query_embedder import (
    query_embedder_paths,
    request_query_embedding,
    serve_query_embedder,
    shutdown_query_embedder,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import BinaryIO


def _wait_for(
    condition: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return
        Event().wait(0.01)
    raise AssertionError("condition was not observed before the test deadline")


def _socket_ready(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _request(path: Path, request: dict[str, object]) -> list[dict[str, object]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        client.sendall(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        responses = []
        with client.makefile("r", encoding="utf-8") as stream:
            for line in stream:
                response = json.loads(line)
                assert isinstance(response, dict)
                responses.append(response)
    return responses


def _stub_embed(
    text: str,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> list[float]:
    assert text
    if progress is not None:
        progress("model_load", "running")
        progress("model_load", "complete")
    vector = [0.0] * DIMENSIONS
    vector[0] = 1.0
    return vector


def _start_server(
    runtime_dir: Path,
    *,
    warm_seconds: float = 1.0,
) -> Thread:
    thread = Thread(
        target=serve_query_embedder,
        kwargs={
            "runtime_dir": runtime_dir,
            "warm_seconds": warm_seconds,
            "embed": _stub_embed,
            "package_version": "test-package",
            "model_revision": "a" * 40,
        },
        daemon=True,
    )
    thread.start()
    _wait_for(lambda: _socket_ready(query_embedder_paths(runtime_dir).socket))
    return thread


def test_helper_protocol_hello_embed_and_shutdown(tmp_path: Path) -> None:
    thread = _start_server(tmp_path)
    paths = query_embedder_paths(tmp_path)
    path = paths.socket
    assert stat.S_IMODE(paths.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.socket.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.lock.stat().st_mode) == 0o600

    hello = _request(path, {"kind": "hello"})
    assert hello == [
        {
            "embedder": "real",
            "kind": "hello",
            "loaded": False,
            "model_revision": "a" * 40,
            "package_version": "test-package",
            "test_token_digest": None,
            "warm_since": None,
        }
    ]

    responses = _request(
        path,
        {"kind": "embed", "query": "semantic protocol sentinel", "quiet": True},
    )
    progress = [
        (response["phase"], response["state"])
        for response in responses
        if response["kind"] == "progress"
    ]
    assert progress == [
        ("query_embed", "running"),
        ("model_load", "running"),
        ("model_load", "complete"),
        ("query_embed", "complete"),
    ]
    result = responses[-1]
    assert result["kind"] == "result"
    assert result["warm_reused"] is False
    assert isinstance(result["model_load_ms"], int)
    assert isinstance(result["query_embed_ms"], int)
    embedding = result["embedding"]
    assert isinstance(embedding, list)
    assert len(embedding) == DIMENSIONS

    warmed = _request(path, {"kind": "hello"})[0]
    assert warmed["loaded"] is True
    assert isinstance(warmed["warm_since"], str)
    reused_responses = _request(
        path,
        {"kind": "embed", "query": "warm protocol sentinel", "quiet": True},
    )
    assert all(response.get("phase") != "model_load" for response in reused_responses)
    assert reused_responses[-1]["warm_reused"] is True
    assert reused_responses[-1]["model_load_ms"] == 0
    assert _request(path, {"kind": "shutdown"}) == [
        {"kind": "shutdown", "state": "complete"}
    ]
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert not path.exists()


def test_helper_preserves_model_unavailable_protocol_fields(tmp_path: Path) -> None:
    def unavailable(
        _text: str,
        *,
        progress: Callable[[str, str], None] | None = None,
    ) -> list[float]:
        assert progress is not None
        raise ModelUnavailable(
            "fixture VRAM unavailable",
            code="insufficient_vram",
            phase="model_preflight",
            available_vram_bytes=100,
            required_vram_bytes=200,
            total_vram_bytes=300,
            gpu_processes=(
                GpuProcess(
                    gpu_uuid="GPU-fixture",
                    pid=7654,
                    process_name="/opt/breeze-tts/bin/python3",
                    used_memory_mib=7982,
                    current_process=False,
                ),
            ),
        )

    thread = Thread(
        target=serve_query_embedder,
        kwargs={
            "runtime_dir": tmp_path,
            "warm_seconds": 1,
            "embed": unavailable,
            "package_version": "test-package",
            "model_revision": "a" * 40,
        },
        daemon=True,
    )
    thread.start()
    path = query_embedder_paths(tmp_path).socket
    _wait_for(lambda: _socket_ready(path))

    responses = _request(
        path,
        {"kind": "embed", "query": "unavailable sentinel", "quiet": True},
    )

    assert responses == [
        {"kind": "progress", "phase": "query_embed", "state": "running"},
        {"kind": "progress", "phase": "query_embed", "state": "degraded"},
        {
            "available_vram_bytes": 100,
            "code": "insufficient_vram",
            "gpu_processes": [
                {
                    "current_process": False,
                    "gpu_uuid": "GPU-fixture",
                    "pid": 7654,
                    "process_name": "/opt/breeze-tts/bin/python3",
                    "used_memory_mib": 7982,
                }
            ],
            "gpu_processes_unavailable_reason": None,
            "kind": "model_unavailable",
            "message": "fixture VRAM unavailable",
            "phase": "model_preflight",
            "required_vram_bytes": 200,
            "total_vram_bytes": 300,
        },
    ]
    with pytest.raises(ModelUnavailable) as reconstructed:
        query_embedder._embedding_result(responses[-1])
    assert reconstructed.value.gpu_processes == (
        GpuProcess(
            gpu_uuid="GPU-fixture",
            pid=7654,
            process_name="/opt/breeze-tts/bin/python3",
            used_memory_mib=7982,
            current_process=False,
        ),
    )
    assert reconstructed.value.gpu_processes_unavailable_reason is None
    _request(path, {"kind": "shutdown"})
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_helper_idle_window_exits_and_activity_resets_it(tmp_path: Path) -> None:
    thread = _start_server(tmp_path, warm_seconds=0.3)
    path = query_embedder_paths(tmp_path).socket
    Event().wait(0.2)
    _request(
        path,
        {"kind": "embed", "query": "reset warm window", "quiet": True},
    )
    Event().wait(0.2)
    assert thread.is_alive()
    thread.join(timeout=0.3)
    assert not thread.is_alive()
    assert not path.exists()


def test_helper_refuses_a_peer_with_another_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _start_server(tmp_path)
    path = query_embedder_paths(tmp_path).socket
    original = query_embedder._peer_uid
    monkeypatch.setattr(
        query_embedder, "_peer_uid", lambda _connection: os.getuid() + 1
    )

    assert _request(path, {"kind": "hello"}) == [
        {
            "code": "peer_uid_mismatch",
            "detail": "query embedder accepts only same-user clients",
            "kind": "error",
        }
    ]

    monkeypatch.setattr(query_embedder, "_peer_uid", original)
    _request(path, {"kind": "shutdown"})
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_helper_replaces_a_stale_socket_file(tmp_path: Path) -> None:
    path = query_embedder_paths(tmp_path).socket
    path.write_text("stale", encoding="utf-8")

    thread = _start_server(tmp_path)

    assert _request(path, {"kind": "hello"})[0]["kind"] == "hello"
    _request(path, {"kind": "shutdown"})
    thread.join(timeout=1)
    assert not thread.is_alive()


@pytest.mark.parametrize("removed_resource", ["socket", "lock"])
def test_helper_exits_if_its_socket_or_lifetime_lock_is_lost(
    tmp_path: Path,
    removed_resource: str,
) -> None:
    thread = _start_server(tmp_path)
    paths = query_embedder_paths(tmp_path)
    target = getattr(paths, removed_resource)
    target.unlink()
    if removed_resource == "lock":
        target.write_text("replacement lock", encoding="utf-8")

    thread.join(timeout=1)

    assert not thread.is_alive()
    assert not paths.socket.exists()


def test_lifetime_lock_allows_only_one_concurrent_helper(
    tmp_path: Path,
) -> None:
    barrier = Barrier(3)
    results: list[bool] = []
    results_lock = Lock()

    def start() -> None:
        barrier.wait()
        result = serve_query_embedder(
            runtime_dir=tmp_path,
            warm_seconds=4,
            embed=_stub_embed,
            package_version="test-package",
            model_revision="a" * 40,
        )
        with results_lock:
            results.append(result)

    threads = [Thread(target=start, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    path = query_embedder_paths(tmp_path).socket
    _wait_for(lambda: _socket_ready(path))
    _wait_for(lambda: results == [False], timeout=3)
    _request(path, {"kind": "shutdown"})
    for thread in threads:
        thread.join(timeout=1)

    assert sorted(results) == [False, True]
    assert not any(thread.is_alive() for thread in threads)


def test_helper_retries_lifetime_lock_during_transient_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = query_embedder_paths(tmp_path)
    probe_holds_lock = Event()
    first_helper_attempt = Event()
    release_first_probe = Event()
    stop_probe = Event()
    original_flock = fcntl.flock

    def observed_flock(file_descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
            first_helper_attempt.set()
        original_flock(file_descriptor, operation)

    def probe() -> None:
        with paths.lock.open("a+", encoding="utf-8") as lock:
            original_flock(lock, fcntl.LOCK_EX)
            probe_holds_lock.set()
            release_first_probe.wait(timeout=1)
            original_flock(lock, fcntl.LOCK_UN)
            while not stop_probe.is_set():
                try:
                    original_flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    Event().wait(0.001)
                    continue
                Event().wait(0.001)
                original_flock(lock, fcntl.LOCK_UN)
                Event().wait(0.002)

    probe_thread = Thread(target=probe, daemon=True)
    probe_thread.start()
    assert probe_holds_lock.wait(timeout=1)
    monkeypatch.setattr(query_embedder.fcntl, "flock", observed_flock)
    results: list[bool] = []
    helper = Thread(
        target=lambda: results.append(
            serve_query_embedder(
                runtime_dir=tmp_path,
                warm_seconds=1,
                embed=_stub_embed,
                package_version="test-package",
                model_revision="a" * 40,
            )
        ),
        daemon=True,
    )
    helper.start()
    assert first_helper_attempt.wait(timeout=1)
    release_first_probe.set()
    try:
        _wait_for(lambda: _socket_ready(paths.socket))
    finally:
        stop_probe.set()
        release_first_probe.set()
        probe_thread.join(timeout=1)
    _request(paths.socket, {"kind": "shutdown"})
    helper.join(timeout=1)

    assert results == [True]
    assert not helper.is_alive()
    assert not probe_thread.is_alive()


def test_lifetime_probe_preserves_the_short_spawn_lock(tmp_path: Path) -> None:
    paths = query_embedder_paths(tmp_path)
    probe = (
        "import fcntl,sys; "
        "handle=open(sys.argv[1], 'a+'); "
        "fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)"
    )
    with paths.lock.open("a+", encoding="utf-8") as spawn_lock:
        fcntl.lockf(spawn_lock, fcntl.LOCK_EX)

        assert query_embedder._descriptor_lifetime_lock_free(spawn_lock.fileno())
        contender = subprocess.run(
            [sys.executable, "-c", probe, str(paths.lock)],
            check=False,
            capture_output=True,
        )

        assert contender.returncode != 0
        fcntl.lockf(spawn_lock, fcntl.LOCK_UN)


def _guarded_helper_command(token: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "cc_search_chats.semantic.query_embedder",
        "--test-embedder-token",
        token,
    ]


def test_two_concurrent_clients_spawn_one_helper_and_reuse_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "concurrent-spawn-test-token"
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CC_SEARCH_SEMANTIC_WARM_SECONDS", "1")
    monkeypatch.setenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", token)
    monkeypatch.setenv("CC_SEARCH_MODEL_PATH", str(tmp_path / "missing-model"))
    monkeypatch.setattr(
        query_embedder,
        "_query_embedder_command",
        lambda: _guarded_helper_command(token),
    )
    spawned: list[subprocess.Popen[bytes]] = []
    spawn_detached_helper = query_embedder._spawn_detached_helper

    def observed_spawn(paths: query_embedder.QueryEmbedderPaths):
        process = spawn_detached_helper(paths)
        spawned.append(process)
        return process

    monkeypatch.setattr(query_embedder, "_spawn_detached_helper", observed_spawn)
    barrier = Barrier(3)
    results = []
    result_lock = Lock()

    def request(number: int) -> None:
        barrier.wait()
        result = request_query_embedding(
            f"concurrent query {number}",
            progress=lambda _phase, _state: None,
            quiet=True,
        )
        with result_lock:
            results.append(result)

    threads = [Thread(target=request, args=(number,)) for number in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert len(spawned) == 1
    assert sorted(result.warm_reused for result in results) == [False, True]
    assert all(len(result.embedding) == DIMENSIONS for result in results)
    hello = _request(query_embedder_paths().socket, {"kind": "hello"})[0]
    assert hello["embedder"] == "test"
    shutdown_query_embedder()
    spawned[0].wait(timeout=2)
    assert spawned[0].returncode == 0
    assert not query_embedder_paths().socket.exists()


def test_shutdown_returns_only_after_recorded_helper_pid_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "shutdown-pid-test-token"
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CC_SEARCH_SEMANTIC_WARM_SECONDS", "5")
    monkeypatch.setenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", token)
    monkeypatch.setenv("CC_SEARCH_MODEL_PATH", str(tmp_path / "missing-model"))
    monkeypatch.setattr(
        query_embedder,
        "_query_embedder_command",
        lambda: _guarded_helper_command(token),
    )
    spawned: list[subprocess.Popen[bytes]] = []
    spawn_detached_helper = query_embedder._spawn_detached_helper

    def observed_spawn(paths: query_embedder.QueryEmbedderPaths):
        process = spawn_detached_helper(paths)
        spawned.append(process)
        return process

    monkeypatch.setattr(query_embedder, "_spawn_detached_helper", observed_spawn)

    try:
        result = request_query_embedding(
            "recorded helper pid",
            progress=lambda _phase, _state: None,
            quiet=True,
        )
        assert len(result.embedding) == DIMENSIONS
        assert len(spawned) == 1
        recorded_pid = int(query_embedder_paths().lock.read_text().strip())
        assert recorded_pid == spawned[0].pid
        assert _process_exists(recorded_pid)

        shutdown_query_embedder()

        _wait_for(lambda: not _process_exists(recorded_pid), timeout=1)
        assert spawned[0].wait(timeout=1) == 0
    finally:
        if spawned and spawned[0].poll() is None:
            spawned[0].terminate()
            spawned[0].wait(timeout=2)


def test_shutdown_names_recorded_pid_and_log_if_process_does_not_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path))
    paths = query_embedder_paths()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    paths.lock.write_text(f"{process.pid}\n", encoding="utf-8")
    monkeypatch.setattr(
        query_embedder,
        "_exchange",
        lambda _path, _request: {"kind": "shutdown"},
    )
    monkeypatch.setattr(query_embedder, "_lifetime_lock_free", lambda _paths: True)
    monkeypatch.setattr(query_embedder, "_PROCESS_EXIT_WAIT_SECONDS", 0.02)

    try:
        with pytest.raises(RuntimeError) as raised:
            shutdown_query_embedder()

        detail = str(raised.value)
        assert str(process.pid) in detail
        assert str(paths.log) in detail
    finally:
        process.terminate()
        process.wait(timeout=2)


def test_client_replaces_a_test_helper_with_a_different_guard_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_token = "old-test-token"
    new_token = "new-test-token"
    old_environment = {
        **os.environ,
        "CC_SEARCH_RUNTIME_DIR": str(tmp_path),
        "CC_SEARCH_SEMANTIC_WARM_SECONDS": "2",
        "CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN": old_token,
        "CC_SEARCH_MODEL_PATH": str(tmp_path / "missing-model"),
    }
    old_process = subprocess.Popen(
        _guarded_helper_command(old_token),
        env=old_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    Thread(
        target=old_process.wait,
        name="old-test-helper-reaper",
        daemon=True,
    ).start()
    path = query_embedder_paths(tmp_path).socket
    _wait_for(lambda: _socket_ready(path))
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CC_SEARCH_SEMANTIC_WARM_SECONDS", "1")
    monkeypatch.setenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", new_token)
    monkeypatch.setenv("CC_SEARCH_MODEL_PATH", str(tmp_path / "missing-model"))
    monkeypatch.setattr(
        query_embedder,
        "_query_embedder_command",
        lambda: _guarded_helper_command(new_token),
    )

    result = request_query_embedding(
        "replacement query",
        progress=lambda _phase, _state: None,
        quiet=True,
    )

    old_process.wait(timeout=2)
    assert old_process.returncode == 0
    assert len(result.embedding) == DIMENSIONS
    hello = _request(path, {"kind": "hello"})[0]
    assert hello["embedder"] == "test"
    assert hello["test_token_digest"] == query_embedder._token_digest(new_token)
    shutdown_query_embedder()


@pytest.mark.parametrize(
    ("package_version", "model_revision"),
    [("old-package", None), (__version__, "b" * 40)],
)
def test_client_replaces_a_helper_with_stale_code_or_model_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_version: str,
    model_revision: str | None,
) -> None:
    token = "stale-helper-test-token"
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CC_SEARCH_SEMANTIC_WARM_SECONDS", "1")
    monkeypatch.setenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", token)
    monkeypatch.setenv("CC_SEARCH_MODEL_PATH", str(tmp_path / "missing-model"))
    monkeypatch.setattr(
        query_embedder,
        "_query_embedder_command",
        lambda: _guarded_helper_command(token),
    )
    thread = Thread(
        target=serve_query_embedder,
        kwargs={
            "runtime_dir": tmp_path,
            "warm_seconds": 2,
            "embed": _stub_embed,
            "package_version": package_version,
            "model_revision": model_revision,
        },
        daemon=True,
    )
    thread.start()
    _wait_for(lambda: _socket_ready(query_embedder_paths(tmp_path).socket))

    result = request_query_embedding(
        "stale helper replacement",
        progress=lambda _phase, _state: None,
        quiet=True,
    )

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(result.embedding) == DIMENSIONS
    assert (
        _request(query_embedder_paths().socket, {"kind": "hello"})[0]["embedder"]
        == "test"
    )
    shutdown_query_embedder()


def test_test_embedder_requires_both_guard_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", raising=False)

    with pytest.raises(ValueError, match="matching"):
        query_embedder._server_configuration("flag-only-token")


@pytest.mark.parametrize(
    ("client_token", "compatible"),
    [(None, False), ("different-token", False), ("helper-token", True)],
)
def test_client_accepts_test_helper_only_with_its_matching_guard_token(
    monkeypatch: pytest.MonkeyPatch,
    client_token: str | None,
    compatible: bool,
) -> None:
    if client_token is None:
        monkeypatch.delenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", client_token)
    hello = {
        "package_version": __version__,
        "model_revision": None,
        "embedder": "test",
        "test_token_digest": query_embedder._token_digest("helper-token"),
    }
    monkeypatch.setattr(query_embedder, "local_model_revision", lambda: None)

    assert query_embedder._compatible_hello(hello) is compatible


def test_normal_helper_command_never_selects_the_test_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN", "environment-only")

    assert query_embedder._query_embedder_command() == [
        sys.executable,
        "-m",
        "cc_search_chats.semantic.query_embedder",
    ]


def test_helper_death_before_bind_names_its_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CC_SEARCH_MODEL_PATH", str(tmp_path / "missing-model"))
    monkeypatch.setattr(
        query_embedder,
        "_query_embedder_command",
        lambda: [sys.executable, "-c", "raise SystemExit(3)"],
    )

    with pytest.raises(
        RuntimeError, match=r"exited before binding.*query-embedder.log"
    ):
        request_query_embedding(
            "helper death sentinel",
            progress=lambda _phase, _state: None,
            quiet=True,
        )

    log = query_embedder_paths().log
    assert log.is_file()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_helper_spawn_replaces_the_previous_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = query_embedder_paths(tmp_path)
    paths.log.write_bytes(b"previous helper output\n")

    class Process:
        def wait(self) -> int:
            return 0

    def popen(
        _command: list[str],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        **_kwargs: object,
    ) -> Process:
        assert stdout is stderr
        stdout.write(b"current helper output\n")
        stdout.flush()
        return Process()

    monkeypatch.setattr(query_embedder.subprocess, "Popen", popen)

    query_embedder._spawn_detached_helper(paths)

    assert paths.log.read_bytes() == b"current helper output\n"


def test_client_normalizes_runtime_setup_failure_as_a_helper_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_paths() -> None:
        raise OSError("fixture runtime directory unavailable")

    monkeypatch.setattr(query_embedder, "query_embedder_paths", fail_paths)

    with pytest.raises(
        RuntimeError,
        match=r"configured runtime directory.*fixture runtime directory unavailable",
    ):
        request_query_embedding(
            "runtime failure sentinel",
            progress=lambda _phase, _state: None,
            quiet=True,
        )
