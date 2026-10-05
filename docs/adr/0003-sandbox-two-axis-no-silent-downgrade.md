# ADR-0003: Two-Axis Sandbox Isolation With No Silent Downgrade

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §11.1, §11.2, §11.3
- **Supersedes:** the single-axis isolation requirements in architecture v1 §11.1–§11.2

## Context

Tenants author `Script` nodes and HTTP endpoints that the platform executes on their behalf. That is untrusted code in our process space, and it is the surface where this product category has the worst track record.

The competitive survey found remote-code-execution advisories in four separate projects:

- **Flowise**, three advisories against a NodeVM "sandbox": missing route-level authorization on the custom-function endpoint; caller-supplied `nodeVMOptions` spread over and overriding the security defaults, including `require.builtin: ["*"]`; and an SSRF filter bypass because the deny-list was attached to two HTTP clients while raw `http`/`https`/`net` remained reachable, reaching the cloud instance metadata endpoint. It also silently fell back from a real sandbox to the in-process isolate whenever a provider key was unset.
- **MaxKB**, CVE-2025-53927 (RCE in the tool module) and CVE-2025-53928 (RCE during MCP calls).

Three recurring root causes:

1. **Isolation treated as one property.** A sandbox was considered "isolated" if it restricted code execution, with network reach handled by ad-hoc filtering in the HTTP client layer. Those are two different controls with two different enforcement points, and conflating them produces a sandbox that is neither.
2. **Degradation instead of failure.** Unconfigured or unavailable isolation fell back to something weaker rather than erroring. Every fallback path is an unadvertised, unmonitored, weaker security boundary.
3. **Caller-controlled security configuration.** Node config flowed over isolation options, so a tenant-authored definition could widen a boundary.

None of these products had an adversarial tenant; the exploit worked against the default configuration.

## Decision

Execution isolation and network isolation are **two independent axes**, each with its own configuration, its own enforcement point, and its own default.

### Axis A — Execution isolation

A tiered ladder (architecture §11.1), from `UNSANDBOXED` through an unprivileged per-execution isolate, to namespace-isolated containers, to kernel-enforced isolation (nsjail or gVisor class). Each tier documents its privilege requirement and its blast radius.

### Axis B — Network isolation

Default deny. Enforced at the sandbox runner's egress boundary and at the HTTP egress proxy — not inside individual HTTP libraries. Metadata endpoints blocked, DNS resolution checked, allowlists explicit.

### Cross-cutting rules

1. **No silent downgrade, ever.** If the configured tier is unavailable or unconfigured, the execution fails closed with an error. Never fall back.
2. **Security configuration is not caller-configurable.** Isolation and network settings come from platform policy. A workflow definition may *request* a tier; it may not widen one.
3. **One audited egress path.** HTTP nodes, Script nodes, MCP tool invocations, and sandbox callbacks all traverse the same egress policy and produce the same audit record.
4. **Secrets are per-call and narrowly scoped.** Injected under an alias for the specific invocation, never persisted into the sandbox filesystem or a general environment.
5. **Least privileged tier that works.** Prefer the tier with the smallest blast radius that satisfies the node. Document that raising per-worker concurrency above 1 widens the resource-exhaustion blast radius from one job to every in-flight job in that container.

## Consequences

**Positive**

- The three root causes above become structurally impossible: there is no fallback to be silent about, no per-library filter to bypass, and no caller-supplied security config to merge.
- The isolation matrix becomes a sales artifact. "Here is the blast radius of each tier, here is the audit log, here is the evidence that the tier you require was actually used on your last 10,000 runs" answers the enterprise security questionnaire that currently stalls deals in this category.
- One egress choke point means the audit log is complete by construction rather than by remembering to instrument each node type.

**Negative**

- Kernel-enforced isolation is an operational dependency with real cost — a dedicated sandbox-runner fleet, image management, and capacity planning that must keep pace with tenant demand.
- No general-purpose credential broker inside the sandbox. We inject narrowly-scoped credentials per command, which is more verbose to author and means some legitimate workflows cannot be expressed without a code change. We will document this limitation rather than imply stronger isolation than exists.
- Fail-closed means a misconfigured sandbox takes a feature offline rather than degrading it. Deliberate, and reversible by fixing configuration.

**Operational requirement.** Adversarial review of the sandbox and tool hub is a standing task, not a one-time review. Four separate projects in this category shipped RCEs in exactly this path, and the failure mode is a default configuration, not an exotic one.

## Rejected alternatives

- **In-process V8 isolate as the sandbox** (Flowise's primary design). Rejected: three advisories, including a CVSS 9.9. An isolate is a resource-containment mechanism, not a security boundary against a determined caller.
- **Trusted execution only (no tenant-authored code).** Rejected: `Script` nodes are a core value proposition and Phase 2 requirement. Revisit only if Phase 1 shows tenants will not use them.
- **Per-library SSRF filtering.** Rejected: it is bypassed by any dependency that opens a socket, and the bypass is trivially available to anyone who reads the advisory.
- **Provider-managed sandbox.** Deferred, not rejected — the isolation axis may be delegated to a managed provider, but the *no-downgrade* rule and the network axis stay ours either way, since a provider-managed runtime cannot be assumed to enforce our egress policy.