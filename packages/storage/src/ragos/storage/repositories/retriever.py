from __future__ import annotations

from uuid import UUID

from ragos.core.errors import NotFoundError, PermissionDeniedError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import RetrieverProfileVersion
from ragos.storage.repositories.base import TenantRepository
from sqlmodel import Session


class RetrieverProfileRepository(TenantRepository[RetrieverProfileVersion]):
    """Frozen retrieval configurations, one row per version.

    Versions are append-only. An evaluation result records the version it ran
    against, so editing a row in place would silently rewrite the meaning of a
    stored metric — the profile that produced ``ndcg@10 = 0.71`` would no longer
    be the profile that is live now.

    ``scope_key`` is a required keyword on every method rather than ambient
    state: a repository that resolved its own scope would let a caller who
    reached into the context widen the query, and the equality check below is
    what makes the widening an error rather than a wider result set.
    """

    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        tenant_ctx.require_scope_key()
        super().__init__(
            session=session, tenant_ctx=tenant_ctx, model_type=RetrieverProfileVersion
        )
        self.scope_key = tenant_ctx.scope_key

    def _require_scope_key(self, scope_key: str) -> None:
        if not scope_key:
            raise PermissionDeniedError(
                "scope_key is required and must not be empty",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        if scope_key != self.scope_key:
            raise PermissionDeniedError(
                "scope_key does not match the resolved tenant scope",
                tenant_id=str(self.tenant_ctx.tenant_id),
            )

    def _rows_for_profile(self, profile_id: UUID) -> list[RetrieverProfileVersion]:
        statement = (
            self.scoped_statement()
            .where(RetrieverProfileVersion.profile_id == profile_id)
            .order_by(RetrieverProfileVersion.version.desc())
        )
        return list(self.session.exec(statement).all())

    def list_profiles(
        self, *, scope_key: str, limit: int = 50, offset: int = 0
    ) -> list[RetrieverProfileVersion]:
        """Latest version of every profile, newest version first."""
        self._require_scope_key(scope_key)
        rows = list(
            self.session.exec(
                self.scoped_statement().order_by(
                    RetrieverProfileVersion.profile_id,
                    RetrieverProfileVersion.version.desc(),
                )
            ).all()
        )
        latest: list[RetrieverProfileVersion] = []
        for row in rows:
            if not any(existing.profile_id == row.profile_id for existing in latest):
                latest.append(row)
        return latest[offset : offset + limit]

    def list_versions(
        self, *, scope_key: str, profile_id: UUID
    ) -> list[RetrieverProfileVersion]:
        self._require_scope_key(scope_key)
        return self._rows_for_profile(profile_id)

    def require_latest_version(
        self, *, scope_key: str, profile_id: UUID
    ) -> RetrieverProfileVersion:
        self._require_scope_key(scope_key)
        rows = self._rows_for_profile(profile_id)
        if not rows:
            raise NotFoundError(
                "Retriever profile not found",
                resource="RetrieverProfile",
                profile_id=str(profile_id),
                tenant_id=str(self.tenant_ctx.tenant_id),
            )
        return rows[0]

    def require_version(
        self, *, scope_key: str, profile_id: UUID, version: int
    ) -> RetrieverProfileVersion:
        """Fetch one exact version — the read an eval replay needs."""
        self._require_scope_key(scope_key)
        for row in self._rows_for_profile(profile_id):
            if row.version == version:
                return row
        raise NotFoundError(
            "Retriever profile version not found",
            resource="RetrieverProfileVersion",
            profile_id=str(profile_id),
            version=version,
            tenant_id=str(self.tenant_ctx.tenant_id),
        )

    def create_version(
        self, *, scope_key: str, profile_id: UUID, **fields: object
    ) -> RetrieverProfileVersion:
        """Append a new version. The previous rows are never touched.

        The version number is derived from the rows already stored rather than
        accepted from the caller: a client-supplied version is either a
        collision or a rollback, and both make a stored eval result ambiguous.
        """
        self._require_scope_key(scope_key)
        existing = self._rows_for_profile(profile_id)
        next_version = max((row.version for row in existing), default=0) + 1
        row = RetrieverProfileVersion(
            tenant_id=self.tenant_ctx.tenant_id,
            profile_id=profile_id,
            version=next_version,
            **fields,
        )
        self.session.add(row)
        self.session.flush()
        self.session.refresh(row)
        return row
