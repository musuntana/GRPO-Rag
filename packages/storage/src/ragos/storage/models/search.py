from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from ragos.storage.models.base import RagosModel, utc_now
from sqlalchemy import Column, Computed, Index, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlmodel import Field

SEARCH_SCHEMA = "search"
SEARCH_VECTOR_SQL = """
setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
setweight(to_tsvector('simple', coalesce(headings_text, '')), 'B') ||
setweight(to_tsvector('simple', coalesce(body, '')), 'C') ||
setweight(to_tsvector('simple', coalesce(tags_text, '')), 'D')
""".strip()


def metadata_json_field() -> Field:
    return Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )


def search_vector_field() -> Field:
    return Field(
        sa_column=Column(
            TSVECTOR(),
            Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=False,
        )
    )


class SearchChunkCommon(RagosModel, table=False):
    id: UUID = Field(default_factory=uuid4, primary_key=True, nullable=False)
    tenant_id: UUID = Field(nullable=False, index=True)
    kb_id: UUID | None = Field(default=None, index=True)
    document_id: UUID | None = Field(default=None, index=True)
    parent_chunk_id: UUID | None = Field(default=None, index=True)
    layer: str = Field(default="L1", max_length=16)
    lang: str = Field(default="simple", max_length=16)
    title: str | None = Field(default=None)
    headings_text: str | None = Field(default=None)
    body: str = Field(nullable=False)
    tags_text: str | None = Field(default=None)
    updated_at: datetime = Field(default_factory=utc_now, nullable=False)
    expires_at: datetime | None = Field(default=None, index=True)


class SearchChunkStable(SearchChunkCommon, table=True):
    __tablename__ = "chunk_stable"
    __table_args__ = (
        Index("ix_chunk_stable_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_chunk_stable_scope", "tenant_id", "kb_id", "layer", "updated_at"),
        {"schema": SEARCH_SCHEMA},
    )

    metadata_json: dict[str, Any] = metadata_json_field()
    search_vector: str = search_vector_field()


class SearchChunkHot(SearchChunkCommon, table=True):
    __tablename__ = "chunk_hot"
    __table_args__ = (
        Index("ix_chunk_hot_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_chunk_hot_scope", "tenant_id", "kb_id", "layer", "updated_at"),
        {"schema": SEARCH_SCHEMA},
    )

    metadata_json: dict[str, Any] = metadata_json_field()
    search_vector: str = search_vector_field()


class SearchMemory(SearchChunkCommon, table=True):
    __tablename__ = "memory"
    __table_args__ = (
        Index("ix_search_memory_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_search_memory_scope", "tenant_id", "updated_at"),
        {"schema": SEARCH_SCHEMA},
    )

    metadata_json: dict[str, Any] = metadata_json_field()
    search_vector: str = search_vector_field()
