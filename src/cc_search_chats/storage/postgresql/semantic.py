"""Chunk-aware reusable semantic embeddings and hybrid retrieval."""

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from time import monotonic

import psycopg

from cc_search_chats.semantic import ModelUnavailable, SemanticChunk
from cc_search_chats.semantic.model import CHUNKER_ID, PASSAGE_PREFIX
from cc_search_chats.storage.postgresql.guardrails import (
    INDEX_NOTIFY_CHANNEL,
    INDEX_QUEUE_LOCK,
    DatabaseHeartbeat,
    queued_read_operation,
)
from cc_search_chats.storage.postgresql.index import SearchHit, search_messages

_DIMENSIONS = 1024
_PROFILE_ID = "nemotron-3-embed-8b-bf16:chunks-v1"
_EMBEDDABLE_PROSE = "content_class = 'prose' AND prose_content ~ '[^[:space:]]'"
_RUN_HEARTBEAT_SECONDS = 5.0
_PROGRESS_CHECKPOINT_SECONDS = 1.0
type Chunker = Callable[[Sequence[str]], tuple[tuple[SemanticChunk, ...], ...]]


@dataclass(frozen=True, slots=True)
class HybridHit:
    message: SearchHit
    score: Fraction
    literal_rank: int | None
    semantic_rank: int | None
    literal_score: float | None
    semantic_score: float | None
    semantic_chunk_ordinal: int | None
    rank_constant: int
    component_depth: int


@dataclass(frozen=True, slots=True)
class CandidateSemanticBuild:
    """Prepared semantic state that remains invisible until joint publication."""

    semantic_build: int
    embedded_count: int


def _vector(value: Sequence[float]) -> str:
    if len(value) != _DIMENSIONS:
        raise ValueError(f"embedding must contain {_DIMENSIONS} dimensions")
    components = tuple(float(component) for component in value)
    if any(not math.isfinite(component) for component in components):
        raise ValueError("embedding components must be finite")
    norm = math.sqrt(math.fsum(component * component for component in components))
    if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-6):
        raise ValueError("embedding must be normalized")
    return "[" + ",".join(str(component) for component in components) + "]"


def _current_revision(connection: psycopg.Connection) -> int:
    revision = next(
        connection.execute(
            "SELECT current_corpus_generation FROM cc_search_chats.corpus_state "
            "WHERE singleton"
        )
    )[0]
    if revision is None:
        raise ValueError("no selected corpus revision")
    return revision


def _eligible_message_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            "SELECT count(*) FROM cc_search_chats.message_current "
            f"WHERE {_EMBEDDABLE_PROSE}"
        )
    )[0]


def _chunked_message_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            SELECT count(*)
            FROM cc_search_chats.message_current AS message
            WHERE message.content_class = 'prose'
              AND message.prose_content ~ '[^[:space:]]'
              AND EXISTS (
                  SELECT 1
                  FROM cc_search_chats.semantic_chunk_current AS chunk
                  WHERE (chunk.provider, chunk.source_session_id,
                         chunk.logical_message_id, chunk.content_class) =
                        (message.provider, message.source_session_id,
                         message.logical_message_id, message.content_class)
                    AND chunk.profile_id = %s
                    AND chunk.chunker_id = %s
                    AND chunk.source_text_digest = message.embedding_input_digest
              )
            """,
            (_PROFILE_ID, CHUNKER_ID),
        )
    )[0]


def _eligible_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            SELECT count(*)
            FROM cc_search_chats.semantic_chunk_current AS chunk
            JOIN cc_search_chats.message_current AS message
              USING (provider, source_session_id, logical_message_id, content_class)
            WHERE chunk.profile_id = %s
              AND chunk.chunker_id = %s
              AND chunk.source_text_digest = message.embedding_input_digest
              AND message.content_class = 'prose'
              AND message.prose_content ~ '[^[:space:]]'
            """,
            (_PROFILE_ID, CHUNKER_ID),
        )
    )[0]


def _profile_chunk_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            SELECT count(*)
            FROM cc_search_chats.semantic_chunk_current AS chunk
            JOIN cc_search_chats.message_current AS message
              USING (provider, source_session_id, logical_message_id, content_class)
            WHERE chunk.profile_id = %s
            """,
            (_PROFILE_ID,),
        )
    )[0]


def _mapped_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            SELECT count(*)
            FROM cc_search_chats.semantic_chunk_current AS chunk
            JOIN cc_search_chats.message_current AS message
              USING (provider, source_session_id, logical_message_id, content_class)
            JOIN cc_search_chats.embedding_value AS value
              ON (value.profile_id, value.input_digest) =
                 (chunk.profile_id, chunk.input_digest)
            WHERE chunk.profile_id = %s
              AND chunk.chunker_id = %s
              AND chunk.source_text_digest = message.embedding_input_digest
              AND message.content_class = 'prose'
              AND message.prose_content ~ '[^[:space:]]'
            """,
            (_PROFILE_ID, CHUNKER_ID),
        )
    )[0]


def _semantic_chunks_complete(connection: psycopg.Connection) -> bool:
    eligible_messages = _eligible_message_count(connection)
    eligible_chunks = _eligible_count(connection)
    return (
        _chunked_message_count(connection) == eligible_messages
        and _profile_chunk_count(connection) == eligible_chunks
        and _mapped_count(connection) == eligible_chunks
    )


