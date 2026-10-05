# ADR-0006: Page-Anchored Retrieval and Verifiable Citations

- **Status:** Accepted
- **Date:** 2026-10-04
- **Affects:** `docs/architecture.md` §6.3, §7.4, §8.5, §8.6, §13.3
- **Supersedes:** nothing

## Context

The ingestion design in §7.4 defines a two-level hierarchy: 256-512 token children for retrieval matching, 1024-2048 token parents for generation context. The parent is a **token window**, chosen by greedy packing. That works when the corpus is flat text — Markdown, HTML, unformatted prose.

It degrades on the document classes enterprises actually buy a knowledge base for:

- **Financial statements and annual reports.** A quarter's revenue table sits on one page; the page *is* the semantic unit. A 1600-token window cuts through it, separating the header row from the figures, and a window boundary that lands mid-table produces a chunk that reads as a column of numbers with no column names.
- **Slide decks.** A slide is a self-contained claim. Its title, bullet body, and footnote belong together or not at all.
- **Contracts.** Clause boundaries are legal, not token-counted. A window that merges clause 4.2 and 4.3 produces a chunk that answers a different question than either clause.
- **Dashboards and chart-heavy reports.** Most of the meaning is visual. A text window over a chart page produces prose describing nothing.

The measured failure of fixed windows is recorded in §7.4 already — a fixed window "splits tables away from their header row and separates headings from the content they introduce, and both failures silently degrade retrieval because the embedded chunk loses its meaning." Page anchoring applies the same principle that §7.4 applies to flat text, one level up.

The second problem is citations. §7.4 requires `start_char_idx` / `end_char_idx` per chunk and states that "citation linking depends on it." The pipeline emits a `citation_linked` event carrying `{doc_id, chunk_id, text}`. Three things follow from that shape:

1. A user cannot navigate to a page. Enterprise knowledge questions are answered by pointing at a page in a PDF or a slide number in a deck, not a character span.
2. **Nothing verifies that the cited span supports the claim.** `chunk_id` is produced by the generator and consumed by the renderer. Faithfulness is an offline RAGAS number (§13.3), never a check on a delivered answer. A confidently wrong citation is indistinguishable from a correct one at the API boundary.
3. An unverifiable citation cannot be repaired or suppressed programmatically. There is no denominator to reason about — no count of claims, no count of supported claims, nothing to gate on.

## Decision

**The page is the parent unit for paginated sources, and every delivered citation is verified in two layers before it leaves the service.**

### 1. `parent_unit` is recorded, never inferred

```text
parent_unit = "page"          # paginated sources: PDF, PPT/PPTX, DOCX
parent_unit = "token_window"  # flat text: Markdown, HTML, plain text, CSV-as-text
```

This is a property of `chunker_version`, not a runtime guess. A document is parsed, its structure is classified, and the strategy is frozen onto the chunker version alongside the child/parent sizes. At query time nothing re-derives it — the profile points at a chunker version and the hierarchy is already in the index.

Inferring at query time would mean the same chunk id resolving to different granularity depending on which code path read it, which is exactly the class of defect that produced the confirmed cross-tenant index bug in the survey (endpoints disagreeing about which index a dataset lives in). The granularity is a property of the *index*, and it is versioned with the index.

Under `parent_unit = "page"`:

- the page is the parent chunk, carrying `page_no` and `page_count`
- children are semantic spans **within** that page, 256-512 tokens, packed with the existing greedy sentence packer
- a page whose text exceeds the child budget produces multiple children; a page with less produces one
- retrieval still matches on children, and expansion still resolves children to parents — the parent is simply a page instead of a token window, so the generation context is a whole financial page rather than an arbitrary 1600 tokens of it

Under `parent_unit = "token_window"` nothing changes from §7.4.

### 2. Element boundaries are hard

A table, a figure caption, a list block, a code block, and a heading's section body are atomic. A child chunk never spans a boundary between two element types, and a single element larger than the child budget becomes one **oversize** child rather than a broken one.

This is the same rule §7.4 already states for tables and headings, promoted from "prefer snapping boundaries to structure" to a hard constraint with an oversize fallback. Preferring is not enough, because a boundary violation degrades silently — the chunk still embeds, still matches, still returns, and is wrong.

Tables are **linearized to text at parse time** (markdown with the header row bound to every body row) so a table is retrievable through the text channel without a table-specific embedding path. The linearized form is what gets embedded and searched; the original geometry is retained as layout metadata for display.

### 3. Offsets are document-global and page-relative, both

`start_char_idx` / `end_char_idx` remain **document-global**, computed with the advancing-cursor rule §7.4 already mandates. Searching from offset zero is still forbidden and still corrupts citations under overlap.

On top of that, every chunk carries `page_no`, `page_offset_start`, and `page_offset_end`. A citation therefore resolves to a precise span *and* a page. The page number is the navigation affordance; the span is the precision guarantee. Neither replaces the other — a page without a span cannot be verified, and a span without a page cannot be presented to a human looking at a PDF.

