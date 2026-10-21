"""Operator-only real Chat+Embedding probe on a generated genuine PDF.

Only synthetic data is passed through the provider. The result is a report
of *one execution*, not a production inference benchmark or tenant gateway.
"""

from __future__ import annotations

import hashlib
from typing import Any

from qdrant_client import QdrantClient
from ragos.model_gateway.providers import BaseProvider
from ragos.retrieval.local_probe import PAGES, run_probe
from ragos.retrieval.synthetic_pdf import make_synthetic_pdf
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec


def run_synthetic_pdf_model_probe(
    provider: BaseProvider,
    *,
    embedding_spec: FrozenEmbeddingSpec,
    chat_model: str,
    client: QdrantClient | None = None,
    collection_prefix: str = "ragos_local",
) -> dict[str, Any]:
    """Run real PDF extraction and models with an explicit frozen vector size.

    `embedding_spec.model_digest` is supplied by the operator: this path
    cannot cryptographically attest which weights served the API request.
    """
    pdf_bytes = make_synthetic_pdf([text for _, text in PAGES])
    result = run_probe(
        provider,
        embedding_model=embedding_spec.model,
        chat_model=chat_model,
        embedding_version=embedding_spec.version,
        expected_embedding_dim=embedding_spec.dimension,
        embedding_profile=embedding_spec.profile,
        client=client,
        collection_prefix=collection_prefix,
        pdf_bytes=pdf_bytes,
    )
    return {
        "schema_version": "s1-local-pdf-model-smoke-v1",
        "source_kind": "generated_synthetic_pdf_bytes",
        "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "source_page_count": len(PAGES),
        "embedding_model": embedding_spec.model,
        "embedding_revision_id": embedding_spec.model_digest,
        "embedding_dimension": embedding_spec.dimension,
        "embedding_profile": embedding_spec.profile,
        "embedding_version": embedding_spec.version,
        "chat_model": chat_model,
        "vector_store": (
            "operator_provided_local_qdrant"
            if client is not None else "embedded_qdrant"
        ),
        "retrieved_page_numbers": list(result.retrieved_pages),
        "hit_at_2": result.hit_at_2,
        "structural_citation_verdicts": list(result.structural_verdicts),
        "semantic_verification": result.semantic_verification,
        "readback_digest": result.readback_digest,
        "model_weight_attestation": "not_verified",
        "production_gateway_verification": "not_performed",
        "evaluation_scope": "one_synthetic_question_only",
    }
