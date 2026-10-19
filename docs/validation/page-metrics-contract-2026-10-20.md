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

Remaining blocker: `training/grpo.py::_grades_from_order` still takes sampled
document IDs and copies page IDs from gold. It is not a valid source of
page-anchored GRPO rewards; change it to require explicit sampled page
identities before running page-based RL. No real model or training run is
claimed by this patch.
