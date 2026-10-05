# ADR-0002: Human-Input Resume Requires POST

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §9.2.3
- **Supersedes:** nothing

## Context

Human-in-the-loop delivery in this product is expected to happen over channels we do not control: email, Slack, Teams, Telegram. The approval link is a bearer token for a state transition — whoever holds it decides the outcome of a suspended workflow run.

Email and chat security scanners prefetch links. Safe Links, Mimecast, Proofpoint and comparable products fetch a URL in the background on receipt to determine whether it is safe, before any human sees it. That fetch is an ordinary GET. It arrives at our resume endpoint carrying whatever value the scanner's request contains.

If resume is a GET, the scanner completes the approval. The workflow resumes with a value derived from a link-rewriting or tracking parameter, or with an empty or malformed value, before the intended approver has read the request. Nobody is paged. The run simply completes with the wrong answer.

This is not a theoretical concern: it is a documented production failure in a comparable workflow platform, where the fix was to require an explicit POST and render a confirmation page for safe methods.

## Decision

**A resume endpoint must not change workflow state on GET or HEAD.**

1. Resume links are single-use, unguessable tokens.
2. `GET` / `HEAD` on a resume URL renders a confirmation page describing the pending action and does **not** consume the token or mutate state.
3. `POST` on that URL performs the resume and consumes the token.
4. An already-consumed token returns a terminal "already responded" state with the recorded outcome, rather than an error that looks retryable.
5. Negotiate on `Accept` so programmatic clients can receive a machine-readable result without an HTML interstitial.
6. Require an authenticated approver identity in addition to the token, for any approval that is tenant-scoped. Possession of the link should not be sufficient where we can avoid it.

The same rule applies to every channel: Slack interactive buttons, Teams task modules, and Telegram callbacks are POST-shaped by construction, so the constraint is naturally satisfied there — but the link-based fallback for those channels must obey it too.

## Consequences

**Positive**

- Link scanners, chat unfurlers, and security middleboxes can fetch any link in our messages without consequence.
- Resume becomes auditable: a resume is an authenticated request with a recorded actor, which the audit log (architecture §13.5) can join to `trace_id`.
- The confirmation page gives us a place to render the pending action for the approver, which improves the approval UX we would otherwise ship with a bare "click to approve" URL.

**Negative**

- Two requests instead of one for approval, and a confirmation page to build and maintain per channel.
- Bookmarkable approval links stop working, which some users will find mildly annoying. Accepted: a bookmark that silently approves on visit is worse.
- Requires that the confirmation page render without executing tenant-controlled script content, so it must be a server-rendered template with escaped output rather than a client-rendered surface.

## Rejected alternatives

- **Resume on GET.** Rejected: link scanners complete the approval.
- **Signed, non-replayable links on GET.** Rejected as a partial mitigation: a scanner's GET is indistinguishable from a legitimate one, so signing authenticates the *link* without authenticating the *actor*. It does not fix the problem.
- **Never link to resume; require in-app approval only.** Rejected as the primary model — it excludes email-only and mobile workflows, which is where approval volume actually lands. Retained as an option for tenants whose security posture requires it.