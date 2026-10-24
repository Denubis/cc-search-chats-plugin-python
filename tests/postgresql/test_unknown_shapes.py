"""Unknown record shapes through the PostgreSQL CLI: ignore mode plumbing."""

import json
import shutil
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import main
from cc_search_chats.semantic import SemanticChunk

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
):
    monkeypatch.setattr(sys, "argv", ["cc-search-chats", *args])
    with pytest.raises(SystemExit) as stopped:
        main()
    output = capsys.readouterr()
    return stopped.value.code, output


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def _unknown_item(ordinal: int, kind: str = "future_item_kind") -> bytes:
    record = {
        "timestamp": "2026-08-11T03:00:02Z",
        "type": "response_item",
        "ordinal": ordinal,
        "payload": {"type": kind, "id": "fi_synthetic", "text": "cli-decoy-rue"},
    }
    return json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"


@pytest.fixture
def cli_corpus(postgres_cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the CLI at the disposable cluster and an empty-but-valid root set."""
    monkeypatch.setattr(
        "cc_search_chats.cli._contain_semantic_index", lambda _args: None
    )
    monkeypatch.setenv("CC_SEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    connection = conninfo_to_dict(postgres_cluster.dsn)
    for variable, key in (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGDATABASE", "dbname"),
        ("PGUSER", "user"),
    ):
        monkeypatch.setenv(variable, str(connection[key]))
    claude_root, codex_root = tmp_path / "claude", tmp_path / "codex"
    claude_root.mkdir()
    codex_day = codex_root / "2026" / "10" / "24"
    codex_day.mkdir(parents=True)
    antigravity_root = tmp_path / "brain"
    antigravity_root.mkdir()
    monkeypatch.setenv("CC_SEARCH_CLAUDE_ROOTS", str(claude_root))
    monkeypatch.setenv("CC_SEARCH_CODEX_ROOTS", str(codex_root))
    monkeypatch.setenv("CC_SEARCH_ANTIGRAVITY_ROOTS", str(antigravity_root))
    vector = [0.0] * 1024
    vector[0] = 1.0
    monkeypatch.setattr(
        "cc_search_chats.cli.embed_passages",
        lambda texts, **_kwargs: [vector for _ in texts],
    )
    monkeypatch.setattr("cc_search_chats.cli.chunk_passages", _single_chunks)
    return claude_root, codex_day, antigravity_root


def _codex_source_with_unknown_item(codex_day: Path, name: str) -> Path:
    fixture_records = (
        (FIXTURES / "codex_modern_primary_145.jsonl")
        .read_bytes()
        .splitlines(keepends=True)
    )
    source = codex_day / name
    source.write_bytes(
        fixture_records[0] + fixture_records[3] + _unknown_item(2) + fixture_records[5]
    )
    return source


@pytest.mark.parametrize("route", ["flag", "environment"])
def test_index_ignore_mode_reaches_the_refresh_through_flag_or_variable(
    cli_corpus,
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
) -> None:
    """AC13: either route enables ignore mode; the default blocks."""
    claude_root, codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    _codex_source_with_unknown_item(codex_day, "rollout-unknown.jsonl")
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0

    code, blocked = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (blocked.out, blocked.err)
    blocked_payload = json.loads(blocked.out)
    assert blocked_payload["coverage"]["blocked_files"] == 1
    assert blocked_payload["coverage"]["completeness"] == "partial"

    if route == "flag":
        arguments = ("index", "--ignore-unknown-records", "--json")
    else:
        monkeypatch.setenv("CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS", "1")
        arguments = ("index", "--json")
    code, ignored = _run(monkeypatch, capsys, *arguments)
    assert code == 0, (ignored.out, ignored.err)
    ignored_payload = json.loads(ignored.out)
    assert ignored_payload["coverage"]["blocked_files"] == 0
    assert ignored_payload["coverage"]["completeness"] == "complete"
    with psycopg.connect(postgres_cluster.dsn, autocommit=True) as connection:
        assert next(
            connection.execute(
                "SELECT ignored_unknown_record_count FROM cc_search_chats.refresh_run "
                "WHERE run_id = %s",
                (ignored_payload["refresh"]["run_id"],),
            )
        ) == (1,)
        stored = [
            text
            for (text,) in connection.execute(
                "SELECT prose_content FROM cc_search_chats.message_current"
            )
        ]
    assert {"modern visible assistant", "modern visible user"} <= set(stored)
    assert not any("cli-decoy-rue" in text for text in stored)


_SHAPE_WARNING_KEYS = {
    "code",
    "provider",
    "record_path",
    "keys",
    "record_count",
    "file_count",
    "first_source_root_id",
    "first_source_file_relative",
    "first_source_line",
    "dispositions",
    "parser_state_version",
}


def _shape_warnings(payload: dict) -> list[dict]:
    return [
        warning
        for warning in payload["warnings"]
        if warning["code"] == "unknown_record_shape"
    ]


def _other_warnings(payload: dict) -> list[dict]:
    return [
        warning
        for warning in payload["warnings"]
        if warning["code"] != "unknown_record_shape"
    ]


def test_pending_shape_warnings_persist_on_every_reader_until_resolved(
    cli_corpus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC7, AC11 and the AC14 baseline: empty on a clean corpus, then persistent."""
    claude_root, codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0

    code, baseline = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (baseline.out, baseline.err)
    baseline_payload = json.loads(baseline.out)
    assert baseline_payload["coverage"]["unknown_shape_records"] == 0
    assert baseline_payload["coverage"]["unknown_shapes_pending"] == 0
    assert baseline_payload["coverage"]["unknown_shapes_acknowledged"] == 0
    assert _shape_warnings(baseline_payload) == []
    code, clean_status = _run(monkeypatch, capsys, "index", "--status", "--json")
    assert code == 0
    assert json.loads(clean_status.out)["unknown_shapes"] == {
        "pending": [],
        "acknowledged": [],
    }

    fixture_records = (
        (FIXTURES / "codex_modern_primary_145.jsonl")
        .read_bytes()
        .splitlines(keepends=True)
    )
    (codex_day / "rollout-ignored.jsonl").write_bytes(
        fixture_records[0]
        + fixture_records[3]
        + _unknown_item(2, "ignored_item_kind")
        + fixture_records[5]
    )
    code, ignored = _run(
        monkeypatch, capsys, "index", "--ignore-unknown-records", "--json"
    )
    assert code == 0, (ignored.out, ignored.err)
    (codex_day / "rollout-blocked.jsonl").write_bytes(
        fixture_records[0]
        + fixture_records[3]
        + _unknown_item(2, "blocked_item_kind")
        + fixture_records[5]
    )
    code, blocked = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (blocked.out, blocked.err)
    assert json.loads(blocked.out)["coverage"]["blocked_files"] == 1

    code, unchanged = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0
    unchanged_payload = json.loads(unchanged.out)
    assert unchanged_payload["refresh"]["run_id"] is None
    by_path = {
        warning["record_path"]: warning
        for warning in _shape_warnings(unchanged_payload)
    }
    assert set(by_path) == {
        "response_item/ignored_item_kind",
        "response_item/blocked_item_kind",
    }
    assert by_path["response_item/ignored_item_kind"]["dispositions"] == ["ignored"]
    assert by_path["response_item/blocked_item_kind"]["dispositions"] == ["blocked"]
    for warning in by_path.values():
        assert set(warning) == _SHAPE_WARNING_KEYS
        assert warning["provider"] == "codex"
        assert warning["record_count"] == 1
        assert warning["file_count"] == 1
        assert warning["first_source_line"] == 3
        assert warning["keys"] == [
            "ordinal",
            "payload",
            "timestamp",
            "type",
            "payload.id",
            "payload.text",
            "payload.type",
        ]
    assert "cli-decoy-rue" not in json.dumps(unchanged_payload)
    coverage = unchanged_payload["coverage"]
    assert coverage["unknown_shape_records"] == 2
    assert coverage["unknown_shapes_pending"] == 2
    assert coverage["unknown_shapes_acknowledged"] == 0
    assert coverage["completeness"] == "partial"
    assert {key for key, value in coverage.items() if isinstance(value, list)} == {
        "roots"
    }

    code, status = _run(monkeypatch, capsys, "index", "--status", "--json")
    assert code == 0
    status_payload = json.loads(status.out)
    assert len(_shape_warnings(status_payload)) == 2
    assert status_payload["unknown_shapes"]["acknowledged"] == []
    pending = status_payload["unknown_shapes"]["pending"]
    assert [entry["record_path"] for entry in pending] == sorted(
        entry["record_path"] for entry in pending
    )
    assert {entry["record_path"] for entry in pending} == set(by_path)
    assert all(set(entry) >= _SHAPE_WARNING_KEYS - {"code"} for entry in pending)

    code, searched = _run(
        monkeypatch, capsys, "search", "modern visible", "--literal", "--json"
    )
    assert code == 0, (searched.out, searched.err)
    search_payload = json.loads(searched.out)
    assert "unknown_shapes" not in search_payload
    assert {w["record_path"] for w in _shape_warnings(search_payload)} == set(by_path)
    assert search_payload["coverage"]["unknown_shapes_pending"] == 2
    assert [result["text"] for result in search_payload["results"]] == [
        "modern visible user",
        "modern visible assistant",
    ] or len(search_payload["results"]) == 2

    code, human = _run(monkeypatch, capsys, "search", "modern visible", "--literal")
    assert code == 0
    lines = human.out.splitlines()
    assert not any(line.startswith("unknown shape ") for line in lines)
    note_lines = [line for line in lines if line.startswith("Note: ")]
    assert note_lines == [
        (
            "Note: 2 unrecognised record shapes await a parser update or "
            "acknowledgement; the results above are unaffected. "
            "See `cc-search-chats index --status`."
        )
    ]
    assert lines.index(note_lines[0]) > max(
        index for index, line in enumerate(lines) if "ccchat:v1:" in line
    )
    code, human_status = _run(monkeypatch, capsys, "index", "--status")
    assert code == 0
    status_lines = human_status.out.splitlines()
    status_shape_lines = [
        line for line in status_lines if line.startswith("unknown shape ")
    ]
    assert len(status_shape_lines) == 2
    assert (
        "unknown shape codex response_item/blocked_item_kind "
        "{ordinal, payload, timestamp, type, payload.id, payload.text, "
        "payload.type}: 1 record in 1 file, first "
        "2026/10/24/rollout-blocked.jsonl:3, blocked"
    ) in status_shape_lines
    assert not any(line.startswith("Note: ") for line in status_lines)


def test_one_shape_across_files_is_one_warning_with_summed_counts(
    cli_corpus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8: seven records of one shape in five files, one warning, lowest example."""
    claude_root, codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    fixture_records = (
        (FIXTURES / "codex_modern_primary_145.jsonl")
        .read_bytes()
        .splitlines(keepends=True)
    )
    for index, repeats in enumerate((1, 1, 1, 2, 2)):
        body = fixture_records[0] + fixture_records[3]
        for ordinal in range(2, 2 + repeats):
            body += _unknown_item(ordinal)
        body += fixture_records[5]
        (codex_day / f"rollout-{index}.jsonl").write_bytes(body)
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0

    code, indexed = _run(
        monkeypatch, capsys, "index", "--ignore-unknown-records", "--json"
    )

    assert code == 0, (indexed.out, indexed.err)
    payload = json.loads(indexed.out)
    [warning] = _shape_warnings(payload)
    assert warning["record_count"] == 7
    assert warning["file_count"] == 5
    assert warning["first_source_file_relative"] == "2026/10/24/rollout-0.jsonl"
    assert warning["first_source_line"] == 3
    assert warning["dispositions"] == ["ignored"]
    assert payload["coverage"]["unknown_shape_records"] == 7
    assert payload["coverage"]["unknown_shapes_pending"] == 1
    assert payload["coverage"]["completeness"] == "complete"
    assert _other_warnings(payload) == []


_ACK_KEYS = [
    "ordinal",
    "payload",
    "timestamp",
    "type",
    "payload.id",
    "payload.text",
    "payload.type",
]


def _acknowledgement(record_path: str, keys: list[str]) -> str:
    quoted = ", ".join(f'"{key}"' for key in keys)
    return (
        "[[shape]]\n"
        'provider = "codex"\n'
        f'record_path = "{record_path}"\n'
        f"keys = [{quoted}]\n"
        'note = "synthetic acknowledgement"\n'
        "acknowledged = 2026-10-24\n"
    )


@pytest.fixture
def acknowledgement_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "xdg" / "cc-search-chats"
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return config / "acknowledged-shapes.toml"


def test_acknowledged_shape_stops_warning_but_stays_counted(
    cli_corpus,
    acknowledgement_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC9: acknowledge, re-warn on an extra key, revert on removal."""
    claude_root, codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    fixture_records = (
        (FIXTURES / "codex_modern_primary_145.jsonl")
        .read_bytes()
        .splitlines(keepends=True)
    )
    (codex_day / "rollout-ack.jsonl").write_bytes(
        fixture_records[0]
        + fixture_records[3]
        + _unknown_item(2, "ack_item_kind")
        + fixture_records[5]
    )
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0
    code, first = _run(
        monkeypatch, capsys, "index", "--ignore-unknown-records", "--json"
    )
    assert code == 0, (first.out, first.err)
    assert json.loads(first.out)["coverage"]["unknown_shapes_pending"] == 1

    acknowledgement_file.write_text(
        _acknowledgement("response_item/ack_item_kind", _ACK_KEYS), encoding="utf-8"
    )
    code, acknowledged = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (acknowledged.out, acknowledged.err)
    acknowledged_payload = json.loads(acknowledged.out)
    assert acknowledged_payload["refresh"]["run_id"] is None
    assert _shape_warnings(acknowledged_payload) == []
    coverage = acknowledged_payload["coverage"]
    assert coverage["unknown_shape_records"] == 1
    assert coverage["unknown_shapes_pending"] == 0
    assert coverage["unknown_shapes_acknowledged"] == 1
    code, status = _run(monkeypatch, capsys, "index", "--status", "--json")
    assert code == 0
    status_payload = json.loads(status.out)
    assert status_payload["unknown_shapes"]["pending"] == []
    [entry] = status_payload["unknown_shapes"]["acknowledged"]
    assert entry["record_path"] == "response_item/ack_item_kind"
    assert entry["dispositions"] == ["acknowledged"]
    assert entry["note"] == "synthetic acknowledgement"
    assert entry["acknowledged"] == "2026-10-24"
    assert entry["record_count"] == 1
    code, searched = _run(
        monkeypatch, capsys, "search", "modern visible", "--literal", "--json"
    )
    assert code == 0
    assert _shape_warnings(json.loads(searched.out)) == []
    code, human_search = _run(
        monkeypatch, capsys, "search", "modern visible", "--literal"
    )
    assert code == 0
    search_lines = human_search.out.splitlines()
    assert any("ccchat:v1:" in line for line in search_lines)
    assert not any(line.startswith("Note: ") for line in search_lines)
    code, human = _run(monkeypatch, capsys, "index", "--status")
    assert code == 0
    assert "unknown shape " not in human.out

    extended = {
        "timestamp": "2026-08-11T03:00:02Z",
        "type": "response_item",
        "ordinal": 2,
        "payload": {
            "type": "ack_item_kind",
            "id": "fi_extended",
            "text": "cli-decoy-rue",
            "extra": True,
        },
    }
    (codex_day / "rollout-extended.jsonl").write_bytes(
        fixture_records[0]
        + fixture_records[3]
        + json.dumps(extended, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + fixture_records[5]
    )
    code, extended_run = _run(
        monkeypatch, capsys, "index", "--ignore-unknown-records", "--json"
    )
    assert code == 0, (extended_run.out, extended_run.err)
    extended_payload = json.loads(extended_run.out)
    [warning] = _shape_warnings(extended_payload)
    assert warning["record_path"] == "response_item/ack_item_kind"
    assert "payload.extra" in warning["keys"]
    assert extended_payload["coverage"]["unknown_shapes_pending"] == 1
    assert extended_payload["coverage"]["unknown_shapes_acknowledged"] == 1
    assert extended_payload["coverage"]["unknown_shape_records"] == 2

    acknowledgement_file.unlink()
    code, reverted = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (reverted.out, reverted.err)
    reverted_payload = json.loads(reverted.out)
    assert reverted_payload["refresh"]["run_id"] is None
    assert len(_shape_warnings(reverted_payload)) == 2
    assert reverted_payload["coverage"]["unknown_shapes_acknowledged"] == 0
    code, status = _run(monkeypatch, capsys, "index", "--status", "--json")
    assert code == 0
    pending = json.loads(status.out)["unknown_shapes"]["pending"]
    assert len(pending) == 2
    assert all("note" not in entry for entry in pending)


def test_readers_never_open_the_acknowledgement_file(
    cli_corpus,
    acknowledgement_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC12 and AC10: readers succeed with an unreadable file; index fails closed."""
    claude_root, codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    _codex_source_with_unknown_item(codex_day, "rollout-unknown.jsonl")
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0
    code, indexed = _run(
        monkeypatch, capsys, "index", "--ignore-unknown-records", "--json"
    )
    assert code == 0, (indexed.out, indexed.err)
    acknowledgement_file.write_text(
        _acknowledgement("response_item/future_item_kind", _ACK_KEYS), encoding="utf-8"
    )
    acknowledgement_file.chmod(0)
    try:
        code, searched = _run(
            monkeypatch, capsys, "search", "modern visible", "--literal", "--json"
        )
        assert code == 0, (searched.out, searched.err)
        locator = json.loads(searched.out)["results"][0]["locator"]
        for arguments in (
            ("extract", "codex-modern-primary", "--provider", "codex", "--json"),
            ("resolve", locator, "--json"),
            (
                "events",
                "--from",
                "2026-08-11T00:00:00Z",
                "--until",
                "2026-08-12T00:00:00Z",
                "--json",
            ),
            ("index", "--status", "--json"),
        ):
            code, output = _run(monkeypatch, capsys, *arguments)
            assert code == 0, (arguments, output.out, output.err)

        code, refused = _run(monkeypatch, capsys, "index", "--json")
        assert code == 2, (refused.out, refused.err)
        refused_payload = json.loads(refused.out)
        assert refused_payload["status"] == "configuration_invalid"
        assert refused_payload["error"]["code"] == "acknowledged_shapes_invalid"
    finally:
        acknowledgement_file.chmod(0o600)