def _selected_revision(
    connection: psycopg.Connection,
    corpus_revision: int,
) -> int | None:
    row = next(
        connection.execute(
            """
            SELECT build.semantic_build
            FROM cc_search_chats.corpus_generation AS generation
            JOIN cc_search_chats.semantic_build AS build
              ON (build.semantic_build, build.corpus_generation) =
                 (generation.semantic_build, generation.corpus_generation)
            WHERE generation.corpus_generation = %s
              AND build.profile_id = %s
              AND build.status = 'complete'
              AND build.completed_at IS NOT NULL
            """,
            (corpus_revision, _PROFILE_ID),
        ),
        None,
    )
    return None if row is None else row[0]


def _create_candidate_semantic_tables(connection: psycopg.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS pg_temp.semantic_candidate_message")
    connection.execute("DROP TABLE IF EXISTS pg_temp.semantic_candidate_chunk")
    connection.execute("DROP TABLE IF EXISTS pg_temp.semantic_embedding_queue")
    connection.execute(
        """
        CREATE TEMP TABLE semantic_candidate_message (
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            content_class text NOT NULL,
            PRIMARY KEY (
                provider, source_session_id, logical_message_id, content_class
            )
        ) ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE semantic_candidate_chunk
        (LIKE cc_search_chats.semantic_chunk_current
         INCLUDING DEFAULTS INCLUDING CONSTRAINTS)
        ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE semantic_embedding_queue (
            input_digest text PRIMARY KEY,
            passage_text text NOT NULL,
            provider text NOT NULL,
            source_session_id text NOT NULL,
            logical_message_id text NOT NULL,
            chunk_ordinal integer NOT NULL,
            mapped_units bigint NOT NULL CHECK (mapped_units > 0)
        ) ON COMMIT PRESERVE ROWS
        """
    )


def _candidate_messages_requiring_chunks(
    connection: psycopg.Connection,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        connection.execute(
            """
            SELECT candidate.provider, candidate.source_session_id,
                   candidate.logical_message_id, candidate.content_class,
                   candidate.prose_content, candidate.embedding_input_digest
            FROM pg_temp.refresh_candidate_message AS candidate
            WHERE candidate.content_class = 'prose'
              AND candidate.prose_content ~ '[^[:space:]]'
            UNION ALL
            SELECT current.provider, current.source_session_id,
                   current.logical_message_id, current.content_class,
                   current.prose_content, current.embedding_input_digest
            FROM cc_search_chats.message_current AS current
            WHERE current.content_class = 'prose'
              AND current.prose_content ~ '[^[:space:]]'
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_temp.refresh_affected_message AS affected
                  WHERE (affected.provider, affected.source_session_id,
                         affected.logical_message_id, affected.content_class) =
                        (current.provider, current.source_session_id,
                         current.logical_message_id, current.content_class)
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM cc_search_chats.semantic_chunk_current AS chunk
                  WHERE (chunk.provider, chunk.source_session_id,
                         chunk.logical_message_id, chunk.content_class) =
                        (current.provider, current.source_session_id,
                         current.logical_message_id, current.content_class)
                    AND chunk.profile_id = %s
                    AND chunk.chunker_id = %s
                    AND chunk.source_text_digest =
                        current.embedding_input_digest
              )
            ORDER BY provider, source_session_id, logical_message_id,
                     content_class
            """,
            (_PROFILE_ID, CHUNKER_ID),
        )
    )


def _stage_candidate_chunks(
    connection: psycopg.Connection,
    chunker: Chunker,
) -> None:
    rows = _candidate_messages_requiring_chunks(connection)
    for offset in range(0, len(rows), 128):
        batch = rows[offset : offset + 128]
        chunk_groups = chunker([str(row[4]) for row in batch])
        if len(chunk_groups) != len(batch):
            raise ValueError("chunker returned the wrong message count")
        connection.cursor().executemany(
            """
            INSERT INTO pg_temp.semantic_candidate_message (
                provider, source_session_id, logical_message_id, content_class
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            [row[:4] for row in batch],
        )
        inserts: list[tuple[object, ...]] = []
        for row, chunks in zip(batch, chunk_groups, strict=True):
            text = str(row[4])
            _validate_chunks(text, chunks)
            for chunk in chunks:
                inserts.append(
                    (
                        *row[:4],
                        _PROFILE_ID,
                        chunk.ordinal,
                        CHUNKER_ID,
                        chunk.token_start,
                        chunk.token_end,
                        chunk.char_start,
                        chunk.char_end,
                        row[5],
                        chunk.text,
                        hashlib.sha256(
                            f"{PASSAGE_PREFIX}{chunk.text}".encode()
                        ).hexdigest(),
                    )
                )
        connection.cursor().executemany(
            """
            INSERT INTO pg_temp.semantic_candidate_chunk (
                provider, source_session_id, logical_message_id,
                content_class, profile_id, chunk_ordinal, chunker_id,
                token_start, token_end, char_start, char_end,
                source_text_digest, passage_text, input_digest
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            inserts,
        )


def _prepare_candidate_embedding_queue(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            WITH candidate_chunk AS (
                SELECT candidate.*
                FROM pg_temp.semantic_candidate_chunk AS candidate
                UNION ALL
                SELECT current.*
                FROM cc_search_chats.semantic_chunk_current AS current
                JOIN cc_search_chats.message_current AS message
                  USING (provider, source_session_id,
                         logical_message_id, content_class)
                WHERE current.profile_id = %s
                  AND current.chunker_id = %s
                  AND current.source_text_digest =
                      message.embedding_input_digest
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_temp.refresh_affected_message AS affected
                      WHERE (affected.provider, affected.source_session_id,
                             affected.logical_message_id,
                             affected.content_class) =
                            (current.provider, current.source_session_id,
                             current.logical_message_id,
                             current.content_class)
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_temp.semantic_candidate_message AS replacement
                      WHERE (replacement.provider,
                             replacement.source_session_id,
                             replacement.logical_message_id,
                             replacement.content_class) =
                            (current.provider, current.source_session_id,
                             current.logical_message_id,
                             current.content_class)
                  )
            ), queued AS (
                INSERT INTO pg_temp.semantic_embedding_queue (
                    input_digest, passage_text, provider, source_session_id,
                    logical_message_id, chunk_ordinal, mapped_units
                )
                SELECT chunk.input_digest, min(chunk.passage_text),
                       min(chunk.provider), min(chunk.source_session_id),
                       min(chunk.logical_message_id), min(chunk.chunk_ordinal),
                       count(*)
                FROM candidate_chunk AS chunk
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM cc_search_chats.embedding_value AS value
                    WHERE (value.profile_id, value.input_digest) =
                          (%s, chunk.input_digest)
                )
                GROUP BY chunk.input_digest
                RETURNING mapped_units
            )
            SELECT COALESCE(sum(mapped_units), 0)::bigint FROM queued
            """,
            (_PROFILE_ID, CHUNKER_ID, _PROFILE_ID),
        )
    )[0]


def _candidate_semantic_counts(
    connection: psycopg.Connection,
) -> tuple[int, int, int, int]:
    return next(
        connection.execute(
            """
            WITH candidate_message AS (
                SELECT candidate.provider, candidate.source_session_id,
                       candidate.logical_message_id, candidate.content_class
                FROM pg_temp.refresh_candidate_message AS candidate
                WHERE candidate.content_class = 'prose'
                  AND candidate.prose_content ~ '[^[:space:]]'
                UNION ALL
                SELECT current.provider, current.source_session_id,
                       current.logical_message_id, current.content_class
                FROM cc_search_chats.message_current AS current
                WHERE current.content_class = 'prose'
                  AND current.prose_content ~ '[^[:space:]]'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_temp.refresh_affected_message AS affected
                      WHERE (affected.provider, affected.source_session_id,
                             affected.logical_message_id,
                             affected.content_class) =
                            (current.provider, current.source_session_id,
                             current.logical_message_id,
                             current.content_class)
                  )
            ), candidate_chunk AS (
                SELECT candidate.*
                FROM pg_temp.semantic_candidate_chunk AS candidate
                UNION ALL
                SELECT current.*
                FROM cc_search_chats.semantic_chunk_current AS current
                JOIN cc_search_chats.message_current AS message
                  USING (provider, source_session_id,
                         logical_message_id, content_class)
                WHERE current.profile_id = %s
                  AND current.chunker_id = %s
                  AND current.source_text_digest =
                      message.embedding_input_digest
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_temp.refresh_affected_message AS affected
                      WHERE (affected.provider, affected.source_session_id,
                             affected.logical_message_id,
                             affected.content_class) =
                            (current.provider, current.source_session_id,
                             current.logical_message_id,
                             current.content_class)
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_temp.semantic_candidate_message AS replacement
                      WHERE (replacement.provider,
                             replacement.source_session_id,
                             replacement.logical_message_id,
                             replacement.content_class) =
                            (current.provider, current.source_session_id,
                             current.logical_message_id,
                             current.content_class)
                  )
            )
            SELECT
                (SELECT count(*) FROM candidate_message),
                (SELECT count(DISTINCT (
                    provider, source_session_id,
                    logical_message_id, content_class
                )) FROM candidate_chunk),
                (SELECT count(*) FROM candidate_chunk),
                (SELECT count(*)
                 FROM candidate_chunk AS chunk
                 JOIN cc_search_chats.embedding_value AS value
                   ON (value.profile_id, value.input_digest) =
                      (chunk.profile_id, chunk.input_digest))
            """,
            (_PROFILE_ID, CHUNKER_ID),
        )
    )


def prepare_candidate_semantics(
    connection: psycopg.Connection,
    corpus_generation: int,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    chunker: Chunker,
    batch_size: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> CandidateSemanticBuild:
    """Prepare candidate chunks and reusable vectors without selecting them."""
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("batch_size must be positive")
    semantic_build = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.semantic_build (
                corpus_generation, profile_id, status, owner_pid, phase,
                heartbeat_at, completed_units, total_units
            ) VALUES (
                %s, %s, 'building', pg_backend_pid(), 'semantic_embed',
                now(), 0, 0
            )
            RETURNING semantic_build
            """,
            (corpus_generation, _PROFILE_ID),
        )
    )[0]
    heartbeat = DatabaseHeartbeat(
        connection.info.dsn,
        """
        UPDATE cc_search_chats.semantic_build
        SET heartbeat_at = now()
        WHERE semantic_build = %s AND status = 'building'
        """,
        (semantic_build,),
        interval_seconds=_RUN_HEARTBEAT_SECONDS,
        label=f"semantic heartbeat {semantic_build}",
    )
    completed = 0
    total = 0
    heartbeat.start()
    try:
        _create_candidate_semantic_tables(connection)
        _stage_candidate_chunks(connection, chunker)
        queued_units = _prepare_candidate_embedding_queue(connection)
        message_count, chunked_messages, total, mapped = _candidate_semantic_counts(
            connection
        )
        if message_count != chunked_messages or mapped > total:
            raise ValueError(
                "candidate semantic projection is inconsistent: "
                f"messages={chunked_messages}/{message_count}, "
                f"mapped={mapped}/{total}"
            )
        completed = total - queued_units
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_build
            SET completed_units = %s, total_units = %s, heartbeat_at = now()
            WHERE semantic_build = %s AND status = 'building'
            """,
            (completed, total, semantic_build),
        )
        if progress is not None:
            progress(completed, total)
        last_checkpoint = monotonic()
        while completed < total:
            rows = tuple(
                connection.execute(
                    """
                    WITH batch AS MATERIALIZED (
                        SELECT provider, source_session_id, logical_message_id,
                               chunk_ordinal, passage_text, input_digest,
                               mapped_units
                        FROM pg_temp.semantic_embedding_queue
                        ORDER BY input_digest
                        LIMIT %s
                    )
                    SELECT provider, source_session_id, logical_message_id,
                           chunk_ordinal, passage_text, input_digest, mapped_units
                    FROM batch
                    ORDER BY input_digest
                    """,
                    (batch_size,),
                    prepare=True,
                )
            )
            if not rows:
                raise ValueError("candidate embedding queue made no progress")
            try:
                vectors = embed([row[4] for row in rows])
            except ModelUnavailable:
                raise
            except Exception as error:
                raise RuntimeError(
                    f"semantic embedding failed after {completed}/{total} passages "
                    f"at {rows[0][0]}:{rows[0][1]}:{rows[0][2]}:{rows[0][3]}: "
                    f"{error}"
                ) from error
            if len(vectors) != len(rows):
                raise ValueError("embedding model returned the wrong batch size")
            with connection.transaction():
                connection.cursor().executemany(
                    """
                    INSERT INTO cc_search_chats.embedding_value (
                        profile_id, input_digest, embedding
                    ) VALUES (%s, %s, %s::vector)
                    ON CONFLICT (profile_id, input_digest) DO NOTHING
                    """,
                    [
                        (_PROFILE_ID, row[5], _vector(vector))
                        for row, vector in zip(rows, vectors, strict=True)
                    ],
                )
                deleted_digests = connection.execute(
                    """
                    DELETE FROM pg_temp.semantic_embedding_queue
                    WHERE input_digest = ANY(%s::text[])
                    """,
                    ([row[5] for row in rows],),
                    prepare=True,
                ).rowcount
            if deleted_digests != len(rows):
                raise ValueError("candidate embedding queue made no progress")
            completed += sum(int(row[6]) for row in rows)
            checkpoint_at = monotonic()
            if (
                completed == total
                or checkpoint_at - last_checkpoint >= _PROGRESS_CHECKPOINT_SECONDS
            ):
                connection.execute(
                    """
                    UPDATE cc_search_chats.semantic_build
                    SET completed_units = %s, heartbeat_at = now()
                    WHERE semantic_build = %s AND status = 'building'
                    """,
                    (completed, semantic_build),
                    prepare=True,
                )
                if progress is not None:
                    progress(completed, total)
                last_checkpoint = checkpoint_at
        heartbeat.raise_if_failed()
        message_count, chunked_messages, total, mapped = _candidate_semantic_counts(
            connection
        )
        if message_count != chunked_messages or mapped != total or completed != total:
            raise ValueError(
                "candidate semantic publication is incomplete: "
                f"messages={chunked_messages}/{message_count}, "
                f"mapped={mapped}/{total}, completed={completed}"
            )
        return CandidateSemanticBuild(
            semantic_build=semantic_build,
            embedded_count=total,
        )
    except Exception as error:
        code = (
            error.code
            if isinstance(error, ModelUnavailable)
            else "semantic_candidate_failed"
        )
        phase = error.phase if isinstance(error, ModelUnavailable) else "semantic_embed"
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_build
            SET status = 'failed', completed_at = now(), phase = %s,
                heartbeat_at = now(), completed_units = %s,
                failure = jsonb_build_object(
                    'code', %s::text, 'phase', %s::text,
                    'completed', %s::bigint, 'total', %s::bigint,
                    'error', %s::text
                )
            WHERE semantic_build = %s AND status = 'building'
            """,
            (
                phase,
                completed,
                code,
                phase,
                completed,
                total,
                str(error),
                semantic_build,
            ),
        )
        raise
    finally:
        heartbeat.stop()


