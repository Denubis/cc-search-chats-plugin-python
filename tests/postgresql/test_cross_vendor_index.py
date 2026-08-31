"""One end-to-end PostgreSQL corpus behavior for both native providers."""

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import psycopg
import pytest

from cc_search_chats.providers.claude import ClaudeSessionContext, parse_claude_session
from cc_search_chats.providers.codex import CodexSessionContext, parse_codex_session
from cc_search_chats.providers.source_discovery import read_bounded_jsonl
from cc_search_chats.semantic import ModelUnavailable, SemanticChunk
from cc_search_chats.storage.postgresql import (
    context_messages,
    exhaustive_search_page,
    extract_session,
    hybrid_search,
    index_embeddings,
    list_sessions,
    migrate,
    replace_messages,
    resolve_message,
    search_messages,
    semantic_search,
)
from cc_search_chats.storage.postgresql import semantic as semantic_storage

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


def _read(name: str):
    path = FIXTURES / name
    return read_bounded_jsonl(
        path,
        source_file_relative=Path(name),
        target_size=path.stat().st_size,
    )


def _single_chunks(texts):
    return tuple((SemanticChunk(0, 0, 1, 0, len(text), text),) for text in texts)


def test_cross_vendor_messages_are_atomically_searchable(
    postgres_connection: psycopg.Connection,
) -> None:
    claude_read = _read("claude_primary.jsonl")
    claude = parse_claude_session(
        claude_read.envelopes,
        context=ClaudeSessionContext(
            source_session_id="claude-session-primary",
            repository="/synthetic/repository",
        ),
    )
    codex_read = _read("codex_modern_primary_145.jsonl")
    codex = parse_codex_session(
        codex_read.envelopes,
        context=CodexSessionContext(repository="/synthetic/repository"),
        source_diagnostics=codex_read.diagnostics,
    )

    migrate(postgres_connection)
    claude_messages = tuple(
        replace(message, text=message.text + " x" * 600_000)
        if message.content_class.value == "tool_output"
        else message
        for message in claude.messages
    )
    revision = replace_messages(
        postgres_connection, (*claude_messages, *claude_messages, *codex.messages)
    )

    assert revision > 0
    assert {
        hit.provider for hit in search_messages(postgres_connection, "visible")
    } == {
        "claude",
        "codex",
    }
    codex_hits = search_messages(postgres_connection, "modern assistant")
    assert len(codex_hits) == 1
    assert codex_hits[0].canonical_locator.startswith("ccchat:v1:codex:")
    assert len(search_messages(postgres_connection, "visible", limit=1)) == 1
    assert {
        hit.provider
        for hit in search_messages(postgres_connection, "visible", provider="codex")
    } == {"codex"}
    assert (
        search_messages(
            postgres_connection,
            "visible",
            role="assistant",
            project="/synthetic/repository",
            since="2027-01-01T00:00:00+00:00",
        )
        == ()
    )
    assert {
        (value.provider, value.source_session_id)
        for value in list_sessions(postgres_connection)
    } == {
        ("claude", "claude-session-primary"),
        ("codex", "codex-modern-primary"),
    }
    extracted = extract_session(
        postgres_connection, "codex-modern-primary", provider="codex"
    )
    assert {value.provider for value in extracted} == {"codex"}
    target = codex_hits[0].canonical_locator
    assert {
        value.logical_message_id
        for value in resolve_message(postgres_connection, target)
    } == {codex_hits[0].logical_message_id}
    assert any(
        value.logical_message_id == codex_hits[0].logical_message_id
        for value in context_messages(postgres_connection, target, depth=1)
    )

    claude_vector = [0.0] * 1024
    claude_vector[0] = 1.0
    codex_vector = [0.0] * 1024
    codex_vector[1] = 1.0
    other_vector = [0.0] * 1024
    other_vector[2] = 1.0
    prose = [
        *extract_session(
            postgres_connection, "claude-session-primary", provider="claude"
        ),
        *extracted,
    ]

    def embed_passages(texts):
        return [
            (
                codex_vector
                if text == "modern visible assistant"
                else claude_vector
                if "Claude" in text or "visible" in text.lower()
                else other_vector
            )
            for text in texts
        ]

    expected = sum(value.content_class == "prose" for value in prose)
    assert (
        index_embeddings(
            postgres_connection,
            embed_passages,
            chunker=_single_chunks,
            batch_size=2,
        )
        == expected
    )
    assert (
        semantic_search(postgres_connection, codex_vector, limit=1)[0].canonical_locator
        == target
    )
    hybrid = hybrid_search(
        postgres_connection, "modern assistant", codex_vector, limit=1
    )[0]
    assert hybrid.message.canonical_locator == target
    assert hybrid.literal_rank == hybrid.semantic_rank == 1


