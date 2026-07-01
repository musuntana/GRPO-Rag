from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from ragos.core.tenant import TenantContext
from ragos.storage.models.base import utc_now
from ragos.storage.models.search import SearchChunkHot, SearchChunkStable, SearchMemory
from sqlalchemy import func, literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select


class SearchTier(StrEnum):
    STABLE = "stable"
    HOT = "hot"
    MEMORY = "memory"


@dataclass(slots=True)
class SearchChunkUpsert:
    id: UUID
    tenant_id: UUID
    kb_id: UUID | None
    document_id: UUID | None
    parent_chunk_id: UUID | None
    layer: str
    lang: str
    title: str | None
    headings_text: str | None
    body: str
    tags_text: str | None
    metadata_json: dict[str, Any]
    updated_at: datetime
    expires_at: datetime | None = None


@dataclass(slots=True)
class SearchHit:
    chunk_id: UUID
    document_id: UUID | None
    kb_id: UUID | None
    layer: str
    body: str
    metadata_json: dict[str, Any]
    rank: float


class SearchRepository:
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        self.session = session
        self.tenant_ctx = tenant_ctx

    @staticmethod
    def model_for_tier(tier: SearchTier):
        if tier == SearchTier.STABLE:
            return SearchChunkStable
        if tier == SearchTier.HOT:
            return SearchChunkHot
        return SearchMemory

    def upsert_chunks(self, *, tier: SearchTier, chunks: list[SearchChunkUpsert]) -> None:
        if not chunks:
            return

        model = self.model_for_tier(tier)
        rows = []
        now = utc_now()
        for chunk in chunks:
            if chunk.tenant_id != self.tenant_ctx.tenant_id:
                raise ValueError("attempted to upsert search data for a different tenant")
            rows.append(
                {
                    "id": chunk.id,
                    "tenant_id": chunk.tenant_id,
                    "kb_id": chunk.kb_id,
                    "document_id": chunk.document_id,
                    "parent_chunk_id": chunk.parent_chunk_id,
                    "layer": chunk.layer,
                    "lang": chunk.lang,
                    "title": chunk.title,
                    "headings_text": chunk.headings_text,
                    "body": chunk.body,
                    "tags_text": chunk.tags_text,
                    "metadata_json": chunk.metadata_json,
                    "updated_at": chunk.updated_at or now,
                    "expires_at": chunk.expires_at,
                }
            )

        statement = insert(model).values(rows)
        update_columns = {
            "kb_id": statement.excluded.kb_id,
            "document_id": statement.excluded.document_id,
            "parent_chunk_id": statement.excluded.parent_chunk_id,
            "layer": statement.excluded.layer,
            "lang": statement.excluded.lang,
            "title": statement.excluded.title,
            "headings_text": statement.excluded.headings_text,
            "body": statement.excluded.body,
            "tags_text": statement.excluded.tags_text,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": statement.excluded.updated_at,
            "expires_at": statement.excluded.expires_at,
        }
        upsert = statement.on_conflict_do_update(
            index_elements=[model.id],
            set_=update_columns,
        )
        self.session.exec(upsert)

    def search(
        self,
        *,
        tier: SearchTier,
        query: str,
        limit: int = 10,
        kb_ids: list[UUID] | None = None,
        layer: str | None = None,
    ) -> list[SearchHit]:
        model = self.model_for_tier(tier)
        ts_query = func.websearch_to_tsquery(literal_column("'simple'"), query)
        rank = func.ts_rank_cd(model.search_vector, ts_query).label("rank")
        statement = (
            select(
                model.id,
                model.document_id,
                model.kb_id,
                model.layer,
                model.body,
                model.metadata_json,
                rank,
            )
            .where(model.tenant_id == self.tenant_ctx.tenant_id)
            .where(model.search_vector.op("@@")(ts_query))
            .order_by(rank.desc(), model.updated_at.desc())
            .limit(limit)
        )
        if kb_ids:
            statement = statement.where(model.kb_id.in_(kb_ids))
        if layer:
            statement = statement.where(model.layer == layer)

        rows = self.session.exec(statement).all()
        return [
            SearchHit(
                chunk_id=row[0],
                document_id=row[1],
                kb_id=row[2],
                layer=row[3],
                body=row[4],
                metadata_json=row[5],
                rank=float(row[6]),
            )
            for row in rows
        ]