def _validate_chunks(text: str, chunks: tuple[SemanticChunk, ...]) -> None:
    if not chunks:
        raise ValueError("chunker returned no passages for nonblank prose")
    for ordinal, chunk in enumerate(chunks):
        if chunk.ordinal != ordinal:
            raise ValueError("chunk ordinals must be contiguous from zero")
        if not 0 <= chunk.token_start < chunk.token_end:
            raise ValueError("chunk token bounds are invalid")
        if not 0 <= chunk.char_start < chunk.char_end <= len(text):
            raise ValueError("chunk character bounds are invalid")
        if chunk.text != text[chunk.char_start : chunk.char_end]:
            raise ValueError("chunk text does not match its character bounds")
        if not chunk.text.strip():
            raise ValueError("chunk text must not be blank")


def _sync_chunks(connection: psycopg.Connection, chunker: Chunker) -> None:
    """Persist only messages whose current profile chunks are absent or stale."""
    after: tuple[str, str, str, str] | None = None
    while True:
        params: list[object] = [_PROFILE_ID, _PROFILE_ID, CHUNKER_ID]
        keyset = ""
        if after is not None:
            keyset = (
                "AND (message.provider, message.source_session_id, "
                "message.logical_message_id, message.content_class) > "
                "(%s, %s, %s, %s)"
            )
            params.extend(after)
        rows = tuple(
            connection.execute(
                f"""
                SELECT message.provider, message.source_session_id,
                       message.logical_message_id, message.content_class,
                       message.prose_content, message.embedding_input_digest
                FROM cc_search_chats.message_current AS message
                WHERE message.content_class = 'prose'
                  AND message.prose_content ~ '[^[:space:]]'
                  AND (
                    NOT EXISTS (
                      SELECT 1
                      FROM cc_search_chats.semantic_chunk_current AS chunk
                      WHERE (chunk.provider, chunk.source_session_id,
                             chunk.logical_message_id, chunk.content_class) =
                            (message.provider, message.source_session_id,
                             message.logical_message_id, message.content_class)
                        AND chunk.profile_id = %s
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM cc_search_chats.semantic_chunk_current AS chunk
                      WHERE (chunk.provider, chunk.source_session_id,
                             chunk.logical_message_id, chunk.content_class) =
                            (message.provider, message.source_session_id,
                             message.logical_message_id, message.content_class)
                        AND chunk.profile_id = %s
                        AND (
                          chunk.source_text_digest IS DISTINCT FROM
                              message.embedding_input_digest
                          OR chunk.chunker_id <> %s
                        )
                    )
                  )
                  {keyset}
                ORDER BY message.provider, message.source_session_id,
                         message.logical_message_id, message.content_class
                LIMIT 128
                """.encode(),
                params,
            )
        )
        if not rows:
            return
        chunk_groups = chunker([row[4] for row in rows])
        if len(chunk_groups) != len(rows):
            raise ValueError("chunker returned the wrong message count")
        inserts: list[tuple[object, ...]] = []
        for row, chunks in zip(rows, chunk_groups, strict=True):
            _validate_chunks(row[4], chunks)
            for chunk in chunks:
                input_digest = hashlib.sha256(
                    f"{PASSAGE_PREFIX}{chunk.text}".encode()
                ).hexdigest()
                inserts.append(
                    (
                        *row[:4],
                        _PROFILE_ID,
                        chunk.ordinal,
                        CHUNKER_ID,
                        chunk.token_start,
                        chunk.token_end,
                        chunk.char_start,
                        chunk.char_end,
                        row[5],
                        chunk.text,
                        input_digest,
                    )
                )
        with connection.transaction():
            connection.cursor().executemany(
                """
                DELETE FROM cc_search_chats.semantic_chunk_current
                WHERE provider = %s AND source_session_id = %s
                  AND logical_message_id = %s AND content_class = %s
                  AND profile_id = %s
                """,
                [(*row[:4], _PROFILE_ID) for row in rows],
            )
            connection.cursor().executemany(
                """
                INSERT INTO cc_search_chats.semantic_chunk_current (
                    provider, source_session_id, logical_message_id,
                    content_class, profile_id, chunk_ordinal, chunker_id,
                    token_start, token_end, char_start, char_end,
                    source_text_digest, passage_text, input_digest
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                inserts,
            )
        after = rows[-1][:4]


def _publish_semantic_revision(
    connection: psycopg.Connection,
    *,
    semantic_revision: int,
    corpus_revision: int,
    embedded_count: int,
) -> None:
    if _current_revision(connection) != corpus_revision:
        raise ValueError("corpus changed while embeddings were being built")
    message_count = _eligible_message_count(connection)
    chunked_messages = _chunked_message_count(connection)
    eligible = _eligible_count(connection)
    profile_chunks = _profile_chunk_count(connection)
    mapped = _mapped_count(connection)
    if (
        message_count != chunked_messages
        or profile_chunks != eligible
        or eligible != embedded_count
        or mapped != embedded_count
    ):
        raise ValueError(
            "semantic publication is incomplete: "
            f"messages={chunked_messages}/{message_count}, mapped={mapped}, "
            f"profile_chunks={profile_chunks}, eligible={eligible}, "
            f"expected={embedded_count}"
        )
    connection.execute(
        """
        UPDATE cc_search_chats.semantic_revision
        SET status = 'complete', completed_at = now(),
            embedded_count = %s, failure = NULL, phase = 'done',
            heartbeat_at = now(), completed_units = %s, total_units = %s
        WHERE semantic_revision_id = %s
        """,
        (embedded_count, embedded_count, embedded_count, semantic_revision),
    )
    connection.execute(
        "UPDATE cc_search_chats.semantic_state "
        "SET current_semantic_revision_id = %s WHERE singleton",
        (semantic_revision,),
    )
    connection.execute(
        """
        DELETE FROM cc_search_chats.embedding_value AS value
        WHERE NOT EXISTS (
            SELECT 1
            FROM cc_search_chats.semantic_chunk_current AS chunk
            WHERE (chunk.profile_id, chunk.input_digest) =
                  (value.profile_id, value.input_digest)
        )
        """
    )


def index_embeddings(
    connection: psycopg.Connection,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    chunker: Chunker,
    batch_size: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Wait for and run one resumable chunk/vector worker per database."""
    connection.execute(
        "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
        (INDEX_QUEUE_LOCK,),
    )
    try:
        return _index_embeddings(
            connection,
            embed,
            chunker=chunker,
            batch_size=batch_size,
            progress=progress,
        )
    except Exception as error:
        _record_unexpected_semantic_failure(connection, error)
        raise
    finally:
        connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (INDEX_QUEUE_LOCK,),
        )
        connection.execute(
            "SELECT pg_notify(%s, %s)",
            (INDEX_NOTIFY_CHANNEL, "released"),
        )


