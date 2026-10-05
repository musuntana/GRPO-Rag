"""Long-term user memory.

Architecturally separate from the enterprise knowledge base (architecture §7.3,
AGENTS.md guardrail 8). A fact about *this user* and a passage about *the product*
answer different questions, are indexed differently, expire differently, and are
governed differently — a DSAR deletion hits one and must not hit the other, and a
fact must never be able to satisfy a query the corpus should have answered.

Three tiers, in the order they are consulted:

1. working memory — in-graph state, per run
2. conversation buffer — recent turns verbatim plus rolling summaries of older ones
3. long-term memory — structured facts with a validity interval, here
"""

from ragos.memory.decay import (
    DEFAULT_HALF_LIFE_DAYS,
    archive_threshold,
    decay_score,
    is_expired,
)
from ragos.memory.extractor import FactExtractor, parse_extraction
from ragos.memory.fact_store import MemoryFactStore
from ragos.memory.manager import MemoryContext, MemoryManager
from ragos.memory.rerank import rank_facts
from ragos.memory.summarizer import (
    DEFAULT_SUMMARY_PROMPT,
    RollingSummarizer,
    SummaryResult,
    Turn,
)
from ragos.memory.types import (
    CompleteFn,
    ExtractedFact,
    FactCategory,
    FactOperation,
    MemoryFact,
)

__all__ = [
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_SUMMARY_PROMPT",
    "CompleteFn",
    "ExtractedFact",
    "FactCategory",
    "FactExtractor",
    "FactOperation",
    "MemoryContext",
    "MemoryFact",
    "MemoryFactStore",
    "MemoryManager",
    "RollingSummarizer",
    "SummaryResult",
    "Turn",
    "archive_threshold",
    "decay_score",
    "is_expired",
    "parse_extraction",
    "rank_facts",
]
