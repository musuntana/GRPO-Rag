"""Immutable enterprise evidence identity, shared by retrieval, citations and gold."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ragos.core.errors import ValidationError


class EvidenceIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    document_version_id: UUID
    representation_id: UUID
    index_id: UUID
    chunk_id: UUID
    representation_char_count: int = Field(ge=0, strict=True)
    parent_unit: Literal["page", "token_window"]
    page_count: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def validate_pagination(self):
        if (self.parent_unit == "page") != (self.page_count is not None):
            raise ValueError("page count is required only for a frozen paginated representation")
        return self

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


def identity_from_hit(hit, *, tenant_id: UUID) -> EvidenceIdentity:
    """Map a scoped storage projection; missing lineage is unavailable, never v1."""
    fields = {
        "tenant_id": tenant_id,
        "kb_id": hit.kb_id,
        "document_id": hit.document_id,
        "document_version_id": hit.document_version_id,
        "representation_id": hit.representation_id,
        "index_id": hit.index_id,
        "chunk_id": hit.chunk_id,
        "representation_char_count": hit.representation_char_count,
        "parent_unit": hit.parent_unit,
        "page_count": hit.page_count,
    }
    try:
        return EvidenceIdentity.model_validate(fields)
    except ValueError as exc:
        raise ValidationError("enterprise evidence has no validated source identity") from exc
