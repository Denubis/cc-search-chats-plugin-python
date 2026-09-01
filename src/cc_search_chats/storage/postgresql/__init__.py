"""PostgreSQL-backed cross-vendor chat index."""

from cc_search_chats.storage.postgresql.events import (
    EventExport,
    EventPopulation,
    HumanMessageEvent,
    export_human_message_events,
)
from cc_search_chats.storage.postgresql.index import (
    ExhaustiveCursor,
    ExhaustivePage,
    MessageResolution,
    SearchHit,
    StoredAlias,
    StoredMessage,
    StoredSession,
    context_messages,
    exhaustive_search_page,
    extract_session,
    list_sessions,
    migrate,
    replace_messages,
    resolve_message,
    resolve_messages,
    search_messages,
)
from cc_search_chats.storage.postgresql.refresh import (
    CorpusIndexResult,
    RefreshProgress,
    RefreshResult,
    index_corpus,
    refresh_native_sources,
)
from cc_search_chats.storage.postgresql.resolution import (
    ExactResolution,
    resolve_exact_messages,
)
from cc_search_chats.storage.postgresql.semantic import (
    HybridHit,
    hybrid_search,
    index_embeddings,
    semantic_search,
)

__all__ = [
    "EventExport",
    "EventPopulation",
    "ExhaustiveCursor",
    "ExhaustivePage",
    "ExactResolution",
    "CorpusIndexResult",
    "SearchHit",
    "MessageResolution",
    "RefreshResult",
    "RefreshProgress",
    "HybridHit",
    "HumanMessageEvent",
    "StoredMessage",
    "StoredAlias",
    "StoredSession",
    "context_messages",
    "extract_session",
    "export_human_message_events",
    "exhaustive_search_page",
    "hybrid_search",
    "index_embeddings",
    "index_corpus",
    "list_sessions",
    "migrate",
    "refresh_native_sources",
    "replace_messages",
    "resolve_message",
    "resolve_exact_messages",
    "resolve_messages",
    "search_messages",
    "semantic_search",
]
