"""PostgreSQL-backed cross-vendor chat index."""

from cc_search_chats.storage.postgresql.index import (
    MessageResolution,
    SearchHit,
    StoredMessage,
    StoredSession,
    context_messages,
    extract_session,
    list_sessions,
    migrate,
    replace_messages,
    resolve_message,
    resolve_messages,
    search_messages,
)
from cc_search_chats.storage.postgresql.refresh import (
    RefreshProgress,
    RefreshResult,
    refresh_native_sources,
)
from cc_search_chats.storage.postgresql.semantic import (
    HybridHit,
    hybrid_search,
    index_embeddings,
    replace_embeddings,
    semantic_search,
)

__all__ = [
    "SearchHit",
    "MessageResolution",
    "RefreshResult",
    "RefreshProgress",
    "HybridHit",
    "StoredMessage",
    "StoredSession",
    "context_messages",
    "extract_session",
    "hybrid_search",
    "index_embeddings",
    "list_sessions",
    "migrate",
    "refresh_native_sources",
    "replace_embeddings",
    "replace_messages",
    "resolve_message",
    "resolve_messages",
    "search_messages",
    "semantic_search",
]
