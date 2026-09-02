"""The pinned local model revision is adopted only by index and verified by search."""

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
from cc_search_chats.semantic.model import MODEL_REVISION
from cc_search_chats.storage.postgresql import migrate

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
PROFILE_ID = "nemotron-3-embed-8b-bf16:chunks-v1"
OBSERVED_REVISION = "a" * 40


def _configure_cli(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    shutil.copy(
        FIXTURES / "claude_primary.jsonl",
        claude_root / "claude-session-primary.jsonl",
    )
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOT", str(codex_root))
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *args: str,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", *args])
    with pytest.raises(SystemExit) as stopped:
        main()
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert isinstance(stopped.value.code, int)
    assert isinstance(payload, dict)
    return stopped.value.code, payload


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def _vector() -> list[float]:
    vector = [0.0] * 1024
    vector[0] = 1.0
    return vector


def _passage_embeddings(texts, **kwargs):
    progress = kwargs.get("progress")
    if progress is not None:
        progress("model_preflight", "running")
        progress("model_preflight", "complete")
        progress("model_load", "running")
        progress("model_load", "complete")
    return [_vector() for _ in texts]


def _stub_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cc_search_chats.cli.embed_passages", _passage_embeddings)
    monkeypatch.setattr("cc_search_chats.cli.chunk_passages", _single_chunks)

    def query_embedding(_query, *, timeout_seconds, progress, quiet):
        assert timeout_seconds > 0
        assert progress is not None
        assert quiet is True
        return _vector()

    monkeypatch.setattr("cc_search_chats.cli._bounded_query_embedding", query_embedding)


def _profile_version(connection: psycopg.Connection) -> tuple[str, str]:
    return next(
        connection.execute(
            "SELECT model_revision, xmin::text "
            "FROM cc_search_chats.embedding_profile WHERE profile_id = %s",
            (PROFILE_ID,),
        )
    )


def test_index_adopts_unknown_revision_once(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(postgres_cluster, tmp_path, monkeypatch)
    _stub_semantics(monkeypatch)
    monkeypatch.setattr(
        "cc_search_chats.cli.local_model_revision",
        lambda: OBSERVED_REVISION,
        raising=False,
    )
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "UPDATE cc_search_chats.embedding_profile "
            "SET model_revision = 'unknown' WHERE profile_id = %s",
            (PROFILE_ID,),
        )

    first_code, _ = _run(monkeypatch, capsys, "index", "--json")
    assert first_code == 0
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        first = _profile_version(connection)
    assert first[0] == OBSERVED_REVISION

    second_code, _ = _run(monkeypatch, capsys, "index", "--json")
    assert second_code == 0
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        second = _profile_version(connection)
    assert second == first


@pytest.mark.parametrize(
    ("observed_revision", "warning_detail"),
    [
        (
            OBSERVED_REVISION,
            f"{OBSERVED_REVISION}; run cc-search-chats index to record it",
        ),
        (
            None,
            "no local revision available; run cc-search-chats index to record it",
        ),
    ],
)
def test_semantic_search_warns_without_writing_unknown_revision(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    observed_revision: str | None,
    warning_detail: str,
) -> None:
    _configure_cli(postgres_cluster, tmp_path, monkeypatch)
    _stub_semantics(monkeypatch)
    monkeypatch.setattr(
        "cc_search_chats.cli.local_model_revision",
        lambda: observed_revision,
        raising=False,
    )
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "UPDATE cc_search_chats.embedding_profile "
            "SET model_revision = 'unknown' WHERE profile_id = %s",
            (PROFILE_ID,),
        )
    code, _ = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        if observed_revision is None:
            assert _profile_version(connection)[0] == "unknown"
        connection.execute(
            "UPDATE cc_search_chats.embedding_profile "
            "SET model_revision = 'unknown' WHERE profile_id = %s",
            (PROFILE_ID,),
        )
        before = _profile_version(connection)

    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "visible",
        "--semantic",
        "--json",
    )

    assert code == 0
    warnings = cast("list[dict[str, str]]", payload["warnings"])
    assert {
        "code": "model_revision_unverified",
        "detail": warning_detail,
    } in warnings
    assert payload["results"]
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        assert _profile_version(connection) == before


