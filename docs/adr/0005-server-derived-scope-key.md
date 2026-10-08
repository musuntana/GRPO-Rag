# ADR-0005: Server-Derived Scope Key

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §5.1.1, §5.3.1, §6.3, §9.2.1, §11.4
- **Supersedes:** nothing
- **Amended:** 2026-10-09 — clarify shared resource versus private user scopes; preserve server-derived identity.

## Context

Every read and write in this system is tenant-scoped. The scope identifier therefore arrives from outside the trust boundary on every request, and the natural implementation — accept a tenant identifier from the client and filter on it — is an authorization bug in waiting.

The failure mode is not always a dramatic cross-tenant read. Often it is a *mis-scoped* read: a query that is internally consistent, returns plausible data, and belongs to the wrong tenant or the wrong user. Nothing raises. The result is either a leak or a silently wrong answer, and both are discovered by a customer rather than by us.

Evidence from the survey:

- LiteLLM documents the same defect in its own cost accounting — an end user can pass a `user` field in the request body and have spend attributed away from the key owner. Their stated mitigation is to always bind `user_id` at key-creation time.
- RAGFlow shipped a confirmed bug where five endpoints derived the tenant from the resource owner and two from the requester, so a non-owner re-parsing a shared dataset wrote against the wrong index and served stale chunks beside new ones. The root cause was described as ad-hoc decorator-injected tenant context rather than a single enforced resolution point.
- Mastra publishes the cleanest counter-pattern we found: derive a resource id in trusted code after authentication, store it server-side, and **ignore whatever the client supplied**. They document that omitting the mapping means built-in routes fall back to a resource id taken from the request body, so any authenticated caller can read and write another user's threads — and they log a startup warning in that state.

## Decision

A canonical `tenant_ctx` establishes trusted identity and resource authorization. `scope_key` is an encoded, server-derived scope identifier reused at subsystem boundaries; it is not itself an authorization grant or a universal exact-equality filter for every resource.

1. **Derive, never accept identity.** Tenant selection may originate from host/domain mapping or a verified token, but access requires authenticated membership/role checks. Unverified headers, body and query parameters cannot grant a tenant, actor or role.
2. **Validate resource references.** KB, document, brand and model-binding IDs may be legitimate request references. Resolve them inside the actor's authorized tenant/workspace and ACL; reject mismatches. A payload `scope_key` or claimed identity is never trusted.
3. **Distinguish scope granularity.** Shared KB/document/index resources use an authorized tenant/workspace/resource scope. Private conversations/messages/memory additionally require the user owner. A private `scope_key` may encode tenant/workspace/user, but shared evidence must not be filtered as if it were owned only by that querying user.
4. **Compose once per required scope, pass explicitly.** Trusted code serializes the required scope components without collisions, carries them on `tenant_ctx` and passes them to repositories/adapters. Storage, vector filters, files, secrets, audit and cost records use the scope appropriate to the resource. No feature defines a parallel identity scheme.
5. **Fail closed and check ownership.** Missing/invalid scope or resource ownership is refused. Checking only a string prefix is insufficient. Same-tenant user isolation and authorized shared reads both require tests.
6. **Namespace caller request IDs.** Platform-owned run/thread/signal IDs are generated server-side. A client retry key is only an opaque request correlation value; the server namespaces it by authorized scope and operation, binds the payload digest and rejects reuse for different input. It cannot choose another tenant's cache/idempotency namespace.
7. **Reconstruct workers and replay.** Workers use a service identity and the authoritative task's resource scope, not arbitrary identity in a payload. Resume/replay/export repeat actor/resource authorization. Privileged cross-tenant tasks need an explicit actor type, audit and constrained capability, never a missing tenant.

The LangGraph thread format remains `{tenant_id}:{workflow_id}:{run_id}`; the run's owner/resource scope is checked separately. Future workspace fields are introduced only with their membership model, not invented from request text.

## Consequences

**Positive**

- Centralized identity and ownership policies reduce ad-hoc scope derivation. Each repository/adapter still enforces its resource ACL and required scope; one correct middleware does not excuse an unscoped data path.
- Required repository parameters make an unscoped query impossible to write by accident, and make the requirement greppable in review and in CI.
- Because scope derivation is centralized across storage, retrieval, workflow execution and secrets, each new subsystem must reuse the same ownership policy while retaining the scope granularity its resource needs.
- The cross-tenant and same-tenant cross-user probes have a stable property to assert: trusted identity, resource ownership and the appropriate required scope on every path.

**Negative**

- Platform-level and cross-tenant operations need an explicit escape hatch, and every escape hatch is a place isolation can leak. Mitigated by requiring a distinct actor type for them rather than a nullable `tenant_id`.
- Callers that legitimately need to reference a tenant must be threaded through a resolved-membership check rather than a trust decision, which is more plumbing at each such call site.
- Internal callers (workers, schedulers) cannot resolve scope from ambient state, so context must be passed or reconstructed explicitly. This is friction by design.

## Rejected alternatives

- **Trust a client-supplied tenant identifier.** Rejected: it is the defect this ADR exists to prevent.
- **Compare-and-trust** (accept the client's value when it matches a membership check). We do this for *membership*, but the stored value is always the derived one, so a mismatch is an error rather than a silent pass.
- **Row-Level Security as the primary control.** Rejected as primary: RLS depends on a session variable being set correctly on every connection path, including ones that bypass the middleware. It remains a secondary guard, which is what architecture §5.3 already says.
- **One collection or index per tenant.** Rejected: operationally unscalable in both topology and blast radius. Payload filters on a shared collection, with native shard keys for tenants that need promotion, is the correct default.