"""Frozen representation and index plans, validated against original artifact bytes."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.artifacts import canonical_json, require_artifact_ref
from ragos.storage.index_projection import projection_digest, projection_from_chunk
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.search import SearchChunkUpsert


class PageSpan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    page_no: int = Field(ge=1, strict=True)
    start: int = Field(ge=0, strict=True)
    end: int = Field(ge=0, strict=True)


class FrozenRepresentation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    document_version_id: UUID
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_type: Literal["pdf", "pptx", "docx", "markdown", "html", "plain"]
    parser_version: str = Field(min_length=1, max_length=128)
    render_version: str = Field(min_length=1, max_length=128)
    parent_unit: Literal["page", "token_window"]
    text: str
    pages: tuple[PageSpan, ...] = ()

    @model_validator(mode="after")
    def validate_pages(self):
        if self.parent_unit == "page":
            if not self.pages or self.source_type not in {"pdf", "pptx", "docx"}:
                raise ValueError("page representation requires frozen paginated source")
            if self.source_type == "docx" and self.render_version == "none":
                raise ValueError("DOCX page representation requires a frozen render")
            previous_end = 0
            for expected, page in enumerate(self.pages, start=1):
                if page.page_no != expected or not previous_end <= page.start <= page.end <= len(
                    self.text
                ):
                    raise ValueError(
                        "page spans must be contiguous identities with ordered in-bounds spans"
                    )
                previous_end = page.end
        elif self.pages:
            raise ValueError("token-window representation has no page navigation")
        return self

    def frozen_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))


class PlannedChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    parent_chunk_id: UUID | None
    is_parent: bool
    body: str = Field(min_length=1)
    start_char_idx: int = Field(ge=0, strict=True)
    end_char_idx: int = Field(ge=1, strict=True)
    page_no: int | None = Field(default=None, ge=1, strict=True)
    page_offset_start: int | None = Field(default=None, ge=0, strict=True)
    page_offset_end: int | None = Field(default=None, ge=1, strict=True)
    element_type: Literal["text", "heading", "table", "figure", "list", "code", "caption"]
    oversize: bool = False


class IndexPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    document_version_id: UUID
    representation_id: UUID
    index_id: UUID
    tier: Literal["stable", "hot"]
    source_ref: str
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    representation_ref: str
    representation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunker_version: str = Field(min_length=1, max_length=64)
    chunker_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_profile: str = Field(min_length=1, max_length=64)
    embedding_version: str = Field(min_length=1, max_length=128)
    parent_unit: Literal["page", "token_window"]
    representation_char_count: int = Field(ge=1, strict=True)
    page_count: int | None = Field(default=None, ge=1, strict=True)
    expected_active_index_id: UUID | None
    chunks: tuple[PlannedChunk, ...] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def validate_plan(self):
        require_artifact_ref(self.source_ref, self.tenant_id)
        require_artifact_ref(self.representation_ref, self.tenant_id)
        if (self.parent_unit == "page") != (self.page_count is not None):
            raise ValueError("index pagination must match its representation")
        by_id = {chunk.id: chunk for chunk in self.chunks}
        if len(by_id) != len(self.chunks):
            raise ValueError("plan chunk identifiers must be unique")
        for chunk in self.chunks:
            if not 0 <= chunk.start_char_idx < chunk.end_char_idx <= self.representation_char_count:
                raise ValueError("plan chunk span exceeds representation")
            if len(chunk.body) != chunk.end_char_idx - chunk.start_char_idx:
                raise ValueError("plan original text and span length disagree")
            if chunk.is_parent:
                if chunk.parent_chunk_id is not None:
                    raise ValueError("parent chunk cannot reference another parent")
            else:
                parent = by_id.get(chunk.parent_chunk_id)
                if parent is None or not parent.is_parent:
                    raise ValueError("child must reference a parent in the same frozen plan")
                start = chunk.start_char_idx - parent.start_char_idx
                end = chunk.end_char_idx - parent.start_char_idx
                if not 0 <= start < end <= len(parent.body) or parent.body[start:end] != chunk.body:
                    raise ValueError("child source is not its parent original text")
            if self.parent_unit == "page":
                if chunk.page_no is None or chunk.page_no > self.page_count:
                    raise ValueError("chunk page is unavailable in its representation")
                if chunk.page_offset_start is None or chunk.page_offset_end is None:
                    raise ValueError("page chunk requires page-relative coordinates")
                if chunk.page_offset_end - chunk.page_offset_start != len(chunk.body):
                    raise ValueError("page-relative span disagrees with original text")
                if not chunk.is_parent and by_id[chunk.parent_chunk_id].page_no != chunk.page_no:
                    raise ValueError("child must remain on its page parent")
            elif any(
                value is not None
                for value in (chunk.page_no, chunk.page_offset_start, chunk.page_offset_end)
            ):
                raise ValueError("token-window chunks cannot claim page navigation")
        return self

    def verify_representation(self, representation: FrozenRepresentation) -> None:
        if hashlib.sha256(representation.frozen_bytes()).hexdigest() != self.representation_digest:
            raise ValidationError("representation bytes do not match frozen index plan digest")
        if (
            representation.document_version_id != self.document_version_id
            or representation.source_digest != self.source_digest
            or representation.parent_unit != self.parent_unit
            or len(representation.text) != self.representation_char_count
            or (len(representation.pages) if representation.pages else None) != self.page_count
        ):
            raise ValidationError("index plan disagrees with its frozen representation")
        pages = {page.page_no: page for page in representation.pages}
        for chunk in self.chunks:
            if representation.text[chunk.start_char_idx : chunk.end_char_idx] != chunk.body:
                raise ValidationError("chunk original text does not match representation bytes")
            if self.parent_unit == "page":
                page = pages[chunk.page_no]
                if (page.start + chunk.page_offset_start, page.start + chunk.page_offset_end) != (
                    chunk.start_char_idx,
                    chunk.end_char_idx,
                ):
                    raise ValidationError("page-relative and document-global coordinates disagree")
                if chunk.end_char_idx > page.end:
                    raise ValidationError("chunk crosses its frozen page boundary")
                if chunk.is_parent and (chunk.start_char_idx, chunk.end_char_idx) != (
                    page.start,
                    page.end,
                ):
                    raise ValidationError("page parent must retain its whole page original text")

    def frozen_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def digest(self) -> str:
        return hashlib.sha256(self.frozen_bytes()).hexdigest()

    def projection_digest(self, tenant_ctx: TenantContext) -> str:
        chunks = self.as_upserts(tenant_ctx)
        return projection_digest(
            [
                projection_from_chunk(
                    chunk,
                    representation_char_count=self.representation_char_count,
                    parent_unit=self.parent_unit,
                )
                for chunk in chunks
            ]
        )

    def as_upserts(self, tenant_ctx: TenantContext) -> list[SearchChunkUpsert]:
        tenant_ctx.require_scope_key()
        if self.tenant_id != tenant_ctx.tenant_id:
            raise PermissionDeniedError("index plan belongs to another tenant")
        if tenant_ctx.allowed_kb_ids is not None and self.kb_id not in tenant_ctx.allowed_kb_ids:
            raise PermissionDeniedError("index knowledge base is not authorized")
        return [
            SearchChunkUpsert(
                id=chunk.id,
                tenant_id=self.tenant_id,
                kb_id=self.kb_id,
                document_id=self.document_id,
                document_version_id=self.document_version_id,
                representation_id=self.representation_id,
                index_id=self.index_id,
                representation_char_count=self.representation_char_count,
                parent_unit=self.parent_unit,
                page_count=self.page_count,
                parent_chunk_id=chunk.parent_chunk_id,
                is_parent=chunk.is_parent,
                body=chunk.body,
                start_char_idx=chunk.start_char_idx,
                end_char_idx=chunk.end_char_idx,
                page_no=chunk.page_no,
                page_offset_start=chunk.page_offset_start,
                page_offset_end=chunk.page_offset_end,
                element_type=chunk.element_type,
                layer="L1" if self.tier == "stable" else "L3",
                lang="simple",
                title=None,
                headings_text=None,
                tags_text=None,
                metadata_json={"oversize": chunk.oversize},
                updated_at=utc_now(),
            )
            for chunk in self.chunks
        ]