def test_semantic_search_fails_closed_on_model_revision_mismatch(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(postgres_cluster, tmp_path, monkeypatch)
    _stub_semantics(monkeypatch)
    monkeypatch.setattr(
        "cc_search_chats.cli.local_model_revision",
        lambda: OBSERVED_REVISION,
        raising=False,
    )
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)

    code, payload = _run(
        monkeypatch,
        capsys,
        "search",
        "visible",
        "--semantic",
        "--json",
    )

    assert code == 8
    error = cast("dict[str, object]", payload["error"])
    assert error["code"] == "model_revision_mismatch"
    assert OBSERVED_REVISION in str(error["message"])
    assert MODEL_REVISION in str(error["message"])


def test_index_mismatch_requires_a_full_semantic_rebuild(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(postgres_cluster, tmp_path, monkeypatch)
    _stub_semantics(monkeypatch)
    monkeypatch.setattr(
        "cc_search_chats.cli.local_model_revision",
        lambda: OBSERVED_REVISION,
        raising=False,
    )
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)

    code, payload = _run(monkeypatch, capsys, "index", "--json")

    assert code == 8
    error = cast("dict[str, object]", payload["error"])
    assert error["code"] == "model_revision_mismatch"
    assert "full semantic rebuild under the new revision is required" in str(
        error["message"]
    )
    assert MODEL_REVISION in str(error["message"])
    assert OBSERVED_REVISION in str(error["message"])
    assert "docs/runbooks/postgresql-index-maintenance.md" in str(error["message"])


def test_human_semantic_search_does_not_print_revision_warning(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(postgres_cluster, tmp_path, monkeypatch)
    _stub_semantics(monkeypatch)
    monkeypatch.setattr(
        "cc_search_chats.cli.local_model_revision",
        lambda: None,
        raising=False,
    )
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "UPDATE cc_search_chats.embedding_profile "
            "SET model_revision = 'unknown' WHERE profile_id = %s",
            (PROFILE_ID,),
        )
    index_code, _ = _run(monkeypatch, capsys, "index", "--json")
    assert index_code == 0

    monkeypatch.setattr(
        sys,
        "argv",
        ["cc-search-chats", "search", "visible", "--semantic"],
    )
    with pytest.raises(SystemExit) as stopped:
        main()
    output = capsys.readouterr()

    assert stopped.value.code == 0
    assert "semantic search (hybrid model ranking): visible" in output.out
    assert "visible primary user" in output.out
    assert "WARNING:" not in output.out
    assert "WARNING:" not in output.err
    events = [json.loads(line) for line in output.err.splitlines()]
    terminal = cast("dict[str, object]", events[-1])
    warnings = cast("list[dict[str, str]]", terminal["warning"])
    assert any(warning["code"] == "model_revision_unverified" for warning in warnings)


def test_json_index_stderr_is_pure_v4_ndjson(
    postgres_cluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(postgres_cluster, tmp_path, monkeypatch)
    monkeypatch.setattr("cc_search_chats.cli.chunk_passages", _single_chunks)
    monkeypatch.setattr(
        "cc_search_chats.cli.local_model_revision",
        lambda: OBSERVED_REVISION,
        raising=False,
    )

    def noisy_embeddings(texts, **_kwargs):
        sys.stderr.write("Loading weights: 0%|\n")
        return [_vector() for _ in texts]

    monkeypatch.setattr("cc_search_chats.cli.embed_passages", noisy_embeddings)
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "UPDATE cc_search_chats.embedding_profile "
            "SET model_revision = 'unknown' WHERE profile_id = %s",
            (PROFILE_ID,),
        )

    monkeypatch.setattr(sys, "argv", ["cc-search-chats", "index", "--json"])
    with pytest.raises(SystemExit) as stopped:
        main()
    output = capsys.readouterr()

    assert stopped.value.code == 0
    events = [json.loads(line) for line in output.err.splitlines()]
    assert events
    assert all(event["schema_version"] == 4 for event in events)
