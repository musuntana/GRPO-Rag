from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

SENTENCE_END = ".!?。！？"


def estimate_tokens(text: str) -> int:
    """Cross-language token proxy.

    English: 1 token ≈ 0.75 words. Chinese: 1 token ≈ 1.5 chars.
    Returning ``max(word_count, char_count // 3)`` gives a conservative
    upper-bound that works for mixed-language corpora without pulling in a
    tokenizer dependency. Only used for greedy packing decisions — the real
    token budget is enforced downstream by the embedding provider.
    """
    if not text:
        return 0
    return max(len(text.split()), len(text) // 3)


def split_sentences(text: str) -> list[str]:
    """Greedy sentence splitter on ASCII + CJK punctuation.

    Intentionally simple: walks the string once, cuts after any sentence-end
    character. Good enough for chunker-level greedy packing — it does not
    need to be linguistically perfect. Whitespace is trimmed per sentence
    and empty sentences are dropped.
    """
    sentences: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in SENTENCE_END:
            sent = "".join(buf).strip()
            if sent:
                sentences.append(sent)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)
    return sentences


@dataclass(slots=True)
class ChunkNode:
    """A single chunk in a document's chunk tree.

    Parents aggregate N children; children carry ``parent_chunk_id`` so
    retrieval can expand a matched child back to its parent for generation
    context (AGENTS.md §Retrieval Rules).
    """

    chunk_id: UUID
    parent_chunk_id: UUID | None
    is_parent: bool
    text: str
    token_count: int
    order: int
    metadata: dict[str, Any] = field(default_factory=dict)


class SemanticChunker:
    """Sentence-aware greedy chunker with parent-child hierarchy.

    Algorithm:
        1. Split text into sentences on ASCII + CJK sentence-end punctuation.
        2. Greedy-pack sentences into parent buckets, target ``parent_size``
           tokens each.
        3. Inside each parent bucket, re-pack the same sentences into child
           chunks, target ``child_size`` tokens each.
        4. Emit ``[parent, child, child, ..., parent, child, ...]`` in
           document order. Children reference their parent via
           ``parent_chunk_id``.

    The chunker is deterministic given the same text + parameters (sentence
    splitting is pure, UUID generation is the only non-determinism and is
    isolated to ``chunk_id``). Target sizes are measured with
    ``estimate_tokens``, not a real tokenizer — see that function's docstring.
    """

    def __init__(
        self,
        *,
        child_size: int = 400,
        parent_size: int = 1600,
    ) -> None:
        if child_size <= 0 or parent_size <= 0:
            raise ValueError("child_size and parent_size must be positive")
        if parent_size < child_size:
            raise ValueError("parent_size must be >= child_size")
        self.child_size = child_size
        self.parent_size = parent_size

    def chunk(self, text: str) -> list[ChunkNode]:
        sentences = split_sentences(text)
        if not sentences:
            return []

        parent_buckets = self._group_sentences(sentences, self.parent_size)

        nodes: list[ChunkNode] = []
        order = 0
        for parent_sentences in parent_buckets:
            parent_text = self._join(parent_sentences)
            parent_id = uuid4()
            nodes.append(
                ChunkNode(
                    chunk_id=parent_id,
                    parent_chunk_id=None,
                    is_parent=True,
                    text=parent_text,
                    token_count=estimate_tokens(parent_text),
                    order=order,
                )
            )
            order += 1

            for child_sentences in self._group_sentences(parent_sentences, self.child_size):
                child_text = self._join(child_sentences)
                nodes.append(
                    ChunkNode(
                        chunk_id=uuid4(),
                        parent_chunk_id=parent_id,
                        is_parent=False,
                        text=child_text,
                        token_count=estimate_tokens(child_text),
                        order=order,
                    )
                )
                order += 1

        return nodes

    @staticmethod
    def _group_sentences(sentences: list[str], target_size: int) -> list[list[str]]:
        """Greedy bucketing: pack sentences until adding one more would
        overshoot ``target_size``. An oversized single sentence still becomes
        its own bucket rather than being split mid-word.
        """
        buckets: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for sent in sentences:
            sent_tokens = estimate_tokens(sent)
            if current and current_tokens + sent_tokens > target_size:
                buckets.append(current)
                current = []
                current_tokens = 0
            current.append(sent)
            current_tokens += sent_tokens
        if current:
            buckets.append(current)
        return buckets

    @staticmethod
    def _join(sentences: list[str]) -> str:
        return " ".join(sentences)
