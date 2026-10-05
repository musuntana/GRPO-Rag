# ADR-0007: Per-Tenant Offline Reranker Training

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §8.5, §11.0, §12, §13.3
- **Supersedes:** nothing

## Context

§8.5 fixes the retrieval shape: oversample at recall (top 50), rerank a smaller set (top 20), grade on the reranked set, pass top K into generation. The reranker is named as an abstraction and deliberately not resolved — `reranker.py  # Cross-encoder or LLM reranker via model gateway`. No model is named anywhere in the document set.

That ambiguity is the correct default for a system whose whole advantage is measured retrieval quality (§13.3) and whose stated differentiation is that every retrieval change is gated on measured results. Renting a cross-encoder is the right first position: it works, it costs one gateway call, and it requires no training pipeline to maintain.

The problem is that a rented reranker is a constant. Its quality is fixed at whatever the provider shipped, and it cannot be adapted to a tenant's corpus — which is exactly where the headroom is. A reranker trained on a tenant's own enterprise documents, with negatives drawn from that tenant's own confusable pages, sees a decision boundary that no general-purpose model has been trained on. In document-heavy domains the confusable negatives are structural rather than random: Q1 beside Q2, FY24 beside FY25, the summary page beside the detail page it summarizes, a superseded policy version beside the current one. A general cross-encoder has no way to learn those distinctions from a vendor's training mixture.

The survey does not offer evidence against doing this, and one-directional evidence for it: no surveyed platform ships any reranker training or per-tenant retrieval adaptation at all. RAGFlow, the strongest document-ingestion platform in the set, ships rerank and stops there.

Two decisions have to be made, and they are separable:

**Where does training run?** Never in a request path. A training step is minutes-to-hours of compute; a rerank is a latency-budgeted call. This is not a close call — it is the same class of separation as execution isolation from network isolation (ADR-0003), and conflating them is how a request-path regression becomes a training regression.

**Who owns the trained artifact?** A reranker trained on one tenant's documents is a derivative of that tenant's data. Serving it to a second tenant is a data-governance event, not a scaling win. Conversely, refusing to train at all forfeits the one part of retrieval quality that a SaaS can genuinely differentiate on, because the eval corpus and the training corpus are the same tenant's data and the price book (§12) and eval set (§13.3) are ours.

## Decision

**ragos owns an offline, per-tenant reranker training capability. Its artifact is registered as a `model_profile`, served through the existing gateway, and promoted only through the regression gate.**

### 1. Four stages, in order, and the fourth is optional

```text
1. mine    hard negatives from the tenant's own eval set + feedback
2. sft     distilled cross-encoder baseline          -> shippable
3. grpo    outcome-reward policy optimization         -> experiment
4. promote artifact -> model_profile -> registry      -> gated
```

**Stage 2 is the deliverable. Stage 3 is an experiment that must beat stage 2 on the eval set to exist.** This mirrors ADR-0004's reasoning: we own the corpus and the eval, we rent nothing we can measure ourselves, and we do not adopt complexity because it is more capable. If GRPO does not beat the SFT baseline on a tenant's eval set, the SFT baseline ships and GRPO stays unused. That outcome is a success for this decision, not a failure of it.

### 2. Hard negatives are **ungraded**, not irrelevant

This is the detail that decides whether the capability helps or quietly hurts.

Mining selects negatives the retriever already scores highly: same-document other pages, adjacent pages, cross-period siblings, retriever-top-ranked-but-ungraded. The resulting negative set is graded `1` (relevant-but-not-answering) more often than graded `0` (irrelevant).

Collapsing that into a binary label is the common failure. A `grade 1` negative is a **harder and more useful** training signal than a `grade 0` negative — it teaches the temporal, entity, and document-structure distinctions that are the entire source of the headroom. But it is also, by definition, partially relevant, and training on it as if it were noise pushes the model toward over-confident rejection. A retriever tuned that way stops returning the summary page when the detail page is also present, which is a regression that will not show up on any metric that only asks "was a relevant page retrieved".

So: `grade 1` negatives are mined, capped in count relative to `grade 0` negatives, and excluded from the `grade 0` bucket in every reported metric. Mining more of them is not automatically better, and the ratio is a recorded, evaluated parameter of the miner.

### 3. Reward design, if GRPO runs

```text
R = alpha * hit_at_k + beta * ndcg_at_k + gamma * hard_negative_demotion
```

- `hit_at_k` — did a gold page reach the top K evidence set
- `ndcg_at_k` — graded position quality, which is what distinguishes `grade 1` from `grade 2`
- `hard_negative_demotion` — did the mined negatives end up below the gold

Per-term weights are bounded and recorded. Reward is computed **against frozen gold labels on the holdout split only**, never against the model's own output. A KL penalty to the SFT reference is applied.

### 4. Reward-hacking guards

