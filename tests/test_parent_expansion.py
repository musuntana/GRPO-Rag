"""Child → parent expansion, wired to storage.

Architecture §7.4, ADR-0006. Under ``parent_unit = "page"`` the parent is a page
of unbounded length, so the majority-sibling rule is what stops one page being
emitted three times by three of its children. The rule needs a denominator, which
means the resolver has to fetch sibling counts rather than only parents — that
denominator is the claim under test here, along with the scoped parent lookup.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from ragos.core.errors import PermissionDeniedError
from ragos.core.tenant import TenantContext
from ragos.retrieval.identity import EvidenceIdentity
from ragos.retrieval.pipeline import RAGPipeline
from ragos.retrieval.recall.parents import SearchParentResolver
from ragos.retrieval.types import RetrieverProfile, ScoredChunk
from ragos.storage.repositories.search import SearchHit, SearchRepository, SearchTier

TENANT_ID = uuid4()


SYNTHETIC_TENANT_ID = UUID("00000000-0000-0000-0000-000000000101")


def _synthetic_candidate(**fields) -> ScoredChunk:
    """Explicit synthetic immutable lineage for unit tests, never production data."""
    doc = fields.get("doc_id") or uuid4()
    kb = fields.get("kb_id") or UUID("00000000-0000-0000-0000-000000000102")
    fields.update(doc_id=doc, kb_id=kb)
    start = fields.get("start_char_idx") or 0
    fields.setdefault("start_char_idx", start)
    fields.setdefault("end_char_idx", start + len(fields["text"]))
    page = fields.get("page_no")
    count = fields.get("page_count") or 20
    fields["page_count"] = count if page is not None else None
    fields["source_identity"] = EvidenceIdentity(
        tenant_id=TENANT_ID,
        kb_id=kb,
        document_id=doc,
        document_version_id=uuid5(doc, "synthetic-content"),
        representation_id=uuid5(doc, "synthetic-representation"),
        index_id=uuid5(doc, "synthetic-index"),
        chunk_id=fields["chunk_id"],
        representation_char_count=100000,
        parent_unit="page" if page is not None else "token_window",
        page_count=count if page is not None else None,
    )
    fields["source_text"] = fields["text"]
    return ScoredChunk(**fields)


def _ctx() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID, tenant_slug="acme", actor_id="u_1", trace_id="trace-expand"
    )


def _child(parent: UUID, *, score: float, page: int = 7) -> ScoredChunk:
    return _synthetic_candidate(
        chunk_id=uuid4(),
        doc_id=parent or uuid4(),
        kb_id=None,
        text="a child span",
        score=score,
        channel="dense",
        parent_chunk_id=parent,
        page_no=page,
        start_char_idx=page * 1000,
        end_char_idx=page * 1000 + len("a child span"),
    )


def _hit(chunk_id: UUID, body: str, *, page: int = 7, doc_id: UUID | None = None) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        document_id=doc_id or chunk_id,
        kb_id=UUID("00000000-0000-0000-0000-000000000102"),
        document_version_id=uuid5(doc_id or chunk_id, "synthetic-content"),
        representation_id=uuid5(doc_id or chunk_id, "synthetic-representation"),
        index_id=uuid5(doc_id or chunk_id, "synthetic-index"),
        representation_char_count=100000,
        parent_unit="page",
        layer="L1",
        body=body,
        metadata_json={},
        rank=0.0,
        page_no=page,
        page_count=20,
        start_char_idx=page * 1000,
        end_char_idx=page * 1000 + len(body),
        element_type="text",
    )


class _FakeRepo:
    """Records what the resolver asked for and replays staged results."""

    def __init__(
        self,
        hits: list[SearchHit] | None = None,
        counts: dict[UUID, int] | None = None,
    ) -> None:
        self.hits = hits or []
        self.counts = counts or {}
        self.get_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []

    def get_chunks_by_ids(self, *, tier, scope_key: str, chunk_ids: list[UUID]):
        self.get_calls.append({"tier": tier, "scope_key": scope_key, "chunk_ids": chunk_ids})
        wanted = set(chunk_ids)
        return [hit for hit in self.hits if hit.chunk_id in wanted]

    def count_children(self, *, tier, scope_key: str, parent_ids: list[UUID]):
        self.count_calls.append({"tier": tier, "scope_key": scope_key, "parent_ids": parent_ids})
        return {pid: self.counts[pid] for pid in parent_ids if pid in self.counts}


def _resolver(repo: Any) -> SearchParentResolver:
    return SearchParentResolver(repo)


# -- resolver ---------------------------------------------------------------


def test_a_parent_inherits_the_score_of_its_best_selected_child() -> None:
    """Otherwise a parent reached by one strong and one weak child ranks below the
    weak child's own parent."""
    parent = uuid4()
    repo = _FakeRepo([_hit(parent, "the whole page")], {parent: 4})

    resolved = asyncio.run(
        _resolver(repo).resolve([_child(parent, score=0.4), _child(parent, score=0.9)], _ctx())
    )

    assert resolved.parents[parent].score == pytest.approx(0.9)
    assert resolved.parents[parent].text == "the whole page"


