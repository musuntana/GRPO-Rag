"""Child → parent expansion and the majority-sibling merge.

Architecture §7.4: expansion is not optional and the merge rule is not
cosmetic. Published measurements on one reference implementation: a plain chunk
retriever scores 0.778 hit rate / 0.563 MRR; resolving children to parents after
retrieval reaches 0.897 / 0.691.

Under ``parent_unit = "page"`` the parent is a page of unbounded token length —
a dense appendix page and a title page are both one parent. The majority-sibling
rule is what stops a page being emitted three times by three of its children.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from ragos.retrieval.types import ScoredChunk


def majority_sibling_parents(
    chunks: Sequence[ScoredChunk],
    children_by_parent: Mapping[UUID, Sequence[UUID]],
) -> set[UUID]:
    """Return parents to emit, given the selected children.

    A parent is emitted when a majority of its children were selected. Emitting
    per-child unconditionally returns the same parent several times and wastes
    context, which is the common failure when several children of one parent
    rank together.
    """
    counts: dict[UUID, int] = {}
    for chunk in chunks:
        if chunk.parent_chunk_id is None:
            continue
        counts[chunk.parent_chunk_id] = counts.get(chunk.parent_chunk_id, 0) + 1

    selected: set[UUID] = set()
    for parent_id, selected_count in counts.items():
        siblings = children_by_parent.get(parent_id, ())
        if not siblings:
            selected.add(parent_id)
            continue
        if selected_count * 2 > len(siblings):
            selected.add(parent_id)
    return selected


def expand_to_parents(
    chunks: Sequence[ScoredChunk],
    parents: Mapping[UUID, ScoredChunk],
    *,
    children_by_parent: Mapping[UUID, Sequence[UUID]] | None = None,
) -> list[ScoredChunk]:
    """Replace matched children with their parents, deduplicated.

    With ``children_by_parent`` supplied the majority-sibling rule applies.
    Without it every distinct referenced parent is emitted once, which is the
    correct behaviour when the sibling count is unknown — it can under-select,
    never over-select.
    """
    if not chunks:
        return []

    if children_by_parent is None:
        wanted = {c.parent_chunk_id for c in chunks if c.parent_chunk_id is not None}
        out = [parents[pid] for pid in wanted if pid in parents]
        # Carry leaves that matched directly and were not absorbed into a parent.
        out.extend(c for c in chunks if c.parent_chunk_id is None)
        out.sort(key=lambda c: c.score, reverse=True)
        return _dedupe_by_id(out)

    wanted = majority_sibling_parents(chunks, children_by_parent)
    emitted = {pid for pid in wanted if pid in parents}
    out = [parents[pid] for pid in wanted if pid in parents]
    # A child whose parent was *not* emitted is kept as a leaf. The rule exists to
    # stop one page being emitted three times; letting it also discard the single
    # matching span on a page trades a context-budget problem for an answer with
    # no evidence at all, which is the worse failure.
    for chunk in chunks:
        if chunk.parent_chunk_id not in emitted:
            out.append(chunk)
    out.sort(key=lambda c: c.score, reverse=True)
    return _dedupe_by_id(out)


def _dedupe_by_id(chunks: list[ScoredChunk]) -> list[ScoredChunk]:
    seen: set[UUID] = set()
    out: list[ScoredChunk] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        out.append(chunk)
    return out


def dedupe_overlapping_parents(parents: Sequence[ScoredChunk]) -> list[ScoredChunk]:
    """Deduplicate parents that overlap by page span or document span.

    Overlap is checked on ``(doc_id, page_no)`` under page anchoring and on
    ``(doc_id, start_char_idx, end_char_idx)`` under token windows. Two parents
    can reach this function from different recall channels, which is why the
    dedupe lives here rather than inside one channel.
    """
    seen: set[tuple] = set()
    out: list[ScoredChunk] = []
    for parent in parents:
        if parent.page_no is not None:
            key = (
                parent.source_identity.representation_id
                if parent.source_identity
                else parent.doc_id,
                parent.source_identity.index_id if parent.source_identity else None,
                "page",
                parent.page_no,
            )
        else:
            key = (
                parent.source_identity.representation_id
                if parent.source_identity
                else parent.doc_id,
                parent.source_identity.index_id if parent.source_identity else None,
                "span",
                parent.start_char_idx,
                parent.end_char_idx,
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(parent)
    return out
