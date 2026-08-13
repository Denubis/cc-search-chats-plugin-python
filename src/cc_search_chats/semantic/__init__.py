"""Optional local semantic-model support."""

from cc_search_chats.semantic.model import ModelUnavailable, embed_passages, embed_query

__all__ = ["ModelUnavailable", "embed_passages", "embed_query"]
