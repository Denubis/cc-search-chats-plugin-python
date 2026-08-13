"""Exact pgvector retrieval and reciprocal-rank fusion."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import psycopg

from cc_search_chats.storage.postgresql.index import SearchHit, search_messages

_DIMENSIONS = 1024
_EMBEDDABLE_PROSE = "content_class = 'prose' AND prose_content ~ '[^[:space:]]'"


@dataclass(frozen=True, slots=True)
class HybridHit:
    message: SearchHit
    score: float
    literal_rank: int | None
    semantic_rank: int | None


def _vector(value: Sequence[float]) -> str:
    if len(value) != _DIMENSIONS:
        raise ValueError(f"embedding must contain {_DIMENSIONS} dimensions")
    return "[" + ",".join(str(float(component)) for component in value) + "]"


def replace_embeddings(
    connection: psycopg.Connection,
    embeddings: Mapping[str, Sequence[float]],
) -> int:
    """Replace vectors for canonical prose locators in the selected revision."""
    with connection.transaction():
        revision = next(
            connection.execute(
                "SELECT current_revision_id FROM cc_search_chats.corpus_state "
                "WHERE singleton"
            )
        )[0]
        if revision is None:
            raise ValueError("no selected corpus revision")
        semantic_revision = next(
            connection.execute(
                "INSERT INTO cc_search_chats.semantic_revision (corpus_revision_id) "
                "VALUES (%s) RETURNING semantic_revision_id",
                (revision,),
            )
        )[0]
        inserted = 0
        for locator, embedding in embeddings.items():
            inserted += connection.execute(
                """
                INSERT INTO cc_search_chats.message_embedding (
                    semantic_revision_id, revision_id, provider, source_session_id,
                    logical_message_id, content_class, embedding
                )
                SELECT %s, revision_id, provider, source_session_id,
                       logical_message_id, content_class, %s::vector
                FROM cc_search_chats.message
                WHERE revision_id = %s AND canonical_locator = %s
                  AND content_class = 'prose'
                  AND prose_content ~ '[^[:space:]]'
                """,
                (semantic_revision, _vector(embedding), revision, locator),
            ).rowcount
        expected = next(
            connection.execute(
                "SELECT count(*) FROM cc_search_chats.message "
                f"WHERE revision_id = %s AND {_EMBEDDABLE_PROSE}",
                (revision,),
            )
        )[0]
        if inserted != expected:
            raise ValueError(f"semantic revision is incomplete: {inserted}/{expected}")
        connection.execute(
            "UPDATE cc_search_chats.semantic_state "
            "SET current_semantic_revision_id = %s WHERE singleton",
            (semantic_revision,),
        )
    return inserted


def index_embeddings(
    connection: psycopg.Connection,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    batch_size: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Run one resumable semantic worker per database."""
    locked = next(
        connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            ("cc_search_chats.semantic_index",),
        )
    )[0]
    if not locked:
        raise RuntimeError("semantic indexing is already running")
    try:
        return _index_embeddings(
            connection, embed, batch_size=batch_size, progress=progress
        )
    finally:
        connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            ("cc_search_chats.semantic_index",),
        )