def test_semantic_chunks_are_profiled_reused_and_collapsed_to_one_message(
    postgres_connection: psycopg.Connection,
) -> None:
    codex_read = _read("codex_modern_primary_145.jsonl")
    codex = parse_codex_session(
        codex_read.envelopes,
        context=CodexSessionContext(repository="/synthetic/repository"),
        source_diagnostics=codex_read.diagnostics,
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, codex.messages)

    target_text = "modern visible assistant"

    def chunks(texts):
        values = []
        for text in texts:
            if text == target_text:
                values.append(
                    (
                        SemanticChunk(0, 0, 2, 0, 14, "modern visible"),
                        SemanticChunk(1, 1, 3, 7, len(text), "visible assistant"),
                    )
                )
            else:
                values.append((SemanticChunk(0, 0, 1, 0, len(text), text),))
        return tuple(values)

    target_vector = [0.0] * 1024
    target_vector[0] = 1.0
    other_vector = [0.0] * 1024
    other_vector[1] = 1.0
    embedded: list[str] = []

    def embed(texts):
        embedded.extend(texts)
        return [target_vector if "visible" in text else other_vector for text in texts]

    eligible_messages = sum(
        message.content_class.value == "prose" and bool(message.text.strip())
        for message in codex.messages
    )
    assert (
        index_embeddings(
            postgres_connection,
            embed,
            chunker=chunks,
        )
        == eligible_messages + 1
    )
    assert embedded.count("modern visible") == 1
    assert embedded.count("visible assistant") == 1

    profile = next(
        postgres_connection.execute(
            """
            SELECT model_id, model_revision, dimensions, passage_prefix,
                   query_prefix, pooling, normalized, attention_implementation,
                   chunker_id, target_content_tokens, max_tokens, overlap_tokens
            FROM cc_search_chats.embedding_profile
            WHERE profile_id = 'nemotron-3-embed-8b-bf16:chunks-v1'
            """
        )
    )
    assert profile == (
        "nvidia/Nemotron-3-Embed-8B-BF16",
        "c44c20ab3f6b430336706847a6372de4b2eb3dbd",
        1024,
        "passage: ",
        "query: ",
        "attention-mask-mean",
        True,
        "sdpa",
        "nemotron-token-chunks-768-1024-96:v1",
        768,
        1024,
        96,
    )
    digests = {
        digest
        for (digest,) in postgres_connection.execute(
            """
            SELECT input_digest
            FROM cc_search_chats.semantic_chunk_current
            WHERE passage_text IN ('modern visible', 'visible assistant')
            """
        )
    }
    assert digests == {
        hashlib.sha256(f"passage: {text}".encode()).hexdigest()
        for text in ("modern visible", "visible assistant")
    }
    assert (
        next(
            postgres_connection.execute(
                "SELECT to_regclass('cc_search_chats.message_embedding_current')"
            )
        )[0]
        is None
    )

    postgres_connection.execute(
        """
        UPDATE cc_search_chats.semantic_chunk_current
        SET chunker_id = 'stale-chunker:v0'
        WHERE passage_text = 'visible assistant'
        """
    )
    with pytest.raises(ValueError, match="semantic chunks are unavailable or stale"):
        semantic_search(postgres_connection, target_vector, limit=20)
    embedded.clear()
    assert (
        index_embeddings(postgres_connection, embed, chunker=chunks)
        == eligible_messages + 1
    )
    assert embedded == []
    assert set(
        postgres_connection.execute(
            "SELECT DISTINCT chunker_id FROM cc_search_chats.semantic_chunk_current"
        )
    ) == {("nemotron-token-chunks-768-1024-96:v1",)}

    hits = semantic_search(postgres_connection, target_vector, limit=20)
    target_hits = [hit for hit in hits if hit.text == target_text]
    assert len(target_hits) == 1
    assert target_hits[0].semantic_chunk_ordinal == 0

    embedded.clear()
    assert (
        index_embeddings(
            postgres_connection,
            embed,
            chunker=chunks,
        )
        == eligible_messages + 1
    )
    assert embedded == []


