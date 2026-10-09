"""One typed fingerprint of the evidence both search stores must retain."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from ragos.core.errors import ValidationError
from ragos.storage.artifacts import canonical_json


class ChunkProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    chunk_id: UUID
    tenant_id: UUID
    kb_id: UUID
    doc_id: UUID
    document_version_id: UUID
    representation_id: UUID
    index_id: UUID
    parent_chunk_id: UUID | None
    is_parent: bool = Field(strict=True)
    body_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_fields_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_no: int | None = Field(default=None, ge=1, strict=True)
    page_count: int | None = Field(default=None, ge=1, strict=True)
    page_offset_start: int | None = Field(default=None, ge=0, strict=True)
    page_offset_end: int | None = Field(default=None, ge=1, strict=True)
    start_char_idx: int = Field(ge=0, strict=True)
    end_char_idx: int = Field(ge=1, strict=True)
    element_type: str
    representation_char_count: int = Field(ge=1, strict=True)
    parent_unit: Literal["page", "token_window"]


def projection_from_chunk(
    chunk, *, representation_char_count: int, parent_unit: str
) -> ChunkProjection:
    """Map an authorized upsert or ORM row; never synthesize missing source fields."""
    metadata = canonical_json(
        {
            "title": chunk.title,
            "headings_text": chunk.headings_text,
            "tags_text": chunk.tags_text,
            "lang": chunk.lang,
            "layer": chunk.layer,
            "metadata_json": chunk.metadata_json,
        }
    )
    try:
        return ChunkProjection(
            chunk_id=chunk.id,
            tenant_id=chunk.tenant_id,
            kb_id=chunk.kb_id,
            doc_id=chunk.document_id,
            document_version_id=chunk.document_version_id,
            representation_id=chunk.representation_id,
            index_id=chunk.index_id,
            parent_chunk_id=chunk.parent_chunk_id,
            is_parent=chunk.is_parent,
            body_digest=hashlib.sha256(chunk.body.encode("utf-8")).hexdigest(),
            search_fields_digest=hashlib.sha256(metadata).hexdigest(),
            page_no=chunk.page_no,
            page_count=chunk.page_count,
            page_offset_start=chunk.page_offset_start,
            page_offset_end=chunk.page_offset_end,
            start_char_idx=chunk.start_char_idx,
            end_char_idx=chunk.end_char_idx,
            element_type=chunk.element_type,
            representation_char_count=representation_char_count,
            parent_unit=parent_unit,
        )
    except ValueError as exc:
        raise ValidationError("index projection is missing validated evidence identity") from exc


def projection_digest(projections: Sequence[ChunkProjection]) -> str:
    ids = {item.chunk_id for item in projections}
    if not projections or len(ids) != len(projections):
        raise ValidationError("index readback must be nonempty with unique chunk identities")
    data = [
        item.model_dump(mode="json")
        for item in sorted(projections, key=lambda item: str(item.chunk_id))
    ]
    return hashlib.sha256(canonical_json(data)).hexdigest()
