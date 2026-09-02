"""Cross-process single-flight admission for local CLI operations."""

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


def _runtime_dir() -> Path:
    configured = os.environ.get("CC_SEARCH_RUNTIME_DIR")
    directory = (
        Path(configured) if configured is not None else Path.home() / ".cc-search-chats"
    )
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def client_admission(name: str) -> Iterator[None]:
    """Block once until this process owns the local single-flight gate."""
    lock_path = _runtime_dir() / f"postgres-{name}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lock.seek(0)
            lock.truncate()
            json.dump(
                {"command": sys_argv(), "pid": os.getpid(), "started": time.time()},
                lock,
                sort_keys=True,
            )
            lock.flush()
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def sys_argv() -> list[str]:
    """Return argv without importing CLI state into the queue primitive."""
    return list(sys.argv)
