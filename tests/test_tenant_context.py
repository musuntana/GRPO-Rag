from ragos.core.tenant import ActorType, TenantContext
from ragos.runtime.checkpoints import build_checkpoint_dsn


def test_tenant_context_builds_thread_id() -> None:
    ctx = TenantContext(
        tenant_id="fa9d4df0-81ce-4f1a-9820-4c29259779a6",
        tenant_slug="acme",
        actor_id="u_123",
        actor_type=ActorType.END_USER,
        roles=["user"],
        trace_id="trace-1",
    )

    assert (
        ctx.build_thread_id("wf_001", "run_001")
        == "fa9d4df0-81ce-4f1a-9820-4c29259779a6:wf_001:run_001"
    )


def test_checkpoint_dsn_sets_search_path_option() -> None:
    dsn = "postgresql://postgres:postgres@localhost:5432/ragos?sslmode=disable"

    rewritten = build_checkpoint_dsn(dsn, "checkpoint")

    assert "options=-csearch_path%3Dcheckpoint" in rewritten
    assert "sslmode=disable" in rewritten


def test_private_context_requires_server_resolved_user() -> None:
    from uuid import uuid4

    import pytest
    from ragos.core.errors import PermissionDeniedError

    ctx = TenantContext(
        tenant_id=uuid4(), tenant_slug="acme", actor_id="user/one", trace_id="trace"
    )
    with pytest.raises(PermissionDeniedError, match="server-resolved"):
        ctx.require_user_id()
    user_id = uuid4()
    bound = ctx.bind_user(user_id)
    assert bound.require_user_id() == user_id
    assert ctx.user_id is None
    assert bound.require_scope_key() == ctx.require_scope_key()
    assert bound.require_private_scope_key().endswith("/actor/user%2Fone")
    with pytest.raises(PermissionDeniedError, match="rebind"):
        bound.bind_user(uuid4())
    with pytest.raises(PermissionDeniedError, match="UUID"):
        ctx.bind_user(str(user_id))


def test_context_rejects_missing_private_actor_and_identity_mutation() -> None:
    from uuid import uuid4

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TenantContext(tenant_id=uuid4(), tenant_slug="acme", actor_id="", trace_id="trace")
    ctx = TenantContext(tenant_id=uuid4(), tenant_slug="acme", actor_id="user", trace_id="trace")
    with pytest.raises(ValidationError):
        ctx.actor_id = "another-user"
    with pytest.raises(ValidationError):
        TenantContext(
            tenant_id=uuid4(),
            tenant_slug="acme",
            actor_id="user",
            trace_id="trace",
            workspace_id=uuid4(),
        )