def _record_unexpected_semantic_failure(
    connection: psycopg.Connection,
    error: BaseException,
) -> None:
    """Fail a still-building owned generation without masking its root error."""
    try:
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_revision
            SET status = 'failed', completed_at = now(), heartbeat_at = now(),
                failure = jsonb_build_object(
                    'code', 'semantic_refresh_failed',
                    'phase', phase,
                    'completed', completed_units,
                    'total', total_units,
                    'error', %s::text
                )
            WHERE status = 'building' AND owner_pid = pg_backend_pid()
            """,
            (str(error),),
        )
    except psycopg.Error:
        return


def _partial_revision(
    connection: psycopg.Connection,
    corpus_revision: int,
) -> int:
    row = next(
        connection.execute(
            """
            SELECT semantic_revision_id
            FROM cc_search_chats.semantic_revision
            WHERE corpus_revision_id = %s
              AND profile_id = %s
              AND status IN ('building', 'failed')
            ORDER BY semantic_revision_id DESC
            LIMIT 1
            """,
            (corpus_revision, _PROFILE_ID),
        ),
        None,
    )
    if row is not None:
        revision = row[0]
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_revision
            SET status = 'building', failure = NULL, completed_at = NULL,
                owner_pid = pg_backend_pid(), phase = 'semantic_embed',
                heartbeat_at = now(), completed_units = 0, total_units = 0
            WHERE semantic_revision_id = %s
            """,
            (revision,),
        )
        return revision
    return next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.semantic_revision (
                corpus_revision_id, profile_id, status, owner_pid, phase,
                heartbeat_at, completed_units, total_units
            ) VALUES (
                %s, %s, 'building', pg_backend_pid(), 'semantic_embed',
                now(), 0, 0
            )
            RETURNING semantic_revision_id
            """,
            (corpus_revision, _PROFILE_ID),
        )
    )[0]


def _prepare_embedding_queue(connection: psycopg.Connection) -> int:
    """Snapshot each missing chunk once and return its mapped-unit count."""
    connection.execute(
        """
        CREATE TEMPORARY TABLE IF NOT EXISTS semantic_embedding_queue (
            input_digest text PRIMARY KEY,
            mapped_units bigint NOT NULL CHECK (mapped_units > 0)
        ) ON COMMIT PRESERVE ROWS
        """
    )
    connection.execute("TRUNCATE pg_temp.semantic_embedding_queue")
    return next(
        connection.execute(
            t"""
            WITH queued AS (
                INSERT INTO pg_temp.semantic_embedding_queue (
                    input_digest, mapped_units
                )
                SELECT chunk.input_digest, count(*)
                FROM cc_search_chats.semantic_chunk_current AS chunk
                JOIN cc_search_chats.message_current AS message
                  USING (provider, source_session_id,
                         logical_message_id, content_class)
                WHERE chunk.profile_id = {_PROFILE_ID}
                  AND chunk.chunker_id = {CHUNKER_ID}
                  AND chunk.source_text_digest = message.embedding_input_digest
                  AND message.content_class = 'prose'
                  AND message.prose_content ~ '[^[:space:]]'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cc_search_chats.embedding_value AS value
                      WHERE (value.profile_id, value.input_digest) =
                            (chunk.profile_id, chunk.input_digest)
                  )
                GROUP BY chunk.input_digest
                RETURNING mapped_units
            )
            SELECT COALESCE(sum(mapped_units), 0)::bigint
            FROM queued
            """
        )
    )[0]


def _index_embeddings(
    connection: psycopg.Connection,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    chunker: Chunker,
    batch_size: int,
    progress: Callable[[int, int], None] | None,
) -> int:
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("batch_size must be positive")
    corpus_revision = _current_revision(connection)
    selected = _selected_revision(connection, corpus_revision)
    if selected is not None and _semantic_chunks_complete(connection):
        return _eligible_count(connection)

    semantic_revision = _partial_revision(connection, corpus_revision)
    heartbeat = DatabaseHeartbeat(
        connection.info.dsn,
        """
        UPDATE cc_search_chats.semantic_revision
        SET heartbeat_at = now()
        WHERE semantic_revision_id = %s AND status = 'building'
        """,
        (semantic_revision,),
        interval_seconds=_RUN_HEARTBEAT_SECONDS,
        label=f"semantic heartbeat {semantic_revision}",
    )
    heartbeat.start()
    try:
        _sync_chunks(connection, chunker)
        total = _eligible_count(connection)
        queued_units = _prepare_embedding_queue(connection)
        completed = total - queued_units
        if completed < 0:
            raise ValueError("semantic embedding queue exceeds the eligible corpus")
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_revision
            SET completed_units = %s, total_units = %s, heartbeat_at = now()
            WHERE semantic_revision_id = %s AND status = 'building'
            """,
            (completed, total, semantic_revision),
        )
        if progress is not None:
            progress(completed, total)

        last_checkpoint = monotonic()
        while completed < total:
            queued_rows = tuple(
                connection.execute(
                    t"""
                    WITH batch AS MATERIALIZED (
                        SELECT input_digest, mapped_units
                        FROM pg_temp.semantic_embedding_queue
                        ORDER BY input_digest
                        LIMIT {batch_size}
                    )
                    SELECT representative.provider,
                           representative.source_session_id,
                           representative.logical_message_id,
                           representative.chunk_ordinal,
                           representative.passage_text,
                           batch.input_digest, batch.mapped_units
                    FROM batch
                    JOIN LATERAL (
                        SELECT chunk.provider, chunk.source_session_id,
                               chunk.logical_message_id, chunk.chunk_ordinal,
                               chunk.passage_text
                        FROM cc_search_chats.semantic_chunk_current AS chunk
                        WHERE chunk.profile_id = {_PROFILE_ID}
                          AND chunk.input_digest = batch.input_digest
                        ORDER BY chunk.provider, chunk.source_session_id,
                                 chunk.logical_message_id,
                                 chunk.content_class, chunk.chunk_ordinal
                        LIMIT 1
                    ) AS representative ON true
                    ORDER BY batch.input_digest
                    """,
                    prepare=True,
                )
            )
            if not queued_rows:
                raise ValueError("corpus changed while embeddings were being built")
            rows = queued_rows
            try:
                vectors = embed([row[4] for row in rows])
            except Exception as error:
                code = (
                    error.code
                    if isinstance(error, ModelUnavailable)
                    else "semantic_embedding_failed"
                )
                phase = (
                    error.phase
                    if isinstance(error, ModelUnavailable)
                    else "semantic_embed"
                )
                connection.execute(
                    """
                    UPDATE cc_search_chats.semantic_revision
                    SET status = 'failed', completed_at = now(), phase = %s,
                        heartbeat_at = now(), completed_units = %s,
                        failure = jsonb_build_object(
                            'completed', %s::bigint, 'total', %s::bigint,
                            'provider', %s::text, 'session_id', %s::text,
                            'logical_message_id', %s::text,
                            'chunk_ordinal', %s::integer, 'error', %s::text,
                            'code', %s::text, 'phase', %s::text
                        )
                    WHERE semantic_revision_id = %s
                    """,
                    (
                        phase,
                        completed,
                        completed,
                        total,
                        rows[0][0],
                        rows[0][1],
                        rows[0][2],
                        rows[0][3],
                        str(error),
                        code,
                        phase,
                        semantic_revision,
                    ),
                )
                if isinstance(error, ModelUnavailable):
                    raise
                raise RuntimeError(
                    f"semantic embedding failed after {completed}/{total} passages "
                    f"at {rows[0][0]}:{rows[0][1]}:{rows[0][2]}:"
                    f"{rows[0][3]}"
                ) from error
            if len(vectors) != len(rows):
                raise ValueError("embedding model returned the wrong batch size")
            values = [
                (_PROFILE_ID, row[5], _vector(vector))
                for row, vector in zip(rows, vectors, strict=True)
            ]
            with connection.transaction():
                connection.cursor().executemany(
                    """
                    INSERT INTO cc_search_chats.embedding_value (
                        profile_id, input_digest, embedding
                    ) VALUES (%s, %s, %s::vector)
                    ON CONFLICT (profile_id, input_digest) DO NOTHING
                    """,
                    values,
                )
                input_digests = [row[5] for row in rows]
                deleted_digests = connection.execute(
                    t"""
                    DELETE FROM pg_temp.semantic_embedding_queue
                    WHERE input_digest = ANY({input_digests})
                    """,
                    prepare=True,
                ).rowcount
            if deleted_digests != len(rows):
                raise ValueError("semantic embedding queue made no progress")
            mapped_units = sum(row[6] for row in rows)
            completed += mapped_units
            checkpoint_at = monotonic()
            if (
                completed == total
                or checkpoint_at - last_checkpoint >= _PROGRESS_CHECKPOINT_SECONDS
            ):
                connection.execute(
                    t"""
                    UPDATE cc_search_chats.semantic_revision
                    SET completed_units = {completed}, heartbeat_at = now()
                    WHERE semantic_revision_id = {semantic_revision}
                      AND status = 'building'
                    """,
                    prepare=True,
                )
                if progress is not None:
                    progress(completed, total)
                last_checkpoint = checkpoint_at

        heartbeat.raise_if_failed()
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_revision
            SET phase = 'semantic_commit', heartbeat_at = now()
            WHERE semantic_revision_id = %s AND status = 'building'
            """,
            (semantic_revision,),
        )
        with connection.transaction():
            _publish_semantic_revision(
                connection,
                semantic_revision=semantic_revision,
                corpus_revision=corpus_revision,
                embedded_count=completed,
            )
        return completed
    finally:
        heartbeat.stop()


@queued_read_operation
def semantic_search(
    connection: psycopg.Connection,
    embedding: Sequence[float],
    *,
    limit: int = 20,
    provider: str | None = None,
    role: str | None = None,
    project: str | None = None,
    since: str | None = None,
    epoch: int | None = None,
    include_agents: bool = False,
    allow_partial: bool = False,
) -> tuple[SearchHit, ...]:
    """Return one best-chunk exact inner-product hit per logical message."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be positive")
    vector = _vector(embedding)
    state = next(
        connection.execute(
            """
            SELECT build.status = 'complete'
                   AND build.completed_at IS NOT NULL
                   AND build.profile_id = %s,
                   build.corpus_generation = generation.corpus_generation
            FROM cc_search_chats.corpus_state AS state
            JOIN cc_search_chats.corpus_generation AS generation
              ON generation.corpus_generation =
                 state.current_corpus_generation
            JOIN cc_search_chats.semantic_build AS build
              ON (build.semantic_build, build.corpus_generation) =
                 (generation.semantic_build, generation.corpus_generation)
            WHERE state.singleton
            """,
            (_PROFILE_ID,),
        ),
        None,
    )
    if state is None or state[0] is not True:
        raise ValueError("semantic revision is unavailable or stale")
    if not allow_partial and state[1] is not True:
        raise ValueError("semantic revision is unavailable or stale")
    if not allow_partial and not _semantic_chunks_complete(connection):
        raise ValueError("semantic chunks are unavailable or stale")
    filters = [
        "message.session_kind IN ('primary', 'agent', 'unknown')"
        if include_agents
        else "message.session_kind = 'primary'"
    ]
    params: list[object] = [vector, vector, _PROFILE_ID, CHUNKER_ID]
    for value, clause in (
        (provider, "message.provider = %s"),
        (role, "message.role = %s"),
        (project, "COALESCE(message.repository, message.cwd) = %s"),
        (since, "message.timestamp_text >= %s"),
        (epoch, "message.conversation_epoch = %s"),
    ):
        if value is not None:
            filters.append(clause)
            params.append(value)
    params.append(limit)
    where = " AND ".join(filters)
    rows = connection.execute(
        f"""
        WITH ranked_chunk AS (
            SELECT message.provider, message.source_session_id,
                   message.logical_message_id, message.canonical_locator,
                   message.timestamp_text, message.role, message.session_kind,
                   message.conversation_epoch, message.content_class,
                   message.prose_content, message.repository, message.cwd,
                   -(value.embedding <#> %s::vector) AS score,
                   chunk.chunk_ordinal,
                   row_number() OVER (
                       PARTITION BY message.provider, message.source_session_id,
                                    message.logical_message_id,
                                    message.content_class
                       ORDER BY value.embedding <#> %s::vector,
                                chunk.chunk_ordinal
                   ) AS chunk_rank
            FROM cc_search_chats.semantic_chunk_current AS chunk
            JOIN cc_search_chats.embedding_value AS value
              ON (value.profile_id, value.input_digest) =
                 (chunk.profile_id, chunk.input_digest)
            JOIN cc_search_chats.message_current AS message
              USING (provider, source_session_id,
                     logical_message_id, content_class)
            WHERE chunk.profile_id = %s
              AND chunk.chunker_id = %s
              AND chunk.source_text_digest = message.embedding_input_digest
              AND {where}
        )
        SELECT provider, source_session_id, logical_message_id,
               canonical_locator, timestamp_text, role, session_kind,
               conversation_epoch, content_class, prose_content,
               repository, cwd, score, chunk_ordinal
        FROM ranked_chunk
        WHERE chunk_rank = 1
        ORDER BY score DESC, provider, source_session_id, logical_message_id
        LIMIT %s
        """.encode(),
        params,
    )
    return tuple(SearchHit(*row) for row in rows)


