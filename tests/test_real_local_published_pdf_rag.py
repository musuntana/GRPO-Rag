"""Operator acceptance: REAL local models over a published PG/Qdrant PDF corpus.

This validation is skipped by default. It runs only when an operator exports
``RAGOS_RUN_REAL_LOCAL_MODELS=1`` and explicitly supplies a disposable migrated
PostgreSQL server (existing ``database`` fixture), a disposable local Qdrant
HTTP server (existing ``qdrant_url`` fixture) and the operator-attested local
model identity; see :func:`operator_env` for the exact variable names.

Everything on the identity chain is real: generated synthetic PDF bytes are
parsed with real Poppler, every embedding call goes through the real
``ModelRouter`` (real Provider / ModelProfile / tenant use-case binding rows,
the real profile resolver and local admission — no direct provider fallback),
publication reuses the frozen IngestionService runtime, and recall is rebuilt
through ``build_recall_channels`` against a separate Qdrant HTTP reader with
exact PostgreSQL hydration.

The evidence file contains hashes, counts and timing only: never model answers,
vectors, document text, DSNs or credentials. Model identity is operator
attested; nothing here attests weights cryptographically, and nothing here
measures retrieval quality against a human-audited gold set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from ragos.core.config import Settings
from ragos.core.errors import NotFoundError, ValidationError
from ragos.model_gateway.local_admission import LocalModelAdmission
from ragos.model_gateway.providers.openai_compatible import _http_json
from ragos.model_gateway.router import ModelRouter
from ragos.retrieval.recall.dense import DenseRecall
from ragos.retrieval.types import RetrieverProfile
from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec
from ragos.runtime.retrieval import build_recall_channels, dense_collection_name
from ragos.storage.db import bind_session_tenant
from ragos.storage.models.app import (
    ChunkManifest,
    DocumentRecord,
    ModelProfile,
    Provider,
    TenantModelBinding,
)
from ragos.storage.repositories.search import SearchRepository, SearchTier
from ragos.storage.vector import QdrantVectorIndexWriter
from sqlmodel import Session
from test_pdf_pg_index_integration import _real_pdf_fixture
from test_qdrant_server_pg_reconnect import qdrant_url
from test_storage_integration import context, database, resources, scoped_session

__all__ = ["database", "qdrant_url"]

MODEL_GATE_ENV = "RAGOS_RUN_REAL_LOCAL_MODELS"
EVIDENCE_ENV = "RAGOS_S1PE_EVIDENCE_PATH"
MODEL_HTTP_BUDGET = 20
WALL_SECONDS_BUDGET = 180


class _ModelHTTPLedger:
    """Count real model HTTP calls while delegating to the production transport.

    Instrumentation only: requests are forwarded unchanged to the real
    ``_http_json`` transport, nothing is fabricated, and no authorization path
    is monkey-patched. Persisted credentials must never appear in headers.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.deadline = time.monotonic() + WALL_SECONDS_BUDGET

    @property
    def count(self) -> int:
        return len(self.paths)

    def __call__(self, url, payload, headers, timeout):
        if "Authorization" in headers:
            raise AssertionError("local model transport must never send stored credentials")
        if self.count >= MODEL_HTTP_BUDGET:
            raise ValidationError("model HTTP budget exhausted")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValidationError("model wall-time budget exhausted")
        self.paths.append(urlsplit(url).path)
        return _http_json(url, payload, headers, min(timeout, remaining))


