from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from ragos.core.errors import ConflictError, PermissionDeniedError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.index_projection import ChunkProjection, projection_from_chunk
from ragos.storage.models.app import ChunkManifest, DocumentRecord, DocumentRepresentation
from ragos.storage.models.base import utc_now
from ragos.storage.models.search import SearchChunkHot, SearchChunkStable, SearchMemory
from ragos.storage.repositories.kb import require_manifest_contract
from sqlalchemy import and_, func, literal, literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select


class SearchTier(StrEnum):
    STABLE = "stable"
    HOT = "hot"
    MEMORY = "memory"


@dataclass(slots=True)
class SearchChunkUpsert:
    id: UUID
    tenant_id: UUID
    kb_id: UUID | None
    document_id: UUID | None
    parent_chunk_id: UUID | None
    layer: str
    lang: str
    title: str | None
    headings_text: str | None
    body: str
    tags_text: str | None
    metadata_json: dict[str, Any]
    updated_at: datetime
    expires_at: datetime | None = None
    page_no: int | None = None
    page_count: int | None = None
    page_offset_start: int | None = None
    page_offset_end: int | None = None
    start_char_idx: int | None = None
    end_char_idx: int | None = None
    element_type: str = "text"
    is_parent: bool = False
    brand_id: UUID | None = None
    source_type: str = "upload"
    security_level: str = "internal"
    page_image_ref: str | None = None
    user_id: UUID | None = None
    document_version_id: UUID | None = None
    representation_id: UUID | None = None
    index_id: UUID | None = None

    representation_char_count: int | None = None
    parent_unit: str | None = None


@dataclass(slots=True)
class SearchHit:
    chunk_id: UUID
    document_id: UUID | None
    kb_id: UUID | None
    layer: str
    body: str
    metadata_json: dict[str, Any]
    rank: float
    parent_chunk_id: UUID | None = None
    page_no: int | None = None
    page_count: int | None = None
    page_offset_start: int | None = None
    page_offset_end: int | None = None
    start_char_idx: int | None = None
    end_char_idx: int | None = None
    element_type: str = "text"
    is_parent: bool = False
    brand_id: UUID | None = None
    source_type: str = "upload"
    security_level: str = "internal"
    page_image_ref: str | None = None
    user_id: UUID | None = None
    document_version_id: UUID | None = None
    representation_id: UUID | None = None
    index_id: UUID | None = None

    representation_char_count: int | None = None
    parent_unit: str | None = None