def _index_embeddings(
    connection: psycopg.Connection,
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    batch_size: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Build and atomically select a complete semantic revision in batches."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    revision = next(
        connection.execute(
            "SELECT current_revision_id FROM cc_search_chats.corpus_state WHERE singleton"
        )
    )[0]
    if revision is None:
        raise ValueError("no selected corpus revision")
    total = next(
        connection.execute(
            "SELECT count(*) FROM cc_search_chats.message "
            f"WHERE revision_id = %s AND {_EMBEDDABLE_PROSE}",
            (revision,),
        )
    )[0]
    selected = next(
        connection.execute(
            """
            SELECT sr.semantic_revision_id
            FROM cc_search_chats.semantic_state AS ss
            JOIN cc_search_chats.semantic_revision AS sr
              ON sr.semantic_revision_id = ss.current_semantic_revision_id
            WHERE ss.singleton AND sr.corpus_revision_id = %s
            """,
            (revision,),
        ),
        None,
    )
    if selected is not None:
        return total
    partial = next(
        connection.execute(
            """
            SELECT sr.semantic_revision_id, count(e.semantic_revision_id)
            FROM cc_search_chats.semantic_revision AS sr
            LEFT JOIN cc_search_chats.message_embedding AS e
              ON e.semantic_revision_id = sr.semantic_revision_id
            LEFT JOIN cc_search_chats.semantic_state AS ss
              ON ss.current_semantic_revision_id = sr.semantic_revision_id
            WHERE sr.corpus_revision_id = %s
              AND ss.current_semantic_revision_id IS NULL
            GROUP BY sr.semantic_revision_id
            ORDER BY count(e.semantic_revision_id) DESC, sr.semantic_revision_id DESC
            LIMIT 1
            """,
            (revision,),
        ),
        None,
    )
    if partial is None:
        semantic_revision = next(
            connection.execute(
                "INSERT INTO cc_search_chats.semantic_revision (corpus_revision_id) "
                "VALUES (%s) RETURNING semantic_revision_id",
                (revision,),
            )
        )[0]
        connection.execute(
            """
            INSERT INTO cc_search_chats.message_embedding (
                semantic_revision_id, revision_id, provider, source_session_id,
                logical_message_id, content_class, embedding
            )
            SELECT %s, %s, m.provider, m.source_session_id,
                   m.logical_message_id, m.content_class, old.embedding
            FROM cc_search_chats.message AS m
            JOIN cc_search_chats.semantic_state AS ss ON ss.singleton
            JOIN cc_search_chats.message_embedding AS old
              ON old.semantic_revision_id = ss.current_semantic_revision_id
             AND old.provider = m.provider
             AND old.source_session_id = m.source_session_id
             AND old.logical_message_id = m.logical_message_id
             AND old.content_class = m.content_class
            JOIN cc_search_chats.message AS previous
              ON previous.revision_id = old.revision_id
             AND previous.provider = old.provider
             AND previous.source_session_id = old.source_session_id
             AND previous.logical_message_id = old.logical_message_id
             AND previous.content_class = old.content_class
             AND previous.prose_content = m.prose_content
            WHERE m.revision_id = %s AND m.content_class = 'prose'
              AND m.prose_content ~ '[^[:space:]]'
            """,
            (semantic_revision, revision, revision),
        )
        completed = next(
            connection.execute(
                "SELECT count(*) FROM cc_search_chats.message_embedding "
                "WHERE semantic_revision_id = %s",
                (semantic_revision,),
            )
        )[0]
    else:
        semantic_revision, completed = partial
    if progress is not None and completed:
        progress(completed, total)
    while completed < total:
        rows = tuple(
            connection.execute(
                """
                SELECT provider, source_session_id, logical_message_id,
                       content_class, prose_content
                FROM cc_search_chats.message AS m
                WHERE revision_id = %s
                  AND content_class = 'prose'
                  AND prose_content ~ '[^[:space:]]'
                  AND NOT EXISTS (
                      SELECT 1 FROM cc_search_chats.message_embedding AS e
                      WHERE e.semantic_revision_id = %s
                        AND e.revision_id = m.revision_id
                        AND e.provider = m.provider
                        AND e.source_session_id = m.source_session_id
                        AND e.logical_message_id = m.logical_message_id
                        AND e.content_class = m.content_class
                  )
                ORDER BY provider, source_session_id, logical_message_id, content_class
                LIMIT %s
                """,
                (revision, semantic_revision, batch_size),
            )
        )
        if not rows:
            raise ValueError("corpus changed while embeddings were being built")
        try:
            vectors = embed([row[4] for row in rows])
        except Exception as error:
            raise RuntimeError(
                f"semantic embedding failed after {completed}/{total} passages "
                f"at {rows[0][0]}:{rows[0][1]}:{rows[0][2]}"
            ) from error
        if len(vectors) != len(rows):
            raise ValueError("embedding model returned the wrong batch size")
        with connection.transaction():
            connection.cursor().executemany(
                """
                    INSERT INTO cc_search_chats.message_embedding (
                        semantic_revision_id, revision_id, provider,
                        source_session_id, logical_message_id, content_class,
                        embedding
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                    """,
                [
                    (semantic_revision, revision, *row[:4], _vector(vector))
                    for row, vector in zip(rows, vectors, strict=True)
                ],
            )
        completed += len(rows)
        if progress is not None:
            progress(completed, total)
    with connection.transaction():
        current = next(
            connection.execute(
                "SELECT current_revision_id FROM cc_search_chats.corpus_state "
                "WHERE singleton"
            )
        )[0]
        if current != revision:
            raise ValueError("corpus changed while embeddings were being built")
        connection.execute(
            "UPDATE cc_search_chats.semantic_state "
            "SET current_semantic_revision_id = %s WHERE singleton",
            (semantic_revision,),
        )
    return completed