class RouterAuthoritativeEmbedding:
    """Fixture seam for ``_real_pdf_fixture`` that routes through the real router.

    ``LocalFrozenEmbedding`` is the direct-provider local-probe bridge; this
    adapter is its tenant-bound counterpart: every execution call opens a fresh
    RLS-bound session, constructs the real ``ModelRouter`` and resolves the
    tenant's real model profile (local admission is rechecked on every call).
    The frozen ``FrozenEmbeddingSpec`` is the single source for the plan
    identity, writer identity, PG manifest identity and the query-side dense
    channel identity.
    """

    def __init__(self, engine, admission, spec, ledger) -> None:
        self._engine = engine
        self._admission = admission
        self.spec = spec
        self._ledger = ledger
        self._ctx = None

    def bind_tenant_ctx(self, tenant_ctx) -> None:
        self._ctx = tenant_ctx

    def embed_via_router(self, text: str) -> list[float]:
        if self._ctx is None:
            raise ValidationError("router embedding requires an explicit tenant context")
        with Session(self._engine, expire_on_commit=False) as session:
            bind_session_tenant(session, self._ctx.tenant_id)
            with session.begin():
                router = ModelRouter(session, local_model=self._admission)
                result = router.embed(self._ctx, texts=[text], use_case="embed")
        vectors = result.embeddings
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise ValidationError("router embedding returned an unexpected vector count")
        vector = vectors[0]
        if (
            not isinstance(vector, list)
            or len(vector) != self.spec.dimension
            or any(not isinstance(x, (int, float)) or isinstance(x, bool) for x in vector)
            or not any(x != 0 for x in vector)
        ):
            raise ValidationError("router embedding differs from the frozen model identity")
        return [float(x) for x in vector]

    def require_plan(self, plan) -> None:
        if (
            plan.embedding_profile != self.spec.profile
            or plan.embedding_version != self.spec.version
        ):
            raise ValidationError("index embedding identity differs from pinned model")

    def writer(self, client, *, collection_prefix: str = "ragos") -> QdrantVectorIndexWriter:
        return QdrantVectorIndexWriter(
            client=client,
            embedding_dim=self.spec.dimension,
            embedding_fn=self.embed_via_router,
            embedding_profile=self.spec.profile,
            embedding_version=self.spec.version,
            collection_prefix=collection_prefix,
        )

    def dense_channel(self, session, *, qdrant_client, settings) -> DenseRecall:
        """build_recall_channels with the real tenant router on the caller session."""
        router = ModelRouter(session, local_model=self._admission)
        channels = build_recall_channels(
            session=session,
            tenant_ctx=self._ctx,
            settings=settings,
            router=router,
            qdrant_client=qdrant_client,
            dense=True,
        )
        return channels["dense"]


def _retriever_profile(tenant_id):
    return RetrieverProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        recall_channels=["dense"],
        channel_budgets={"dense": 1.0},
        top_k_recall=10,
    )


def _register_local_model_rows(engine, tenant_ctx, provider_id, base_url, model_name):
    """Real Provider / ModelProfile / use-case binding rows inside the RLS scope.

    The provider row is platform-level data; profile and binding are tenant
    scoped rows written through the tenant-bound session so RLS applies.
    """
    profile_id = uuid4()
    with scoped_session(engine, tenant_ctx) as session:
        provider = session.get(Provider, provider_id)
        if provider is None:
            session.add(
                Provider(
                    id=provider_id,
                    slug=f"local_openai_{uuid4().hex[:16]}",
                    provider_type="local_openai",
                    display_name="S1 published-PDF local model acceptance",
                    base_url=base_url,
                    is_active=True,
                )
            )
            session.flush()
        elif (
            provider.provider_type != "local_openai"
            or provider.base_url != base_url
            or not provider.is_active
        ):
            raise ValidationError("existing test provider identity does not match admission")
        session.add(
            ModelProfile(
                id=profile_id,
                tenant_id=tenant_ctx.tenant_id,
                provider_id=provider_id,
                name=f"embed_profile_{uuid4().hex[:8]}",
                use_case="embed",
                model_name=model_name,
            )
        )
        session.flush()
        session.add(
            TenantModelBinding(
                tenant_id=tenant_ctx.tenant_id,
                profile_id=profile_id,
                use_case="embed",
            )
        )
        session.flush()
    return profile_id


def _evidence_path(tmp_path: Path) -> Path:
    value = os.environ.get(EVIDENCE_ENV)
    path = Path(value).expanduser().absolute() if value else tmp_path / "evidence.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _write_evidence(path, outcome, spec, ledger, elapsed, plan, collection, rows, probes):
    """Content-free provenance: hashes, counts, routes and timing only."""
    record = {
        "outcome": outcome,
        "gate_env": MODEL_GATE_ENV,
        "test_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": plan.source_digest if plan is not None else None,
        "representation_sha256": plan.representation_digest if plan is not None else None,
        "model": spec.model if spec is not None else None,
        "model_revision": spec.model_digest if spec is not None else None,
        "model_identity_scope": "operator_attested_only_not_cryptographic",
        "embedding_dimension": spec.dimension if spec is not None else None,
        "embedding_profile": spec.profile if spec is not None else None,
        "embedding_version": spec.version if spec is not None else None,
        "model_route_paths": sorted(set(ledger.paths)),
        "model_http_calls": ledger.count,
        "model_http_budget": MODEL_HTTP_BUDGET,
        "wall_seconds": round(elapsed, 3),
        "qdrant_namespace": collection,
        "rows": rows,
        "probes": probes,
        "model_answers_or_vectors_or_text_recorded": False,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, indent=2) + "\n")


