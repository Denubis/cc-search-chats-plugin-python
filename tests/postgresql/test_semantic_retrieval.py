"""Exact, bounded semantic retrieval behavior on PostgreSQL."""

import hashlib
import math
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql

from cc_search_chats.storage.postgresql import guardrails as postgresql_guardrails
from cc_search_chats.storage.postgresql import migrate, search_messages, semantic_search
from cc_search_chats.storage.postgresql import semantic as semantic_storage

pytestmark = pytest.mark.postgresql
_PROFILE_ID = "nemotron-3-embed-8b-bf16:chunks-v1"
_CHUNKER_ID = "nemotron-token-chunks-768-1024-96:v1"
_QUERY_VECTOR = (1.0, *([0.0] * 1023))


@dataclass(frozen=True, slots=True)
class _ChunkFixture:
    ordinal: int
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _MessageFixture:
    provider: str
    session_id: str
    message_id: str
    role: str
    session_kind: str
    repository: str | None
    cwd: str | None
    chunks: tuple[_ChunkFixture, ...]
    prose: str | None = None

    @property
    def locator(self) -> str:
        return f"fixture:{self.provider}:{self.session_id}:{self.message_id}"


def _unit_vector(score: float) -> tuple[float, ...]:
    return (score, math.sqrt(1.0 - score * score), *([0.0] * 1022))


