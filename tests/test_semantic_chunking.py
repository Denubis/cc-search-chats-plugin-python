"""Tokenizer-aware semantic chunk boundaries."""

from cc_search_chats.semantic.model import (
    CHUNK_OVERLAP_TOKENS,
    CHUNK_TARGET_TOKENS,
    MAX_MODEL_TOKENS,
    SemanticChunk,
    chunk_passages,
)


class CharacterTokenizer:
    """Deterministic fast-tokenizer stand-in with one token per character."""

    def num_special_tokens_to_add(self, *, pair: bool) -> int:
        assert pair is False
        return 2

    def __call__(
        self,
        text: str,
        *,
        add_special_tokens: bool,
        return_offsets_mapping: bool = False,
    ) -> dict[str, list[int] | list[tuple[int, int]]]:
        assert add_special_tokens is False
        value: dict[str, list[int] | list[tuple[int, int]]] = {
            "input_ids": list(range(len(text)))
        }
        if return_offsets_mapping:
            value["offset_mapping"] = [(index, index + 1) for index in range(len(text))]
        return value


def test_chunker_targets_768_with_96_overlap_and_hard_model_limit() -> None:
    text = "x" * 1_700
    tokenizer = CharacterTokenizer()
    chunks = chunk_passages((text,), tokenizer=tokenizer)[0]

    assert CHUNK_TARGET_TOKENS == 768
    assert CHUNK_OVERLAP_TOKENS == 96
    assert MAX_MODEL_TOKENS == 1_024
    assert [(chunk.token_start, chunk.token_end) for chunk in chunks] == [
        (0, 768),
        (672, 1_440),
        (1_344, 1_700),
    ]
    assert [(chunk.char_start, chunk.char_end) for chunk in chunks] == [
        (0, 768),
        (672, 1_440),
        (1_344, 1_700),
    ]
    assert [chunk.ordinal for chunk in chunks] == [0, 1, 2]
    assert all(
        chunk.text == text[chunk.char_start : chunk.char_end] for chunk in chunks
    )

    prefix_tokens = len(tokenizer("passage: ", add_special_tokens=False)["input_ids"])
    assert all(
        (chunk.token_end - chunk.token_start) + prefix_tokens + 2 <= MAX_MODEL_TOKENS
        for chunk in chunks
    )


def test_short_message_remains_one_exact_chunk() -> None:
    text = "short message"

    assert chunk_passages((text,), tokenizer=CharacterTokenizer()) == (
        (
            SemanticChunk(
                ordinal=0,
                token_start=0,
                token_end=len(text),
                char_start=0,
                char_end=len(text),
                text=text,
            ),
        ),
    )
