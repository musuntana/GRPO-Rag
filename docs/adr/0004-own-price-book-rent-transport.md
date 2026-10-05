# ADR-0004: Own the Price Book, Rent the Transport

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §12, §12.1, §11.0.2
- **Supersedes:** nothing

## Context

`packages/model-gateway` must provide four objects — `provider`, `key_pool`, `model_profile`, `tenant_binding` — and record the resolved provider and profile for every run so cost can be attributed. The competitive question is whether to build that control plane or adopt one.

The serious candidates:

- **LiteLLM** (MIT outside `enterprise/`) — deepest available tenant-key and quota model: four nesting levels, budgets at seven levels including per-model, error-class-aware fallback maps, per-deployment cooldowns, JWT-claim-to-key mapping. Its documented caveats matter to us: budgets are enforced against spend read from a database and *none of them cap anything on a DB-less deployment*; the hot-path Redis counter can under-report after a restart until reconciled; and a request whose usage prices to zero is still written to the spend log with a zero cost, which they flag rather than silently absorb.
- **Bifrost** (Apache-2.0 core) — virtual keys as the governance entity and reusable access profiles, but adaptive load balancing and circuit breaking — arguably its most valuable routing feature — are enterprise-gated.
- **Portkey gateway** (MIT) — an excellent request-config schema, but virtual keys, budgets, and guardrails live in Portkey's managed control plane; the hybrid self-host model syncs deltas on a one-minute heartbeat and serves from local cache.
- **Helicone `ai-gateway`** — repository metadata reports GPL-3.0 while the README and npm package say Apache. Unresolved; blocking for a proprietary product.
- **OpenRouter** — excellent routing semantics, closed source, 5.5% markup, not embeddable.
- **Envoy AI Gateway** — solves a different problem (routing across self-hosted inference pods), and its code is currently split across three repositories with the flagship integration already reverted once.

## Decision

**Build our own gateway control plane. Use LiteLLM as a library inside `packages/model-gateway` for provider normalization only. We do not deploy its proxy and it is never our billing authority.**

Two independent reasons, either sufficient on its own:

**1. Identity has exactly one home.** Our guardrail is that every tenant-scoped request resolves a canonical `tenant_ctx` before business logic runs. If a gateway resolves the tenant from a virtual key, `tenant_id` is established outside our service layer and must be re-validated inside it — and any divergence is a cross-tenant data-leak path, not a billing discrepancy. Keeping resolution in our own auth middleware means the gateway receives an already-validated derived identity instead of being trusted to establish one. Portkey's hybrid model makes this concrete: configs and keys sync from a control plane we would not own.

**2. The price book is the actual lock-in, and it is invisible until adopted.** Every gateway maintains its own cost model. If theirs is authoritative, our invoices inherit its pricing-table cadence, its provider-tier detection, and its zero-cost gaps, with no version pinned to our prompt and retriever versions. If ours is authoritative, we are reimplementing its completion-cost calculation anyway — which is a few hundred lines, not a reason to inherit someone else's ledger.

What we take from LiteLLM, concretely: provider normalization and its exception taxonomy (which maps onto our `ProviderError` / `TimeoutError` hierarchy), its cost map as a *seed* for our price book, error-class-aware fallback maps, per-deployment cooldowns, `order`-based priority failover, `retry_policy` keyed by exception type, and the rule that a fallback re-checks both model entitlement and remaining budget.

What we take from OpenRouter, as design only: soft-reorder performance thresholds (demote a non-compliant endpoint to the back of the list rather than excluding it, so a soft preference never becomes a hard failure), a short outage-memory signal, and in-flight spend pre-authorization.

What we take from Bifrost: the access-profile concept, so `/tenant/model-center` is self-serve without a platform administrator in the loop.

## Consequences

**Positive**

- `tenant_ctx` has exactly one source of truth (see [ADR-0005](0005-server-derived-scope-key.md)).
- Our cost model is versioned alongside prompt and retriever versions, so any historical invoice is reproducible.
- Switching gateway implementations later is a bounded exercise, because the only things we would need to migrate are transport and provider normalization — not identity, not the ledger, not the entitlement model.
- Quotas, key pools, and bindings stay in one schema with one transaction boundary, which is what makes fail-closed budget enforcement (ADR-0001) implementable at all.

**Negative**

- We own provider-normalization bugs, which is real ongoing work as providers change behavior.
- We must maintain our own cost table, including provider tier detection. Under-billing or over-billing is now our defect to own.
- We forgo some routing sophistication — adaptive load balancing in particular is a genuine gap that Bifrost gates and we will not build in phase 1. Simple-shuffle with cooldowns is an acceptable phase-1 position; adaptive balancing is a Phase 2 candidate once we have traffic to fit against.

**Revisit when.** Per-binding quota complexity outgrows what a few hundred lines can express, or adaptive load balancing becomes material to reliability. At that point Bifrost is the first candidate to re-evaluate — and because our price book and eval corpus are ours, that evaluation is a contained one.

## Rejected alternatives

- **LiteLLM proxy as the execution path.** Rejected on both reasons above. Its own documentation on the caller-supplied-identity defect is the clearest statement of the problem in the category.
- **Portkey as the control plane.** Rejected: the control plane is not ours to own.
- **OpenRouter as the execution path.** Rejected: we would surrender resolved-endpoint visibility needed for cost attribution, pay a markup, and inherit an in-flight budget that does not apply to enterprise accounts. Retained as an optional `provider` entry in our `provider` table, routed through our gateway like any other.
- **Helicone as a dependency.** Rejected pending license resolution.
- **Envoy AI Gateway.** Rejected: wrong problem domain and actively unstable.
- **Bifrost now.** Deferred, for the reason in Revisit when.