class SearchRepository:
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        self.session = session
        tenant_ctx.require_scope_key()
        self.tenant_ctx = tenant_ctx

    @staticmethod
    def model_for_tier(tier: SearchTier):
        if tier == SearchTier.STABLE:
            return SearchChunkStable
        if tier == SearchTier.HOT:
            return SearchChunkHot
        return SearchMemory

    def upsert_chunks(self, *, tier: SearchTier, chunks: list[SearchChunkUpsert]) -> None:
        if not chunks:
            return

        model = self.model_for_tier(tier)
        rows = []
        now = utc_now()
        if len({chunk.id for chunk in chunks}) != len(chunks):
            raise ValidationError("duplicate chunk identity in the same batch")
        for chunk in chunks:
            if chunk.element_type not in {
                "text",
                "heading",
                "table",
                "figure",
                "list",
                "code",
                "caption",
            }:
                raise ValidationError("unknown element_type")
        if tier != SearchTier.MEMORY:
            self._validate_enterprise_batch(model, chunks)
        elif any(
            chunk.document_version_id is not None
            or chunk.representation_id is not None
            or chunk.index_id is not None
            or chunk.representation_char_count is not None
            or chunk.parent_unit is not None
            for chunk in chunks
        ):
            raise ValidationError("Personal memory cannot claim an enterprise source identity")
        for chunk in chunks:
            if chunk.tenant_id != self.tenant_ctx.tenant_id:
                raise PermissionDeniedError(
                    "attempted to upsert tenant other than the resolved scope"
                )
            if tier == SearchTier.MEMORY and chunk.user_id != self.tenant_ctx.require_user_id():
                raise PermissionDeniedError("memory owner does not match")
            allowed = self.tenant_ctx.allowed_kb_ids
            if tier != SearchTier.MEMORY and allowed is not None and chunk.kb_id not in allowed:
                raise PermissionDeniedError("knowledge base is not authorized")
            rows.append(
                {
                    "id": chunk.id,
                    "tenant_id": chunk.tenant_id,
                    "kb_id": chunk.kb_id,
                    "document_id": chunk.document_id,
                    "parent_chunk_id": chunk.parent_chunk_id,
                    "layer": chunk.layer,
                    "lang": chunk.lang,
                    "title": chunk.title,
                    "headings_text": chunk.headings_text,
                    "body": chunk.body,
                    "tags_text": chunk.tags_text,
                    "metadata_json": chunk.metadata_json,
                    "updated_at": chunk.updated_at or now,
                    "expires_at": chunk.expires_at,
                    "page_no": chunk.page_no,
                    "page_count": chunk.page_count,
                    "page_offset_start": chunk.page_offset_start,
                    "page_offset_end": chunk.page_offset_end,
                    "start_char_idx": chunk.start_char_idx,
                    "end_char_idx": chunk.end_char_idx,
                    "element_type": chunk.element_type,
                    "is_parent": chunk.is_parent,
                    "brand_id": chunk.brand_id,
                    "source_type": chunk.source_type,
                    "security_level": chunk.security_level,
                    "page_image_ref": chunk.page_image_ref,
                    **(
                        {"user_id": chunk.user_id}
                        if tier == SearchTier.MEMORY
                        else {
                            "document_version_id": chunk.document_version_id,
                            "representation_id": chunk.representation_id,
                            "index_id": chunk.index_id,
                        }
                    ),
                }
            )

        statement = insert(model).values(rows)
        if tier == SearchTier.MEMORY:
            update_columns = {
                key: getattr(statement.excluded, key)
                for key in rows[0]
                if key not in {"id", "tenant_id"}
            }
            conflict_guard = (
                (model.tenant_id == self.tenant_ctx.tenant_id)
                & model.kb_id.is_not_distinct_from(statement.excluded.kb_id)
                & model.document_id.is_not_distinct_from(statement.excluded.document_id)
                & model.user_id.is_not_distinct_from(statement.excluded.user_id)
            )
        else:
            # Idempotent replay can refresh the bookkeeping timestamp, never
            # overwrite raw text, coordinates, source identity or access policy.
            update_columns = {"updated_at": statement.excluded.updated_at}
            conflict_guard = and_(
                *(
                    getattr(model, key).is_not_distinct_from(getattr(statement.excluded, key))
                    for key in rows[0]
                    if key not in {"id", "updated_at"}
                )
            )
        upsert = statement.on_conflict_do_update(
            index_elements=[model.id], set_=update_columns, where=conflict_guard
        )
        # A conflict refusal must roll back the whole batch even if a caller
        # catches the domain error and retains its enclosing transaction.
        with self.session.begin_nested():
            written = self.session.exec(upsert.returning(model.id)).all()
            if len(written) != len(rows):
                raise PermissionDeniedError("chunk identity conflicts with existing resource owner")

    def _validate_enterprise_batch(self, model, chunks: list[SearchChunkUpsert]) -> None:
        manifests = {}
        for chunk in sorted(chunks, key=lambda item: str(item.index_id)):
            if chunk.tenant_id != self.tenant_ctx.tenant_id:
                raise PermissionDeniedError("chunk tenant does not match")
            if any(
                value is None
                for value in (
                    chunk.kb_id,
                    chunk.document_id,
                    chunk.document_version_id,
                    chunk.representation_id,
                    chunk.index_id,
                )
            ):
                raise ValidationError(
                    "New enterprise chunks require complete immutable source identity"
                )
            if chunk.user_id is not None:
                raise ValidationError("Enterprise evidence cannot carry a private memory owner")
            if chunk.index_id not in manifests:
                manifest = self.session.exec(
                    select(ChunkManifest)
                    .where(ChunkManifest.tenant_id == self.tenant_ctx.tenant_id)
                    .where(ChunkManifest.id == chunk.index_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).first()
                if manifest is None:
                    raise PermissionDeniedError("Index is not accessible under this scope")
                representation = require_manifest_contract(self.session, self.tenant_ctx, manifest)
                manifests[chunk.index_id] = (manifest, representation)
            manifest, representation = manifests[chunk.index_id]
            if (
                chunk.kb_id,
                chunk.document_id,
                chunk.document_version_id,
                chunk.representation_id,
            ) != (
                manifest.kb_id,
                manifest.document_id,
                manifest.document_version_id,
                manifest.representation_id,
            ):
                raise PermissionDeniedError("Chunk source does not match its index")
            if (chunk.representation_char_count, chunk.parent_unit, chunk.page_count) != (
                representation.char_count,
                representation.parent_unit,
                representation.page_count,
            ):
                raise ValidationError(
                    "Chunk extent or pagination does not match its representation"
                )
            validate_chunk_coordinates(chunk)
            if manifest.indexing_state not in {"pending", "indexing", "indexed"}:
                raise ConflictError("Index is not accepting chunks")
            if manifest.indexing_state == "indexed":
                existing = self.session.exec(
                    select(model)
                    .where(model.tenant_id == self.tenant_ctx.tenant_id)
                    .where(model.id == chunk.id)
                ).first()
                if existing is None:
                    raise ConflictError("Published index cannot accept additional chunks")
        batch = {chunk.id: chunk for chunk in chunks}
        for chunk in chunks:
            if chunk.is_parent:
                continue
            parent = batch.get(chunk.parent_chunk_id)
            if parent is None:
                parent = self.session.exec(
                    select(model)
                    .where(model.tenant_id == self.tenant_ctx.tenant_id)
                    .where(model.id == chunk.parent_chunk_id)
                ).first()
            if parent is None:
                raise ValidationError("Child chunk requires a stored or same-batch parent")
            validate_parent_link(chunk, parent)

    def _scope(self, model, *, tier: SearchTier, scope_key: str):
        if not scope_key:
            raise PermissionDeniedError("scope_key is required")
        expected_scope = (
            self.tenant_ctx.require_private_scope_key()
            if tier == SearchTier.MEMORY
            else self.tenant_ctx.require_scope_key()
        )
        if not scope_key or scope_key != expected_scope:
            raise PermissionDeniedError("scope_key does not match resolved tenant scope")
        if tier == SearchTier.MEMORY:
            statement = select(model, literal(None), literal(None))
        else:
            statement = (
                select(model, DocumentRepresentation.char_count, DocumentRepresentation.parent_unit)
                .select_from(model)
                .join(
                    ChunkManifest,
                    and_(
                        model.tenant_id == ChunkManifest.tenant_id,
                        model.kb_id == ChunkManifest.kb_id,
                        model.document_id == ChunkManifest.document_id,
                        model.document_version_id == ChunkManifest.document_version_id,
                        model.representation_id == ChunkManifest.representation_id,
                        model.index_id == ChunkManifest.id,
                    ),
                )
                .join(
                    DocumentRepresentation,
                    and_(
                        ChunkManifest.tenant_id == DocumentRepresentation.tenant_id,
                        ChunkManifest.kb_id == DocumentRepresentation.kb_id,
                        ChunkManifest.document_id == DocumentRepresentation.document_id,
                        ChunkManifest.document_version_id
                        == DocumentRepresentation.document_version_id,
                        ChunkManifest.representation_id == DocumentRepresentation.id,
                    ),
                )
                .join(
                    DocumentRecord,
                    and_(
                        model.tenant_id == DocumentRecord.tenant_id,
                        model.kb_id == DocumentRecord.kb_id,
                        model.document_id == DocumentRecord.id,
                        model.index_id == DocumentRecord.active_index_id,
                    ),
                )
                .where(
                    ChunkManifest.indexing_state == "indexed",
                    ChunkManifest.pg_digest.is_not(None),
                    ChunkManifest.pg_digest.op("~")("^[0-9a-f]{64}$"),
                    ChunkManifest.vector_digest == ChunkManifest.pg_digest,
                    ChunkManifest.reconciled_at.is_not(None),
                    ChunkManifest.plan_ref.is_not(None),
                    ChunkManifest.plan_digest.is_not(None),
                    ChunkManifest.chunker_config_digest.is_not(None),
                    ChunkManifest.embedding_profile.is_not(None),
                    ChunkManifest.embedding_version.is_not(None),
                    ChunkManifest.chunker_version.is_not(None),
                )
            )
        statement = statement.where(model.tenant_id == self.tenant_ctx.tenant_id)
        if tier == SearchTier.MEMORY:
            statement = statement.where(model.user_id == self.tenant_ctx.require_user_id())
        elif self.tenant_ctx.allowed_kb_ids is not None:
            statement = statement.where(model.kb_id.in_(self.tenant_ctx.allowed_kb_ids))
        if self.tenant_ctx.brand_id is not None:
            statement = statement.where(
                (model.brand_id.is_(None)) | (model.brand_id == self.tenant_ctx.brand_id)
            )
        return statement

    @staticmethod
    def _hit(
        row, rank: float = 0.0, *, representation_char_count=None, parent_unit=None
    ) -> SearchHit:
        fields = {
            key: getattr(row, key)
            for key in SearchHit.__dataclass_fields__
            if key
            not in {
                "chunk_id",
                "rank",
                "user_id",
                "representation_char_count",
                "parent_unit",
                "document_version_id",
                "representation_id",
                "index_id",
            }
        }
        return SearchHit(
            chunk_id=row.id,
            rank=rank,
            user_id=row.user_id if isinstance(row, SearchMemory) else None,
            representation_char_count=representation_char_count,
            parent_unit=parent_unit,
            document_version_id=None if isinstance(row, SearchMemory) else row.document_version_id,
            representation_id=None if isinstance(row, SearchMemory) else row.representation_id,
            index_id=None if isinstance(row, SearchMemory) else row.index_id,
            **fields,
        )

    def search(
        self,
        *,
        tier: SearchTier,
        query: str,
        scope_key: str,
        limit: int = 10,
        kb_ids: list[UUID] | None = None,
        layer: str | None = None,
        user_id: UUID | None = None,
    ) -> list[SearchHit]:
        if user_id is not None and (
            tier != SearchTier.MEMORY or user_id != self.tenant_ctx.require_user_id()
        ):
            raise PermissionDeniedError("user_id does not match memory owner")
        model = self.model_for_tier(tier)
        ts_query = func.websearch_to_tsquery(literal_column("'simple'"), query)
        rank = func.ts_rank_cd(model.search_vector, ts_query).label("rank")
        statement = self._scope(model, tier=tier, scope_key=scope_key).add_columns(rank)
        statement = (
            statement.where(model.search_vector.op("@@")(ts_query))
            .order_by(rank.desc(), model.updated_at.desc())
            .limit(limit)
        )
        if kb_ids is not None:
            statement = statement.where(model.kb_id.in_(kb_ids))
        if layer:
            statement = statement.where(model.layer == layer)
        return [
            self._hit(
                row, float(score), representation_char_count=char_count, parent_unit=parent_unit
            )
            for row, char_count, parent_unit, score in self.session.execute(statement).all()
        ]

    def get_chunks_by_ids(
        self, *, tier: SearchTier, scope_key: str, chunk_ids: list[UUID]
    ) -> list[SearchHit]:
        model = self.model_for_tier(tier)
        statement = self._scope(model, tier=tier, scope_key=scope_key).where(
            model.id.in_(chunk_ids)
        )
        return [
            self._hit(row, representation_char_count=char_count, parent_unit=parent_unit)
            for row, char_count, parent_unit in self.session.execute(statement).all()
        ]

    def read_index_for_reconciliation(
        self, *, scope_key: str, index_id: UUID, tier: SearchTier
    ) -> list[ChunkProjection]:
        """Read staging state only for the recovery use case, never public recall.

        The caller owns a short PG transaction. Manifest locking prevents a
        concurrent staging writer from changing rows while this readback runs.
        No network/file I/O belongs inside this method or transaction.
        """
        if scope_key != self.tenant_ctx.require_scope_key():
            raise PermissionDeniedError("Reconciliation scope does not match trusted tenant")
        if tier not in {SearchTier.STABLE, SearchTier.HOT}:
            raise ValidationError(
                "Private memory does not participate in enterprise reconciliation"
            )
        if not isinstance(index_id, UUID):
            raise ValidationError("Reconciliation requires a frozen index UUID")
        manifest = self.session.exec(
            select(ChunkManifest)
            .where(ChunkManifest.tenant_id == self.tenant_ctx.tenant_id)
            .where(ChunkManifest.id == index_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if manifest is None:
            raise PermissionDeniedError("Index is not accessible under this scope")
        representation = require_manifest_contract(self.session, self.tenant_ctx, manifest)
        if manifest.indexing_state == "unversioned":
            raise ValidationError("Unversioned index cannot be reconciled")
        model = self.model_for_tier(tier)
        rows = list(
            self.session.exec(
                select(model)
                .where(model.tenant_id == self.tenant_ctx.tenant_id)
                .where(model.index_id == index_id)
                .order_by(model.id)
                .execution_options(populate_existing=True)
            ).all()
        )
        # Do not filter malformed/extra rows away and accidentally attest a
        # subset. Every same-tenant row bearing this index must match its owner.
        for row in rows:
            if (row.kb_id, row.document_id, row.document_version_id, row.representation_id) != (
                manifest.kb_id,
                manifest.document_id,
                manifest.document_version_id,
                manifest.representation_id,
            ):
                raise PermissionDeniedError("Index readback contains a different source owner")
        if len(rows) != manifest.chunk_total:
            raise ConflictError("Index readback does not contain the complete frozen chunk set")
        return [
            projection_from_chunk(
                row,
                representation_char_count=representation.char_count,
                parent_unit=representation.parent_unit,
            )
            for row in rows
        ]

    def count_children(
        self, *, tier: SearchTier, scope_key: str, parent_ids: list[UUID]
    ) -> dict[UUID, int]:
        model = self.model_for_tier(tier)
        statement = self._scope(model, tier=tier, scope_key=scope_key)
        if not parent_ids:
            return {}
        statement = (
            statement.with_only_columns(model.parent_chunk_id, func.count(model.id))
            .where(model.parent_chunk_id.in_(parent_ids))
            .group_by(model.parent_chunk_id)
        )
        return {parent_id: count for parent_id, count in self.session.execute(statement).all()}


def validate_chunk_coordinates(chunk: SearchChunkUpsert) -> None:
    start, end, extent = chunk.start_char_idx, chunk.end_char_idx, chunk.representation_char_count
    if (
        any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end, extent))
        or not 0 <= start < end <= extent
    ):
        raise ValidationError("Chunk span must be nonempty and within its frozen representation")
    if not isinstance(chunk.body, str) or len(chunk.body) != end - start:
        raise ValidationError("Chunk raw text length must match its source span")
    if chunk.is_parent:
        if chunk.parent_chunk_id is not None:
            raise ValidationError("Parent chunk cannot reference another parent")
    elif chunk.parent_chunk_id is None or chunk.parent_chunk_id == chunk.id:
        raise ValidationError("Child chunk must reference a different parent")
    if chunk.parent_unit == "page":
        page, count = chunk.page_no, chunk.page_count
        offsets = (chunk.page_offset_start, chunk.page_offset_end)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (page, count, *offsets)
            )
            or not 1 <= page <= count
        ):
            raise ValidationError("Chunk page must belong to its frozen representation")
        if (
            not 0 <= offsets[0] < offsets[1]
            or offsets[1] - offsets[0] != end - start
            or offsets[0] > start
        ):
            raise ValidationError("Page-relative and representation spans must agree")
        if chunk.is_parent and offsets[0] != 0:
            raise ValidationError("A page parent starts at the beginning of the page")
    elif chunk.parent_unit == "token_window":
        if any(
            value is not None
            for value in (
                chunk.page_no,
                chunk.page_count,
                chunk.page_offset_start,
                chunk.page_offset_end,
                chunk.page_image_ref,
            )
        ):
            raise ValidationError("Token-window evidence cannot claim page coordinates")
    else:
        raise ValidationError("Chunk requires a frozen parent unit")


