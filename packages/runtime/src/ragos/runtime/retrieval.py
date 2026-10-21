"""Retrieval orchestration: stored profile → wired pipeline → result.

The pipeline in ``ragos.retrieval`` is transport-agnostic and takes its model
calls as injected callables. This module is the seam: it resolves a profile from
storage, builds the components that profile asks for, and reports anything the
request asked for but could not get.

Two capabilities are refused rather than degraded, because a silent downgrade
here is invisible in the response:

- a profile that names a recall channel with no wired implementation would
  otherwise retrieve from fewer channels than it was evaluated on
- a profile that requests semantic citation verification, or that enables the
  relevance gate, with no callable to serve it would otherwise return unverified
  citations or un-gated answers while claiming the profile's guarantees

A retriever that silently runs a different configuration from the one on the
eval record is worse than an error, because the eval numbers still look valid.
Channels and a configured-but-unwired rerank are reported as warnings rather than
refused: they change ranking quality, not a stated guarantee.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID
from urllib.parse import urlsplit

from ragos.core.config import Settings, get_settings
from ragos.core.errors import ValidationError
from ragos.core.logging import get_logger
from ragos.core.tenant import TenantContext
from ragos.retrieval.gateway import (
    GatewayComplete,
    GatewayEmbedder,
    GatewayGenerator,
    GatewayJudge,
)
from ragos.retrieval.grader import RelevanceGrader, rails_prompt_id
from ragos.retrieval.pipeline import GenerateFn, RAGPipeline, RAGResult, RecallFn
from ragos.retrieval.profile import profile_from_row
from ragos.retrieval.query.decomposer import Decomposer
from ragos.retrieval.query.engine import QueryEngine
from ragos.retrieval.query.hyde import HyDE
from ragos.retrieval.query.rewriter import ContextualRewriter
from ragos.retrieval.query.types import ConversationTurn
from ragos.retrieval.recall.dense import DenseRecall
from ragos.retrieval.recall.hot import HotTierRecall
from ragos.retrieval.recall.lexical import LexicalRecall
from ragos.retrieval.recall.parents import ParentResolver, SearchParentResolver
from ragos.retrieval.refine import RefineFn, SentenceRefiner
from ragos.retrieval.reranker import RerankFn
from ragos.retrieval.types import RetrieverProfile
from ragos.retrieval.verify import EntailmentFn
from ragos.storage.repositories.retriever import RetrieverProfileRepository
from ragos.storage.repositories.search import SearchRepository, SearchTier
from sqlmodel import Session

LOGGER = get_logger(__name__)

JudgeLike = Callable[[str, list[dict], TenantContext], Awaitable[str]]


@dataclass(slots=True)
class RetrievalOutcome:
    """What a retrieval request produced, plus what it could not do."""

    result: RAGResult
    profile: RetrieverProfile
    profile_name: str | None = None
    #: Requested but unavailable capabilities. Non-empty means the run was not
    #: the configuration the profile describes.
    warnings: list[str] = field(default_factory=list)


def build_recall_channels(
    *,
    session: Session,
    tenant_ctx: TenantContext,
    settings: Settings | None = None,
    router: object | None = None,
    qdrant_client: object | None = None,
    dense: bool = True,
) -> dict[str, RecallFn]:
    """Wire the channels this deployment can serve.

    Lexical and hot recall use PostgreSQL. Dense can be disabled independently,
    including when a client and router were injected. Until the gateway can prove
    its actual immutable embedding identity, dense is available only for explicit
    local hash tests; these mechanical checks do not measure retrieval quality.

    Collections are shared by tenants and separated by embedding profile, frozen
    model version and physical tier. Mandatory payload filters and PostgreSQL
    hydration enforce resource scope and the published source chain.
    """
    cfg = settings or get_settings()
    search_repo = SearchRepository(session=session, tenant_ctx=tenant_ctx)
    channels: dict[str, RecallFn] = {
        "lexical": LexicalRecall(search_repo),
        "hot": HotTierRecall(search_repo),
    }

    client = qdrant_client
    if client is None and dense:
        client = _default_qdrant_client(cfg)
    local_dense = dense and cfg.local_model_enabled and cfg.app_env == "dev"
    if local_dense:
        # ModelRoute authorization still happens on every query; this config
        # only freezes the physical Qdrant namespace and PG index identity.
        from ragos.runtime.local_embedding_probe import FrozenEmbeddingSpec

        if cfg.api_host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValidationError("dev dense requires a loopback-bound API")
        qdrant_url = urlsplit(cfg.qdrant_url)
        if cfg.qdrant_url and (
            qdrant_url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or qdrant_url.scheme != "http"
            or qdrant_url.username is not None
            or qdrant_url.password is not None
            or qdrant_url.query
            or qdrant_url.fragment
        ):
            raise ValidationError("dev dense requires local Qdrant HTTP")
        if not cfg.local_model_embed_revision:
            raise ValidationError("dev dense requires a frozen embedding revision")
        spec = FrozenEmbeddingSpec(
            model=cfg.local_model_embed,
            model_digest=cfg.local_model_embed_revision,
            dimension=cfg.local_model_embed_dimension,
            profile=cfg.embedding_profile,
        )
        if cfg.embedding_version != spec.version:
            raise ValidationError("dev dense index/model embedding identity mismatch")
        admitted = getattr(router, "_local", None)
        if admitted is None or admitted.embedding_model != spec.model:
            raise ValidationError("dev dense requires the admitted local model gateway")
        if admitted.embedding_dimension != spec.dimension:
            raise ValidationError("dev dense model dimension does not match index")
        if client is None:
            raise ValidationError("dev dense requires an explicit Qdrant client")
        channels["dense"] = DenseRecall(
            client,
            dense_collection_name(cfg),
            GatewayEmbedder(router),
            source=search_repo,
            embedding_profile=spec.profile,
            embedding_version=spec.version,
        )
    elif dense and client is not None and router is not None:
        if (
            cfg.app_env != "test"
            or not cfg.task_test_embedding
            or cfg.embedding_profile != "hash-test"
            or cfg.embedding_version != "sha256-hash-v1"
        ):
            raise ValidationError(
                "dense retrieval unavailable: actual gateway embedding identity is not frozen"
            )
        from ragos.storage.vector import hash_embed

        async def test_embedding(query, context):
            context.require_scope_key()
            return hash_embed(query)

        channels["dense"] = DenseRecall(
            client,
            dense_collection_name(cfg),
            test_embedding,
            source=search_repo,
        )
    elif dense:
        LOGGER.warning(
            "dense recall not wired: required test client or router dependency is missing",
            extra={"trace_id": tenant_ctx.trace_id},
        )
    return channels


def dense_collection_name(settings: Settings | None = None) -> str:
    """Stable-tier namespace for the full configured immutable embedding identity."""
    cfg = settings or get_settings()
    from ragos.storage.vector import collection_name

    return collection_name(
        embedding_profile=cfg.embedding_profile,
        embedding_version=cfg.embedding_version,
        tier=SearchTier.STABLE,
    )


def build_parent_resolver(*, session: Session, tenant_ctx: TenantContext) -> SearchParentResolver:
    return SearchParentResolver(SearchRepository(session=session, tenant_ctx=tenant_ctx))


def _default_qdrant_client(settings: Settings) -> object | None:
    if not settings.qdrant_url:
        return None
    try:
        from qdrant_client import QdrantClient
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.warning("qdrant-client is not installed; dense recall unavailable")
        return None
    try:
        return QdrantClient(url=settings.qdrant_url, timeout=5)
    except Exception:
        LOGGER.exception("could not construct a qdrant client; dense recall unavailable")
        return None


class RetrievalService:
    """Runs the retrieval chain for one tenant under one profile version."""

    def __init__(
        self,
        *,
        session: Session,
        tenant_ctx: TenantContext,
        recall_channels: dict[str, RecallFn] | None = None,
        generator: GenerateFn | None = None,
        judge: JudgeLike | None = None,
        reranker: RerankFn | None = None,
        entailment: EntailmentFn | None = None,
        complete: Callable[..., Awaitable[str]] | None = None,
        router: object | None = None,
        query_engine: QueryEngine | None = None,
        refiner: RefineFn | None = None,
        parent_resolver: ParentResolver | None = None,
    ) -> None:
        self.tenant_ctx = tenant_ctx
        self.scope_key = tenant_ctx.require_scope_key()
        self.repo = RetrieverProfileRepository(session=session, tenant_ctx=tenant_ctx)
        self.recall_channels = dict(recall_channels or {})
        self.generator = generator
        self.judge = judge
        self.reranker = reranker
        self.entailment = entailment
        self.complete = complete
        self.router = router
        self.query_engine = query_engine
        self.refiner = refiner
        self.parent_resolver = parent_resolver

    # -- profile resolution -------------------------------------------------

    def require_profile(self, profile_id: UUID) -> tuple[RetrieverProfile, str | None]:
        row = self.repo.require_latest_version(scope_key=self.scope_key, profile_id=profile_id)
        return profile_from_row(row), row.name

    def require_profile_version(
        self, profile_id: UUID, version: int
    ) -> tuple[RetrieverProfile, str | None]:
        """Resolve an exact version — what an eval replay and a metric audit need."""
        row = self.repo.require_version(
            scope_key=self.scope_key, profile_id=profile_id, version=version
        )
        return profile_from_row(row), row.name

    # -- execution ----------------------------------------------------------

    async def query(
        self,
        *,
        query: str,
        profile_id: UUID | None = None,
        profile_version: int | None = None,
        conversation_history: list[ConversationTurn] | list[str] | None = None,
    ) -> RetrievalOutcome:
        if profile_id is None:
            raise ValidationError(
                "retrieval requires an explicit retriever_profile_id",
                field="retriever_profile_id",
            )
        if profile_version is not None:
            profile, name = self.require_profile_version(profile_id, profile_version)
        else:
            profile, name = self.require_profile(profile_id)

        pipeline, warnings = self.build_pipeline(profile)
        result = await pipeline.run(
            query,
            self.tenant_ctx,
            profile,
            conversation_history=conversation_history,
        )
        return RetrievalOutcome(
            result=result, profile=profile, profile_name=name, warnings=warnings
        )

    def build_pipeline(self, profile: RetrieverProfile) -> tuple[RAGPipeline, list[str]]:
        warnings: list[str] = []

        missing = [c for c in profile.recall_channels if c not in self.recall_channels]
        for channel in missing:
            message = f"recall channel '{channel}' is configured but not wired in this deployment"
            warnings.append(message)
            LOGGER.error(message, extra={"trace_id": self.tenant_ctx.trace_id})

        if not profile.citation_verify_semantic and self.entailment is not None:
            LOGGER.debug("semantic citation verification is wired but disabled by the profile")
        if profile.citation_verify_semantic and self.entailment is None:
            raise ValidationError(
                "profile requests semantic citation verification but no entailment "
                "callable is wired; refusing to return citations that appear verified",
                profile_id=str(profile.id),
                citation_verify_semantic=True,
            )
        if profile.rerank_model_profile and self.reranker is None:
            message = (
                f"profile names rerank model '{profile.rerank_model_profile}' but no "
                "rerank callable is wired; running without rerank"
            )
            warnings.append(message)
            LOGGER.error(message, extra={"trace_id": self.tenant_ctx.trace_id})

        generator = self.generator
        if generator is None:
            if self.router is None:
                raise ValidationError(
                    "retrieval requires a generator; none is wired in this deployment",
                    profile_id=str(profile.id),
                )
            generator = GatewayGenerator(self.router)  # type: ignore[arg-type]

        grader = None
        if profile.crag_enabled:
            judge = self.judge
            if judge is None and self.router is not None:
                judge = GatewayJudge(self.router)  # type: ignore[arg-type]
            if judge is None:
                raise ValidationError(
                    "profile enables the relevance gate but no judge is wired; refusing "
                    "to answer without the gate the profile claims to have applied",
                    profile_id=str(profile.id),
                    crag_enabled=True,
                )
            grader = RelevanceGrader(
                judge=judge,
                thresholds=profile.crag_thresholds,
                prompt_id=rails_prompt_id(),
            )

        return (
            RAGPipeline(
                recall_channels=self.recall_channels,
                generator=generator,
                grader=grader,
                reranker=self.reranker,
                entailment=self.entailment,
                query_engine=self._query_engine(),
                refiner=self._refiner(),
                parent_resolver=self.parent_resolver,
            ),
            warnings,
        )

    # -- query-side components ---------------------------------------------

    def _query_engine(self) -> QueryEngine:
        if self.query_engine is not None:
            return self.query_engine
        complete = self.complete
        if complete is None and self.router is not None:
            complete = GatewayComplete(self.router)  # type: ignore[arg-type]
        if complete is None:
            # No model wired: rewriting degrades to the deterministic path,
            # HyDE and decomposition report themselves as unavailable.
            return QueryEngine()
        return QueryEngine(
            rewriter=ContextualRewriter(complete),
            hyde=HyDE(complete),
            decomposer=Decomposer(complete),
        )

    def _refiner(self) -> RefineFn:
        if self.refiner is not None:
            return self.refiner
        complete = self.complete
        if complete is None and self.router is not None:
            complete = GatewayComplete(self.router)  # type: ignore[arg-type]
        if complete is None:
            from ragos.retrieval.refine import deterministic_refine

            async def fallback(request, _ctx: TenantContext) -> str:
                return deterministic_refine(request.query)

            return fallback
        return SentenceRefiner(complete).refine