def test_the_sibling_denominator_is_asked_for() -> None:
    parent = uuid4()
    repo = _FakeRepo([_hit(parent, "page")], {parent: 5})

    resolved = asyncio.run(_resolver(repo).resolve([_child(parent, score=0.9)], _ctx()))

    assert repo.count_calls[0]["parent_ids"] == [parent]
    assert len(resolved.children_by_parent[parent]) == 5


def test_duplicate_parent_references_are_fetched_once() -> None:
    parent = uuid4()
    repo = _FakeRepo([_hit(parent, "page")], {parent: 3})

    asyncio.run(
        _resolver(repo).resolve([_child(parent, score=0.9), _child(parent, score=0.8)], _ctx())
    )

    assert repo.get_calls[0]["chunk_ids"] == [parent]


def test_parents_are_fetched_under_the_request_scope() -> None:
    parent = uuid4()
    repo = _FakeRepo([_hit(parent, "page")], {parent: 2})
    ctx = _ctx()

    asyncio.run(_resolver(repo).resolve([_child(parent, score=0.9)], ctx))

    assert repo.get_calls[0]["scope_key"] == ctx.scope_key
    assert repo.count_calls[0]["scope_key"] == ctx.scope_key


def test_chunks_without_a_parent_never_touch_storage() -> None:
    repo = _FakeRepo()

    resolved = asyncio.run(_resolver(repo).resolve([_child(None, score=0.9)], _ctx()))

    assert resolved.is_empty
    assert repo.get_calls == []
    assert repo.count_calls == []


def test_an_unreadable_parent_does_not_silently_become_a_leaf() -> None:
    """A parent id that resolves to nothing must not degrade into delivering the child
    with a citation pointing at a page the reader cannot open."""
    parent = uuid4()
    repo = _FakeRepo([], {})

    resolved = asyncio.run(_resolver(repo).resolve([_child(parent, score=0.9)], _ctx()))

    assert resolved.is_empty


def test_resolution_without_a_scope_key_is_refused() -> None:
    ctx = _ctx()
    ctx = ctx.model_copy(update={"tenant_id": None})
    parent = uuid4()

    with pytest.raises(PermissionDeniedError, match="scope_key"):
        asyncio.run(_resolver(_FakeRepo()).resolve([_child(parent, score=0.9)], ctx))


# -- pipeline integration ---------------------------------------------------


def _profile(**overrides) -> RetrieverProfile:
    fields = {
        "id": uuid4(),
        "tenant_id": TENANT_ID,
        "recall_channels": ["dense"],
        "channel_budgets": {"dense": 1.0},
        "top_k_generate": 6,
    }
    fields.update(overrides)
    return RetrieverProfile(**fields)


async def _generator(_question, evidence, _ctx) -> str:
    return " ".join(f"page {c.page_no} [1]" for c in evidence[:1])