@pytest.fixture
def operator_env():
    """Explicit operator gate; missing model identity fails closed, never stubs."""
    if os.environ.get(MODEL_GATE_ENV) != "1":
        pytest.skip(f"{MODEL_GATE_ENV}=1 is required to run real local model acceptance")
    if shutil.which("pdftotext") is None:
        pytest.skip("Poppler pdftotext is required for real PDF parsing")

    def required(name):
        value = os.environ.get(name)
        if not value or not value.strip():
            pytest.fail(f"{name} must match the operator's installed model; no fallback")
        return value.strip()

    dimension = required("RAGOS_S1PE_LOCAL_EMBED_DIM")
    if not dimension.isdigit():
        pytest.fail("RAGOS_S1PE_LOCAL_EMBED_DIM must be the frozen integer vector dimension")
    return {
        "base_url": required("RAGOS_S1PE_LOCAL_BASE_URL"),
        "chat_model": required("RAGOS_S1PE_LOCAL_CHAT_MODEL"),
        "embed_model": required("RAGOS_S1PE_LOCAL_EMBED_MODEL"),
        "revision": required("RAGOS_S1PE_LOCAL_EMBED_REVISION"),
        "dimension": int(dimension),
    }


def test_real_local_model_published_pdf_rag_operator_acceptance(
    operator_env, database, qdrant_url, tmp_path, monkeypatch
):  # noqa: C901
    """Poppler -> frozen plan -> PG publish -> Qdrant server -> real router recall."""
    ledger = _ModelHTTPLedger()
    started = time.monotonic()
    outcome = "FAIL"
    plan = collection = spec = None
    reader = writer_client = None
    fixture = None
    owned_collection = False
    stale_hits = foreign_hits = []
    probes = {"passed": False}
    rows = {"owned_tenant_via_fixture": 0}
    evidence_path = _evidence_path(tmp_path)
    try:
        # -- one frozen spec drives plan, writer, manifest and query identity --
        spec = FrozenEmbeddingSpec(
            model=operator_env["embed_model"],
            model_digest=operator_env["revision"],
            dimension=operator_env["dimension"],
            # Random per-run profile gives a dedicated, disposable namespace;
            # the version still encodes model name + revision + dimension.
            profile="s1run_" + uuid4().hex[:12],
        )
        provider_id = uuid4()
        admission = LocalModelAdmission(
            provider_id=provider_id,
            base_url=operator_env["base_url"],
            chat_model=operator_env["chat_model"],
            embedding_model=operator_env["embed_model"],
            embedding_dimension=operator_env["dimension"],
            transport=ledger,
        )
        settings = Settings(
            _env_file=None,
            app_env="dev",
            api_host="127.0.0.1",
            local_model_enabled=True,
            local_model_provider_id=provider_id,
            local_model_base_url=admission.base_url,
            local_model_chat=admission.chat_model,
            local_model_embed=admission.embedding_model,
            local_model_embed_revision=spec.model_digest,
            local_model_embed_dimension=spec.dimension,
            embedding_profile=spec.profile,
            embedding_version=spec.version,
            qdrant_url=qdrant_url,
        )
        # Deployment-level dev gate only (dev API host / explicit loopback);
        # database rows and the real profile resolver still authorize every
        # single call below. No tenant field can enable model access.
        monkeypatch.setattr("ragos.model_gateway.router.get_settings", lambda: settings)

        embedding = RouterAuthoritativeEmbedding(database, admission, spec, ledger)
        fixture = _real_pdf_fixture(database, tmp_path, embedding=embedding)
        embedding.bind_tenant_ctx(fixture.ctx)
        embedding.require_plan(fixture.plan)
        rows["owned_tenant_via_fixture"] = 1
        plan = fixture.plan
        collection = spec.collection()
        assert fixture.writer.embedding_profile == spec.profile
        assert fixture.writer.embedding_version == spec.version

        # Refuse to share one pre-existing physical namespace, before any write.
        precheck = QdrantClient(url=qdrant_url, timeout=5)
        try:
            preexisting = {c.name for c in precheck.get_collections().collections}
            assert collection not in preexisting, (
                "refusing to share an existing Qdrant namespace with this run"
            )
            precheck.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=spec.dimension, distance=Distance.COSINE),
            )
            owned_collection = True
        finally:
            precheck.close()

        # -- real tenant identity rows before any model call --
        _register_local_model_rows(
            database, fixture.ctx, provider_id, admission.base_url, spec.model
        )
        rows["provider"] = 1
        rows["model_profile"] = 1
        rows["tenant_model_binding"] = 1

        # -- publication with writer vectors computed by the real router --
        fixture.client.close()  # discard the shared fixture's in-memory Qdrant
        writer_client = QdrantClient(url=qdrant_url, timeout=5)
        fixture.client = writer_client
        fixture.writer = embedding.writer(writer_client)
        assert fixture.writer.embedding_dim == spec.dimension
        result = fixture.service().execute(manifest_id=fixture.plan.index_id)
        assert result.indexing_state == "indexed"
        assert result.chunk_total == len(fixture.plan.chunks) == 4
        expected_digest = fixture.plan.projection_digest(fixture.ctx)
        with scoped_session(database, fixture.ctx) as session:
            manifest = session.get(ChunkManifest, fixture.plan.index_id)
            doc = session.get(DocumentRecord, fixture.plan.document_id)
            assert manifest.pg_digest == manifest.vector_digest == expected_digest
            assert manifest.reconciled_at is not None
            assert doc.active_index_id == fixture.plan.index_id

        # -- close the writer client; recall through a separate HTTP reader --
        writer_client.close()
        writer_client = None
        reader = QdrantClient(url=qdrant_url, timeout=5)
        readback = embedding.writer(reader)
        assert (
            readback.reconcile(
                tenant_ctx=fixture.ctx,
                tier=SearchTier.STABLE,
                chunks=fixture.plan.as_upserts(fixture.ctx),
            )
            == expected_digest
        )

        # The vector payload never carries the page body: canonical text and
        # source identity come from published PostgreSQL rows only.
        points, _ = reader.scroll(collection_name=collection, limit=10)
        assert len(points) == len(fixture.plan.chunks) == 4
        assert {point.payload["page_no"] for point in points} == {1, 2}
        assert all("body" not in (point.payload or {}) for point in points)
        rows["published_pages_read_back"] = sorted({point.payload["page_no"] for point in points})

        # -- positive recall: build_recall_channels + real ModelRouter embedding --
        calls_before = ledger.count
        with scoped_session(database, fixture.ctx) as session:
            channel = embedding.dense_channel(session, qdrant_client=reader, settings=settings)
            assert channel.collection == dense_collection_name(settings)
            hits = asyncio.run(
                channel.recall(
                    "Contract warranty period is 24 months.",
                    fixture.ctx,
                    _retriever_profile(fixture.ctx.tenant_id),
                )
            )
        assert len(hits) == 4
        assert {hit.page_no for hit in hits} == {1, 2}
        assert {hit.text for hit in hits} == {
            "Contract warranty period is 24 months.",
            "Contract retained deposit is 5 percent.",
        }
        assert all(hit.source_text == hit.text for hit in hits)
        assert {hit.source_identity.index_id for hit in hits} == {fixture.plan.index_id}
        assert all(
            hit.source_identity.representation_id == fixture.plan.representation_id
            and hit.source_identity.document_version_id == fixture.plan.document_version_id
            and hit.source_identity.representation_char_count
            == fixture.plan.representation_char_count
            for hit in hits
        )
        assert ledger.count == calls_before + 1

        # -- wrong embedding version must never backfill PG evidence --
        stale_settings = settings.model_copy(update={"embedding_version": "stale-probe-v0"})
        calls_before = ledger.count
        with scoped_session(database, fixture.ctx) as session:
            with pytest.raises(ValidationError, match="embedding identity"):
                embedding.dense_channel(session, qdrant_client=reader, settings=stale_settings)
        assert ledger.count == calls_before  # refused before any model call
        with scoped_session(database, fixture.ctx) as session:
            channel = embedding.dense_channel(session, qdrant_client=reader, settings=settings)
            stale = DenseRecall(
                reader,
                channel.collection,
                channel.embed,
                source=SearchRepository(session, fixture.ctx),
                tier=SearchTier.STABLE,
                embedding_profile=spec.profile,
                embedding_version="stale-wrong-version-probe",
            )
            stale_hits = asyncio.run(
                stale.recall(
                    "Contract warranty period is 24 months.",
                    fixture.ctx,
                    _retriever_profile(fixture.ctx.tenant_id),
                )
            )
        assert stale_hits == []
        assert ledger.count == calls_before + 1

        # -- unbound tenant: refused before any model call happens --
        unbound_tenant, unbound_kb, _ = resources(database)
        unbound_ctx = context(unbound_tenant, unbound_kb)
        calls_before = ledger.count
        with Session(database, expire_on_commit=False) as session:
            bind_session_tenant(session, unbound_ctx.tenant_id)
            with session.begin(), pytest.raises(NotFoundError, match="profile"):
                router = ModelRouter(session, local_model=admission)
                router.embed(unbound_ctx, texts=["s1pe probe"], use_case="embed")
        assert ledger.count == calls_before
        probes["unbound_tenant_refused_before_model_call"] = True

        # -- foreign tenant with its OWN binding: embeds fine, sees no evidence --
        foreign_tenant, foreign_kb, _ = resources(database)
        foreign_ctx = context(foreign_tenant, foreign_kb)
        _register_local_model_rows(
            database, foreign_ctx, provider_id, admission.base_url, spec.model
        )
        rows["model_profile"] += 1
        rows["tenant_model_binding"] += 1
        foreign_embedding = RouterAuthoritativeEmbedding(database, admission, spec, ledger)
        foreign_embedding.bind_tenant_ctx(foreign_ctx)
        calls_before = ledger.count
        with scoped_session(database, foreign_ctx) as session:
            foreign_channel = foreign_embedding.dense_channel(
                session, qdrant_client=reader, settings=settings
            )
            foreign_hits = asyncio.run(
                foreign_channel.recall(
                    "Contract warranty period is 24 months.",
                    foreign_ctx,
                    _retriever_profile(foreign_ctx.tenant_id),
                )
            )
        assert foreign_hits == []
        assert ledger.count == calls_before + 1
        probes["foreign_tenant_embeds_but_no_evidence"] = True
        probes["wrong_version_refused_before_model_call"] = True
        probes["wrong_version_points_without_pg_backfill"] = True

        # -- operator budgets: bounded HTTP calls and wall clock --
        assert ledger.count <= MODEL_HTTP_BUDGET
        assert ledger.count >= 1, "the real model transport must have been exercised"
        assert time.monotonic() - started <= WALL_SECONDS_BUDGET
        rows["negative_tenant_rows"] = 2
        probes["passed"] = True
        outcome = "PASS"
    finally:
        elapsed = time.monotonic() - started
        cleanup_client = reader if reader is not None else writer_client
        temporary_cleanup = False
        try:
            if owned_collection:
                if cleanup_client is None:
                    cleanup_client = QdrantClient(url=qdrant_url, timeout=5)
                    temporary_cleanup = True
                if collection in {c.name for c in cleanup_client.get_collections().collections}:
                    cleanup_client.delete_collection(collection_name=collection)
                probes["owned_collection_removed"] = True
        except Exception:
            outcome = "FAIL"
            probes["cleanup_failed"] = True
            raise
        finally:
            if temporary_cleanup:
                cleanup_client.close()
            if reader is not None:
                reader.close()
            if writer_client is not None:
                writer_client.close()
            if fixture is not None:
                fixture.client.close()
            _write_evidence(
                evidence_path,
                outcome=outcome,
                spec=spec,
                ledger=ledger,
                elapsed=elapsed,
                plan=plan,
                collection=collection,
                rows=rows,
                probes=probes,
            )


def test_model_http_budget_refuses_before_transport(monkeypatch):
    ledger = _ModelHTTPLedger()
    ledger.paths = ["/v1/embeddings"] * MODEL_HTTP_BUDGET

    def forbidden_transport(*args, **kwargs):
        raise AssertionError("budget exhausted: HTTP transport must not run")

    monkeypatch.setattr(
        __import__(__name__, fromlist=["_http_json"]), "_http_json", forbidden_transport
    )
    with pytest.raises(ValidationError, match="budget"):
        ledger("http://127.0.0.1:12434/v1/embeddings", {}, {}, 1)
    assert ledger.count == MODEL_HTTP_BUDGET


def test_evidence_never_overwrites_an_existing_file(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text("retained prior evidence")
    with pytest.raises(FileExistsError):
        _write_evidence(path, "FAIL", None, _ModelHTTPLedger(), 0, None, None, {}, {})
    assert path.read_text() == "retained prior evidence"
