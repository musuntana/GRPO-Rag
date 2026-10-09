"""Parent resolution: turning matched children into the context generation sees.

Architecture §7.4. The pipeline's ``expand_to_parents`` is a pure function over a
mapping; this module supplies that mapping from storage.

The second half of the job is the denominator. The majority-sibling rule decides
whether a parent is emitted by comparing the number of its selected children
against the number it has in total, so a resolver that returned parents without
sibling counts would silently reduce to "emit every referenced parent" — the
failure mode the rule was written to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ragos.core.errors import ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.identity import identity_from_hit
from ragos.retrieval.types import ScoredChunk
from ragos.storage.repositories.search import SearchRepository, SearchTier

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class ResolvedParents:
    parents: dict[UUID, ScoredChunk] = field(default_factory=dict)
    children_by_parent: dict[UUID, list[UUID]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.parents


class ParentResolver(Protocol):
    """What the pipeline asks for. Injected, so tests need no storage."""

    async def resolve(
        self, chunks: list[ScoredChunk], tenant_ctx: TenantContext
    ) -> ResolvedParents: ...


class SearchParentResolver:
    """Fetch parents and sibling counts from the ``search`` schema.

    Scoped like every other read: ``scope_key`` is passed in and checked by the
    repository, so a parent belonging to another tenant cannot be reached by
    naming its id.
    """

    def __init__(self, repo: SearchRepository, *, tier: SearchTier = SearchTier.STABLE) -> None:
        self.repo = repo
        self.tier = tier

    async def resolve(
        self, chunks: list[ScoredChunk], tenant_ctx: TenantContext
    ) -> ResolvedParents:
        scope_key = tenant_ctx.require_scope_key()
        parent_ids = [c.parent_chunk_id for c in chunks if c.parent_chunk_id is not None]
        unique_ids = list(dict.fromkeys(parent_ids))
        if not unique_ids:
            return ResolvedParents()

        hits = self.repo.get_chunks_by_ids(
            tier=self.tier, scope_key=scope_key, chunk_ids=unique_ids
        )
        parents = {
            hit.chunk_id: ScoredChunk(
                chunk_id=hit.chunk_id,
                doc_id=hit.document_id,
                kb_id=hit.kb_id,
                text=hit.body,
                # A parent inherits the best score among the children that led to
                # it. Without this a parent reached by one strong child and one
                # weak one ranks below the weak child's own parent.
                score=max(
                    (c.score for c in chunks if c.parent_chunk_id == hit.chunk_id),
                    default=0.0,
                ),
                channel="parent",
                layer=hit.layer,
                page_no=hit.page_no,
                page_count=hit.page_count,
                start_char_idx=hit.start_char_idx,
                end_char_idx=hit.end_char_idx,
                element_type=hit.element_type,
                source_identity=identity_from_hit(hit, tenant_id=tenant_ctx.tenant_id),
                source_text=hit.body,
            )
            for hit in hits
        }

        for child in chunks:
            parent = parents.get(child.parent_chunk_id)
            if parent is None:
                continue
            child_identity = child.require_source_identity()
            parent_identity = parent.require_source_identity()
            child_lineage = child_identity.model_dump(exclude={"chunk_id"})
            parent_lineage = parent_identity.model_dump(exclude={"chunk_id"})
            if child_lineage != parent_lineage:
                raise ValidationError("parent and child evidence source identity do not match")

        counts = self.repo.count_children(
            tier=self.tier, scope_key=scope_key, parent_ids=list(parents)
        )
        # Counts are integers; the merge rule only compares a selected count
        # against a total, so placeholders stand in for the sibling ids.
        children_by_parent = {
            parent_id: [UUID(int=index) for index in range(total)]
            for parent_id, total in counts.items()
        }

        if len(parents) < len(unique_ids):
            LOGGER.warning(
                "some parents were not readable under the resolved scope",
                extra={
                    "requested": len(unique_ids),
                    "resolved": len(parents),
                    "trace_id": tenant_ctx.trace_id,
                },
            )

        return ResolvedParents(parents=parents, children_by_parent=children_by_parent)