def test_semantic_worker_embeds_each_missing_digest_once(
    postgres_connection: psycopg.Connection,
) -> None:
    codex_read = _read("codex_modern_primary_145.jsonl")
    codex = parse_codex_session(
        codex_read.envelopes,
        context=CodexSessionContext(repository="/synthetic/repository"),
        source_diagnostics=codex_read.diagnostics,
    )
    prose = tuple(
        message
        for message in codex.messages
        if message.content_class.value == "prose" and message.text.strip()
    )
    assert len(prose) >= 2
    duplicate_ids = {message.identity.logical_message_id for message in prose[:2]}
    duplicate_text = "one reusable duplicate passage"
    messages = tuple(
        replace(message, text=duplicate_text)
        if message.identity.logical_message_id in duplicate_ids
        else message
        for message in codex.messages
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, messages)

    vector = [0.0] * 1024
    vector[0] = 1.0
    embedded: list[str] = []
    progress: list[tuple[int, int]] = []

    def embed(texts):
        embedded.extend(texts)
        return [vector for _ in texts]

    completed = index_embeddings(
        postgres_connection,
        embed,
        chunker=_single_chunks,
        batch_size=128,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert completed == len(prose)
    assert embedded.count(duplicate_text) == 1
    assert progress[0] == (0, completed)
    assert progress[-1] == (completed, completed)


def test_semantic_worker_materializes_missing_work_before_embedding(
    postgres_connection: psycopg.Connection,
) -> None:
    codex_read = _read("codex_modern_primary_145.jsonl")
    codex = parse_codex_session(
        codex_read.envelopes,
        context=CodexSessionContext(repository="/synthetic/repository"),
        source_diagnostics=codex_read.diagnostics,
    )
    prose_texts = {
        message.text
        for message in codex.messages
        if message.content_class.value == "prose" and message.text.strip()
    }
    prose_count = len(prose_texts)
    assert prose_count > 1
    migrate(postgres_connection)
    replace_messages(postgres_connection, codex.messages)

    vector = [0.0] * 1024
    vector[0] = 1.0
    queued_counts: list[int] = []
    prepared_batch_query: list[bool] = []

    def embed(texts):
        queued_counts.append(
            next(
                postgres_connection.execute(
                    "SELECT count(*) FROM pg_temp.semantic_embedding_queue"
                )
            )[0]
        )
        if not prepared_batch_query:
            prepared_batch_query.append(
                next(
                    postgres_connection.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_prepared_statements
                            WHERE statement LIKE '%WITH batch AS MATERIALIZED%'
                              AND statement LIKE '%semantic_embedding_queue%'
                        )
                        """
                    )
                )[0]
            )
        return [vector for _ in texts]

    completed = index_embeddings(
        postgres_connection,
        embed,
        chunker=_single_chunks,
        batch_size=1,
    )

    assert completed == prose_count
    assert queued_counts == list(range(prose_count, 0, -1))
    assert prepared_batch_query == [True]


def test_semantic_worker_reconciles_full_progress_only_at_publication(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_read = _read("codex_modern_primary_145.jsonl")
    codex = parse_codex_session(
        codex_read.envelopes,
        context=CodexSessionContext(repository="/synthetic/repository"),
        source_diagnostics=codex_read.diagnostics,
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, codex.messages)

    mapped_count = semantic_storage._mapped_count
    mapped_count_calls = 0

    def observed_mapped_count(connection: psycopg.Connection) -> int:
        nonlocal mapped_count_calls
        mapped_count_calls += 1
        return mapped_count(connection)

    monkeypatch.setattr(semantic_storage, "_mapped_count", observed_mapped_count)
    vector = [0.0] * 1024
    vector[0] = 1.0

    completed = index_embeddings(
        postgres_connection,
        lambda texts: [vector for _ in texts],
        chunker=_single_chunks,
        batch_size=1,
    )

    assert completed > 1
    assert mapped_count_calls == 1


def test_semantic_worker_throttles_progress_checkpoints_between_boundaries(
    postgres_connection: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_read = _read("codex_modern_primary_145.jsonl")
    codex = parse_codex_session(
        codex_read.envelopes,
        context=CodexSessionContext(repository="/synthetic/repository"),
        source_diagnostics=codex_read.diagnostics,
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, codex.messages)

    monkeypatch.setattr(semantic_storage, "monotonic", lambda: 0.0, raising=False)
    vector = [0.0] * 1024
    vector[0] = 1.0
    progress: list[tuple[int, int]] = []

    completed = index_embeddings(
        postgres_connection,
        lambda texts: [vector for _ in texts],
        chunker=_single_chunks,
        batch_size=1,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert completed > 1
    assert progress == [(0, completed), (completed, completed)]


def test_search_scope_defaults_to_primary_prose_and_requires_agent_tool_opt_in(
    postgres_connection: psycopg.Connection,
) -> None:
    primary_read = _read("claude_multiple_tools.jsonl")
    primary = parse_claude_session(
        primary_read.envelopes,
        context=ClaudeSessionContext(source_session_id="claude-multiple-tools"),
    )
    agent_path = FIXTURES / "claude_agent.jsonl"
    agent_read = read_bounded_jsonl(
        agent_path,
        source_file_relative=Path(
            "project/parent-session/subagents/claude-agent-session.jsonl"
        ),
        target_size=agent_path.stat().st_size,
    )
    agent = parse_claude_session(
        agent_read.envelopes,
        context=ClaudeSessionContext(source_session_id="claude-agent-session"),
    )
    unknown_read = _read("claude_primary.jsonl")
    unknown = parse_claude_session(
        unknown_read.envelopes,
        context=ClaudeSessionContext(source_session_id="unknown-session"),
    )
    migrate(postgres_connection)
    replace_messages(
        postgres_connection,
        (*primary.messages, *agent.messages, *unknown.messages),
    )

    assert search_messages(postgres_connection, "synthetic delegated") == ()
    agent_hits = search_messages(
        postgres_connection,
        "synthetic delegated",
        include_agents=True,
    )
    assert agent_hits
    assert {hit.session_kind for hit in agent_hits} == {"agent"}
    assert search_messages(postgres_connection, "visible primary") == ()
    unknown_hits = search_messages(
        postgres_connection,
        "visible primary",
        include_agents=True,
    )
    assert unknown_hits
    assert {hit.session_kind for hit in unknown_hits} == {"unknown"}

    assert search_messages(postgres_connection, "one.txt") == ()
    tool_hits = search_messages(
        postgres_connection,
        "one.txt",
        include_tools=True,
    )
    assert tool_hits
    assert {hit.content_class for hit in tool_hits} == {"tool_input"}


def test_exhaustive_literal_search_pages_every_occurrence_in_stable_order(
    postgres_connection: psycopg.Connection,
) -> None:
    read = _read("claude_multiple_tools.jsonl")
    parsed = parse_claude_session(
        read.envelopes,
        context=ClaudeSessionContext(source_session_id="claude-multiple-tools"),
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, parsed.messages)
    cursor = None
    hits = []

    while True:
        page = exhaustive_search_page(
            postgres_connection,
            "Read OR Grep OR one.txt OR needle",
            include_tools=True,
            page_size=1,
            after=cursor,
        )
        hits.extend(page.hits)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert len(hits) == 2
    assert [hit.content_class for hit in hits] == [
        "tool_name",
        "tool_input",
    ]
    assert [hit.text for hit in hits] == [
        "Read\nGrep",
        '{"file_path":"one.txt"}\n{"pattern":"needle"}',
    ]
    assert len(
        {(hit.logical_message_id, hit.content_class, hit.text) for hit in hits}
    ) == len(hits)


def test_semantic_index_skips_blank_prose_and_resumes_failures(
    postgres_connection: psycopg.Connection,
) -> None:
    read = _read("claude_primary.jsonl")
    parsed = parse_claude_session(
        read.envelopes,
        context=ClaudeSessionContext(source_session_id="claude-session-primary"),
    )
    messages = tuple(
        replace(message, text=" \n\t")
        if message.content_class.value == "prose" and message.role == "user"
        else message
        for message in parsed.messages
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, messages)

    eligible = sum(
        message.content_class.value == "prose" and bool(message.text.strip())
        for message in messages
    )
    vector = [0.0] * 1024
    vector[0] = 1.0
    batch_sizes = []

    def embed_batch(texts):
        batch_sizes.append(len(texts))
        return [vector for _ in texts]

    assert (
        index_embeddings(
            postgres_connection,
            embed_batch,
            chunker=_single_chunks,
        )
        == eligible
    )
    assert sum(batch_sizes) == eligible
    selected = next(
        postgres_connection.execute(
            "SELECT current_semantic_revision_id "
            "FROM cc_search_chats.semantic_state WHERE singleton"
        )
    )[0]
    revision_count = next(
        postgres_connection.execute(
            "SELECT count(*) FROM cc_search_chats.semantic_revision"
        )
    )[0]

    changed = tuple(
        replace(message, text=f"{message.text} changed")
        if message.content_class.value == "prose" and message.text.strip()
        else message
        for message in messages
    )
    replace_messages(postgres_connection, changed)

    calls = 0

    def fail_after_one_batch(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("fixture failure")
        return [vector for _ in texts]

    with pytest.raises(RuntimeError, match=r"failed after 1/.* at claude:"):
        index_embeddings(
            postgres_connection,
            fail_after_one_batch,
            chunker=_single_chunks,
            batch_size=1,
        )

    assert (
        next(
            postgres_connection.execute(
                "SELECT current_semantic_revision_id "
                "FROM cc_search_chats.semantic_state WHERE singleton"
            )
        )[0]
        == selected
    )
    assert (
        next(
            postgres_connection.execute(
                "SELECT count(*) FROM cc_search_chats.semantic_revision"
            )
        )[0]
        == revision_count + 1
    )
    assert search_messages(postgres_connection, "changed")
    with pytest.raises(ValueError, match="semantic revision is unavailable or stale"):
        semantic_search(postgres_connection, vector)

    resumed = []

    def embed_remaining(texts):
        resumed.extend(texts)
        return [vector for _ in texts]

    assert (
        index_embeddings(
            postgres_connection,
            embed_remaining,
            chunker=_single_chunks,
            batch_size=1,
        )
        == eligible
    )
    assert len(resumed) == eligible - 1


def test_semantic_worker_preserves_named_model_failure_and_records_its_phase(
    postgres_connection: psycopg.Connection,
) -> None:
    read = _read("claude_primary.jsonl")
    parsed = parse_claude_session(
        read.envelopes,
        context=ClaudeSessionContext(source_session_id="claude-session-primary"),
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, parsed.messages)
    failure = ModelUnavailable(
        "fixture model is unavailable",
        code="model_load_failed",
        phase="model_load",
    )

    with pytest.raises(ModelUnavailable) as raised:
        index_embeddings(
            postgres_connection,
            lambda texts: (_ for _ in ()).throw(failure),
            chunker=_single_chunks,
        )

    assert raised.value is failure
    recorded = next(
        postgres_connection.execute(
            """
            SELECT failure
            FROM cc_search_chats.semantic_revision
            ORDER BY semantic_revision_id DESC
            LIMIT 1
            """
        )
    )[0]
    assert recorded["code"] == "model_load_failed"
    assert recorded["phase"] == "model_load"


def test_semantic_generation_heartbeats_during_a_long_embedding_phase(
    postgres_cluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cc_search_chats.storage.postgresql.semantic._RUN_HEARTBEAT_SECONDS",
        0.02,
        raising=False,
    )
    entered = Event()
    release = Event()
    vector = [0.0] * 1024
    vector[0] = 1.0

    def paused_embed(texts):
        entered.set()
        if not release.wait(timeout=2):
            raise AssertionError("semantic heartbeat test did not release model call")
        return [vector for _ in texts]

    with (
        psycopg.connect(
            postgres_cluster.dsn,
            autocommit=True,
            application_name="semantic-heartbeat-owner",
        ) as owner,
        psycopg.connect(postgres_cluster.dsn, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        read = _read("claude_primary.jsonl")
        parsed = parse_claude_session(
            read.envelopes,
            context=ClaudeSessionContext(source_session_id="claude-session-primary"),
        )
        migrate(owner)
        replace_messages(owner, parsed.messages)
        pending = executor.submit(
            index_embeddings,
            owner,
            paused_embed,
            chunker=_single_chunks,
        )
        assert entered.wait(timeout=1)
        try:
            building = next(
                observer.execute(
                    """
                    SELECT owner_pid, phase, heartbeat_at,
                           completed_units, total_units, status
                    FROM cc_search_chats.semantic_revision
                    ORDER BY semantic_revision_id DESC
                    LIMIT 1
                    """
                )
            )
            deadline = time.monotonic() + 1
            heartbeat_advanced = False
            while time.monotonic() < deadline:
                current = next(
                    observer.execute(
                        """
                        SELECT heartbeat_at
                        FROM cc_search_chats.semantic_revision
                        ORDER BY semantic_revision_id DESC
                        LIMIT 1
                        """
                    )
                )[0]
                if current > building[2]:
                    heartbeat_advanced = True
                    break
                time.sleep(0.01)
        finally:
            release.set()
        completed = pending.result(timeout=2)
        assert building[:2] == (owner.info.backend_pid, "semantic_embed")
        assert building[2] is not None
        assert building[3:] == (0, completed, "building")
        assert heartbeat_advanced
        assert next(
            observer.execute(
                """
                SELECT phase, completed_units, total_units, status
                FROM cc_search_chats.semantic_revision
                ORDER BY semantic_revision_id DESC
                LIMIT 1
                """
            )
        ) == ("done", completed, completed, "complete")


@pytest.mark.parametrize(
    ("invalid_vector", "expected_error"),
    [
        ([1.0], "1024 dimensions"),
        ([float("nan"), *([0.0] * 1023)], "finite"),
        ([1.0] * 1024, "normalized"),
    ],
)
def test_invalid_embedding_output_fails_generation_and_remains_retryable(
    postgres_connection: psycopg.Connection,
    invalid_vector: list[float],
    expected_error: str,
) -> None:
    read = _read("claude_primary.jsonl")
    parsed = parse_claude_session(
        read.envelopes,
        context=ClaudeSessionContext(source_session_id="claude-session-primary"),
    )
    migrate(postgres_connection)
    replace_messages(postgres_connection, parsed.messages)

    with pytest.raises(ValueError, match=expected_error):
        index_embeddings(
            postgres_connection,
            lambda texts: [invalid_vector for _ in texts],
            chunker=_single_chunks,
        )

    failed = next(
        postgres_connection.execute(
            """
            SELECT semantic_revision_id, status, phase, failure
            FROM cc_search_chats.semantic_revision
            ORDER BY semantic_revision_id DESC
            LIMIT 1
            """
        )
    )
    assert failed[1:3] == ("failed", "semantic_embed")
    assert failed[3]["code"] == "semantic_refresh_failed"
    vector = [0.0] * 1024
    vector[0] = 1.0

    completed = index_embeddings(
        postgres_connection,
        lambda texts: [vector for _ in texts],
        chunker=_single_chunks,
    )

    assert completed > 0
    assert next(
        postgres_connection.execute(
            """
            SELECT semantic_revision_id, status
            FROM cc_search_chats.semantic_revision
            ORDER BY semantic_revision_id DESC
            LIMIT 1
            """
        )
    ) == (failed[0], "complete")