### 4. Citations are verified in two layers before delivery

```text
generation
  -> citation extraction
  -> STRUCTURAL verification   (deterministic, always on, zero LLM cost)
  -> SEMANTIC verification     (entailment, eval always / online behind profile flag)
  -> delivery
```

**Structural** checks, all programmatic, all fail-closed:

- every cited `chunk_id` is present in the evidence set actually delivered to the generator
- every cited `page_no` is within `[1, page_count]` for that document version
- spans are within bounds and non-degenerate

**Semantic** checks whether each cited span entails the claim attributed to it, via NLI or an LLM judge. It runs unconditionally in evaluation. Online it is gated on a profile flag because it costs an LLM call per claim, and an unpriced per-claim call on the request path is a cost defect, not a feature.

Both layers must be recoverable from the citation object itself. A citation carries `chunk_id`, `doc_id`, `page_no`, `start_char_idx`, `end_char_idx`, the claim text, and the verification verdict. There is no separate log to join against.

### 5. Verified citations become a scored metric with a denominator

```text
citation_accuracy   = supported_citations / generated_citations
citation_completeness = cited_claims / supported_claims
```

Both are computed from the `supported_claims` recorded on an eval case. Because the denominator is explicit, a regression in attribution is visible as a number rather than as an impression. §13.3 records where these live.

### 6. The verification result is visible to the user

A citation that fails semantic verification is not silently dropped. It is either suppressed (the claim is removed) or rendered as unverified. The API response distinguishes `verified`, `unverified`, and `suppressed` citation states. Presenting an unverified citation identically to a verified one makes the verification pointless.

## Consequences

**Positive**

- Generation context becomes a semantically complete unit for the document classes enterprises actually use. A financial quarter is answered from its whole page, table header included.
- Citations are navigable. A user gets a page number and a span, and the span is verified against the claim.
- Attribution becomes measurable, and therefore regressable — which is what makes it a gate rather than a claim.
- Layout metadata (`element_type`, `layout_bbox`, `page_image_ref`) captured at parse time is reusable by multimodal retrieval later, without re-parsing.

**Negative**

- A paginated source yields **variable-size parents**. `parent_size` no longer bounds a page parent, so the majority-sibling merge rule in §7.4 has to handle parents of unbounded token length. A dense appendix page can be an order of magnitude larger than a title page, and both are one parent.
- A page with almost no text (a full-bleed chart) produces an empty or near-empty child. Whether such a page is indexed at all is a per-KB decision, because a page with no text cannot be matched lexically and its text representation may be a caption that misleads retrieval.
- Parsing is materially harder. Layout analysis is now load-bearing for correctness, not a cosmetic improvement to chunk quality. A parser regression now changes retrieval results, where before it only changed chunk text.
- The `search` schema, the Qdrant payload, `SearchChunkCommon`, the chunker, and the citation type all change. This is the widest-reaching single change in the document set since the v1.1 scope-key revision.

**Revisit when.** The variable-parent-size problem becomes materially annoying — that is, when the majority-sibling merge shows a measurable context-quality cost on page parents, or when page-level parents routinely exceed the generation context budget and force truncation mid-page. At that point a page *segment* hierarchy (heading-bounded sections within a page, page as grandparent) is the natural next step, and it composes with this design rather than replacing it.

## Rejected alternatives

- **Page as the sole retrieval unit — one vector per page, no children.** Rejected: it discards the parent-child machinery that §7.4 justifies with measurements (0.778 → 0.897 hit rate from resolving children to parents), and it makes precision worse, because a page is a coarse matching target. The child's role is precisely to be a fine one. The resume-shaped "page-level retrieval" framing is satisfied by page-as-parent: the retrieval *unit delivered to the generator* is the page.

- **Page as parent with fixed-size children that ignore element boundaries.** Rejected: reintroduces exactly the table-splitting failure §7.4 exists to prevent, just at page scope.

- **Page-aware only at citation time** — keep token-window parents, add `page_no` to chunks, expand to parents as before. Rejected: cheaper, and it leaves the generation-context failure in place. The whole argument for anchoring is that the *delivered context* is semantically wrong without it. If only citations become page-aware, the answer is still assembled from arbitrary 1600-token windows.

- **Replace span citations with page-only citations.** Rejected: a page cannot be verified against a specific numeric claim as cheaply or precisely as the span containing it, and §7.4's advancing-cursor rule exists precisely because span resolution is hard enough to get wrong. Dropping spans would be a regression in the property that was hardest to get right.

- **Semantic verification as the only layer.** Rejected: it is an LLM call per claim on the request path, so it cannot be the thing that guarantees a citation is well-formed. A hallucinated `chunk_id` must be caught deterministically before it reaches a judge that might be talked into accepting it.

- **Verify citations only in evaluation.** Rejected: that is the status quo and it is the gap. A user receives the same unverified output whether or not an offline run happened to grade it.