"""Coverage and staleness reporting for an in-flight JSONL tail."""

import json
import shutil
import sys
from pathlib import Path
from typing import cast

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import main
from cc_search_chats.semantic import SemanticChunk
from cc_search_chats.storage.postgresql import refresh as refresh_module
from cc_search_chats.storage.postgresql import search_messages

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *args: str,
):
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", *args])
    with pytest.raises(SystemExit) as stopped:
        main()
    return stopped.value.code, capsys.readouterr()


def _message_bytes(*, uuid: str, text: str) -> bytes:
    payload = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "claude-session-primary",
        "timestamp": "2026-08-11T05:00:00Z",
        "cwd": "/synthetic/repository",
        "isSidechain": False,
        "message": {"role": "assistant", "content": text},
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def test_pending_tail_is_complete_coverage_and_unindexed_staleness(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection_info = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection_info[key]))
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    claude_root.mkdir()
    codex_root.mkdir()
    source = Path(
        shutil.copy(
            FIXTURES / "claude_primary.jsonl",
            claude_root / "claude-session-primary.jsonl",
        )
    )
    committed_size = source.stat().st_size
    completed_record = _message_bytes(
        uuid="claude-completed-tail",
        text="completed tail sentinel",
    )
    split = len(completed_record) // 2
    with source.open("ab") as stream:
        stream.write(completed_record[:split])

    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    vector = [0.0] * 1024
    vector[0] = 1.0
    monkeypatch.setattr(
        "cc_search_chats.cli.embed_passages",
        lambda texts, **_kwargs: [vector for _ in texts],
    )
    monkeypatch.setattr("cc_search_chats.cli.chunk_passages", _single_chunks)

    code, migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0, migrated.err
    code, indexed = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, indexed.err
    indexed_payload = json.loads(indexed.out)
    coverage = cast("dict[str, object]", indexed_payload["coverage"])
    refresh = cast("dict[str, object]", indexed_payload["refresh"])
    assert indexed_payload["status"] == "complete"
    assert refresh["state"] == "complete"
    assert refresh["pending_bytes"] == split
    assert refresh["failed_sources"] == 0
    assert coverage["pending_tail_files"] == 1
    assert coverage["completeness"] == "complete"
    assert coverage["blocked_files"] == 0
    assert coverage["transient_failure_files"] == 0
    with psycopg.connect(postgres_cluster.dsn) as connection:
        assert search_messages(connection, "visible primary user")
        assert search_messages(connection, "completed tail sentinel") == ()

    code, status = _run(monkeypatch, capsys, "index", "--status", "--json")
    assert code == 0, status.err
    status_payload = json.loads(status.out)
    status_coverage = cast("dict[str, object]", status_payload["coverage"])
    status_refresh = cast("dict[str, object]", status_payload["refresh"])
    index_state = cast("dict[str, object]", status_payload["index_state"])
    assert status_coverage["completeness"] == coverage["completeness"]
    assert status_coverage["pending_tail_files"] == coverage["pending_tail_files"]
    assert status_refresh["pending_bytes"] == refresh["pending_bytes"]
    assert index_state["unindexed"] == {
        "files": 1,
        "directories": 1,
        "bytes": split,
    }

    code, human_status = _run(
        monkeypatch,
        capsys,
        "index",
        "--status",
        "--progress",
        "human",
    )
    assert code == 0, human_status.err
    assert "done: complete" in human_status.err
    assert f"pending tail: 1 file, {split} bytes not searchable yet" in human_status.err

    starts: list[int] = []
    original_reader = refresh_module.read_bounded_jsonl

    def recording_reader(path: Path, **kwargs):
        starts.append(kwargs.get("start_byte_offset", 0))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(refresh_module, "read_bounded_jsonl", recording_reader)
    with source.open("ab") as stream:
        stream.write(completed_record[split:])

    code, completed = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, completed.err
    completed_payload = json.loads(completed.out)
    completed_coverage = cast("dict[str, object]", completed_payload["coverage"])
    completed_refresh = cast("dict[str, object]", completed_payload["refresh"])
    assert completed_refresh["pending_bytes"] == 0
    assert completed_coverage["pending_tail_files"] == 0
    assert starts
    assert starts[0] == committed_size
    with psycopg.connect(postgres_cluster.dsn) as connection:
        assert search_messages(connection, "completed tail sentinel")
