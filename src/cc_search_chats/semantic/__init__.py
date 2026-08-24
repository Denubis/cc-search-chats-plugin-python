"""Optional local semantic-model support."""

from cc_search_chats.semantic.model import (
    ModelUnavailable,
    SemanticChunk,
    chunk_passages,
    embed_passages,
    embed_query,
)

__all__ = [
    "ModelUnavailable",
    "SemanticChunk",
    "chunk_passages",
    "embed_passages",
    "embed_query",
]
