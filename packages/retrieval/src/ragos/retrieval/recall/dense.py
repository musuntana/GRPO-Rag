"""Dense recall: Qdrant vector search, then text hydrated from PostgreSQL.

Two design points that are load-bearing rather than incidental.

**The tenant predicate is mandatory and built by the shared builder.** Every
vector read goes through ``ragos.runtime.vector.build_filter``. A call site that
assembles predicates by hand can omit ``tenant_id``, and that omission is a
cross-tenant read.

**Qdrant returns ids, PostgreSQL returns the text.** The payload deliberately
does not carry ``body``: duplicating the corpus inside the vector store doubles
storage and puts two copies of every chunk on divergent timelines. Hydrating from
``search.*`` also means the citation span a user is handed and the text the model
read come from the same row.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

from ragos.core.errors import ProviderError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.storage.repositories.search import SearchHit, SearchTier

LOGGER = get_logger(__name__)

#: The query embedding call. Takes the tenant context so the call is metered and
#: budget-checked by the gateway like every other model call.
EmbedFn = Callable[[str, TenantContext], Awaitable[list[float]]]

#: Builds the payload filter. Defaults to the shared Qdrant builder; injectable so
#: the tenant-predicate guarantee can be asserted without the optional
#: ``qdrant-client`` dependency installed.
FilterBuilder = Callable[..., Any]


class ChunkSource(Protocol):
    """The slice of ``SearchRepository`` dense recall needs."""

    def get_chunks_by_ids(
        self, *, tier: SearchTier, scope_key: str, chunk_ids: list[UUID]
    ) -> list[SearchHit]: ...


class DenseRecall:
    """Qdrant dense recall with a mandatory ``tenant_id`` predicate."""

    def __init__(
        self,
        client: Any,
        collection: str,
        embed: EmbedFn,
        *,
        source: ChunkSource,
        tier: SearchTier = SearchTier.STABLE,
        filter_builder: FilterBuilder | None = None,
    ) -> None:
        from ragos.runtime.vector import build_filter

        self.client = client
        self.collection = collection
        self.embed = embed
        self.source = source
        self.tier = tier
        self.filter_builder = filter_builder or build_filter

    async def recall(
        self,
        query: str,
        tenant_ctx: TenantContext,
        profile: RetrieverProfile,
    ) -> list[ScoredChunk]:
        scope_key = tenant_ctx.require_scope_key()
        vector = await self.embed(query, tenant_ctx)
        query_filter = self.filter_builder(
            scope_key,
            kb_ids=tenant_ctx.allowed_kb_ids,
            brand_ids=[tenant_ctx.brand_id] if tenant_ctx.brand_id else None,
        )

        # A vector-store failure is raised, not swallowed. Returning zero hits
        # would make an outage look like "this corpus does not contain the
        # answer", and the refusal that follows is indistinguishable from a
        # correct abstention.
        try:
            response = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=profile.top_k_recall,
                with_payload=True,
            )
        except Exception as exc:
            LOGGER.exception(
                "qdrant dense recall failed",
                extra={"collection": self.collection, "trace_id": tenant_ctx.trace_id},
            )
            raise ProviderError(
                "dense retrieval is unavailable",
                provider="qdrant",
                collection=self.collection,
                reason=type(exc).__name__,
            ) from exc

        return self._to_chunks(response, scope_key)

    def _to_chunks(self, response: Any, scope_key: str) -> list[ScoredChunk]:
        ordered: list[ScoredChunk] = []
        for point in getattr(response, "points", []) or []:
            payload = point.payload or {}
            chunk_id = as_uuid(payload.get("chunk_id"))
            if chunk_id is None:
                continue
            ordered.append(
                ScoredChunk(
                    chunk_id=chunk_id,
                    doc_id=as_uuid(payload.get("doc_id")),
                    kb_id=as_uuid(payload.get("kb_id")),
                    # Filled in by hydration below. Empty here is a bug, not a
                    # title: the payload carries no body.
                    text="",
                    score=float(point.score),
                    channel="dense",
                    layer=payload.get("layer", "L1"),
                    parent_chunk_id=as_uuid(payload.get("parent_chunk_id")),
                    page_no=payload.get("page_no"),
                    page_count=payload.get("page_count"),
                    start_char_idx=payload.get("start_char_idx"),
                    end_char_idx=payload.get("end_char_idx"),
                    element_type=payload.get("element_type", "text"),
                )
            )

        return self._hydrate(ordered, scope_key)

    def _hydrate(self, chunks: list[ScoredChunk], scope_key: str) -> list[ScoredChunk]:
        """Fill text from the search schema, preserving Qdrant's ranking."""
        if not chunks:
            return []
        by_id = {
            hit.chunk_id: hit
            for hit in self.source.get_chunks_by_ids(
                tier=self.tier,
                scope_key=scope_key,
                chunk_ids=[c.chunk_id for c in chunks],
            )
        }

        hydrated: list[ScoredChunk] = []
        missing: list[str] = []
        for chunk in chunks:
            hit = by_id.get(chunk.chunk_id)
            if hit is None:
                # A vector without a row means the two indexes are out of step.
                # Delivering the chunk with empty text would put a title-only
                # fragment into the answer with a citation pointing at it.
                missing.append(str(chunk.chunk_id))
                continue
            hydrated.append(
                ScoredChunk(
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id or hit.document_id,
                    kb_id=chunk.kb_id or hit.kb_id,
                    text=hit.body,
                    score=chunk.score,
                    channel=chunk.channel,
                    layer=hit.layer,
                    parent_chunk_id=chunk.parent_chunk_id,
                    page_no=chunk.page_no if chunk.page_no is not None else hit.page_no,
                    page_count=(
                        chunk.page_count if chunk.page_count is not None else hit.page_count
                    ),
                    start_char_idx=(
                        chunk.start_char_idx
                        if chunk.start_char_idx is not None
                        else hit.start_char_idx
                    ),
                    end_char_idx=(
                        chunk.end_char_idx if chunk.end_char_idx is not None else hit.end_char_idx
                    ),
                    element_type=hit.element_type,
                )
            )

        if missing:
            LOGGER.warning(
                "dense recall returned vectors with no search row",
                extra={
                    "collection": self.collection,
                    "missing": len(missing),
                    "sample": missing[:3],
                },
            )
        return hydrated


def as_uuid(value: Any) -> UUID | None:
    """Tolerant UUID coercion for payload values.

    A payload field that cannot be read as a UUID is a data problem in the index;
    dropping the point is better than raising in the middle of a recall, and the
    count is visible in the log.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None
