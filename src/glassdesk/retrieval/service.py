"""检索编排：一条查询从文本走到"可读的段落 + 出处"。

完整链路::

    查询文本
      → bge-m3 编码（dense + sparse）
      → Milvus hybrid 召回（带权威/文档过滤）
      → 回 MySQL 取命中子块的正文        ← Milvus 不存正文
      → reranker 重排
      → 按父块去重（同一父块只留最高分）
      → 回 MySQL 取父块全文               ← small-to-big
      → 结果

为什么要"父子块"：子块小（约 300 字）才好被精确搜到；
但模型和人都需要完整上下文，所以命中子块后要把它的父块整段取出来。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from glassdesk.config import Settings
from glassdesk.logger import get_logger
from glassdesk.retrieval.rerank import Reranker
from glassdesk.retrieval.search import HybridSearcher, Hit
from glassdesk.storage import MilvusStore, MySQLStore

log = get_logger("retrieval.service")


@dataclass(slots=True)
class RetrievedChunk:
    """一条检索结果。"""

    chunk_id: str
    text: str
    score: float
    doc_id: str
    authority: str
    clause: str | None = None
    page_no: int | None = None
    parent_id: str | None = None
    parent_text: str | None = None

    @property
    def context(self) -> str:
        """喂给模型/展示给人看的上下文：优先用父块全文。"""
        return self.parent_text or self.text

    def citation(self) -> str:
        """出处，格式对齐 PRD 的 I3：``(文档名, 页码, 条款号)``。"""
        bits = [self.clause or self.doc_id]
        if self.page_no:
            bits.append(f"p.{self.page_no}")
        return "(" + ", ".join(bits) + ")"


@dataclass(slots=True)
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def total_ms(self) -> float:
        return self.timings_ms.get("total", 0.0)

    def summary(self) -> str:
        parts = ", ".join(f"{k}={v:.0f}ms" for k, v in self.timings_ms.items())
        return f"{len(self.chunks)} 条结果 | {parts}"


class RetrievalService:
    def __init__(
        self,
        settings: Settings,
        store: MySQLStore,
        milvus: MilvusStore,
        embedder,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.milvus = milvus
        self.embedder = embedder
        self.reranker = reranker
        self.cfg = settings.retrieval
        self.searcher = HybridSearcher(milvus, embedder, self.cfg)

    # ------------------------------------------------------------------
    def retrieve(
        self,
        query: str,
        *,
        authorities=None,
        doc_ids=None,
        top_k: int | None = None,
        final_k: int | None = None,
        use_rerank: bool = True,
    ) -> RetrievalResult:
        timings: dict[str, float] = {}
        started = time.perf_counter()

        # 1. 召回
        t = time.perf_counter()
        hits = self.searcher.search(
            query, authorities=authorities, doc_ids=doc_ids, top_k=top_k
        )
        timings["search"] = (time.perf_counter() - t) * 1000
        if not hits:
            timings["total"] = (time.perf_counter() - started) * 1000
            return RetrievalResult(query=query, timings_ms=timings)

        # 2. 取命中子块的正文
        t = time.perf_counter()
        rows = self._fetch_chunks([h.chunk_id for h in hits])
        timings["fetch_child"] = (time.perf_counter() - t) * 1000

        # 3. 重排
        t = time.perf_counter()
        ordered = self._apply_rerank(query, hits, rows, use_rerank)
        timings["rerank"] = (time.perf_counter() - t) * 1000

        # 4. 按父块去重，取 final_k
        limit = final_k or self.cfg.final_k
        selected = self._dedupe_by_parent(ordered, limit)

        # 5. 取父块全文
        t = time.perf_counter()
        self._attach_parents(selected)
        timings["fetch_parent"] = (time.perf_counter() - t) * 1000

        timings["total"] = (time.perf_counter() - started) * 1000
        log.info("检索完成：{}", RetrievalResult(query=query, chunks=selected,
                                                  timings_ms=timings).summary())
        return RetrievalResult(query=query, chunks=selected, timings_ms=timings)

    # ------------------------------------------------------------------
    def _fetch_chunks(self, chunk_ids: list[str]) -> dict[str, dict]:
        if not chunk_ids:
            return {}
        placeholders = ",".join(["%s"] * len(chunk_ids))
        rows = self.store.fetchall(
            f"""
            SELECT chunk_id, text, parent_id, doc_id, clause, page_no
            FROM chunks
            WHERE chunk_id IN ({placeholders}) AND status = 'active'
            """,
            chunk_ids,
        )
        return {r["chunk_id"]: r for r in rows}

    def _apply_rerank(
        self,
        query: str,
        hits: list[Hit],
        rows: dict[str, dict],
        use_rerank: bool,
    ) -> list[tuple[Hit, dict]]:
        """返回 (命中, MySQL 行) 的有序列表。"""
        pairs = [(h, rows[h.chunk_id]) for h in hits if h.chunk_id in rows]

        if not use_rerank or self.reranker is None or len(pairs) < 2:
            return sorted(pairs, key=lambda p: p[0].score, reverse=True)

        items = [(row["text"], (hit, row)) for hit, row in pairs]
        reranked = self.reranker.rerank(query, items)
        return [(payload[0], payload[1]) for payload, _score in reranked]

    def _dedupe_by_parent(
        self, ordered: list[tuple[Hit, dict]], limit: int
    ) -> list[RetrievedChunk]:
        """同一父块只保留分数最高的一条——否则上下文会重复三遍。"""
        out: list[RetrievedChunk] = []
        seen_parents: set[str] = set()

        for hit, row in ordered:
            parent_id = row.get("parent_id")
            key = parent_id or hit.chunk_id
            if key in seen_parents:
                continue
            seen_parents.add(key)
            out.append(
                RetrievedChunk(
                    chunk_id=hit.chunk_id,
                    text=row["text"],
                    score=hit.score,
                    doc_id=hit.doc_id,
                    authority=hit.authority,
                    clause=hit.clause or row.get("clause"),
                    page_no=hit.page_no or row.get("page_no"),
                    parent_id=parent_id,
                )
            )
            if len(out) >= limit:
                break
        return out

    def _attach_parents(self, chunks: list[RetrievedChunk]) -> None:
        parent_ids = [c.parent_id for c in chunks if c.parent_id]
        if not parent_ids:
            return
        placeholders = ",".join(["%s"] * len(parent_ids))
        rows = self.store.fetchall(
            f"SELECT chunk_id, text FROM chunks WHERE chunk_id IN ({placeholders})",
            parent_ids,
        )
        texts = {r["chunk_id"]: r["text"] for r in rows}
        for chunk in chunks:
            if chunk.parent_id:
                chunk.parent_text = texts.get(chunk.parent_id)
