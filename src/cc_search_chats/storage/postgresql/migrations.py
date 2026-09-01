"""Ordered, checksummed PostgreSQL schema migrations."""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files

import psycopg
from psycopg import sql

_MIGRATION_LOCK = "cc_search_chats.schema_migration"


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable packaged SQL migration."""

    version: int
    resource_name: str


_MIGRATIONS = (
    Migration(1, "schema.sql"),
    Migration(2, "refresh_schema.sql"),
    Migration(3, "freshness_schema.sql"),
    Migration(4, "coverage_schema.sql"),
    Migration(5, "semantic_chunk_schema.sql"),
    Migration(6, "incremental_refresh_schema.sql"),
    Migration(7, "coherent_corpus_schema.sql"),
)


class MaintenanceRequired(RuntimeError):
    """The database migration ledger is behind the packaged schema."""

    def __init__(self, pending: tuple[Migration, ...]) -> None:
        self.pending = pending
        versions = ", ".join(str(migration.version) for migration in pending)
        super().__init__(f"pending PostgreSQL schema migrations: {versions}")


def pending_migrations(
    connection: psycopg.Connection,
) -> tuple[Migration, ...]:
    """Inspect and validate the migration ledger without creating schema objects."""
    ledger_exists = next(
        connection.execute(
            "SELECT to_regclass('cc_search_chats.schema_migration') IS NOT NULL"
        )
    )[0]
    if not ledger_exists:
        return _MIGRATIONS
    applied = {
        version: (resource_name, sha256)
        for version, resource_name, sha256 in connection.execute(
            "SELECT version, resource_name, sha256 "
            "FROM cc_search_chats.schema_migration ORDER BY version"
        )
    }
    known_versions = {migration.version for migration in _MIGRATIONS}
    unknown_versions = sorted(set(applied) - known_versions)
    if unknown_versions:
        raise RuntimeError(
            "database contains unknown schema migrations: "
            + ", ".join(str(version) for version in unknown_versions)
        )
    package = files("cc_search_chats.storage.postgresql")
    pending: list[Migration] = []
    for migration in _MIGRATIONS:
        checksum = hashlib.sha256(
            package.joinpath(migration.resource_name).read_bytes()
        ).hexdigest()
        existing = applied.get(migration.version)
        if existing is None:
            pending.append(migration)
        elif existing != (migration.resource_name, checksum):
            raise RuntimeError(
                f"schema migration {migration.version} checksum mismatch"
            )
    return tuple(pending)


def require_current_schema(connection: psycopg.Connection) -> None:
    """Fail without mutation when explicit migration is still required."""
    pending = pending_migrations(connection)
    if pending:
        raise MaintenanceRequired(pending)


@dataclass(frozen=True, slots=True)
class LegacyRelation:
    """One quarantined full-snapshot relation eligible for a gated prune."""

    relation_name: str
    selected_rows: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class LegacySnapshotPlan:
    """Exact read-back required before pruning legacy snapshot copies."""

    corpus_revision_id: int | None
    semantic_revision_id: int | None
    relations: tuple[LegacyRelation, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class LegacyEmbeddingImport:
    """Bounded result of seeding reusable vectors from one selected snapshot."""

    scanned_rows: int
    new_pool_rows: int
    pool_rows_after: int


_LEGACY_RELATIONS = (
    ("message_embedding", "semantic_revision_id", "semantic"),
    ("physical_alias", "revision_id", "corpus"),
    ("message", "revision_id", "corpus"),
)


def apply_migrations(connection: psycopg.Connection) -> None:
    """Apply every packaged migration once and reject changed applied bytes."""
    package = files("cc_search_chats.storage.postgresql")
    with connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_MIGRATION_LOCK,),
        )
        connection.execute("CREATE SCHEMA IF NOT EXISTS cc_search_chats")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cc_search_chats.schema_migration (
                version integer PRIMARY KEY CHECK (version > 0),
                resource_name text NOT NULL,
                sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            version: (resource_name, sha256)
            for version, resource_name, sha256 in connection.execute(
                "SELECT version, resource_name, sha256 "
                "FROM cc_search_chats.schema_migration ORDER BY version"
            )
        }
        known_versions = {migration.version for migration in _MIGRATIONS}
        unknown_versions = sorted(set(applied) - known_versions)
        if unknown_versions:
            raise RuntimeError(
                "database contains unknown schema migrations: "
                + ", ".join(str(version) for version in unknown_versions)
            )

        for migration in _MIGRATIONS:
            sql_bytes = package.joinpath(migration.resource_name).read_bytes()
            checksum = hashlib.sha256(sql_bytes).hexdigest()
            existing = applied.get(migration.version)
            if existing is not None:
                if existing != (migration.resource_name, checksum):
                    raise RuntimeError(
                        f"schema migration {migration.version} checksum mismatch"
                    )
                continue
            connection.execute(sql_bytes)
            connection.execute(
                """
                INSERT INTO cc_search_chats.schema_migration (
                    version, resource_name, sha256
                ) VALUES (%s, %s, %s)
                """,
                (migration.version, migration.resource_name, checksum),
            )


def plan_legacy_snapshot_prune(
    connection: psycopg.Connection,
) -> LegacySnapshotPlan:
    """Inspect quarantined snapshot relations without mutating them."""
    inventory = next(
        connection.execute(
            """
            SELECT corpus_revision_id, semantic_revision_id
            FROM cc_search_chats.legacy_snapshot_inventory
            WHERE singleton
            """
        ),
        None,
    )
    corpus_revision, semantic_revision = inventory or (None, None)
    relations: list[LegacyRelation] = []
    for relation_name, revision_column, revision_kind in _LEGACY_RELATIONS:
        relation = f"cc_search_chats.{relation_name}"
        exists = next(connection.execute("SELECT to_regclass(%s)", (relation,)))[0]
        if exists is None:
            continue
        has_revision_column = next(
            connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'cc_search_chats'
                      AND table_name = %s
                      AND column_name = %s
                )
                """,
                (relation_name, revision_column),
            )
        )[0]
        if not has_revision_column:
            continue
        selected_revision = (
            semantic_revision if revision_kind == "semantic" else corpus_revision
        )
        selected_rows = 0
        if selected_revision is not None:
            selected_rows = next(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}.{} WHERE {} = %s").format(
                        sql.Identifier("cc_search_chats"),
                        sql.Identifier(relation_name),
                        sql.Identifier(revision_column),
                    ),
                    (selected_revision,),
                )
            )[0]
        total_bytes = next(
            connection.execute(
                "SELECT pg_total_relation_size(%s::regclass)", (relation,)
            )
        )[0]
        relations.append(
            LegacyRelation(
                relation_name=relation,
                selected_rows=selected_rows,
                total_bytes=total_bytes,
            )
        )
    evidence = {
        "corpus_revision_id": corpus_revision,
        "semantic_revision_id": semantic_revision,
        "relations": [
            {
                "relation_name": relation.relation_name,
                "selected_rows": relation.selected_rows,
                "total_bytes": relation.total_bytes,
            }
            for relation in relations
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return LegacySnapshotPlan(
        corpus_revision_id=corpus_revision,
        semantic_revision_id=semantic_revision,
        relations=tuple(relations),
        fingerprint=fingerprint,
    )


def import_legacy_embedding_pool(
    connection: psycopg.Connection,
    *,
    batch_size: int = 1_000,
) -> LegacyEmbeddingImport:
    """Seed reusable vectors from revision-matched legacy text without publishing."""
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("batch_size must be a positive integer")
    with connection.transaction():
        inventory = next(
            connection.execute(
                """
                SELECT semantic_revision_id
                FROM cc_search_chats.legacy_snapshot_inventory
                WHERE singleton
                """
            ),
            None,
        )
        if inventory is None or inventory[0] is None:
            raise ValueError("no selected legacy semantic snapshot was captured")
        selected_semantic_revision = inventory[0]
        required = ("message", "message_embedding")
        if any(
            next(
                connection.execute(
                    "SELECT to_regclass(%s) IS NULL",
                    (f"cc_search_chats.{relation_name}",),
                )
            )[0]
            for relation_name in required
        ):
            raise ValueError("captured legacy embedding relations are unavailable")
        before = next(
            connection.execute(
                "SELECT count(*) FROM cc_search_chats.embedding_value "
                "WHERE profile_id = 'nemotron-3-embed-8b-bf16:v1'"
            )
        )[0]
        scanned = 0
        with connection.cursor(name="legacy_embedding_import") as source:
            source.execute(
                """
                SELECT message.prose_content, embedding.embedding::text
                FROM cc_search_chats.message_embedding AS embedding
                JOIN cc_search_chats.message AS message
                  ON (message.revision_id, message.provider,
                      message.source_session_id, message.logical_message_id,
                      message.content_class) =
                     (embedding.revision_id, embedding.provider,
                      embedding.source_session_id,
                      embedding.logical_message_id, embedding.content_class)
                WHERE embedding.semantic_revision_id = %s
                  AND message.content_class = 'prose'
                  AND message.prose_content ~ '[^[:space:]]'
                ORDER BY embedding.provider, embedding.source_session_id,
                         embedding.logical_message_id, embedding.content_class
                """,
                (selected_semantic_revision,),
            )
            while rows := source.fetchmany(batch_size):
                values = [
                    (
                        "nemotron-3-embed-8b-bf16:v1",
                        hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        embedding,
                    )
                    for text, embedding in rows
                ]
                connection.cursor().executemany(
                    """
                    INSERT INTO cc_search_chats.embedding_value (
                        profile_id, input_digest, embedding
                    ) VALUES (%s, %s, %s::vector)
                    ON CONFLICT (profile_id, input_digest) DO NOTHING
                    """,
                    values,
                )
                scanned += len(rows)
        after = next(
            connection.execute(
                "SELECT count(*) FROM cc_search_chats.embedding_value "
                "WHERE profile_id = 'nemotron-3-embed-8b-bf16:v1'"
            )
        )[0]
        connection.execute(
            """
            UPDATE cc_search_chats.legacy_snapshot_inventory
            SET embedding_pool_imported_at = now(),
                imported_embedding_rows = %s
            WHERE singleton
            """,
            (scanned,),
        )
    return LegacyEmbeddingImport(
        scanned_rows=scanned,
        new_pool_rows=after - before,
        pool_rows_after=after,
    )


def prune_legacy_snapshots(
    connection: psycopg.Connection,
    *,
    expected_fingerprint: str,
    accepted_validation_id: int,
) -> LegacySnapshotPlan:
    """Drop only the exact legacy relations confirmed by a fresh dry-run."""
    with connection.transaction():
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_MIGRATION_LOCK,),
        )
        plan = plan_legacy_snapshot_prune(connection)
        if plan.fingerprint != expected_fingerprint:
            raise ValueError("legacy prune plan changed; run a new dry-run")
        validated = next(
            connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM cc_search_chats.cutover_validation AS validation
                    JOIN cc_search_chats.corpus_state AS corpus
                      ON corpus.current_corpus_generation =
                         validation.corpus_generation
                    JOIN cc_search_chats.corpus_generation AS generation
                      ON generation.corpus_generation =
                         validation.corpus_generation
                    JOIN cc_search_chats.semantic_build AS semantic
                      ON semantic.semantic_build = generation.semantic_build
                     AND semantic.corpus_generation =
                         generation.corpus_generation
                     AND semantic.status = 'complete'
                     AND semantic.completed_at IS NOT NULL
                    WHERE validation.validation_id = %s
                      AND validation.accepted_at IS NOT NULL
                      AND validation.uat_evidence @> %s::jsonb
                      AND generation.message_count =
                          (SELECT count(*)
                           FROM cc_search_chats.message_current)
                      AND generation.alias_count =
                          (SELECT count(*)
                           FROM cc_search_chats.physical_alias_current)
                      AND semantic.embedded_count =
                          (SELECT count(*)
                           FROM cc_search_chats.semantic_chunk_current AS chunk
                           JOIN cc_search_chats.embedding_value AS value
                             ON (value.profile_id, value.input_digest) =
                                (chunk.profile_id, chunk.input_digest)
                           WHERE chunk.profile_id = semantic.profile_id)
                )
                """,
                (
                    accepted_validation_id,
                    json.dumps(
                        {
                            "positive_corpora": [
                                "claude",
                                "claude-ponytail",
                                "codex",
                                "codex-ponytail",
                            ],
                            "semantic_join": "passed",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        )[0]
        if not validated:
            raise RuntimeError(
                "legacy pruning requires an accepted current cutover validation"
            )
        planned_names = {relation.relation_name for relation in plan.relations}
        allowed_names = {
            f"cc_search_chats.{relation_name}"
            for relation_name, _, _ in _LEGACY_RELATIONS
        }
        if not planned_names <= allowed_names:
            raise RuntimeError("legacy prune plan contains a non-allowlisted relation")
        for relation_name, _, _ in _LEGACY_RELATIONS:
            qualified = f"cc_search_chats.{relation_name}"
            if qualified in planned_names:
                connection.execute(
                    sql.SQL("DROP TABLE {}.{}").format(
                        sql.Identifier("cc_search_chats"),
                        sql.Identifier(relation_name),
                    )
                )
        connection.execute(
            "DELETE FROM cc_search_chats.legacy_snapshot_inventory WHERE singleton"
        )
    return plan
