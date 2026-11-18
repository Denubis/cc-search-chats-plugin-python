"""Unknown record shapes through the PostgreSQL CLI: warn and index around."""

import dataclasses
import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cc_search_chats.cli import main
from cc_search_chats.core.identity import Provider
from cc_search_chats.providers import claude as claude_module
from cc_search_chats.providers import registry
from cc_search_chats.semantic import SemanticChunk

if TYPE_CHECKING:
    from collections.abc import Mapping

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


@pytest.mark.parametrize("route", ["default", "retired-flag", "retired-variable"])
def test_index_indexes_around_an_unknown_shape_by_default(
    cli_corpus,
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
) -> None:
    """ADR 0011: an unknown shape warns and is indexed around by default.

    The retired ``--ignore-unknown-records`` flag and its variable stay accepted
    so existing invocations and ``index.env`` files keep working; they change
    nothing.
    """
    claude_root, codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    _codex_source_with_unknown_item(codex_day, "rollout-unknown.jsonl")
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0

    arguments: tuple[str, ...] = ("index", "--json")
    if route == "retired-flag":
        arguments = ("index", "--ignore-unknown-records", "--json")
    elif route == "retired-variable":
        monkeypatch.setenv("CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS", "1")
    code, ignored = _run(monkeypatch, capsys, *arguments)
    assert code == 0, (ignored.out, ignored.err)
    ignored_payload = json.loads(ignored.out)
    assert ignored_payload["coverage"]["blocked_files"] == 0
    assert ignored_payload["coverage"]["completeness"] == "complete"
    [warning] = _shape_warnings(ignored_payload)
    assert warning["record_path"] == "response_item/future_item_kind"
    assert warning["dispositions"] == ["ignored"]
    assert warning["record_count"] == 1
    assert ignored_payload["coverage"]["unknown_shapes_pending"] == 1
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
    code, ignored = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (ignored.out, ignored.err)
    (codex_day / "rollout-later.jsonl").write_bytes(
        fixture_records[0]
        + fixture_records[3]
        + _unknown_item(2, "later_item_kind")
        + fixture_records[5]
    )
    code, later = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (later.out, later.err)
    assert json.loads(later.out)["coverage"]["blocked_files"] == 0

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
        "response_item/later_item_kind",
    }
    for warning in by_path.values():
        assert warning["dispositions"] == ["ignored"]
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
    assert coverage["completeness"] == "complete"
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
        "unknown shape codex response_item/later_item_kind "
        "{ordinal, payload, timestamp, type, payload.id, payload.text, "
        "payload.type}: 1 record in 1 file, first "
        "2026/10/24/rollout-later.jsonl:3, ignored"
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

    code, indexed = _run(monkeypatch, capsys, "index", "--json")

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
    code, first = _run(monkeypatch, capsys, "index", "--json")
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
    code, extended_run = _run(monkeypatch, capsys, "index", "--json")
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
    code, indexed = _run(monkeypatch, capsys, "index", "--json")
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


def _jsonl(*records: Mapping[str, object]) -> bytes:
    return b"".join(
        json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records
    )


def _claude_turn(role: str, uuid: str, text: str) -> dict[str, object]:
    return {
        "type": role,
        "uuid": uuid,
        "timestamp": "2026-11-18T00:00:00Z",
        "isSidechain": False,
        "message": {"role": role, "content": text},
    }


def _searched_texts(monkeypatch, capsys, term: str) -> list[str]:
    code, searched = _run(monkeypatch, capsys, "search", term, "--literal", "--json")
    assert code == 0, (searched.out, searched.err)
    return [result["text"] for result in json.loads(searched.out)["results"]]


