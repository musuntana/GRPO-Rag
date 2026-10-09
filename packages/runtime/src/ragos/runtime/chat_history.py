"""Authorized history DTOs disclose stored verdicts without re-verifying evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from uuid import UUID

from ragos.core.tenant import TenantContext
from ragos.retrieval.citation import CitationStatus
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.verify import SemanticVerdict, StructuralVerdict
from ragos.storage.repositories.conversations import MessageRepository

NOT_CHECKED = "not_checked"


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize(record: Mapping, tenant_ctx: TenantContext) -> dict:
    suppressed = record.get("status") == CitationStatus.SUPPRESSED.value
    dto = {
        "chunk_id": None,
        "doc_id": None,
        "page_no": None,
        "start_char_idx": None,
        "end_char_idx": None,
        "claim": record.get("claim") if isinstance(record.get("claim"), str) else "",
        "text": record.get("text") if isinstance(record.get("text"), str) else "",
        "score": 0.0,
        "element_type": "text",
        "marker_index": -1,
        "status": CitationStatus.SUPPRESSED.value
        if suppressed
        else CitationStatus.UNVERIFIED.value,
        "status_reason": "stored_source_unverified",
        "source_identity": None,
        "structural_verdict": NOT_CHECKED,
        "semantic_verdict": NOT_CHECKED,
        "verification_origin": "stored_record",
        "source_available": False,
    }
    raw_identity = record.get("source_identity")
    if isinstance(raw_identity, Mapping):
        # Check the claimed owner even if another identity field is malformed.
        if _uuid(raw_identity.get("tenant_id")) != tenant_ctx.tenant_id:
            dto.update(claim="", text="", status_reason="stored_source_scope_mismatch")
            return dto
        if (
            tenant_ctx.allowed_kb_ids is not None
            and _uuid(raw_identity.get("kb_id")) not in tenant_ctx.allowed_kb_ids
        ):
            dto.update(claim="", text="", status_reason="stored_source_scope_mismatch")
            return dto
    try:
        identity = EvidenceIdentity.model_validate(raw_identity)
    except (ValueError, TypeError):
        return dto
    if identity.tenant_id != tenant_ctx.tenant_id:
        dto.update(claim="", text="", status_reason="stored_source_scope_mismatch")
        return dto
    start, end, page = (
        record.get("start_char_idx"),
        record.get("end_char_idx"),
        record.get("page_no"),
    )
    if (
        _uuid(record.get("chunk_id")) != identity.chunk_id
        or _uuid(record.get("doc_id")) != identity.document_id
        or not _integer(start)
        or not _integer(end)
        or not 0 <= start < end <= identity.representation_char_count
        or not isinstance(record.get("text"), str)
        or len(record["text"]) != end - start
    ):
        return dto
    if identity.parent_unit == "page":
        if not _integer(page) or not 1 <= page <= identity.page_count:
            return dto
    elif page is not None:
        return dto
    structural, semantic = record.get("structural_verdict"), record.get("semantic_verdict")
    if not isinstance(structural, str) or not isinstance(semantic, str):
        return dto
    if structural not in {v.value for v in StructuralVerdict} or semantic not in {
        NOT_CHECKED,
        *(v.value for v in SemanticVerdict),
    }:
        return dto
    dto["structural_verdict"] = structural
    dto["semantic_verdict"] = semantic if structural == StructuralVerdict.OK.value else NOT_CHECKED
    if structural != StructuralVerdict.OK.value:
        dto["status_reason"] = structural
        return dto
    dto.update(
        chunk_id=str(identity.chunk_id),
        doc_id=str(identity.document_id),
        page_no=page,
        start_char_idx=start,
        end_char_idx=end,
        source_identity=identity.to_dict(),
        status_reason="stored_semantic_not_checked" if semantic == NOT_CHECKED else semantic,
    )
    score = record.get("score")
    if (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and (not isinstance(score, int) or score.bit_length() < 1024)
        and math.isfinite(score)
    ):
        dto["score"] = score
    if isinstance(record.get("element_type"), str) and record.get("element_type") in {
        "text",
        "heading",
        "table",
        "figure",
        "list",
        "code",
        "caption",
    }:
        dto["element_type"] = record["element_type"]
    if _integer(record.get("marker_index")):
        dto["marker_index"] = record["marker_index"]
    if (
        not suppressed
        and record.get("status") == CitationStatus.VERIFIED.value
        and semantic == SemanticVerdict.ENTAILED.value
    ):
        dto["status"] = CitationStatus.VERIFIED.value
    return dto


def normalize_stored_citations(raw, tenant_ctx: TenantContext) -> list[dict]:
    """Normalize saved DTOs; no source lookup, judge, mutation, or new verification."""
    tenant_ctx.require_private_scope_key()
    if not isinstance(raw, list):
        return []
    return [_normalize(record, tenant_ctx) for record in raw if isinstance(record, Mapping)]


class MessageHistoryService:
    def __init__(self, session, tenant_ctx: TenantContext):
        tenant_ctx.require_user_id()
        self.tenant_ctx = tenant_ctx
        self.messages = MessageRepository(session, tenant_ctx)

    def list(self, *, conversation_id: UUID, limit: int = 50) -> list[dict]:
        rows = self.messages.list_for_conversation(
            scope_key=self.tenant_ctx.require_private_scope_key(),
            conversation_id=conversation_id,
            limit=limit,
        )
        return [
            {
                "message_id": str(row.id),
                "role": row.role,
                "content": row.content,
                "citations": normalize_stored_citations(row.citations, self.tenant_ctx),
                "run_id": str(row.run_id) if row.run_id else None,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
