"""One end-to-end PostgreSQL corpus behavior for both native providers."""

from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from cc_search_chats.providers.claude import ClaudeSessionContext, parse_claude_session
from cc_search_chats.providers.codex import CodexSessionContext, parse_codex_session
from cc_search_chats.providers.source_discovery import read_bounded_jsonl
from cc_search_chats.storage.postgresql import (
    context_messages,
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

pytestmark = pytest.mark.postgresql
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"


def _read(name: str):
    path = FIXTURES / name
    return read_bounded_jsonl(
        path,
        source_file_relative=Path(name),
        target_size=path.stat().st_size,
    )


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
        index_embeddings(postgres_connection, embed_passages, batch_size=2) == expected
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

    assert index_embeddings(postgres_connection, embed_batch) == eligible
    assert batch_sizes == []
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

    resumed = []

    def embed_remaining(texts):
        resumed.extend(texts)
        return [vector for _ in texts]

    assert index_embeddings(postgres_connection, embed_remaining, batch_size=1) == eligible
    assert len(resumed) == eligible - 1