def validate_parent_link(chunk, parent) -> None:
    fields = (
        "tenant_id",
        "kb_id",
        "document_id",
        "document_version_id",
        "representation_id",
        "index_id",
    )
    if (
        not parent.is_parent
        or parent.id != chunk.parent_chunk_id
        or any(getattr(chunk, field) != getattr(parent, field) for field in fields)
    ):
        raise ValidationError("Child and parent must belong to the same immutable index")
    if (
        parent.start_char_idx is None
        or parent.end_char_idx is None
        or not parent.start_char_idx
        <= chunk.start_char_idx
        < chunk.end_char_idx
        <= parent.end_char_idx
    ):
        raise ValidationError("Child source span must be contained in its parent")
    if (
        parent.body[
            chunk.start_char_idx - parent.start_char_idx : chunk.end_char_idx
            - parent.start_char_idx
        ]
        != chunk.body
    ):
        raise ValidationError("Child raw text must match the corresponding parent span")
    if chunk.parent_unit == "page":
        if (
            chunk.page_no != parent.page_no
            or chunk.page_count != parent.page_count
            or (chunk.start_char_idx - chunk.page_offset_start)
            != (parent.start_char_idx - parent.page_offset_start)
        ):
            raise ValidationError("A child cannot cross its parent's page boundary")
