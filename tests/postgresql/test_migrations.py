"""Normalized PostgreSQL storage and migration behavior."""

import hashlib
import json
import shutil
from pathlib import Path

import psycopg
import pytest

from cc_search_chats.core.identity import Provider
from cc_search_chats.providers.claude import (
    ClaudeSessionContext,
    parse_claude_session,
)
from cc_search_chats.providers.codex import CodexSessionContext, parse_codex_session
from cc_search_chats.providers.source_discovery import (
    ConfiguredSourceRoot,
    read_bounded_jsonl,
    source_root_id,
)
from cc_search_chats.semantic import SemanticChunk
from cc_search_chats.storage.postgresql import (
    index_corpus,
    migrate,
    migrations,
    refresh_native_sources,
)

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
V1_TO_V6_CHECKSUMS = (
    (1, "6d15da8af9fa754224167bec9b8aa10f73cc13b3fb70a6385561eecb5b71ad6e"),
    (2, "8922f17452512547c88b0c3f92f57136e4a06ee03c3154f30d5421234b6f2ce9"),
    (3, "c5febce80b502fae236dd8c7d64ff13b6ab4029388fb8e03e76fc077e8b94f03"),
    (4, "207e390ad806ecf15721ddad75c47e03f9945406158199f974df02c81d1b396a"),
    (5, "91294ec081a5585b5b050cd7510b415064ec16f9e95c5568e247c452f3ebb2f5"),
    (6, "d91283516da1aa2618ce9af144ee6bf22bcd7c71cf0022f02da7279d47ea7235"),
)


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def _source_root(provider: Provider, path: Path) -> ConfiguredSourceRoot:
    resolved = path.resolve()
    return ConfiguredSourceRoot(
        provider=provider,
        path=resolved,
        source_root_id=source_root_id(provider, resolved),
    )


def _index_roots(
    connection: psycopg.Connection,
    roots: tuple[ConfiguredSourceRoot, ...],
):
    vector = [0.0] * 1024
    vector[0] = 1.0
    return index_corpus(
        connection,
        lambda texts: [vector for _ in texts],
        chunker=_single_chunks,
        source_roots=roots,
    )


def _write_claude_messages(path: Path, messages: tuple[tuple[str, str], ...]) -> None:
    payloads = (
        {
            "type": "assistant",
            "uuid": logical_message_id,
            "sessionId": "synthetic-session",
            "timestamp": "2026-09-01T00:00:00Z",
            "cwd": "/synthetic/repository",
            "isSidechain": False,
            "message": {"role": "assistant", "content": text},
        }
        for logical_message_id, text in messages
    )
    path.write_text(
        "".join(
            f"{json.dumps(payload, separators=(',', ':'))}\n" for payload in payloads
        ),
        encoding="utf-8",
    )


def _claude_messages():
    path = FIXTURES / "claude_primary.jsonl"
    bounded = read_bounded_jsonl(
        path,
        source_file_relative=Path(path.name),
        target_size=path.stat().st_size,
    )
    return parse_claude_session(
        bounded.envelopes,
        context=ClaudeSessionContext(source_session_id="claude-session-primary"),
    ).messages


def _codex_messages():
    path = FIXTURES / "codex_modern_primary_145.jsonl"
    bounded = read_bounded_jsonl(
        path,
        source_file_relative=Path(path.name),
        target_size=path.stat().st_size,
    )
    return parse_codex_session(
        bounded.envelopes,
        context=CodexSessionContext(),
        source_diagnostics=bounded.diagnostics,
    ).messages


def _snapshot_cardinality(connection: psycopg.Connection) -> tuple[int, int, int]:
    return next(
        connection.execute(
            """
            SELECT
              (SELECT count(*) FROM cc_search_chats.corpus_generation),
              (SELECT count(*) FROM cc_search_chats.message_current),
              (SELECT count(*) FROM cc_search_chats.physical_alias_current)
            """
        )
    )


def _selected_row_version(
    connection: psycopg.Connection, canonical_locator: str
) -> str:
    row = next(
        connection.execute(
            """
            SELECT m.xmin::text
            FROM cc_search_chats.message_current AS m
            WHERE m.canonical_locator = %s
            ORDER BY m.content_class
            LIMIT 1
            """,
            (canonical_locator,),
        )
    )
    return row[0]