def test_file_blocked_by_an_earlier_release_is_recovered_by_the_next_index(
    cli_corpus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The 2.5.2 cost-state block shows as blocked until the next default index.

    cc-search-chats 2.5.2 (Claude parser-state 11, exact metadata key sets,
    unknown shapes blocking by default) blocked Claude Code 2.1.325 files on a
    ``cost-state`` record carrying ``modelCallCostUSD``. The next index retries
    the unchanged file: its conversation becomes searchable and the record is
    excluded metadata, so no shape remains to warn about.
    """
    claude_root, _codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    cost_state = {
        "type": "cost-state",
        "hasUnknownModelCost": False,
        "modelUsage": {},
        "modelCallCostUSD": 0.0123,
        "sessionId": "cost-session",
        "startTime": 0,
        "totalAPIDuration": 1,
        "totalAPIDurationWithoutRetries": 1,
        "totalCostUSD": 0.01,
        "totalDuration": 1,
        "totalLinesAdded": 0,
        "totalLinesRemoved": 0,
        "totalToolDuration": 0,
    }
    (claude_root / "cost-session.jsonl").write_bytes(
        _jsonl(
            _claude_turn("user", "before-cost", "cost recovery prose before"),
            cost_state,
            _claude_turn("assistant", "after-cost", "cost recovery prose after"),
        )
    )
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0

    def exact_matcher(payload: dict[str, object]) -> bool:
        record_type = payload.get("type")
        return isinstance(record_type, str) and frozenset(payload) in (
            claude_module._CLAUDE_METADATA_KEYSETS.get(record_type, set())
        )

    with monkeypatch.context() as released:
        adapter = registry.provider_adapter(Provider.CLAUDE)
        released.setitem(
            registry._ADAPTERS,
            Provider.CLAUDE,
            dataclasses.replace(
                adapter, parser_state_version=11, unknown_shape_codes=frozenset()
            ),
        )
        released.setattr(claude_module, "_is_known_metadata_record", exact_matcher)
        code, blocked = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (blocked.out, blocked.err)
    assert json.loads(blocked.out)["coverage"]["blocked_files"] == 1

    code, status = _run(monkeypatch, capsys, "index", "--status", "--json")
    assert code == 0
    [warning] = _shape_warnings(json.loads(status.out))
    assert warning["record_path"] == "cost-state"
    assert "modelCallCostUSD" in warning["keys"]
    assert warning["dispositions"] == ["blocked"]
    code, human_status = _run(monkeypatch, capsys, "index", "--status")
    assert code == 0
    [line] = [
        line
        for line in human_status.out.splitlines()
        if line.startswith("unknown shape ")
    ]
    assert line.startswith("unknown shape claude cost-state {")
    assert line.endswith(", blocked")
    assert _searched_texts(monkeypatch, capsys, "cost recovery prose") == []

    code, recovered = _run(monkeypatch, capsys, "index", "--json")

    assert code == 0, (recovered.out, recovered.err)
    payload = json.loads(recovered.out)
    assert payload["coverage"]["blocked_files"] == 0
    assert payload["coverage"]["completeness"] == "complete"
    assert payload["coverage"]["unknown_shape_records"] == 0
    assert _shape_warnings(payload) == []
    assert sorted(_searched_texts(monkeypatch, capsys, "cost recovery prose")) == [
        "cost recovery prose after",
        "cost recovery prose before",
    ]


_UNSEEN_DECOY = "unseen-type-decoy-hornbeam"
_ANTIGRAVITY_MID_SESSION = "7b8c9dae-bf0a-4c1d-8e2f-6a7b8c9d0e1f"


def _write_session_with_unseen_type(provider: str, roots) -> tuple[str, str]:
    """Write one session with prose, a never-seen record type, then prose."""
    claude_root, codex_day, antigravity_root = roots
    if provider == "claude":
        (claude_root / "unseen-type.jsonl").write_bytes(
            _jsonl(
                _claude_turn("user", "unseen-before", "claude hawthorn before"),
                {"type": "never_seen_kind", "uuid": "unseen", "note": _UNSEEN_DECOY},
                _claude_turn("assistant", "unseen-after", "claude hawthorn after"),
            )
        )
        return "claude hawthorn", "never_seen_kind"
    if provider == "codex":
        records = (
            (FIXTURES / "codex_modern_primary_145.jsonl")
            .read_bytes()
            .splitlines(keepends=True)
        )
        unseen = {
            "timestamp": "2026-08-11T03:00:02Z",
            "type": "never_seen_kind",
            "payload": {"note": _UNSEEN_DECOY},
        }
        (codex_day / "rollout-unseen.jsonl").write_bytes(
            records[0] + records[3] + _jsonl(unseen) + records[5]
        )
        return "modern visible", "never_seen_kind"
    logs = antigravity_root / _ANTIGRAVITY_MID_SESSION / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript_full.jsonl").write_bytes(
        _jsonl(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-18T00:00:00Z",
                "content": "<USER_REQUEST>antigravity hawthorn before</USER_REQUEST>",
            },
            {
                "source": "MODEL",
                "type": "NEVER_SEEN_KIND",
                "created_at": "2026-11-18T00:00:01Z",
                "content": _UNSEEN_DECOY,
            },
            {
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-18T00:00:02Z",
                "content": "antigravity hawthorn after",
            },
        )
    )
    return "antigravity hawthorn", "MODEL/NEVER_SEEN_KIND"


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity"])
def test_never_seen_record_type_mid_session_warns_and_keeps_both_sides(
    cli_corpus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
) -> None:
    """A default index searches both sides of an unseen type and warns once."""
    term, record_path = _write_session_with_unseen_type(provider, cli_corpus)
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0

    code, indexed = _run(monkeypatch, capsys, "index", "--json")

    assert code == 0, (indexed.out, indexed.err)
    payload = json.loads(indexed.out)
    assert payload["coverage"]["blocked_files"] == 0
    assert payload["coverage"]["completeness"] == "complete"
    [warning] = _shape_warnings(payload)
    assert (warning["provider"], warning["record_path"]) == (provider, record_path)
    assert warning["record_count"] == 1
    assert warning["dispositions"] == ["ignored"]
    assert len(_searched_texts(monkeypatch, capsys, term)) == 2
    assert _searched_texts(monkeypatch, capsys, _UNSEEN_DECOY) == []
    assert _UNSEEN_DECOY not in json.dumps(payload)


_DRIFT_DECOY = "drift-decoy-blackthorn"


def _antigravity_session(antigravity_root: Path, drifted: Mapping[str, object]) -> None:
    logs = antigravity_root / _ANTIGRAVITY_MID_SESSION / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript_full.jsonl").write_bytes(
        _jsonl(
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-18T00:00:00Z",
                "content": "<USER_REQUEST>antigravity rowan before</USER_REQUEST>",
            },
            drifted,
            {
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-18T00:00:02Z",
                "content": "antigravity rowan after",
            },
        )
    )


def _write_drifted_session(case: str, roots) -> tuple[Provider, str]:
    """Write prose, one drifted record of a known type, then prose."""
    claude_root, codex_day, antigravity_root = roots
    if case.startswith("claude"):
        drifted: dict[str, object] = (
            {
                "type": "user",
                "uuid": "drift",
                "timestamp": "2026-11-18T00:00:01Z",
                "isSidechain": False,
                "content": _DRIFT_DECOY,
            }
            if case == "claude-user-without-message"
            else {"type": "assistant", "uuid": "drift", "message": _DRIFT_DECOY}
        )
        (claude_root / "drift.jsonl").write_bytes(
            _jsonl(
                _claude_turn("user", "drift-before", "claude rowan before"),
                drifted,
                _claude_turn("assistant", "drift-after", "claude rowan after"),
            )
        )
        return Provider.CLAUDE, "claude rowan"
    if case.startswith("codex"):
        records = (
            (FIXTURES / "codex_modern_primary_145.jsonl")
            .read_bytes()
            .splitlines(keepends=True)
        )
        outer = (
            "response_item" if case == "codex-response-item-payload" else "event_msg"
        )
        drifted = {
            "timestamp": "2026-08-11T03:00:02Z",
            "type": outer,
            "payload": _DRIFT_DECOY,
        }
        (codex_day / "rollout-drift.jsonl").write_bytes(
            records[0] + records[3] + _jsonl(drifted) + records[5]
        )
        return Provider.CODEX, "modern visible"
    drifted = {
        "antigravity-user-input-without-request": {
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-11-18T00:00:01Z",
            "content": _DRIFT_DECOY,
        },
        "antigravity-tool-calls-not-a-list": {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-18T00:00:01Z",
            "content": _DRIFT_DECOY,
            "tool_calls": _DRIFT_DECOY,
        },
        "antigravity-missing-created-at": {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": _DRIFT_DECOY,
        },
    }[case]
    _antigravity_session(antigravity_root, drifted)
    return Provider.ANTIGRAVITY, "antigravity rowan"


@pytest.mark.parametrize(
    ("case", "record_path"),
    [
        ("claude-user-without-message", "user/message"),
        ("claude-assistant-message-not-an-object", "assistant/message"),
        ("codex-response-item-payload", "response_item/payload"),
        ("codex-event-payload", "event_msg/payload"),
        ("antigravity-user-input-without-request", "USER_EXPLICIT/USER_INPUT"),
        ("antigravity-tool-calls-not-a-list", "MODEL/PLANNER_RESPONSE"),
        ("antigravity-missing-created-at", "MODEL/PLANNER_RESPONSE"),
    ],
)
def test_drift_inside_a_known_record_type_warns_and_recovers_a_blocked_file(
    cli_corpus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    record_path: str,
) -> None:
    """Drift in a record's body warns; a file it blocked is retried.

    The earlier release keeps the current parser-state version, so the retry
    comes from the failure being an unknown shape, not from a version bump.
    """
    claude_root, _codex_day, _antigravity_root = cli_corpus
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "clean.jsonl")
    provider, term = _write_drifted_session(case, cli_corpus)
    code, _migrated = _run(monkeypatch, capsys, "index", "--migrate", "--json")
    assert code == 0
    with monkeypatch.context() as released:
        released.setitem(
            registry._ADAPTERS,
            provider,
            dataclasses.replace(
                registry.provider_adapter(provider), unknown_shape_codes=frozenset()
            ),
        )
        code, blocked = _run(monkeypatch, capsys, "index", "--json")
    assert code == 0, (blocked.out, blocked.err)
    assert json.loads(blocked.out)["coverage"]["blocked_files"] == 1

    code, indexed = _run(monkeypatch, capsys, "index", "--json")

    assert code == 0, (indexed.out, indexed.err)
    payload = json.loads(indexed.out)
    assert payload["coverage"]["blocked_files"] == 0
    assert payload["coverage"]["completeness"] == "complete"
    [warning] = _shape_warnings(payload)
    assert (warning["provider"], warning["record_path"]) == (
        provider.value,
        record_path,
    )
    assert warning["dispositions"] == ["ignored"]
    assert len(_searched_texts(monkeypatch, capsys, term)) == 2
    assert _searched_texts(monkeypatch, capsys, _DRIFT_DECOY) == []
    assert _DRIFT_DECOY not in json.dumps(payload)
    code, human_status = _run(monkeypatch, capsys, "index", "--status")
    assert code == 0
    [line] = [
        line
        for line in human_status.out.splitlines()
        if line.startswith("unknown shape ")
    ]
    if case == "claude-user-without-message":
        assert line == (
            "unknown shape claude user/message "
            "{content, isSidechain, timestamp, type, uuid}: "
            "1 record in 1 file, first drift.jsonl:2, ignored"
        )
    assert line.startswith(f"unknown shape {provider.value} {record_path} {{")
    assert line.endswith(", ignored")
