from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from ragos.core.errors import ConflictError, ValidationError
from ragos.core.tenant import TenantContext
from ragos.storage.models.app import (
    KeyPool,
    ModelProfile,
    Provider,
    TenantModelBinding,
)
from ragos.storage.models.base import utc_now
from ragos.storage.repositories.base import PlatformRepository, TenantRepository
from sqlmodel import Session

# ---------------------------------------------------------------------------
# Provider (platform-level)
# ---------------------------------------------------------------------------


class ProviderRepository(PlatformRepository[Provider]):
    def __init__(self, session: Session) -> None:
        super().__init__(session=session, model_type=Provider)

    def get_by_slug(self, slug: str) -> Provider | None:
        stmt = self._select().where(Provider.slug == slug)
        return self.session.exec(stmt).first()

    def create(self, *, slug: str, provider_type: str, display_name: str,
               base_url: str | None = None) -> Provider:
        if self.get_by_slug(slug) is not None:
            raise ConflictError("Provider slug already exists", slug=slug)
        provider = Provider(
            slug=slug,
            provider_type=provider_type,
            display_name=display_name,
            base_url=base_url,
        )
        self.session.add(provider)
        self.session.flush()
        self.session.refresh(provider)
        return provider


# ---------------------------------------------------------------------------
# KeyPool (platform-level)
# ---------------------------------------------------------------------------


SECRET_INTAKE_UNAVAILABLE_MESSAGE = (
    "API key intake is unavailable: no trusted secret channel is configured, "
    "raw credentials are not stored"
)


def reject_raw_credential_intake() -> NoReturn:
    raise ValidationError(
        SECRET_INTAKE_UNAVAILABLE_MESSAGE,
        reason="secret_intake_unavailable",
    )


class KeyPoolRepository(PlatformRepository[KeyPool]):
    def __init__(self, session: Session) -> None:
        super().__init__(session=session, model_type=KeyPool)

    def list_for_provider(self, provider_id: UUID) -> list[KeyPool]:
        stmt = self._select().where(KeyPool.provider_id == provider_id)
        return list(self.session.exec(stmt).all())

    def create(self, *, provider_id: UUID, label: str, api_key: str,
               weight: int = 1) -> NoReturn:
        reject_raw_credential_intake()


# ---------------------------------------------------------------------------
# ModelProfile (tenant-scoped)
# ---------------------------------------------------------------------------


class ModelProfileRepository(TenantRepository[ModelProfile]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=ModelProfile)

    def create(self, *, name: str, use_case: str, provider_id: UUID,
               model_name: str, parameters: dict | None = None,
               fallback_profile_id: UUID | None = None) -> ModelProfile:
        profile = ModelProfile(
            tenant_id=self.tenant_ctx.tenant_id,
            name=name,
            use_case=use_case,
            provider_id=provider_id,
            model_name=model_name,
            parameters=parameters or {},
            fallback_profile_id=fallback_profile_id,
        )
        self.session.add(profile)
        self.session.flush()
        self.session.refresh(profile)
        return profile


class PlatformModelProfileRepository(PlatformRepository[ModelProfile]):
    def __init__(self, session: Session) -> None:
        super().__init__(session=session, model_type=ModelProfile)

    def create(self, *, tenant_id: UUID, name: str, use_case: str,
               provider_id: UUID, model_name: str, parameters: dict | None = None,
               fallback_profile_id: UUID | None = None) -> ModelProfile:
        profile = ModelProfile(
            tenant_id=tenant_id,
            name=name,
            use_case=use_case,
            provider_id=provider_id,
            model_name=model_name,
            parameters=parameters or {},
            fallback_profile_id=fallback_profile_id,
        )
        self.session.add(profile)
        self.session.flush()
        self.session.refresh(profile)
        return profile


# ---------------------------------------------------------------------------
# TenantModelBinding (tenant-scoped)
# ---------------------------------------------------------------------------


class TenantModelBindingRepository(TenantRepository[TenantModelBinding]):
    def __init__(self, session: Session, tenant_ctx: TenantContext) -> None:
        super().__init__(session=session, tenant_ctx=tenant_ctx, model_type=TenantModelBinding)

    def get_for_use_case(self, use_case: str) -> TenantModelBinding | None:
        stmt = (
            self.scoped_statement()
            .where(TenantModelBinding.use_case == use_case)
        )
        return self.session.exec(stmt).first()

    def upsert(self, *, use_case: str, profile_id: UUID,
               is_default: bool = False) -> TenantModelBinding:
        existing = self.get_for_use_case(use_case)
        if existing is not None:
            existing.profile_id = profile_id
            existing.is_default = is_default
            existing.updated_at = utc_now()
            self.session.add(existing)
            self.session.flush()
            self.session.refresh(existing)
            return existing

        binding = TenantModelBinding(
            tenant_id=self.tenant_ctx.tenant_id,
            use_case=use_case,
            profile_id=profile_id,
            is_default=is_default,
        )
        self.session.add(binding)
        self.session.flush()
        self.session.refresh(binding)
        return binding
