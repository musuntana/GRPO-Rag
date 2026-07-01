from __future__ import annotations

from uuid import UUID

from ragos.core.errors import NotFoundError
from ragos.storage.models.app import ModelProfile, TenantModelBinding
from sqlmodel import Session


class ProfileResolver:
    """Resolves model profiles for a tenant + use_case.

    Resolution chain:
        1. Explicit profile_override (if provided)
        2. Tenant binding (tenant_model_binding row)
        3. Raises NotFoundError if no profile can be resolved

    Once the primary profile is found, fallback profiles are walked
    via ``fallback_profile_id`` links.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(
        self,
        *,
        tenant_id: UUID,
        use_case: str,
        profile_override: UUID | None = None,
    ) -> ModelProfile:
        """Return the primary model profile for *use_case*."""
        if profile_override is not None:
            return self._get_profile(profile_override)

        # Look up tenant binding first.
        from sqlmodel import select as sql_select

        stmt = (
            sql_select(TenantModelBinding)
            .where(TenantModelBinding.tenant_id == tenant_id)
            .where(TenantModelBinding.use_case == use_case)
        )
        binding = self.session.exec(stmt).first()
        if binding is not None:
            return self._get_profile(binding.profile_id)

        raise NotFoundError(
            "No model profile resolved for use_case",
            resource="ModelProfile",
            use_case=use_case,
            tenant_id=str(tenant_id),
        )

    def walk_fallbacks(self, profile: ModelProfile) -> list[ModelProfile]:
        """Walk the fallback chain starting from *profile*.

        Returns [profile, fallback_1, fallback_2, ...] stopping at
        the first profile without a fallback_profile_id, or at a
        cycle (detected by a set of seen ids).
        """
        chain = [profile]
        seen = {profile.id}
        current = profile
        while current.fallback_profile_id is not None:
            if current.fallback_profile_id in seen:
                break
            fb = self._get_profile(current.fallback_profile_id)
            if not fb.is_active:
                break
            chain.append(fb)
            seen.add(fb.id)
            current = fb
        return chain

    def _get_profile(self, profile_id: UUID) -> ModelProfile:
        profile = self.session.get(ModelProfile, profile_id)
        if profile is None:
            raise NotFoundError(
                "ModelProfile not found",
                resource="ModelProfile",
                id=str(profile_id),
            )
        if not profile.is_active:
            raise NotFoundError(
                "ModelProfile is not active",
                resource="ModelProfile",
                id=str(profile_id),
            )
        return profile
