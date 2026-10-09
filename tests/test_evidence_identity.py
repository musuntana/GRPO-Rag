"""Synthetic lineage probes: no fixture stands for an indexed real document."""

from dataclasses import replace
from uuid import uuid4

import pytest
from ragos.core.errors import ValidationError
from ragos.retrieval.citation import Citation, EvidenceSet
from ragos.retrieval.expander import dedupe_overlapping_parents
from ragos.retrieval.fusion import Fusioner
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.types import FusionConfig, ScoredChunk
from ragos.retrieval.verify import StructuralVerdict, verify_structural


def _source():
    identity = EvidenceIdentity(
        tenant_id=uuid4(),
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
    chunk = ScoredChunk(
        chunk_id=identity.chunk_id,
        doc_id=identity.document_id,
        kb_id=identity.kb_id,
        text="enriched display",
        source_text="text",
        source_identity=identity,
        score=1,
        channel="dense",
        page_no=1,
        page_count=1,
        start_char_idx=0,
        end_char_idx=4,
    )
    citation = Citation(
        chunk_id=chunk.chunk_id,
        doc_id=chunk.doc_id,
        page_no=1,
        start_char_idx=0,
        end_char_idx=4,
        claim="a fact",
        text="text",
        source_identity=identity,
    )
    return chunk, citation


@pytest.mark.parametrize("dimension", ["document_version_id", "representation_id", "index_id"])
def test_wrong_source_version_is_not_structurally_verified(dimension):
    chunk, citation = _source()
    citation.source_identity = citation.source_identity.model_copy(update={dimension: uuid4()})
    assert (
        verify_structural(citation, EvidenceSet((chunk,))).verdict
        == StructuralVerdict.IDENTITY_MISMATCH
    )


@pytest.mark.parametrize(
    "change",
    [
        {"doc_id": uuid4()},
        {"text": "fake"},
        {"end_char_idx": 5},
        {"page_no": 2},
    ],
)
def test_source_pointer_or_original_text_cannot_be_substituted(change):
    chunk, citation = _source()
    assert not verify_structural(replace(citation, **change), EvidenceSet((chunk,))).ok


def test_legacy_page_count_cannot_authorize_an_unversioned_citation():
    chunk, citation = _source()
    citation.source_identity = None
    assert not verify_structural(citation, EvidenceSet((chunk,), {chunk.doc_id: 99})).ok


def test_fusion_preserves_original_text_while_scores_change():
    chunk, _ = _source()
    fused = Fusioner().fuse({"dense": [chunk]}, FusionConfig())
    assert fused[0].source_text == "text"
    assert fused[0].source_identity == chunk.source_identity


def test_same_chunk_id_cannot_merge_conflicting_index_identities():
    chunk, _ = _source()
    other = replace(
        chunk, source_identity=chunk.source_identity.model_copy(update={"index_id": uuid4()})
    )
    with pytest.raises(ValidationError, match="conflicting evidence lineage"):
        Fusioner().fuse({"dense": [chunk], "lexical": [other]}, FusionConfig())


def test_same_document_page_in_two_representations_is_not_deduplicated():
    chunk, _ = _source()
    identity = chunk.source_identity.model_copy(
        update={"representation_id": uuid4(), "chunk_id": uuid4()}
    )
    other = replace(chunk, chunk_id=identity.chunk_id, source_identity=identity)
    assert len(dedupe_overlapping_parents([chunk, other])) == 2