def _vector_text(vector: tuple[float, ...]) -> str:
    return "[" + ",".join(str(component) for component in vector) + "]"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _seed_semantic_corpus(
    connection: psycopg.Connection,
    messages: tuple[_MessageFixture, ...],
) -> None:
    migrate(connection)
    generation = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.corpus_generation (
                completed_at, status, message_count, alias_count
            ) VALUES (now(), 'complete', %s, 0)
            RETURNING corpus_generation
            """,
            (len(messages),),
        )
    )[0]
    message_rows = []
    value_rows = []
    chunk_rows = []
    for message in messages:
        source_digest = _digest(f"source:{message.locator}")
        message_rows.append(
            (
                message.provider,
                message.session_id,
                message.message_id,
                message.locator,
                "2026-09-03T00:00:00Z",
                message.role,
                message.session_kind,
                "prose",
                message.prose or f"text for {message.locator}",
                message.repository,
                message.cwd,
                source_digest,
            )
        )
        for chunk in message.chunks:
            input_digest = _digest(f"chunk:{message.locator}:{chunk.ordinal}")
            value_rows.append((_PROFILE_ID, input_digest, _vector_text(chunk.vector)))
            chunk_rows.append(
                (
                    message.provider,
                    message.session_id,
                    message.message_id,
                    _PROFILE_ID,
                    chunk.ordinal,
                    _CHUNKER_ID,
                    source_digest,
                    f"passage for {message.locator} chunk {chunk.ordinal}",
                    input_digest,
                )
            )
    connection.cursor().executemany(
        """
        INSERT INTO cc_search_chats.message_current (
            provider, source_session_id, logical_message_id,
            canonical_locator, timestamp_text, role, session_kind,
            conversation_epoch, content_class, prose_content, submitted_by,
            repository, cwd, embedding_input_digest
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 'unknown', %s, %s, %s
        )
        """,
        message_rows,
    )
    connection.cursor().executemany(
        """
        INSERT INTO cc_search_chats.embedding_value (
            profile_id, input_digest, embedding
        ) VALUES (%s, %s, %s::vector)
        """,
        value_rows,
    )
    connection.cursor().executemany(
        """
        INSERT INTO cc_search_chats.semantic_chunk_current (
            provider, source_session_id, logical_message_id, content_class,
            profile_id, chunk_ordinal, chunker_id,
            token_start, token_end, char_start, char_end,
            source_text_digest, passage_text, input_digest
        ) VALUES (
            %s, %s, %s, 'prose', %s, %s, %s,
            0, 1, 0, 1, %s, %s, %s
        )
        """,
        chunk_rows,
    )
    chunk_count = sum(len(message.chunks) for message in messages)
    semantic_build = next(
        connection.execute(
            """
            INSERT INTO cc_search_chats.semantic_build (
                corpus_generation, profile_id, completed_at, status,
                embedded_count, completed_units, total_units
            ) VALUES (%s, %s, now(), 'complete', %s, %s, %s)
            RETURNING semantic_build
            """,
            (generation, _PROFILE_ID, chunk_count, chunk_count, chunk_count),
        )
    )[0]
    connection.execute(
        """
        UPDATE cc_search_chats.corpus_generation
        SET semantic_build = %s
        WHERE corpus_generation = %s
        """,
        (semantic_build, generation),
    )
    connection.execute(
        """
        UPDATE cc_search_chats.corpus_state
        SET current_corpus_generation = %s
        WHERE singleton
        """,
        (generation,),
    )


def _parity_messages() -> tuple[_MessageFixture, ...]:
    return (
        _MessageFixture(
            "claude",
            "claude-primary",
            "user-best-second",
            "user",
            "primary",
            "/work/alpha",
            None,
            (
                _ChunkFixture(0, _unit_vector(0.60)),
                _ChunkFixture(1, _unit_vector(0.95)),
            ),
        ),
        _MessageFixture(
            "claude",
            "claude-primary",
            "assistant-beta",
            "assistant",
            "primary",
            "/work/beta",
            None,
            (_ChunkFixture(0, _unit_vector(0.80)),),
        ),
        _MessageFixture(
            "codex",
            "codex-primary",
            "assistant-alpha",
            "assistant",
            "primary",
            None,
            "/work/alpha",
            (_ChunkFixture(0, _unit_vector(0.90)),),
        ),
        _MessageFixture(
            "codex",
            "codex-primary",
            "user-alpha",
            "user",
            "primary",
            "/work/alpha",
            None,
            (_ChunkFixture(0, _unit_vector(0.70)),),
        ),
        _MessageFixture(
            "codex",
            "codex-agent",
            "assistant-agent",
            "assistant",
            "agent",
            "/work/alpha",
            None,
            (_ChunkFixture(0, _unit_vector(0.99)),),
        ),
    )


def _inner_product(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def _expected_ranking(
    messages: tuple[_MessageFixture, ...],
    *,
    include_agents: bool = False,
    provider: str | None = None,
    role: str | None = None,
    project: str | None = None,
) -> tuple[tuple[str, int, float], ...]:
    ranked = []
    for message in messages:
        if not include_agents and message.session_kind != "primary":
            continue
        if provider is not None and message.provider != provider:
            continue
        if role is not None and message.role != role:
            continue
        if project is not None and (message.repository or message.cwd) != project:
            continue
        best = max(
            message.chunks,
            key=lambda chunk: (
                _inner_product(chunk.vector, _QUERY_VECTOR),
                -chunk.ordinal,
            ),
        )
        ranked.append(
            (
                message,
                best.ordinal,
                _inner_product(best.vector, _QUERY_VECTOR),
            )
        )
    ranked.sort(
        key=lambda value: (
            -value[2],
            value[0].provider,
            value[0].session_id,
            value[0].message_id,
        )
    )
    return tuple(
        (message.locator, ordinal, score) for message, ordinal, score in ranked
    )


def _assert_parity(
    connection: psycopg.Connection,
    messages: tuple[_MessageFixture, ...],
    *,
    include_agents: bool = False,
    provider: str | None = None,
    role: str | None = None,
    project: str | None = None,
) -> None:
    expected = _expected_ranking(
        messages,
        include_agents=include_agents,
        provider=provider,
        role=role,
        project=project,
    )
    hits = semantic_search(
        connection,
        _QUERY_VECTOR,
        limit=len(messages),
        include_agents=include_agents,
        provider=provider,
        role=role,
        project=project,
    )

    assert tuple(
        (hit.canonical_locator, hit.semantic_chunk_ordinal) for hit in hits
    ) == tuple((locator, ordinal) for locator, ordinal, _score in expected)
    assert tuple(hit.rank for hit in hits) == pytest.approx(
        tuple(score for _locator, _ordinal, score in expected)
    )


def test_semantic_search_matches_python_best_chunk_ranking_and_filters(
    postgres_connection: psycopg.Connection,
) -> None:
    messages = _parity_messages()
    _seed_semantic_corpus(postgres_connection, messages)

    _assert_parity(postgres_connection, messages)
    _assert_parity(postgres_connection, messages, include_agents=True)
    _assert_parity(postgres_connection, messages, provider="codex")
    _assert_parity(postgres_connection, messages, role="user")
    _assert_parity(postgres_connection, messages, project="/work/alpha")


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


# Negative control copied from the pre-Prompt-15 implementation. Production code
# must never call this full-set query: its window sort carries message prose.
_OLD_FULL_SET_RANKED_CHUNK_STATEMENT = sql.SQL(
    """
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
          AND message.session_kind = 'primary'
    )
    SELECT provider, source_session_id, logical_message_id,
           canonical_locator, timestamp_text, role, session_kind,
           conversation_epoch, content_class, prose_content,
           repository, cwd, score, chunk_ordinal
    FROM ranked_chunk
    WHERE chunk_rank = 1
    ORDER BY score DESC, provider, source_session_id, logical_message_id
    LIMIT %s
    """
)


def _pseudo_random_score(seed: int) -> float:
    """Return a stable LCG-derived score in the open unit interval."""
    return ((seed * 48_271 + 1) % 1_000_003 + 1) / 1_000_005


def _spill_messages(count: int) -> tuple[_MessageFixture, ...]:
    prose_body = "0123456789abcdef" * 64
    return tuple(
        _MessageFixture(
            "claude",
            "spill-session",
            f"spill-message-{index:04d}",
            "assistant",
            "primary",
            "/work/spill",
            None,
            tuple(
                _ChunkFixture(
                    ordinal,
                    _unit_vector(_pseudo_random_score(index + ordinal * count)),
                )
                for ordinal in range(2 if index < 8 else 1)
            ),
            f"spill prose {index:04d} {prose_body}",
        )
        for index in range(count)
    )


def _execute_old_full_set_search(
    connection: psycopg.Connection,
    vector: str,
) -> None:
    with connection.transaction():
        connection.execute("SELECT set_config('work_mem', '64kB', true)")
        connection.execute("SELECT set_config('temp_file_limit', '0', true)")
        tuple(
            connection.execute(
                _OLD_FULL_SET_RANKED_CHUNK_STATEMENT,
                (vector, vector, _PROFILE_ID, _CHUNKER_ID, 20),
            )
        )


def test_candidate_first_semantic_search_avoids_full_set_temp_spill(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _spill_messages(3000)
    _seed_semantic_corpus(postgres_connection, messages)
    vector = _vector_text(_QUERY_VECTOR)
    monkeypatch.setattr(postgresql_guardrails, "_TEMP_FILE_LIMIT", "0")

    with pytest.raises(psycopg.errors.ConfigurationLimitExceeded) as failure:
        _execute_old_full_set_search(postgres_connection, vector)
    assert "temp_file_limit" in str(failure.value)

    with postgres_connection.transaction():
        postgres_connection.execute("SELECT set_config('work_mem', '64kB', true)")
        postgres_connection.execute("SELECT set_config('temp_file_limit', '0', true)")

        hits = semantic_search(postgres_connection, _QUERY_VECTOR, limit=20)

        expected = _expected_ranking(messages)[:20]
        assert tuple(
            (hit.canonical_locator, hit.semantic_chunk_ordinal) for hit in hits
        ) == tuple((locator, ordinal) for locator, ordinal, _score in expected)
        assert tuple(hit.rank for hit in hits) == pytest.approx(
            tuple(score for _locator, _ordinal, score in expected)
        )
        assert next(postgres_connection.execute("SHOW work_mem"))[0] == "256MB"
        assert next(postgres_connection.execute("SHOW temp_file_limit"))[0] == "0"

        where = sql.SQL("message.session_kind = 'primary'")
        statement = sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {}").format(
            semantic_storage._semantic_search_statement(where)
        )
        explained = next(
            postgres_connection.execute(
                statement,
                (vector, _PROFILE_ID, _CHUNKER_ID, vector, 2000, 20),
            )
        )[0]
        nodes = tuple(_plan_nodes(explained[0]["Plan"]))

        assert all(node.get("Temp Written Blocks", 0) == 0 for node in nodes)
        assert all(
            node.get("Sort Method") not in {"external merge", "external sort"}
            for node in nodes
        )


def test_semantic_search_plan_limits_candidates_before_final_sort(
    postgres_connection: psycopg.Connection,
) -> None:
    messages = _parity_messages()
    _seed_semantic_corpus(postgres_connection, messages)
    where = sql.SQL("message.session_kind = 'primary'")
    statement = sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) {}").format(
        semantic_storage._semantic_search_statement(where)
    )
    explained = next(
        postgres_connection.execute(
            statement,
            (
                _vector_text(_QUERY_VECTOR),
                _PROFILE_ID,
                _CHUNKER_ID,
                _vector_text(_QUERY_VECTOR),
                2000,
                20,
            ),
        )
    )[0]
    plan = explained[0]["Plan"]
    nodes = tuple(_plan_nodes(plan))
    cte_scans = tuple(node for node in nodes if node["Node Type"] == "CTE Scan")
    candidate_scans = tuple(
        node for node in cte_scans if node.get("CTE Name") == "candidate"
    )

    assert not any(node["Node Type"] == "WindowAgg" for node in nodes)
    assert 0 < len(candidate_scans) <= 2
    cte_scan_loops = tuple(
        (node.get("CTE Name"), node["Actual Loops"]) for node in cte_scans
    )
    assert all(loops == 1 for _name, loops in cte_scan_loops), cte_scan_loops
    final_sort = next(
        node
        for node in nodes
        if node["Node Type"] == "Sort"
        and any("score DESC" in key for key in node.get("Sort Key", ()))
    )
    candidate_plan = next(
        node for node in nodes if node.get("Subplan Name") == "CTE candidate"
    )
    assert candidate_plan["Node Type"] == "Limit"
    assert any(
        node["Node Type"] == "CTE Scan" and node.get("CTE Name") == "candidate"
        for node in _plan_nodes(final_sort)
    )
    candidate_count_plan = next(
        node
        for node in nodes
        if node["Node Type"] == "Aggregate"
        and any(
            child["Node Type"] == "CTE Scan" and child.get("CTE Name") == "candidate"
            for child in node.get("Plans", ())
        )
    )
    assert candidate_count_plan["Parent Relationship"] == "InitPlan"
    assert candidate_count_plan["Actual Loops"] == 1


def test_semantic_work_mem_is_transaction_local_and_literal_does_not_set_it(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _parity_messages()
    _seed_semantic_corpus(postgres_connection, messages)
    default_work_mem = next(postgres_connection.execute("SHOW work_mem"))[0]
    original_execute = psycopg.Connection.execute
    retrieval_work_mem: list[str] = []
    statements: list[str] = []

    def recording_execute(self, query, *args, **kwargs):
        rendered = (
            query.as_string(self) if isinstance(query, sql.Composable) else str(query)
        )
        statements.append(rendered)
        if "WITH ranked_chunk AS" in rendered or "WITH candidate AS" in rendered:
            retrieval_work_mem.append(next(original_execute(self, "SHOW work_mem"))[0])
        return original_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", recording_execute)

    semantic_search(postgres_connection, _QUERY_VECTOR, limit=2)

    assert retrieval_work_mem == ["256MB"]
    assert next(postgres_connection.execute("SHOW work_mem"))[0] == default_work_mem

    statements.clear()
    assert search_messages(postgres_connection, "text")
    assert any("websearch_to_tsquery" in statement for statement in statements)
    assert not any("work_mem" in statement for statement in statements)


def test_semantic_search_retries_when_one_message_starves_initial_candidates(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic_storage, "_SEMANTIC_CANDIDATE_MINIMUM", 3)
    monkeypatch.setattr(semantic_storage, "_SEMANTIC_CANDIDATE_MULTIPLIER", 1)
    messages = (
        _MessageFixture(
            "claude",
            "dominant-session",
            "dominant-message",
            "assistant",
            "primary",
            "/work/alpha",
            None,
            tuple(
                _ChunkFixture(ordinal, _unit_vector(score))
                for ordinal, score in enumerate((0.99, 0.98, 0.97, 0.96))
            ),
        ),
        _MessageFixture(
            "codex",
            "second-session",
            "second-message",
            "assistant",
            "primary",
            "/work/alpha",
            None,
            (_ChunkFixture(0, _unit_vector(0.80)),),
        ),
        _MessageFixture(
            "codex",
            "third-session",
            "third-message",
            "user",
            "primary",
            "/work/alpha",
            None,
            (_ChunkFixture(0, _unit_vector(0.70)),),
        ),
    )
    _seed_semantic_corpus(postgres_connection, messages)

    hits = semantic_search(postgres_connection, _QUERY_VECTOR, limit=3)

    assert tuple(hit.canonical_locator for hit in hits) == tuple(
        message.locator for message in messages
    )


def test_semantic_search_short_filtered_result_uses_one_candidate_statement(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _parity_messages()
    _seed_semantic_corpus(postgres_connection, messages)
    original_execute = psycopg.Connection.execute
    statements: list[str] = []

    def recording_execute(self, query, *args, **kwargs):
        rendered = (
            query.as_string(self) if isinstance(query, sql.Composable) else str(query)
        )
        statements.append(rendered)
        return original_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", recording_execute)

    hits = semantic_search(
        postgres_connection,
        _QUERY_VECTOR,
        limit=5,
        project="/work/beta",
    )

    assert tuple(hit.canonical_locator for hit in hits) == (messages[1].locator,)
    assert sum("WITH candidate AS" in statement for statement in statements) == 1
    separate_limit_checks = tuple(
        statement
        for statement in statements
        if "SELECT EXISTS (" in statement and "OFFSET %s" in statement
    )
    assert not separate_limit_checks, separate_limit_checks


def test_semantic_search_preserves_final_key_order_at_tied_candidate_boundary(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic_storage, "_SEMANTIC_CANDIDATE_MINIMUM", 2)
    monkeypatch.setattr(semantic_storage, "_SEMANTIC_CANDIDATE_MULTIPLIER", 1)
    best = _MessageFixture(
        "codex",
        "best-session",
        "best-message",
        "assistant",
        "primary",
        None,
        "/work/alpha",
        (_ChunkFixture(0, _unit_vector(0.95)),),
    )
    tied_winner = _MessageFixture(
        "claude",
        "later-inserted-session",
        "tied-winner",
        "assistant",
        "primary",
        "/work/alpha",
        None,
        (_ChunkFixture(5, _unit_vector(0.80)),),
    )
    tied_loser = _MessageFixture(
        "codex",
        "earlier-inserted-session",
        "tied-loser",
        "assistant",
        "primary",
        "/work/alpha",
        None,
        (_ChunkFixture(0, _unit_vector(0.80)),),
    )
    messages = (best, tied_loser, tied_winner)
    _seed_semantic_corpus(postgres_connection, messages)

    hits = semantic_search(postgres_connection, _QUERY_VECTOR, limit=2)

    assert tuple(hit.canonical_locator for hit in hits) == (
        best.locator,
        tied_winner.locator,
    )
    assert tuple(hit.rank for hit in hits) == pytest.approx((0.95, 0.80))