def _ranked_component_depth(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    return min(1000, max(100, 5 * limit))


def _fuse_hybrid(
    literal: Sequence[SearchHit],
    semantic: Sequence[SearchHit],
    *,
    limit: int,
    rank_constant: int,
    component_depth: int,
) -> tuple[HybridHit, ...]:
    if (
        isinstance(rank_constant, bool)
        or not isinstance(rank_constant, int)
        or rank_constant <= 0
    ):
        raise ValueError("rank_constant must be a positive integer")
    literal_ranks = {
        value.canonical_locator: rank for rank, value in enumerate(literal, 1)
    }
    semantic_ranks = {
        value.canonical_locator: rank for rank, value in enumerate(semantic, 1)
    }
    literal_scores = {value.canonical_locator: value.rank for value in literal}
    semantic_scores = {value.canonical_locator: value.rank for value in semantic}
    semantic_chunks = {
        value.canonical_locator: value.semantic_chunk_ordinal for value in semantic
    }
    messages = {value.canonical_locator: value for value in (*literal, *semantic)}
    fused = [
        HybridHit(
            message=message,
            score=sum(
                (
                    Fraction(1, rank_constant + rank)
                    for rank in (
                        literal_ranks.get(locator),
                        semantic_ranks.get(locator),
                    )
                    if rank is not None
                ),
                start=Fraction(0),
            ),
            literal_rank=literal_ranks.get(locator),
            semantic_rank=semantic_ranks.get(locator),
            literal_score=literal_scores.get(locator),
            semantic_score=semantic_scores.get(locator),
            semantic_chunk_ordinal=semantic_chunks.get(locator),
            rank_constant=rank_constant,
            component_depth=component_depth,
        )
        for locator, message in messages.items()
    ]
    return tuple(
        sorted(
            fused, key=lambda value: (-value.score, value.message.canonical_locator)
        )[:limit]
    )


fuse_hybrid = _fuse_hybrid


def _hybrid_search(
    connection: psycopg.Connection,
    query: str,
    embedding: Sequence[float],
    *,
    limit: int = 20,
    provider: str | None = None,
    role: str | None = None,
    project: str | None = None,
    since: str | None = None,
    epoch: int | None = None,
    rank_constant: int = 60,
    include_agents: bool = False,
) -> tuple[HybridHit, ...]:
    depth = _ranked_component_depth(limit)
    if (
        isinstance(rank_constant, bool)
        or not isinstance(rank_constant, int)
        or rank_constant <= 0
    ):
        raise ValueError("rank_constant must be a positive integer")
    literal = search_messages(
        connection,
        query,
        limit=depth,
        provider=provider,
        role=role,
        project=project,
        since=since,
        epoch=epoch,
        include_agents=include_agents,
    )
    semantic = semantic_search(
        connection,
        embedding,
        limit=depth,
        provider=provider,
        role=role,
        project=project,
        since=since,
        epoch=epoch,
        include_agents=include_agents,
    )
    return _fuse_hybrid(
        literal,
        semantic,
        limit=limit,
        rank_constant=rank_constant,
        component_depth=depth,
    )


hybrid_search = queued_read_operation(_hybrid_search)
