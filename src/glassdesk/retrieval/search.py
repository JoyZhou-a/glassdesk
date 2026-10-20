"""hybrid 召回：稠密 + 稀疏，两路并行后加权融合。

为什么必须两路：

- **稠密**管语义相近。"Can you do a big pane" 和 "maximum panel size" 字面不同，
  但意思一样，只有稠密向量能连上。
- **稀疏**管字面命中。"AS1288"、"12mm"、"R-12"、"#4F5A" 这类精确串，稠密向量
  会把它和相近的东西混在一起，必须靠词的权重精确匹配。

融合用 Milvus 的 ``WeightedRanker``，权重来自 ``config.yaml`` 的
``retrieval.hybrid``（默认 dense 0.7 / sparse 0.3）。

**Milvus 只返回 id 与标量字段，不返回正文**——正文的事实源在 MySQL。
"""

from __future__ import annotations

from dataclasses import dataclass

from glassdesk.config import RetrievalConfig
from glassdesk.logger import get_logger
from glassdesk.retrieval import filters
from glassdesk.storage import MilvusStore

log = get_logger("retrieval.search")


@dataclass(slots=True)
class Hit:
    """一条召回结果。只有身份与出处，没有正文。"""

    chunk_id: str
    score: float
    doc_id: str
    authority: str
    clause: str | None = None
    page_no: int | None = None

    @classmethod
    def from_entity(cls, hit: dict) -> "Hit":
        entity = hit.get("entity") or {}
        page = entity.get("page_no")
        return cls(
            chunk_id=hit["id"],
            score=float(hit.get("distance", 0.0)),
            doc_id=entity.get("doc_id", ""),
            authority=entity.get("authority", ""),
            clause=(entity.get("clause") or None),
            page_no=int(page) if page else None,
        )


class HybridSearcher:
    def __init__(
        self,
        milvus: MilvusStore,
        embedder,
        cfg: RetrievalConfig,
    ) -> None:
        self._milvus = milvus
        self._embedder = embedder
        self._cfg = cfg
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """集合只载入一次。

        每次检索都调用 ``load_collection`` 既浪费，也会在 Milvus 后台压缩
        期间偶发 ``index not found`` —— 那是瞬时的元数据不一致，不是真的缺索引。
        """
        if self._loaded:
            return
        self._milvus.load()
        self._loaded = True

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        authorities=None,
        doc_ids=None,
        top_k: int | None = None,
    ) -> list[Hit]:
        """返回按融合分排序的候选。``top_k`` 默认取配置里的召回数。"""
        if not query.strip():
            return []

        limit = top_k or self._cfg.top_k
        expr = filters.combine(
            filters.authority_expr(authorities),
            filters.doc_id_expr(doc_ids),
        )

        vector = self._embedder.encode_query(query)

        from pymilvus import AnnSearchRequest, WeightedRanker

        dense_req = AnnSearchRequest(
            data=[vector.dense],
            anns_field="dense_vector",
            param={"metric_type": "IP", "params": {"ef": max(64, limit * 4)}},
            limit=limit,
            expr=expr,
        )
        sparse_req = AnnSearchRequest(
            data=[vector.sparse],
            anns_field="sparse_vector",
            param={"metric_type": "IP", "params": {}},
            limit=limit,
            expr=expr,
        )

        client = self._milvus.client
        self._ensure_loaded()
        results = client.hybrid_search(
            collection_name=self._milvus.collection,
            reqs=[dense_req, sparse_req],
            ranker=WeightedRanker(
                self._cfg.hybrid.dense_weight, self._cfg.hybrid.sparse_weight
            ),
            limit=limit,
            output_fields=["doc_id", "authority", "clause", "page_no"],
        )[0]

        hits = [Hit.from_entity(h) for h in results]
        log.info(
            "召回 {} 条（limit={}, expr={}）", len(hits), limit, expr or "无过滤"
        )
        return hits
