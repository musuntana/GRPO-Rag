# ADR-0005: Server-Derived Scope Key

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §5.1.1, §5.3.1, §6.3, §9.2.1, §11.4
- **Supersedes:** nothing

## Context

Every read and write in this system is tenant-scoped. The scope identifier therefore arrives from outside the trust boundary on every request, and the natural implementation — accept a tenant identifier from the client and filter on it — is an authorization bug in waiting.

The failure mode is not always a dramatic cross-tenant read. Often it is a *mis-scoped* read: a query that is internally consistent, returns plausible data, and belongs to the wrong tenant or the wrong user. Nothing raises. The result is either a leak or a silently wrong answer, and both are discovered by a customer rather than by us.

Evidence from the survey:

- LiteLLM documents the same defect in its own cost accounting — an end user can pass a `user` field in the request body and have spend attributed away from the key owner. Their stated mitigation is to always bind `user_id` at key-creation time.
- RAGFlow shipped a confirmed bug where five endpoints derived the tenant from the resource owner and two from the requester, so a non-owner re-parsing a shared dataset wrote against the wrong index and served stale chunks beside new ones. The root cause was described as ad-hoc decorator-injected tenant context rather than a single enforced resolution point.
- Mastra publishes the cleanest counter-pattern we found: derive a resource id in trusted code after authentication, store it server-side, and **ignore whatever the client supplied**. They document that omitting the mapping means built-in routes fall back to a resource id taken from the request body, so any authenticated caller can read and write another user's threads — and they log a startup warning in that state.

## Decision

A single derived value, `scope_key`, scopes every tenant-scoped read and write. It is always produced by trusted server code and stored server-side.

1. **Derive, never accept.** `tenant_id` comes from exactly one authoritative source per entry point: host mapping, custom-domain mapping, or a verified token claim. Never from a request body, query parameter, or unverified header.
2. **Discard caller-supplied scope.** `tenant_id`, `brand_id`, `kb_id`, `user_id`, and `model_binding_id` in a payload are ignored. Where a payload field is legitimately required, compare it against the derived scope and reject on mismatch rather than trusting it.
3. **Compose once, reuse everywhere.** `scope_key = {tenant_id}:{workspace_id}:{user_id}`, with `workspace_id` omitted for platform actors. The same value is the basis of the LangGraph `thread_id`, the Qdrant payload filter, the object-storage path prefix, the secret alias namespace, the audit log partition, and the cost ledger.
4. **Pass it explicitly.** Carried on `tenant_ctx` / `RequestContext` and passed as a required repository parameter. Not read from module-level or ambient state, and not accepted as a function argument a caller may have populated from untrusted input.
5. **Fail closed.** A missing `scope_key` is a `PermissionDeniedError`, never an unscoped query.
6. **Derived values that scope identity are also not caller-supplied.** `thread_id`, cache keys, prompt-cache keys, idempotency keys, and waitpoint signal IDs are all derived from `scope_key`. A client that may choose its own identifier can collide with another tenant's namespace.

## Consequences

**Positive**

- Cross-tenant access requires a bug in the resolution logic itself, not a mistake in each of dozens of call sites. The bug class collapses from "every handler" to "one middleware".
- Required repository parameters make an unscoped query impossible to write by accident, and make the requirement greppable in review and in CI.
- Because `scope_key` is uniform across storage, retrieval, workflow execution, and secrets, adding a new subsystem cannot accidentally invent a second isolation mechanism.
- The cross-tenant probe suite (§5.3.1) has a stable thing to assert: one key, every path.

**Negative**

- Platform-level and cross-tenant operations need an explicit escape hatch, and every escape hatch is a place isolation can leak. Mitigated by requiring a distinct actor type for them rather than a nullable `tenant_id`.
- Callers that legitimately need to reference a tenant must be threaded through a resolved-membership check rather than a trust decision, which is more plumbing at each such call site.
- Internal callers (workers, schedulers) cannot resolve scope from ambient state, so context must be passed or reconstructed explicitly. This is friction by design.

## Rejected alternatives

- **Trust a client-supplied tenant identifier.** Rejected: it is the defect this ADR exists to prevent.
- **Compare-and-trust** (accept the client's value when it matches a membership check). We do this for *membership*, but the stored value is always the derived one, so a mismatch is an error rather than a silent pass.
- **Row-Level Security as the primary control.** Rejected as primary: RLS depends on a session variable being set correctly on every connection path, including ones that bypass the middleware. It remains a secondary guard, which is what architecture §5.3 already says.
- **One collection or index per tenant.** Rejected: operationally unscalable in both topology and blast radius. Payload filters on a shared collection, with native shard keys for tenants that need promotion, is the correct default.