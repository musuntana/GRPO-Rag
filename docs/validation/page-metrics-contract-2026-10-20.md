# Page-aware evidence metrics contract (2026-10-20)

This change prevents a non-answer page in the correct PDF from counting as a
gold-page hit. The evaluator matches `(document_id, page_no)`, not just
`document_id`. An unpaged result does **not** match paginated gold. Legacy
unpaged results still match unpaged gold.

Affected: Precision@K, Recall@K, MRR, NDCG@K and Evidence Hit@6. Earlier
metric reports used different semantics and **must be recomputed**, not directly
compared to new page-aware scores.

Regression cases in `tests/test_retrieval_metrics.py`: wrong page, correct
page after wrong page, multiple pages in one document, and missing page number.

Follow-up on the same review branch: `training/grpo.py` now accepts
`RankedPage(document_id, page_no)` for sampled page identity and does not
copy the page number from gold. Same-document/different-page negatives are
mined; reward demotion explicitly compares positive/negative rank positions.
Missing hard negatives do not earn a free demotion reward. Plain string
identities remain for unpaged legacy training data.

This is still **offline reward/data preparation**, not a genuine GRPO
optimization loop. The page-aware SFT text resolver, actual provider,
LoRA optimizer, train/dev/holdout, and real experimental results are
separate unimplemented integration work.

## SFT page-aware text resolution

The offline SFT pair builder now requires an exact `(doc_id, page_no)` text
lookup for all paginated evidence. A plain `doc_id` text key is accepted
only when the mined pair has `page_no=None`. Missing or empty text raises a
validation error instead of silently deleting training pairs.

`dataset_digest` now also includes each example's document/page identity,
case ID, actual text, negative grade and weight. This makes pre/post patch
digest strings **intentionally incompatible**; recompute experiment metadata.
The SFT helper still constructs metadata and pairs, not fitted weights or a
deployable cross-encoder.