Training optimizes a proxy. Four guards, each targeting a specific way the proxy diverges from the goal:

- **Gold-only reward.** Never self-reward. A reranker that ranks what it was rewarded for ranking is not evidence of anything.
- **Holdout-only evaluation.** Split discipline: `dev` drives thresholds and hyperparameters, `holdout` is touched only to decide promotion. Tuning on holdout converts the metric into a report of the tuning.
- **Cross-metric coupling.** If reward rises while `citation_accuracy` or `abstention_accuracy` falls, promotion is blocked. A reranker that improves ordering by promoting confident-but-wrong pages raises hit rate and lowers attribution quality — the exact failure mode of reward on hit rate alone.
- **Train-set improvement is a bug, not a result.** A run that improves the eval set it mined from is treated as a failed run until explained.

### 5. Isolation and cost

- A trained artifact is **per-tenant by default**. It is stored under `{scope_key}/models/reranker/{version}/`, and a profile may only reference an artifact inside its own `scope_key` namespace. There is no cross-tenant artifact path, and adding one is a separate decision with its own ADR.
- Training and eval token spend is **ops cost on the training run**, not tenant request cost. It is not charged against the request path's per-run budget breakdown, because it is not attributable to a tenant conversation.
- Training runs carry an audit entry — actor, `scope_key`, dataset version, artifact version, promotion decision. A trained artifact is a security-relevant object; who produced it from what is an audit question.

### 6. Served through the gateway, always

A trained reranker is a `model_profile` with `kind="reranker"` and an artifact reference. It is invoked through the gateway `rerank()` method, exactly like a hosted cross-encoder. It is never loaded into a request handler, never called directly from feature code, and never bypasses the key pool, quota, or cost accounting. Reinforcing guardrail 9: there is no code path from a retrieval feature to a model that is not the gateway.

## Consequences

**Positive**

- Reranker quality becomes a per-tenant asset that improves with the tenant's own data, which is a real differentiator in a market where nothing surveyed ships any training.
- The pipeline is reusable: the same mined pairs train a future embedding model, and the same eval set grades it.
- Owning training means owning the ability to react when a provider deprecates a reranker model, instead of being pinned to whatever is available.
- GRPO is falsifiable inside our own gate. We can say with evidence whether outcome-reward training helped a given tenant, because we hold both its data and its eval set.

**Negative**

- We own a training loop and its failure modes: seed sensitivity, reward hacking, artifact staleness, and a model that degrades silently as the corpus shifts beneath it.
- Training compute is a real cost with no direct revenue line attached in Phase 1. It is bounded by explicit budget pre-authorization on the training run.
- Stage 3 may simply not earn its place. That is acceptable and is why it is gated behind stage 2 rather than shipped alongside it.
- Per-tenant artifacts multiply: N tenants means N models to version, monitor, and re-promote as corpora change. Artifact staleness needs an explicit policy, not an afterthought.

**Revisit when.** Per-tenant artifact count makes operations unmanageable (hundreds of stale artifacts with no promotion path), or when a cross-tenant artifact becomes operationally necessary — in which case it needs a consent model and its own ADR, because it is a data-governance change and not a scaling change.

## Rejected alternatives

- **No training; rent the reranker permanently.** Rejected: keeps the constant. It is the correct Phase 1 position and it is preserved as stage 2's baseline, but as a terminal decision it forfeits the differentiator that our own survey identifies as the strongest defensible position — retrieval that measurably works, on the customer's data.

- **A single shared platform reranker trained on pooled data.** Rejected for now: pooling tenant documents into a shared artifact is a data-governance event requiring explicit consent, and it fails the isolation posture in ADR-0005 in spirit even when no individual document leaks — the artifact itself is derived from every participant's data. Revisit only with a consent model.

- **GRPO as the primary method, skipping the SFT baseline.** Rejected: it makes the expensive, hackable method the default and the cheap measurable one the fallback. Outcome-reward RL on a ranking task is a reasonable thing to try against a strong baseline and a poor thing to bet on without one.

- **Training in the request path, or on the ingest worker.** Rejected: it couples a latency-budgeted call to minutes-to-hours of compute, and it puts a mutable model on the same worker that must be reproducible. Same separation argument as ADR-0003.

- **Per-tenant fine-tuning of the *embedding* model rather than the reranker.** Rejected for now: changing the embedding model invalidates the Qdrant collection (§6.3.4 — never mix two embedding models in one collection), so it triggers a reindex and a shadow-collection migration. The reranker is the right first training target precisely because it requires no reindex.

- **Training on mined negatives from production traffic rather than the eval set.** Rejected: unlabelled traffic negatives have no gold positive to pair against, and the eval set's graded labels are the only source of the `grade 1` versus `grade 0` distinction the miner depends on. Feedback entries do feed mining, but as candidate negatives to be graded, never as labels.