"""Convert a parsed PDF into the existing immutable index protocol.

This module does not authorize uploads, run an ingestion worker, or publish a
document. It assembles strictly validated inputs for IndexPreparationService
and IngestionService; those services own the transactional dual-store lifecycle.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from ragos.core.errors import PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.retrieval.parse.layout import ParsedDocument, SourceType, validate
from ragos.retrieval.parse.page_chunker import ChunkerConfig, PageChunker
from ragos.runtime.index_plan import FrozenRepresentation, IndexPlan, PageSpan, PlannedChunk
from ragos.storage.artifacts import canonical_json


def freeze_pdf_representation(
    document: ParsedDocument,
    *,
    document_version_id: UUID,
    source_digest: str,
) -> FrozenRepresentation:
    """Freeze 1-based PDF pages and their original document-global char spans."""
    validate(document)
    if document.source_type != SourceType.PDF or document.parent_unit != "page":
        raise ValidationError("PDF index representation requires a paginated PDF")
    if not document.pages or any(page.is_empty() for page in document.pages):
        raise ValidationError("PDF index representation requires text on every page")
    cursor = 0
    pages: list[PageSpan] = []
    for page in document.pages:
        text = page.text()
        pages.append(PageSpan(page_no=page.page_no, start=cursor, end=cursor + len(text)))
        cursor += len(text) + 2  # canonical ParsedDocument.flat_text() page separator
    frozen = FrozenRepresentation(
        schema_version=1,
        document_version_id=document_version_id,
        source_digest=source_digest,
        source_type="pdf",
        parser_version=document.parser_version,
        render_version="none",
        parent_unit="page",
        text=document.flat_text(),
        pages=tuple(pages),
    )
    if pages[-1].end != len(frozen.text):
        raise ValidationError("PDF page offsets disagree with frozen representation")
    return frozen


def build_pdf_index_plan(
    document: ParsedDocument,
    *,
    representation: FrozenRepresentation,
    tenant_ctx: TenantContext,
    kb_id: UUID,
    document_id: UUID,
    representation_id: UUID,
    index_id: UUID,
    source_ref: str,
    representation_ref: str,
    representation_digest: str,
    embedding_profile: str,
    embedding_version: str,
    expected_active_index_id: UUID | None = None,
    chunker_config: ChunkerConfig | None = None,
) -> IndexPlan:
    """Use the actual page chunker for both parent and child persisted records.

    We reject synthesized/repeated table-header child text or otherwise
    non-round-tripping offsets: index-plan citation ranges must always refer
    to literal source text. Layout repairs need a separately versioned
    representation instead of pretending a generated string was in the PDF.
    """
    tenant_ctx.require_private_scope_key()
    if tenant_ctx.allowed_kb_ids is not None and kb_id not in tenant_ctx.allowed_kb_ids:
        raise PermissionDeniedError("PDF knowledge base is outside authorized tenant scope")
    if representation.source_type != "pdf" or representation.parent_unit != "page":
        raise ValidationError("index plan requires frozen PDF page representation")
    expected = freeze_pdf_representation(
        document,
        document_version_id=representation.document_version_id,
        source_digest=representation.source_digest,
    )
    if representation != expected:
        raise ValidationError("parsed PDF disagrees with registered frozen representation")
    if hashlib.sha256(representation.frozen_bytes()).hexdigest() != representation_digest:
        raise ValidationError("PDF representation digest does not match frozen bytes")

    config = chunker_config or ChunkerConfig(
        parent_unit="page", parser_version=document.parser_version
    )
    config.validate()
    if config.parent_unit != "page" or config.parser_version != document.parser_version:
        raise ValidationError("PDF chunker profile does not match the frozen parser")
    nodes = PageChunker(config).chunk(document)
    by_id = {node.chunk_id: node for node in nodes}
    if not nodes or len(by_id) != len(nodes):
        raise ValidationError("PDF chunker has no unique source evidence")
    pages = {p.page_no: p for p in representation.pages}
    chunks: list[PlannedChunk] = []
    for node in nodes:
        start = node.start_char_idx
        end = node.end_char_idx
        page = pages.get(node.page_no)
        if (
            start is None or end is None or page is None
            or node.page_offset_start is None or node.page_offset_end is None
            or not 0 <= start < end <= len(representation.text)
            or not 0 <= node.page_offset_start < node.page_offset_end <= page.end - page.start
            or representation.text[start:end] != node.text
            or (page.start + node.page_offset_start, page.start + node.page_offset_end)
            != (start, end)
            or (
                node.is_parent and (start, end) != (page.start, page.end)
            )
            or (
                not node.is_parent
                and (
                    node.parent_chunk_id not in by_id
                    or not by_id[node.parent_chunk_id].is_parent
                    or by_id[node.parent_chunk_id].page_no != node.page_no
                )
            )
        ):
            raise ValidationError("PDF source chunk does not round-trip to frozen page text")
        chunks.append(
            PlannedChunk(
                id=node.chunk_id,
                parent_chunk_id=node.parent_chunk_id,
                is_parent=node.is_parent,
                body=node.text,
                start_char_idx=start,
                end_char_idx=end,
                page_no=node.page_no,
                page_offset_start=node.page_offset_start,
                page_offset_end=node.page_offset_end,
                element_type=node.element_type,
                oversize=False,
            )
        )

    chunker_version = config.version_id()
    if len(chunker_version) > 64:
        raise ValidationError("PDF chunker version exceeds persistent index limit")
    config_digest = hashlib.sha256(
        canonical_json(
            {
                "child_size": config.child_size,
                "parent_size": config.parent_size,
                "parent_unit": config.parent_unit,
                "parser_version": config.parser_version,
            }
        )
    ).hexdigest()
    plan = IndexPlan(
        schema_version=1,
        tenant_id=tenant_ctx.tenant_id,
        kb_id=kb_id,
        document_id=document_id,
        document_version_id=representation.document_version_id,
        representation_id=representation_id,
        index_id=index_id,
        tier="stable",
        source_ref=source_ref,
        source_digest=representation.source_digest,
        representation_ref=representation_ref,
        representation_digest=representation_digest,
        chunker_version=chunker_version,
        chunker_config_digest=config_digest,
        embedding_profile=embedding_profile,
        embedding_version=embedding_version,
        parent_unit="page",
        representation_char_count=len(representation.text),
        page_count=len(representation.pages),
        expected_active_index_id=expected_active_index_id,
        chunks=tuple(chunks),
    )
    plan.verify_representation(representation)
    plan.as_upserts(tenant_ctx)
    return plan