def _run_pipeline(resolver: Any, *, selected: list[ScoredChunk]) -> Any:
    async def recall(_query, _profile, _ctx):
        return list(selected)

    pipeline = RAGPipeline(
        recall_channels={"dense": recall},
        generator=_generator,
        parent_resolver=resolver,
    )
    return asyncio.run(pipeline.run("q", _ctx(), _profile()))


def test_a_page_is_emitted_once_for_three_of_its_children() -> None:
    parent = uuid4()
    doc_id = parent
    page_hit = _hit(parent, "the whole page", page=7, doc_id=doc_id)
    children = [_child(parent, score=0.9 - i * 0.1) for i in range(3)]
    repo = _FakeRepo([page_hit], {parent: 4})

    result = _run_pipeline(_resolver(repo), selected=children)

    assert [c.chunk_id for c in result.evidence] == [parent]
    assert result.retrieval_explain.parents_resolved == 1
    assert "page 7" in result.answer


def test_a_minority_of_children_keeps_the_span_instead_of_the_page() -> None:
    """The rule deduplicates parents; it must not delete the evidence the retriever found."""
    parent = uuid4()
    selected = [_child(parent, score=0.9)]
    repo = _FakeRepo([_hit(parent, "the whole page")], {parent: 4})

    result = _run_pipeline(_resolver(repo), selected=selected)

    # The page was resolved but not emitted, so the matching child survives as a leaf.
    assert [c.chunk_id for c in result.evidence] == [selected[0].chunk_id]
    assert result.retrieval_explain.parents_resolved == 1


def test_expansion_is_reported_as_zero_when_there_is_nothing_to_expand() -> None:
    """Distinct from "expansion ran and found nothing"."""
    selected = [_child(None, score=0.9)]

    result = _run_pipeline(_resolver(_FakeRepo()), selected=selected)

    assert result.retrieval_explain.parents_resolved == 0
    assert [c.chunk_id for c in result.evidence] == [selected[0].chunk_id]


def test_a_pipeline_without_a_resolver_expands_nothing_and_says_so() -> None:
    async def recall(_query, _profile, _ctx):
        return [_child(uuid4(), score=0.9)]

    async def generator(_question, _evidence, _ctx) -> str:
        return "answer [1]."

    result = asyncio.run(
        RAGPipeline(recall_channels={"dense": recall}, generator=generator).run(
            "q", _ctx(), _profile()
        )
    )

    assert result.retrieval_explain.parents_resolved == 0


# -- storage-side query -----------------------------------------------------


class _RecordingSession:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[str] = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        return _Result(self.rows)


def test_count_children_is_scoped_and_grouped_by_parent() -> None:
    parent_a, parent_b = uuid4(), uuid4()
    session = _RecordingSession([(parent_a, 4), (parent_b, 1)])
    repo = SearchRepository(session=session, tenant_ctx=_ctx())

    counts = repo.count_children(
        tier=SearchTier.STABLE, scope_key=_ctx().scope_key, parent_ids=[parent_a, parent_b]
    )

    assert counts == {parent_a: 4, parent_b: 1}
    sql = session.statements[0]
    assert "parent_chunk_id IN" in sql
    assert "GROUP BY" in sql
    assert str(TENANT_ID) in sql or "tenant_id" in sql


def test_count_children_of_nothing_asks_nothing() -> None:
    session = _RecordingSession()
    repo = SearchRepository(session=session, tenant_ctx=_ctx())

    counts = repo.count_children(tier=SearchTier.STABLE, scope_key=_ctx().scope_key, parent_ids=[])

    assert counts == {}
    assert session.statements == []


def test_count_children_refuses_a_foreign_scope_key() -> None:
    repo = SearchRepository(session=_RecordingSession(), tenant_ctx=_ctx())

    with pytest.raises(PermissionDeniedError, match="does not match"):
        repo.count_children(
            tier=SearchTier.STABLE, scope_key=f"{uuid4()}::u_1", parent_ids=[uuid4()]
        )
