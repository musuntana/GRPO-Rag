# ADR-0001: Budgets and Identity Fail Closed

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §2 principle 8, §5.3.1, §11.0.1
- **Supersedes:** nothing

## Context

Two classes of check in this system are financial or security controls rather than optimizations: verifying *whose* request this is, and verifying *whether the tenant can afford it*.

Both are commonly implemented optimistically. The pattern in comparable systems is to read a cached counter, treat a cache miss or a store outage as "probably fine", and reconcile asynchronously. This is defensible for a throughput optimization. It is not defensible for a budget, because the failure mode is not a slow request — it is revenue that can never be recovered, granted in small increments, at scale, with no signal that it happened.

The same reasoning applies to tenant identity. A repository call that cannot determine its tenant scope and proceeds anyway produces either unscoped data or, worse, a plausible-looking wrong-tenant result. In the surveyed competitive set, nine of ten platforms resolve tenant context ad hoc per call site, and the one confirmed production bug of this class produced silently stale results rather than an error — the worst kind, because nothing alerts.

## Decision

When a required piece of state cannot be verified against an authoritative store at decision time, the request is **rejected**. It is never served optimistically.

Specifically:

1. **Identity.** A missing tenant scope is a `PermissionDeniedError`, not an unfiltered query. There is no code path that widens scope when scope is undetermined. `scope_key` is derived server-side and required (see [ADR-0005](0005-server-derived-scope-key.md)).
2. **Budget.** Remaining balance must be readable from the authoritative ledger before dispatch. Unavailable ledger, replica lag beyond tolerance, or an unverifiable cache state returns `503`. Cost is pre-authorized against the balance before dispatch and settled on completion.
3. **Secrets.** A secret alias that cannot be resolved fails the node. No default, no empty string, no shared fallback value.

Rate limiting is explicitly *not* in this category. A rate limiter that fails open under load is a reasonable degradation; a budget that fails open is unbounded liability. The distinction is availability of service versus integrity of accounting.

## Consequences

**Positive**

- Tenant isolation becomes a property that can be tested rather than reviewed. The absence of a scope is now an observable error, so the cross-tenant probe suite in §5.3.1 has something to assert against.
- Cost accounting is exact at request time, so `plan_tier` enforcement and usage invoices agree with each other by construction.
- Failure modes are loud and typed (`PermissionDeniedError`, `503`), so they surface in error budgets instead of as a slowly diverging ledger.

**Negative**

- A ledger or identity-store outage becomes an availability event for all tenants on that path, not a degradation. This is a deliberate trade: we accept an outage over unbounded unbilled usage.
- Pre-authorization adds a write to the request path. It must be cheap (single-row conditional update, or an authoritative counter we own) or latency suffers.
- Reconciliation is still required, because pre-authorization estimates can diverge from settled cost. It is a repair path, not the enforcement path — which is the point.

**Operational requirement.** This decision is only meaningful if the authoritative store's availability is a monitored, alerted dependency. A fail-closed control in front of an unmonitored store is an outage waiting for a bad week.

## Rejected alternatives

- **Fail open with reconciliation** (the LiteLLM default). LiteLLM's documentation states plainly that budgets are enforced against spend read from the database and that none of them cap anything on a DB-less deployment. It offers hard enforcement as opt-in and notes that its Redis counter can under-report after a restart. Rejected: we do not want the accounting integrity of a multi-tenant SaaS to depend on an opt-in flag being set correctly in production.
- **Best-effort with alerting.** Rejected for the same reason, with more operational noise.
- **Per-tenant isolation stores.** Rejected: multiplies the authoritative-store count and therefore multiplies the outage surface, without changing the trust model.