"""Exact pgvector retrieval, reusable embeddings, and rank fusion."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

import psycopg

from cc_search_chats.semantic import ModelUnavailable
from cc_search_chats.storage.postgresql.guardrails import (
    INDEX_NOTIFY_CHANNEL,
    INDEX_QUEUE_LOCK,
    DatabaseHeartbeat,
    queued_read_operation,
)
from cc_search_chats.storage.postgresql.index import SearchHit, search_messages

_DIMENSIONS = 1024
_PROFILE_ID = "nemotron-3-embed-8b-bf16:v1"
_EMBEDDABLE_PROSE = "content_class = 'prose' AND prose_content ~ '[^[:space:]]'"
_RUN_HEARTBEAT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class HybridHit:
    message: SearchHit
    score: Fraction
    literal_rank: int | None
    semantic_rank: int | None
    literal_score: float | None
    semantic_score: float | None
    rank_constant: int
    component_depth: int


def _vector(value: Sequence[float]) -> str:
    if len(value) != _DIMENSIONS:
        raise ValueError(f"embedding must contain {_DIMENSIONS} dimensions")
    return "[" + ",".join(str(float(component)) for component in value) + "]"


def _current_revision(connection: psycopg.Connection) -> int:
    revision = next(
        connection.execute(
            "SELECT current_revision_id FROM cc_search_chats.corpus_state "
            "WHERE singleton"
        )
    )[0]
    if revision is None:
        raise ValueError("no selected corpus revision")
    return revision


def _eligible_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            "SELECT count(*) FROM cc_search_chats.message_current "
            f"WHERE {_EMBEDDABLE_PROSE}"
        )
    )[0]


def _mapped_count(connection: psycopg.Connection) -> int:
    return next(
        connection.execute(
            """
            SELECT count(*)
            FROM cc_search_chats.message_embedding_current AS embedding
            JOIN cc_search_chats.message_current AS message
              USING (provider, source_session_id, logical_message_id, content_class)
            WHERE embedding.profile_id = %s
              AND embedding.input_digest = message.embedding_input_digest
              AND message.content_class = 'prose'
              AND message.prose_content ~ '[^[:space:]]'
            """,
            (_PROFILE_ID,),
        )
    )[0]


def _publish_semantic_revision(
    connection: psycopg.Connection,
    *,
    semantic_revision: int,
    corpus_revision: int,
    embedded_count: int,
) -> None:
    current = _current_revision(connection)
    if current != corpus_revision:
        raise ValueError("corpus changed while embeddings were being built")
    eligible = _eligible_count(connection)
    mapped = _mapped_count(connection)
    if eligible != embedded_count or mapped != embedded_count:
        raise ValueError(
            "semantic publication is incomplete: "
            f"mapped={mapped}, eligible={eligible}, expected={embedded_count}"
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
            FROM cc_search_chats.message_embedding_current AS mapping
            WHERE (mapping.profile_id, mapping.input_digest) =
                  (value.profile_id, value.input_digest)
        )
        """
    )


def replace_embeddings(
    connection: psycopg.Connection,
    embeddings: Mapping[str, Sequence[float]],
) -> int:
    """Publish complete vectors for canonical prose locators."""
    with connection.transaction():
        corpus_revision = _current_revision(connection)
        semantic_revision = next(
            connection.execute(
                """
                INSERT INTO cc_search_chats.semantic_revision (
                    corpus_revision_id, profile_id, status
                ) VALUES (%s, %s, 'building')
                RETURNING semantic_revision_id
                """,
                (corpus_revision, _PROFILE_ID),
            )
        )[0]
        for locator, embedding in embeddings.items():
            message = next(
                connection.execute(
                    """
                    SELECT provider, source_session_id, logical_message_id,
                           content_class, embedding_input_digest
                    FROM cc_search_chats.message_current
                    WHERE canonical_locator = %s
                      AND content_class = 'prose'
                      AND prose_content ~ '[^[:space:]]'
                    """,
                    (locator,),
                ),
                None,
            )
            if message is None:
                continue
            *key, input_digest = message
            connection.execute(
                """
                INSERT INTO cc_search_chats.embedding_value (
                    profile_id, input_digest, embedding
                ) VALUES (%s, %s, %s::vector)
                ON CONFLICT (profile_id, input_digest) DO NOTHING
                """,
                (_PROFILE_ID, input_digest, _vector(embedding)),
            )
            connection.execute(
                """
                INSERT INTO cc_search_chats.message_embedding_current (
                    provider, source_session_id, logical_message_id,
                    content_class, profile_id, input_digest
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    provider, source_session_id, logical_message_id,
                    content_class, profile_id
                ) DO UPDATE SET input_digest = EXCLUDED.input_digest
                WHERE message_embedding_current.input_digest
                      IS DISTINCT FROM EXCLUDED.input_digest
                """,
                (*key, _PROFILE_ID, input_digest),
            )
        expected = _eligible_count(connection)
        inserted = _mapped_count(connection)
        if inserted != expected:
            raise ValueError(f"semantic revision is incomplete: {inserted}/{expected}")
        _publish_semantic_revision(
            connection,
            semantic_revision=semantic_revision,
            corpus_revision=corpus_revision,
            embedded_count=inserted,
        )
    return inserted