def test_unchanged_replace_creates_no_generation_or_row_copies(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    migrate(postgres_connection)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "session.jsonl")
    roots = (_source_root(Provider.CLAUDE, claude_root),)

    first_generation = _index_roots(postgres_connection, roots).corpus_generation
    first_cardinality = _snapshot_cardinality(postgres_connection)
    second_generation = _index_roots(postgres_connection, roots).corpus_generation

    assert second_generation == first_generation
    assert _snapshot_cardinality(postgres_connection) == first_cardinality


def test_replace_merges_same_native_content_with_changed_cwd(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    migrate(postgres_connection)
    path = tmp_path / "claude-session.jsonl"
    payloads = [
        {
            "type": "assistant",
            "uuid": "replayed-message",
            "sessionId": "claude-session",
            "timestamp": "2026-08-29T00:00:00Z",
            "cwd": cwd,
            "isSidechain": False,
            "message": {"role": "assistant", "content": "visible replay"},
        }
        for cwd in ("/synthetic/earliest", "/synthetic/later")
    ]
    path.write_text(
        "".join(
            f"{json.dumps(payload, separators=(',', ':'))}\n" for payload in payloads
        ),
        encoding="utf-8",
    )
    _index_roots(
        postgres_connection,
        (_source_root(Provider.CLAUDE, tmp_path),),
    )

    assert next(
        postgres_connection.execute(
            """
            SELECT count(*), min(cwd)
            FROM cc_search_chats.message_current
            WHERE logical_message_id = 'replayed-message'
              AND content_class = 'prose'
            """
        )
    ) == (1, "/synthetic/earliest")
    assert (
        next(
            postgres_connection.execute(
                """
            SELECT count(*)
            FROM cc_search_chats.physical_alias_current
            WHERE logical_message_id = 'replayed-message'
              AND content_class = 'prose'
            """
            )
        )[0]
        == 2
    )


def test_append_preserves_unchanged_rows_and_adds_only_new_identities(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    migrate(postgres_connection)
    codex = _codex_messages()
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "session.jsonl")
    claude_roots = (_source_root(Provider.CLAUDE, claude_root),)
    _index_roots(postgres_connection, claude_roots)
    first_cardinality = _snapshot_cardinality(postgres_connection)
    locator_text = next(
        postgres_connection.execute(
            "SELECT canonical_locator FROM cc_search_chats.message_current "
            "ORDER BY canonical_locator, content_class LIMIT 1"
        )
    )[0]
    first_row_version = _selected_row_version(postgres_connection, locator_text)

    codex_root = tmp_path / "codex"
    codex_day = codex_root / "2026" / "08" / "11"
    codex_day.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "codex_modern_primary_145.jsonl",
        codex_day / "rollout-modern.jsonl",
    )
    _index_roots(
        postgres_connection,
        (*claude_roots, _source_root(Provider.CODEX, codex_root)),
    )

    assert _selected_row_version(postgres_connection, locator_text) == first_row_version
    expected_new_messages = len(
        {
            (
                message.identity.canonical_locator.provider,
                message.identity.canonical_locator.source_session_id,
                message.identity.logical_message_id,
                message.content_class,
            )
            for message in codex
        }
    )
    expected_new_aliases = sum(
        len(message.identity.physical_aliases) for message in codex
    )
    assert _snapshot_cardinality(postgres_connection) == (
        first_cardinality[0] + 1,
        first_cardinality[1] + expected_new_messages,
        first_cardinality[2] + expected_new_aliases,
    )


def test_migration_ledger_is_idempotent_and_rejects_changed_bytes(
    postgres_connection: psycopg.Connection,
) -> None:
    migrate(postgres_connection)
    migrate(postgres_connection)

    applied = tuple(
        postgres_connection.execute(
            """
            SELECT version, resource_name, length(sha256)
            FROM cc_search_chats.schema_migration
            ORDER BY version
            """
        )
    )
    assert applied == (
        (1, "schema.sql", 64),
        (2, "refresh_schema.sql", 64),
        (3, "freshness_schema.sql", 64),
        (4, "coverage_schema.sql", 64),
        (5, "semantic_chunk_schema.sql", 64),
        (6, "incremental_refresh_schema.sql", 64),
        (7, "coherent_corpus_schema.sql", 64),
        (8, "skipped_record_coverage_schema.sql", 64),
    )
    assert next(
        postgres_connection.execute(
            "SELECT to_regclass('cc_search_chats.source_failure_current') IS NOT NULL"
        )
    )[0]
    assert next(
        postgres_connection.execute(
            "SELECT to_regclass('cc_search_chats.auto_refresh_state') IS NOT NULL"
        )
    )[0]
    postgres_connection.execute(
        "UPDATE cc_search_chats.schema_migration "
        "SET sha256 = repeat('0', 64) WHERE version = 1"
    )

    with pytest.raises(RuntimeError, match="migration 1 checksum mismatch"):
        migrate(postgres_connection)


