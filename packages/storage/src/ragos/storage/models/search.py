from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from ragos.storage.models.base import RagosModel, utc_now
from sqlalchemy import Column, Computed, DateTime, ForeignKeyConstraint, Index, Text, text
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
    tenant_id: UUID = Field(nullable=False)
    kb_id: UUID | None = Field(default=None)
    document_id: UUID | None = Field(default=None)
    parent_chunk_id: UUID | None = Field(default=None)
    page_no: int | None = Field(default=None)
    page_count: int | None = Field(default=None)
    page_offset_start: int | None = Field(default=None)
    page_offset_end: int | None = Field(default=None)
    start_char_idx: int | None = Field(default=None)
    end_char_idx: int | None = Field(default=None)
    element_type: str = Field(default="text", max_length=16, nullable=False)
    is_parent: bool = Field(default=False, nullable=False)
    brand_id: UUID | None = Field(default=None)
    source_type: str = Field(default="upload", max_length=32, nullable=False)
    security_level: str = Field(default="internal", max_length=16, nullable=False)
    page_image_ref: str | None = Field(default=None, max_length=1024)
    layer: str = Field(default="L1", max_length=16)
    lang: str = Field(default="simple", max_length=16)
    title: str | None = Field(sa_type=Text, default=None)
    headings_text: str | None = Field(sa_type=Text, default=None)
    body: str = Field(sa_type=Text, nullable=False)
    tags_text: str | None = Field(sa_type=Text, default=None)
    updated_at: datetime = Field(
        sa_type=DateTime(timezone=True), default_factory=utc_now, nullable=False
    )
    expires_at: datetime | None = Field(sa_type=DateTime(timezone=True), default=None)


class SearchChunkStable(SearchChunkCommon, table=True):
    document_version_id: UUID | None = Field(default=None)
    representation_id: UUID | None = Field(default=None)
    index_id: UUID | None = Field(default=None)
    __tablename__ = "chunk_stable"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "kb_id",
                "document_id",
                "document_version_id",
                "representation_id",
                "index_id",
            ],
            [
                "app.chunk_manifest.tenant_id",
                "app.chunk_manifest.kb_id",
                "app.chunk_manifest.document_id",
                "app.chunk_manifest.document_version_id",
                "app.chunk_manifest.representation_id",
                "app.chunk_manifest.id",
            ],
            name="fk_stable_manifest_lineage",
        ),
        Index("ix_chunk_stable_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_chunk_stable_scope", "tenant_id", "kb_id", "layer", "updated_at"),
        Index("ix_chunk_stable_page_no", "page_no"),
        Index("ix_chunk_stable_is_parent", "is_parent"),
        Index("ix_chunk_stable_page", "tenant_id", "document_id", "page_no"),
        {"schema": SEARCH_SCHEMA},
    )

    metadata_json: dict[str, Any] = metadata_json_field()
    search_vector: str = search_vector_field()


class SearchChunkHot(SearchChunkCommon, table=True):
    document_version_id: UUID | None = Field(default=None)
    representation_id: UUID | None = Field(default=None)
    index_id: UUID | None = Field(default=None)
    __tablename__ = "chunk_hot"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "kb_id",
                "document_id",
                "document_version_id",
                "representation_id",
                "index_id",
            ],
            [
                "app.chunk_manifest.tenant_id",
                "app.chunk_manifest.kb_id",
                "app.chunk_manifest.document_id",
                "app.chunk_manifest.document_version_id",
                "app.chunk_manifest.representation_id",
                "app.chunk_manifest.id",
            ],
            name="fk_hot_manifest_lineage",
        ),
        Index("ix_chunk_hot_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_chunk_hot_scope", "tenant_id", "kb_id", "layer", "updated_at"),
        Index("ix_chunk_hot_page_no", "page_no"),
        Index("ix_chunk_hot_is_parent", "is_parent"),
        Index("ix_chunk_hot_page", "tenant_id", "document_id", "page_no"),
        {"schema": SEARCH_SCHEMA},
    )

    metadata_json: dict[str, Any] = metadata_json_field()
    search_vector: str = search_vector_field()


class SearchMemory(SearchChunkCommon, table=True):
    user_id: UUID | None = Field(default=None)
    __tablename__ = "memory"
    __table_args__ = (
        Index("ix_memory_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_memory_scope", "tenant_id", "updated_at"),
        Index("ix_memory_page_no", "page_no"),
        Index("ix_memory_is_parent", "is_parent"),
        Index("ix_search_memory_user", "tenant_id", "user_id"),
        {"schema": SEARCH_SCHEMA},
    )

    metadata_json: dict[str, Any] = metadata_json_field()
    search_vector: str = search_vector_field()