def semantic_search(
    connection: psycopg.Connection,
    embedding: Sequence[float],
    *,
    limit: int = 20,
    provider: str | None = None,
) -> tuple[SearchHit, ...]:
    """Return exact cosine-ranked messages from the selected revision."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    vector = _vector(embedding)
    state = next(
        connection.execute(
            """
            SELECT sr.corpus_revision_id = cs.current_revision_id
            FROM cc_search_chats.semantic_state AS ss
            JOIN cc_search_chats.semantic_revision AS sr
              ON sr.semantic_revision_id = ss.current_semantic_revision_id
            CROSS JOIN cc_search_chats.corpus_state AS cs
            WHERE ss.singleton AND cs.singleton
            """
        ),
        None,
    )
    if state is None or state[0] is not True:
        raise ValueError("semantic revision is unavailable or stale")
    rows = connection.execute(
        """
        SELECT m.provider, m.source_session_id, m.logical_message_id,
               m.canonical_locator, m.timestamp_text, m.role, m.session_kind,
               m.prose_content, m.repository, m.cwd,
               1 - (e.embedding <=> %s::vector) AS score
        FROM cc_search_chats.message_embedding AS e
        JOIN cc_search_chats.message AS m
          USING (revision_id, provider, source_session_id,
                 logical_message_id, content_class)
        JOIN cc_search_chats.corpus_state AS s
          ON s.current_revision_id = e.revision_id
        JOIN cc_search_chats.semantic_state AS ss
          ON ss.current_semantic_revision_id = e.semantic_revision_id
        WHERE (%s::text IS NULL OR m.provider = %s)
        ORDER BY e.embedding <=> %s::vector,
                 m.provider, m.source_session_id, m.logical_message_id
        LIMIT %s
        """,
        (vector, provider, provider, vector, limit),
    )
    return tuple(SearchHit(*row) for row in rows)


def hybrid_search(
    connection: psycopg.Connection,
    query: str,
    embedding: Sequence[float],
    *,
    limit: int = 20,
    provider: str | None = None,
    rank_constant: int = 60,
) -> tuple[HybridHit, ...]:
    """Fuse bounded literal and exact-vector ranks with RRF."""
    depth = max(limit * 4, limit)
    literal = search_messages(connection, query, limit=depth, provider=provider)
    semantic = semantic_search(connection, embedding, limit=depth, provider=provider)
    literal_ranks = {
        value.canonical_locator: rank for rank, value in enumerate(literal, 1)
    }
    semantic_ranks = {
        value.canonical_locator: rank for rank, value in enumerate(semantic, 1)
    }
    messages = {value.canonical_locator: value for value in (*literal, *semantic)}
    fused = [
        HybridHit(
            message=message,
            score=sum(
                1 / (rank_constant + rank)
                for rank in (literal_ranks.get(locator), semantic_ranks.get(locator))
                if rank is not None
            ),
            literal_rank=literal_ranks.get(locator),
            semantic_rank=semantic_ranks.get(locator),
        )
        for locator, message in messages.items()
    ]
    return tuple(
        sorted(
            fused, key=lambda value: (-value.score, value.message.canonical_locator)
        )[:limit]
    )
