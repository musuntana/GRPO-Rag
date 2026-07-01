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