def index_embeddings(
    connection: psycopg.Connection,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    batch_size: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Wait for and run one resumable semantic worker per database."""
    connection.execute(
        "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
        (INDEX_QUEUE_LOCK,),
    )
    try:
        return _index_embeddings(
            connection, embed, batch_size=batch_size, progress=progress
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


def _index_embeddings(
    connection: psycopg.Connection,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    batch_size: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Fill only missing reusable vectors, then publish one semantic generation."""
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("batch_size must be positive")
    corpus_revision = _current_revision(connection)
    total = _eligible_count(connection)
    selected = next(
        connection.execute(
            """
            SELECT revision.semantic_revision_id
            FROM cc_search_chats.semantic_state AS state
            JOIN cc_search_chats.semantic_revision AS revision
              ON revision.semantic_revision_id = state.current_semantic_revision_id
            WHERE state.singleton
              AND revision.corpus_revision_id = %s
              AND revision.profile_id = %s
              AND revision.status = 'complete'
            """,
            (corpus_revision, _PROFILE_ID),
        ),
        None,
    )
    if selected is not None:
        mapped = _mapped_count(connection)
        if mapped != total:
            raise ValueError(
                f"selected semantic mapping is incomplete: {mapped}/{total}"
            )
        return total

    partial = next(
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
    if partial is None:
        semantic_revision = next(
            connection.execute(
                """
                INSERT INTO cc_search_chats.semantic_revision (
                    corpus_revision_id, profile_id, status, owner_pid, phase,
                    heartbeat_at, completed_units, total_units
                ) VALUES (
                    %s, %s, 'building', pg_backend_pid(), 'semantic_embed',
                    now(), 0, %s
                )
                RETURNING semantic_revision_id
                """,
                (corpus_revision, _PROFILE_ID, total),
            )
        )[0]
    else:
        semantic_revision = partial[0]
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_revision
            SET status = 'building', failure = NULL,
                owner_pid = pg_backend_pid(), phase = 'semantic_embed',
                heartbeat_at = now(), total_units = %s
            WHERE semantic_revision_id = %s
            """,
            (total, semantic_revision),
        )

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

    connection.execute(
        """
        INSERT INTO cc_search_chats.message_embedding_current (
            provider, source_session_id, logical_message_id, content_class,
            profile_id, input_digest
        )
        SELECT message.provider, message.source_session_id,
               message.logical_message_id, message.content_class,
               value.profile_id, value.input_digest
        FROM cc_search_chats.message_current AS message
        JOIN cc_search_chats.embedding_value AS value
          ON value.profile_id = %s
         AND value.input_digest = message.embedding_input_digest
        WHERE message.content_class = 'prose'
          AND message.prose_content ~ '[^[:space:]]'
        ON CONFLICT (
            provider, source_session_id, logical_message_id, content_class,
            profile_id
        ) DO UPDATE SET input_digest = EXCLUDED.input_digest
        WHERE message_embedding_current.input_digest
              IS DISTINCT FROM EXCLUDED.input_digest
        """,
        (_PROFILE_ID,),
    )
    completed = _mapped_count(connection)
    connection.execute(
        """
        UPDATE cc_search_chats.semantic_revision
        SET completed_units = %s, heartbeat_at = now()
        WHERE semantic_revision_id = %s AND status = 'building'
        """,
        (completed, semantic_revision),
    )
    if progress is not None:
        progress(completed, total)

    while completed < total:
        rows = tuple(
            connection.execute(
                """
                SELECT message.provider, message.source_session_id,
                       message.logical_message_id, message.content_class,
                       message.prose_content, message.embedding_input_digest
                FROM cc_search_chats.message_current AS message
                WHERE message.content_class = 'prose'
                  AND message.prose_content ~ '[^[:space:]]'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cc_search_chats.message_embedding_current AS embedding
                      WHERE embedding.provider = message.provider
                        AND embedding.source_session_id = message.source_session_id
                        AND embedding.logical_message_id = message.logical_message_id
                        AND embedding.content_class = message.content_class
                        AND embedding.profile_id = %s
                        AND embedding.input_digest = message.embedding_input_digest
                  )
                ORDER BY message.provider, message.source_session_id,
                         message.logical_message_id, message.content_class
                LIMIT %s
                """,
                (_PROFILE_ID, batch_size),
            )
        )
        if not rows:
            heartbeat.stop()
            raise ValueError("corpus changed while embeddings were being built")
        try:
            vectors = embed([row[4] for row in rows])
        except Exception as error:
            code = (
                error.code
                if isinstance(error, ModelUnavailable)
                else "semantic_embedding_failed"
            )
            phase = (
                error.phase if isinstance(error, ModelUnavailable) else "semantic_embed"
            )
            connection.execute(
                """
                UPDATE cc_search_chats.semantic_revision
                SET status = 'failed',
                    completed_at = now(), phase = %s, heartbeat_at = now(),
                    completed_units = %s,
                    failure = jsonb_build_object(
                        'completed', %s::bigint, 'total', %s::bigint,
                        'provider', %s::text, 'session_id', %s::text,
                        'logical_message_id', %s::text, 'error', %s::text,
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
                    str(error),
                    code,
                    phase,
                    semantic_revision,
                ),
            )
            heartbeat.stop()
            if isinstance(error, ModelUnavailable):
                raise
            raise RuntimeError(
                f"semantic embedding failed after {completed}/{total} passages "
                f"at {rows[0][0]}:{rows[0][1]}:{rows[0][2]}"
            ) from error
        if len(vectors) != len(rows):
            heartbeat.stop()
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
            connection.cursor().executemany(
                """
                INSERT INTO cc_search_chats.message_embedding_current (
                    provider, source_session_id, logical_message_id,
                    content_class, profile_id, input_digest
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    provider, source_session_id, logical_message_id,
                    content_class, profile_id
                ) DO UPDATE SET input_digest = EXCLUDED.input_digest
                WHERE message_embedding_current.input_digest
                      IS DISTINCT FROM EXCLUDED.input_digest
                """,
                [(*row[:4], _PROFILE_ID, row[5]) for row in rows],
            )
        completed = _mapped_count(connection)
        connection.execute(
            """
            UPDATE cc_search_chats.semantic_revision
            SET completed_units = %s, heartbeat_at = now()
            WHERE semantic_revision_id = %s AND status = 'building'
            """,
            (completed, semantic_revision),
        )
        if progress is not None:
            progress(completed, total)

    heartbeat.raise_if_failed()
    connection.execute(
        """
        UPDATE cc_search_chats.semantic_revision
        SET phase = 'semantic_commit', heartbeat_at = now()
        WHERE semantic_revision_id = %s AND status = 'building'
        """,
        (semantic_revision,),
    )
    try:
        with connection.transaction():
            _publish_semantic_revision(
                connection,
                semantic_revision=semantic_revision,
                corpus_revision=corpus_revision,
                embedded_count=completed,
            )
    finally:
        heartbeat.stop()
    return completed


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
) -> tuple[SearchHit, ...]:
    """Return exact cosine-ranked messages from the current semantic state."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be positive")
    vector = _vector(embedding)
    state = next(
        connection.execute(
            """
            SELECT revision.corpus_revision_id = corpus.current_revision_id
                   AND revision.status = 'complete'
                   AND revision.profile_id = %s
            FROM cc_search_chats.semantic_state AS state
            JOIN cc_search_chats.semantic_revision AS revision
              ON revision.semantic_revision_id = state.current_semantic_revision_id
            CROSS JOIN cc_search_chats.corpus_state AS corpus
            WHERE state.singleton AND corpus.singleton
            """,
            (_PROFILE_ID,),
        ),
        None,
    )
    if state is None or state[0] is not True:
        raise ValueError("semantic revision is unavailable or stale")
    filters = [
        "message.session_kind IN ('primary', 'agent', 'unknown')"
        if include_agents
        else "message.session_kind = 'primary'"
    ]
    params: list[object] = [vector, _PROFILE_ID]
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
    params.extend((vector, limit))
    where = f" AND {' AND '.join(filters)}" if filters else ""
    rows = connection.execute(
        f"""
        SELECT message.provider, message.source_session_id,
               message.logical_message_id, message.canonical_locator,
               message.timestamp_text, message.role, message.session_kind,
               message.conversation_epoch, message.content_class,
               message.prose_content, message.repository, message.cwd,
               1 - (value.embedding <=> %s::vector) AS score
        FROM cc_search_chats.message_embedding_current AS mapping
        JOIN cc_search_chats.embedding_value AS value
          ON (value.profile_id, value.input_digest) =
             (mapping.profile_id, mapping.input_digest)
        JOIN cc_search_chats.message_current AS message
          USING (provider, source_session_id, logical_message_id, content_class)
        WHERE mapping.profile_id = %s {where}
        ORDER BY value.embedding <=> %s::vector,
                 message.provider, message.source_session_id,
                 message.logical_message_id
        LIMIT %s
        """.encode(),
        params,
    )
    return tuple(SearchHit(*row) for row in rows)


def _ranked_component_depth(limit: int) -> int:
    """Validate a public ranked limit and return its bounded component depth."""
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
    """Fuse two already-ranked components with exact RRF arithmetic."""
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
    """Fetch bounded lexical/vector components and fuse them with exact RRF."""
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