def test_pending_migrations_is_read_only_and_reports_the_packaged_suffix(
    postgres_connection: psycopg.Connection,
) -> None:
    assert tuple(
        migration.version
        for migration in migrations.pending_migrations(postgres_connection)
    ) == (1, 2, 3, 4, 5, 6, 7, 8)
    assert (
        next(postgres_connection.execute("SELECT to_regnamespace('cc_search_chats')"))[
            0
        ]
        is None
    )
    with pytest.raises(migrations.MaintenanceRequired):
        refresh_native_sources(postgres_connection, source_roots=())
    assert (
        next(postgres_connection.execute("SELECT to_regnamespace('cc_search_chats')"))[
            0
        ]
        is None
    )

    migrate(postgres_connection)

    assert migrations.pending_migrations(postgres_connection) == ()

    assert next(
        postgres_connection.execute(
            "SELECT to_regclass('cc_search_chats.message_current') IS NOT NULL"
        )
    )[0]


def test_interrupted_later_migration_does_not_advance_the_ledger(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate(postgres_connection)
    monkeypatch.setattr(
        migrations,
        "_MIGRATIONS",
        (*migrations._MIGRATIONS, migrations.Migration(9, "missing-migration.sql")),
    )

    with pytest.raises(FileNotFoundError):
        migrations.apply_migrations(postgres_connection)

    assert tuple(
        postgres_connection.execute(
            "SELECT version FROM cc_search_chats.schema_migration ORDER BY version"
        )
    ) == ((1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,))


def _upgrade_seeded_v6_schema(
    connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected_semantic_revision: int,
) -> None:
    packaged_migrations = migrations._MIGRATIONS
    v6_migrations = tuple(
        migration for migration in packaged_migrations if migration.version <= 6
    )
    monkeypatch.setattr(migrations, "_MIGRATIONS", v6_migrations)
    migrations.apply_migrations(connection)
    connection.execute(
        """
        INSERT INTO cc_search_chats.corpus_revision (
            revision_id, completed_at, status, message_count, alias_count
        ) OVERRIDING SYSTEM VALUE VALUES
            (1, '2026-09-01T00:00:00Z', 'complete', 0, 0),
            (2, '2026-09-01T00:01:00Z', 'complete', 0, 0),
            (3, '2026-09-01T00:02:00Z', 'complete', 0, 0)
        """
    )
    connection.execute(
        "UPDATE cc_search_chats.corpus_state SET current_revision_id = 1"
    )
    connection.execute(
        """
        INSERT INTO cc_search_chats.semantic_revision (
            semantic_revision_id, corpus_revision_id, profile_id,
            completed_at, status, embedded_count
        ) OVERRIDING SYSTEM VALUE VALUES
            (10, 1, 'nemotron-3-embed-8b-bf16:chunks-v1',
             '2026-09-01T00:00:00Z', 'complete', 0),
            (11, 2, 'nemotron-3-embed-8b-bf16:chunks-v1',
             NULL, 'building', 0),
            (12, 3, 'nemotron-3-embed-8b-bf16:chunks-v1',
             '2026-09-01T00:02:00Z', 'complete', 0)
        """
    )
    connection.execute(
        "UPDATE cc_search_chats.semantic_state SET current_semantic_revision_id = %s",
        (selected_semantic_revision,),
    )
    assert (
        tuple(
            connection.execute(
                "SELECT version, sha256 FROM cc_search_chats.schema_migration "
                "WHERE version <= 6 ORDER BY version"
            )
        )
        == V1_TO_V6_CHECKSUMS
    )

    monkeypatch.setattr(migrations, "_MIGRATIONS", packaged_migrations)
    assert tuple(migration.version for migration in packaged_migrations) == (
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
    )
    migrations.apply_migrations(connection)


def test_migration_7_backfills_only_a_matching_complete_pair(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade_seeded_v6_schema(
        postgres_connection,
        monkeypatch,
        selected_semantic_revision=10,
    )

    assert next(
        postgres_connection.execute(
            "SELECT current_corpus_generation FROM cc_search_chats.corpus_state"
        )
    ) == (1,)
    assert next(
        postgres_connection.execute(
            "SELECT semantic_build FROM cc_search_chats.corpus_generation "
            "WHERE corpus_generation = 1"
        )
    ) == (10,)
    assert next(
        postgres_connection.execute(
            "SELECT to_regclass('cc_search_chats.semantic_state') IS NULL"
        )
    ) == (True,)
    assert (
        tuple(
            postgres_connection.execute(
                "SELECT version, sha256 FROM cc_search_chats.schema_migration "
                "WHERE version <= 6 ORDER BY version"
            )
        )
        == V1_TO_V6_CHECKSUMS
    )


def test_migration_7_leaves_an_inconsistent_old_pair_unselected(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade_seeded_v6_schema(
        postgres_connection,
        monkeypatch,
        selected_semantic_revision=12,
    )

    assert next(
        postgres_connection.execute(
            "SELECT current_corpus_generation FROM cc_search_chats.corpus_state"
        )
    ) == (None,)
    assert next(
        postgres_connection.execute(
            "SELECT semantic_build FROM cc_search_chats.corpus_generation "
            "WHERE corpus_generation = 1"
        )
    ) == (None,)


def test_migration_7_rejects_incomplete_or_cross_corpus_selection(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade_seeded_v6_schema(
        postgres_connection,
        monkeypatch,
        selected_semantic_revision=10,
    )

    with (
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        postgres_connection.transaction(),
    ):
        postgres_connection.execute(
            "UPDATE cc_search_chats.corpus_generation "
            "SET semantic_build = 12 WHERE corpus_generation = 2"
        )

    def select_incomplete_semantic_build() -> None:
        with postgres_connection.transaction():
            postgres_connection.execute(
                "UPDATE cc_search_chats.corpus_generation "
                "SET semantic_build = 11 WHERE corpus_generation = 2"
            )
            postgres_connection.execute(
                "UPDATE cc_search_chats.corpus_state SET current_corpus_generation = 2"
            )

    with pytest.raises(psycopg.errors.CheckViolation):
        select_incomplete_semantic_build()


def _seed_legacy_snapshots(connection: psycopg.Connection) -> None:
    connection.execute("CREATE SCHEMA cc_search_chats")
    connection.execute(
        """
        CREATE TABLE cc_search_chats.corpus_revision (
            revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO cc_search_chats.corpus_revision (revision_id)
          OVERRIDING SYSTEM VALUE VALUES (14);
        CREATE TABLE cc_search_chats.corpus_state (
            singleton boolean PRIMARY KEY,
            current_revision_id bigint
        );
        INSERT INTO cc_search_chats.corpus_state VALUES (true, 14);
        CREATE TABLE cc_search_chats.message (
            revision_id bigint NOT NULL,
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            content_class text NOT NULL,
            prose_content text NOT NULL
        );
        INSERT INTO cc_search_chats.message VALUES
          (14, 'claude', 'current', 'one', 'prose', 'current one'),
          (14, 'codex', 'current', 'two', 'prose', 'current two'),
          (13, 'claude', 'legacy', 'one', 'prose', 'reusable one'),
          (13, 'codex', 'legacy', 'two', 'prose', 'reusable two');
        CREATE TABLE cc_search_chats.physical_alias (revision_id bigint NOT NULL);
        INSERT INTO cc_search_chats.physical_alias VALUES (14), (14), (14), (13);
        CREATE TABLE cc_search_chats.semantic_revision (
            semantic_revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            corpus_revision_id bigint NOT NULL
        );
        INSERT INTO cc_search_chats.semantic_revision (
            semantic_revision_id, corpus_revision_id
        ) OVERRIDING SYSTEM VALUE VALUES (9, 13), (10, 14);
        CREATE TABLE cc_search_chats.semantic_state (
            singleton boolean PRIMARY KEY,
            current_semantic_revision_id bigint
        );
        INSERT INTO cc_search_chats.semantic_state VALUES (true, 9);
        CREATE TABLE cc_search_chats.message_embedding (
            semantic_revision_id bigint NOT NULL,
            revision_id bigint NOT NULL,
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            content_class text NOT NULL,
            embedding vector(1024) NOT NULL
        );
        INSERT INTO cc_search_chats.message_embedding
        SELECT semantic_revision_id, revision_id, provider, source_session_id,
               logical_message_id, 'prose',
               array_fill(0::real, ARRAY[1024])::vector
        FROM (VALUES
          (9, 13, 'claude', 'legacy', 'one'),
          (9, 13, 'claude', 'legacy', 'one'),
          (9, 13, 'codex', 'legacy', 'two'),
          (9, 13, 'codex', 'legacy', 'two'),
          (10, 14, 'claude', 'current', 'one')
        ) AS value(
          semantic_revision_id, revision_id, provider,
          source_session_id, logical_message_id
        );
        """
    )


def test_legacy_prune_dry_run_captures_selected_counts_without_mutation(
    postgres_connection: psycopg.Connection,
) -> None:
    _seed_legacy_snapshots(postgres_connection)
    migrate(postgres_connection)

    plan = migrations.plan_legacy_snapshot_prune(postgres_connection)

    assert plan.corpus_revision_id == 14
    assert plan.semantic_revision_id == 9
    assert [
        (relation.relation_name, relation.selected_rows) for relation in plan.relations
    ] == [
        ("cc_search_chats.message_embedding", 4),
        ("cc_search_chats.physical_alias", 3),
        ("cc_search_chats.message", 2),
    ]
    assert all(relation.total_bytes > 0 for relation in plan.relations)
    assert len(plan.fingerprint) == 64
    assert next(
        postgres_connection.execute(
            """
            SELECT to_regclass('cc_search_chats.message') IS NOT NULL
               AND to_regclass('cc_search_chats.message_current') IS NOT NULL
            """
        )
    )[0]

    with pytest.raises(RuntimeError, match="accepted current cutover validation"):
        migrations.prune_legacy_snapshots(
            postgres_connection,
            expected_fingerprint=plan.fingerprint,
            accepted_validation_id=1,
        )
    assert next(
        postgres_connection.execute(
            "SELECT to_regclass('cc_search_chats.message') IS NOT NULL"
        )
    )[0]


def test_legacy_embedding_import_seeds_digest_pool_without_publication(
    postgres_connection: psycopg.Connection,
) -> None:
    _seed_legacy_snapshots(postgres_connection)
    migrate(postgres_connection)

    first = migrations.import_legacy_embedding_pool(postgres_connection, batch_size=1)
    second = migrations.import_legacy_embedding_pool(postgres_connection, batch_size=2)

    assert first.scanned_rows == 4
    assert first.new_pool_rows == 2
    assert first.pool_rows_after == 2
    assert second.scanned_rows == 4
    assert second.new_pool_rows == 0
    assert second.pool_rows_after == 2
    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.semantic_chunk_current"
            )
        )[0]
        == 0
    )
    assert next(
        postgres_connection.execute(
            """
            SELECT imported_embedding_rows,
                   embedding_pool_imported_at IS NOT NULL
            FROM cc_search_chats.legacy_snapshot_inventory
            WHERE singleton
            """
        )
    ) == (4, True)


def _semantic_cardinality(connection: psycopg.Connection) -> tuple[int, int, int]:
    return next(
        connection.execute(
            """
            SELECT
              (SELECT count(*) FROM cc_search_chats.semantic_build),
              (SELECT count(*) FROM cc_search_chats.embedding_value),
              (SELECT count(*)
               FROM cc_search_chats.semantic_chunk_current)
            """
        )
    )


def _mapping_row_version(
    connection: psycopg.Connection,
    *,
    provider: str,
    source_session_id: str,
    logical_message_id: str,
    content_class: str,
) -> str:
    return next(
        connection.execute(
            """
            SELECT xmin::text
            FROM cc_search_chats.semantic_chunk_current
            WHERE provider = %s AND source_session_id = %s
              AND logical_message_id = %s AND content_class = %s
              AND profile_id = 'nemotron-3-embed-8b-bf16:chunks-v1'
              AND chunk_ordinal = 0
            """,
            (provider, source_session_id, logical_message_id, content_class),
        )
    )[0]


def test_semantic_append_reuses_vector_and_mapping_rows(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    migrate(postgres_connection)
    claude = _claude_messages()
    codex = _codex_messages()
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    shutil.copy(FIXTURES / "claude_primary.jsonl", claude_root / "session.jsonl")
    claude_roots = (_source_root(Provider.CLAUDE, claude_root),)
    vector = [0.0] * 1024
    vector[0] = 1.0
    embedded_texts: list[str] = []

    def embed(texts):
        embedded_texts.extend(texts)
        return [vector for _ in texts]

    eligible_claude = [
        message
        for message in claude
        if message.content_class.value == "prose" and message.text.strip()
    ]
    assert index_corpus(
        postgres_connection,
        embed,
        chunker=_single_chunks,
        source_roots=claude_roots,
    ).embedding_count == len(eligible_claude)
    first_cardinality = _semantic_cardinality(postgres_connection)
    (
        target_provider,
        target_session,
        target_message,
        target_content_class,
    ) = next(
        postgres_connection.execute(
            """
            SELECT provider, source_session_id, logical_message_id, content_class
            FROM cc_search_chats.semantic_chunk_current
            ORDER BY provider, source_session_id, logical_message_id,
                     content_class, chunk_ordinal
            LIMIT 1
            """
        )
    )
    mapping_version = _mapping_row_version(
        postgres_connection,
        provider=target_provider,
        source_session_id=target_session,
        logical_message_id=target_message,
        content_class=target_content_class,
    )

    embedded_texts.clear()
    assert index_corpus(
        postgres_connection,
        embed,
        chunker=_single_chunks,
        source_roots=claude_roots,
    ).embedding_count == len(eligible_claude)
    assert embedded_texts == []
    assert _semantic_cardinality(postgres_connection) == first_cardinality

    codex_root = tmp_path / "codex"
    codex_day = codex_root / "2026" / "08" / "11"
    codex_day.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "codex_modern_primary_145.jsonl",
        codex_day / "rollout-modern.jsonl",
    )
    all_roots = (*claude_roots, _source_root(Provider.CODEX, codex_root))
    eligible_all = [
        message
        for message in (*claude, *codex)
        if message.content_class.value == "prose" and message.text.strip()
    ]
    embedded_texts.clear()
    assert index_corpus(
        postgres_connection,
        embed,
        chunker=_single_chunks,
        source_roots=all_roots,
    ).embedding_count == len(eligible_all)

    assert (
        _mapping_row_version(
            postgres_connection,
            provider=target_provider,
            source_session_id=target_session,
            logical_message_id=target_message,
            content_class=target_content_class,
        )
        == mapping_version
    )
    expected_digests = {
        hashlib.sha256(f"passage: {message.text}".encode()).hexdigest()
        for message in eligible_all
    }
    assert _semantic_cardinality(postgres_connection) == (
        first_cardinality[0] + 1,
        len(expected_digests),
        len(eligible_all),
    )
    claude_digests = {
        hashlib.sha256(f"passage: {message.text}".encode()).hexdigest()
        for message in eligible_claude
    }
    assert {
        hashlib.sha256(f"passage: {text}".encode()).hexdigest()
        for text in embedded_texts
    } == expected_digests - claude_digests


def test_successful_semantic_publication_reclaims_unreachable_vectors(
    postgres_connection: psycopg.Connection, tmp_path: Path
) -> None:
    migrate(postgres_connection)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    source = claude_root / "session.jsonl"
    first_messages = (
        ("retained-message", "retained semantic sentinel"),
        ("removed-message", "removed semantic sentinel"),
    )
    _write_claude_messages(source, first_messages)
    roots = (_source_root(Provider.CLAUDE, claude_root),)
    vector = [0.0] * 1024
    vector[0] = 1.0

    def embed(texts):
        return [vector for _ in texts]

    index_corpus(
        postgres_connection,
        embed,
        chunker=_single_chunks,
        source_roots=roots,
    )
    _write_claude_messages(source, first_messages[:1])

    index_corpus(
        postgres_connection,
        embed,
        chunker=_single_chunks,
        source_roots=roots,
    )

    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.embedding_value"
            )
        )[0]
        == 1
    )
    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.semantic_chunk_current"
            )
        )[0]
        == 1
    )
