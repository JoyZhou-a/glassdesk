"""检索层测试：过滤表达式、父块去重、出处格式。

不连数据库、不加载模型——用假对象替换 store / searcher / reranker。
"""

from __future__ import annotations

import pytest

from glassdesk.constants import Authority
from glassdesk.retrieval import filters
from glassdesk.retrieval.search import Hit
from glassdesk.retrieval.service import RetrievalService, RetrievedChunk


# ======================================================================
# 过滤表达式
# ======================================================================
def test_authority_expr_builds_milvus_syntax():
    expr = filters.authority_expr([Authority.MANUAL, Authority.STANDARD])
    assert expr == 'authority in ["manual", "standard"]'


def test_authority_expr_accepts_plain_strings():
    assert filters.authority_expr(["manual"]) == 'authority in ["manual"]'


def test_authority_expr_empty_means_no_filter():
    assert filters.authority_expr(None) == ""
    assert filters.authority_expr([]) == ""


def test_doc_id_expr_quotes_values():
    assert filters.doc_id_expr(["manual-tempering"]) == 'doc_id in ["manual-tempering"]'


def test_expr_escapes_double_quotes():
    """值里出现双引号必须转义，否则表达式会被截断。"""
    expr = filters.doc_id_expr(['a"b'])
    assert '\\"' in expr


def test_combine_joins_with_and():
    expr = filters.combine('authority in ["manual"]', 'doc_id in ["x"]')
    assert expr == '(authority in ["manual"]) and (doc_id in ["x"])'


def test_combine_skips_empty_parts():
    assert filters.combine("", 'authority in ["manual"]') == 'authority in ["manual"]'
    assert filters.combine("", "") == ""


# ======================================================================
# 出处格式（对应 PRD 的 I3）
# ======================================================================
def test_citation_includes_clause_and_page():
    chunk = RetrievedChunk(
        chunk_id="c", text="t", score=1.0, doc_id="d", authority="manual",
        clause="Table 4-1", page_no=12,
    )
    assert chunk.citation() == "(Table 4-1, p.12)"


def test_citation_falls_back_to_doc_id():
    chunk = RetrievedChunk(chunk_id="c", text="t", score=1.0,
                           doc_id="manual-tempering", authority="manual")
    assert chunk.citation() == "(manual-tempering)"


def test_context_prefers_parent_text():
    chunk = RetrievedChunk(chunk_id="c", text="child", score=1.0, doc_id="d",
                           authority="manual", parent_text="parent body")
    assert chunk.context == "parent body"


def test_context_falls_back_to_own_text():
    chunk = RetrievedChunk(chunk_id="c", text="own body", score=1.0,
                           doc_id="d", authority="manual")
    assert chunk.context == "own body"


# ======================================================================
# 父块去重
# ======================================================================
class FakeStore:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[str] = []

    def fetchall(self, sql: str, params=None):
        self.queries.append(sql)
        if "kind" in sql or "status" in sql:
            return self.rows
        # 父块查询：返回 rows 里带 parent_text 的部分
        return self.rows


def make_service(settings, store=None, reranker=None):
    return RetrievalService(
        settings, store or FakeStore(), milvus=None, embedder=None, reranker=reranker
    )


def _hit(cid: str, score: float) -> Hit:
    return Hit(chunk_id=cid, score=score, doc_id="d", authority="manual")


def _row(cid: str, parent: str | None, text: str = "body") -> dict:
    return {"chunk_id": cid, "text": text, "parent_id": parent,
            "doc_id": "d", "clause": "c", "page_no": 1}


def test_dedupe_keeps_highest_scoring_chunk_per_parent(settings):
    service = make_service(settings)
    ordered = [
        (_hit("a", 0.9), _row("a", "p1")),
        (_hit("b", 0.8), _row("b", "p1")),   # 同父块，应被丢弃
        (_hit("c", 0.7), _row("c", "p2")),
    ]
    result = service._dedupe_by_parent(ordered, limit=5)
    assert [c.chunk_id for c in result] == ["a", "c"]


def test_dedupe_respects_limit(settings):
    service = make_service(settings)
    ordered = [(_hit(f"c{i}", 1.0 - i * 0.1), _row(f"c{i}", f"p{i}")) for i in range(10)]
    result = service._dedupe_by_parent(ordered, limit=3)
    assert len(result) == 3


def test_dedupe_treats_missing_parent_as_own_key(settings):
    service = make_service(settings)
    ordered = [(_hit("a", 0.9), _row("a", None)), (_hit("b", 0.8), _row("b", None))]
    result = service._dedupe_by_parent(ordered, limit=5)
    assert [c.chunk_id for c in result] == ["a", "b"]


# ======================================================================
# 重排开关
# ======================================================================
class FakeReranker:
    def __init__(self, order: list[int]) -> None:
        self.order = order
        self.calls = 0

    def rerank(self, query: str, items: list[tuple[str, object]]):
        self.calls += 1
        return [(items[i][1], 1.0 / (i + 1)) for i in self.order]


def test_apply_rerank_uses_reranker_when_available(settings):
    reranker = FakeReranker([1, 0])
    service = make_service(settings, reranker=reranker)
    hits = [_hit("a", 0.9), _hit("b", 0.1)]
    rows = {"a": _row("a", "p1"), "b": _row("b", "p2")}

    ordered = service._apply_rerank("q", hits, rows, use_rerank=True)
    assert [h.chunk_id for h, _ in ordered] == ["b", "a"]
    assert reranker.calls == 1


def test_apply_rerank_falls_back_to_search_score(settings):
    reranker = FakeReranker([1, 0])
    service = make_service(settings, reranker=reranker)
    hits = [_hit("a", 0.9), _hit("b", 0.1)]
    rows = {"a": _row("a", "p1"), "b": _row("b", "p2")}

    ordered = service._apply_rerank("q", hits, rows, use_rerank=False)
    assert [h.chunk_id for h, _ in ordered] == ["a", "b"]
    assert reranker.calls == 0


def test_apply_rerank_skips_hits_missing_from_mysql(settings):
    """Milvus 有、MySQL 没有的脏数据要被丢掉，不能报 KeyError。"""
    service = make_service(settings)
    hits = [_hit("a", 0.9), _hit("ghost", 0.5)]
    rows = {"a": _row("a", "p1")}

    ordered = service._apply_rerank("q", hits, rows, use_rerank=False)
    assert [h.chunk_id for h, _ in ordered] == ["a"]


# ======================================================================
# 空检索
# ======================================================================
def test_retrieve_returns_empty_result_without_hits(settings):
    class EmptySearcher:
        def search(self, *a, **kw):
            return []

    service = make_service(settings)
    service.searcher = EmptySearcher()
    result = service.retrieve("anything")
    assert result.chunks == []
    assert "total" in result.timings_ms
