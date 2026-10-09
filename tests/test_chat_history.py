"""Stored citation DTO probes; repository authorization is mocked, not real RLS."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ragos.core.errors import NotFoundError
from ragos.core.tenant import TenantContext
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.verify import SemanticVerdict, StructuralVerdict
from ragos.runtime import chat_history
from ragos.storage.models.base import utc_now


def _fixture():
    ctx = TenantContext(
        tenant_id=uuid4(), tenant_slug="test", actor_id="actor", trace_id="trace"
    ).bind_user(uuid4())
    identity = EvidenceIdentity(
        tenant_id=ctx.tenant_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        document_version_id=uuid4(),
        representation_id=uuid4(),
        index_id=uuid4(),
        chunk_id=uuid4(),
        representation_char_count=4,
        parent_unit="page",
        page_count=1,
    )
    record = dict(
        chunk_id=str(identity.chunk_id),
        doc_id=str(identity.document_id),
        page_no=1,
        start_char_idx=0,
        end_char_idx=4,
        claim="a fact",
        text="text",
        score=0.8,
        source_identity=identity.to_dict(),
        status="verified",
        status_reason="entailed",
        structural_verdict=StructuralVerdict.OK.value,
        semantic_verdict=SemanticVerdict.ENTAILED.value,
    )
    return ctx, record


def test_full_stored_verification_is_disclosed_as_history_not_current_source_availability():
    ctx, record = _fixture()
    result = chat_history.normalize_stored_citations([record], ctx)[0]
    assert result["status"] == "verified"
    assert result["verification_origin"] == "stored_record"
    assert result["source_available"] is False
    assert result["source_identity"] == record["source_identity"]


@pytest.mark.parametrize("missing", ["source_identity", "structural_verdict", "semantic_verdict"])
def test_legacy_verified_cannot_supply_missing_verification_facts(missing):
    ctx, record = _fixture()
    record.pop(missing)
    result = chat_history.normalize_stored_citations([record], ctx)[0]
    assert result["status"] == "unverified"
    assert result["structural_verdict"] == result["semantic_verdict"] == "not_checked"
    assert result["source_identity"] is result["page_no"] is result["chunk_id"] is None
    assert result["claim"] == "a fact"
    assert result["text"] == "text"


@pytest.mark.parametrize(
    "changes",
    [
        {"chunk_id": str(uuid4())},
        {"doc_id": str(uuid4())},
        {"page_no": 2},
        {"start_char_idx": True},
        {"end_char_idx": 5},
        {"text": "different"},
        {"structural_verdict": "invented"},
        {"semantic_verdict": []},
    ],
)
def test_invalid_stored_pointer_or_verdict_never_claims_verified(changes):
    ctx, record = _fixture()
    record.update(changes)
    result = chat_history.normalize_stored_citations([record], ctx)[0]
    assert result["status"] == "unverified"
    assert result["source_identity"] is None
    assert result["page_no"] is None


@pytest.mark.parametrize("malformed", [False, True])
def test_cross_tenant_identity_redacts_original_claim_and_text_even_if_malformed(malformed):
    ctx, record = _fixture()
    record["source_identity"]["tenant_id"] = str(uuid4())
    if malformed:
        record["source_identity"].pop("index_id")
    result = chat_history.normalize_stored_citations([record], ctx)[0]
    assert result["claim"] == result["text"] == ""
    assert result["source_identity"] is None
    assert result["status"] == "unverified"


def test_suppressed_is_not_upgraded_and_inputs_are_never_mutated():
    ctx, record = _fixture()
    record["status"] = "suppressed"
    original = copy.deepcopy(record)
    result = chat_history.normalize_stored_citations([record, None, "bad"], ctx)
    assert len(result) == 1
    assert result[0]["status"] == "suppressed"
    assert record == original


def test_structurally_failed_source_never_keeps_semantic_entailed_or_navigation():
    ctx, record = _fixture()
    record["structural_verdict"] = StructuralVerdict.UNKNOWN_CHUNK.value
    result = chat_history.normalize_stored_citations([record], ctx)[0]
    assert result["status"] == "unverified"
    assert result["semantic_verdict"] == "not_checked"
    assert result["chunk_id"] is result["source_identity"] is None


def test_mock_history_service_uses_private_scope_and_preserves_repository_denial(monkeypatch):
    ctx, record = _fixture()
    conversation = uuid4()
    calls = []

    class Repository:
        def __init__(self, session, tenant_ctx):
            assert session == "synthetic-session"
            assert tenant_ctx is ctx

        def list_for_conversation(self, **kwargs):
            calls.append(kwargs)
            if kwargs["conversation_id"] != conversation:
                raise NotFoundError("Conversation not found")
            return [
                SimpleNamespace(
                    id=uuid4(),
                    role="assistant",
                    content="answer",
                    citations=[record],
                    run_id=None,
                    created_at=utc_now(),
                )
            ]

    monkeypatch.setattr(chat_history, "MessageRepository", Repository)
    service = chat_history.MessageHistoryService("synthetic-session", ctx)
    result = service.list(conversation_id=conversation)
    assert calls[0]["scope_key"] == ctx.require_private_scope_key()
    assert result[0]["citations"][0]["verification_origin"] == "stored_record"
    with pytest.raises(NotFoundError):
        service.list(conversation_id=uuid4())
