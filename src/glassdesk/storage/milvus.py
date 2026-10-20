"""Milvus 封装：只存**派生索引**。

设计取舍（见 ``docs/09_知识库更新.md`` §3.1）：

- Milvus 里**没有正文**。正文的事实源在 MySQL ``chunks.text``。
- Milvus 里**没有状态字段**。状态易变，而 Milvus 更新一行的语义是
  ``upsert``（delete + insert），改一个 status 就要重写整行含向量——不划算。
  做法是"Milvus 只管召回，MySQL 管有效性"，召回后按 chunk_id 回 MySQL 过滤。

因此：Milvus 中的任何数据都可以从 MySQL 重建。
"""

from __future__ import annotations

from typing import Any, Iterable

from glassdesk.config import MilvusConfig
from glassdesk.logger import get_logger

log = get_logger("storage.milvus")

#: 标量字段。page_no / clause 不设 NULL（Milvus 对 nullable 支持有限），
#: 未知时用 0 / 空串表示。
SCALAR_FIELDS = ("id", "doc_id", "authority", "page_no", "clause")


class MilvusStore:
    def __init__(
        self,
        cfg: MilvusConfig,
        *,
        database: str | None = None,
        collection: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._database = database or cfg.database
        self._collection = collection or cfg.collection
        self._client = None

    # ------------------------------------------------------------------
    def connect(self):
        if self._client is not None:
            return self._client
        from pymilvus import MilvusClient

        uri = f"http://{self._cfg.host}:{self._cfg.port}"
        self._client = MilvusClient(uri=uri, db_name=self._database)
        log.info("Milvus 已连接 {}/{}", uri, self._database)
        return self._client

    def __enter__(self) -> "MilvusStore":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def collection(self) -> str:
        return self._collection

    # ------------------------------------------------------------------
    # 集合
    # ------------------------------------------------------------------
    def has_collection(self) -> bool:
        return self.connect().has_collection(self._collection)

    def ensure_collection(self, dim: int, *, recreate: bool = False) -> bool:
        """建集合与索引。返回 True 表示本次新建。"""
        client = self.connect()
        if client.has_collection(self._collection):
            if not recreate:
                log.info("集合 {} 已存在，跳过", self._collection)
                return False
            client.drop_collection(self._collection)
            log.warning("集合 {} 已删除，准备重建", self._collection)

        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=160)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("authority", DataType.VARCHAR, max_length=16)
        schema.add_field("page_no", DataType.INT64)
        schema.add_field("clause", DataType.VARCHAR, max_length=64)
        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)

        index_params = client.prepare_index_params()
        # HNSW 不需要训练，小数据集上比 IVF_FLAT 更合适
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_hnsw",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_inverted",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )

        client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
        )
        log.info("集合 {} 已创建（dim={}）", self._collection, dim)
        return True

    def drop_collection(self) -> None:
        client = self.connect()
        if client.has_collection(self._collection):
            client.drop_collection(self._collection)
            log.warning("集合 {} 已删除", self._collection)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def upsert(self, rows: Iterable[dict[str, Any]]) -> int:
        """写入或覆盖。调用方负责给出完整的 7 个字段。"""
        data = list(rows)
        if not data:
            return 0
        missing = [f for f in (*SCALAR_FIELDS, "dense_vector", "sparse_vector")
                   if f not in data[0]]
        if missing:
            raise ValueError(f"写入 Milvus 的行缺字段：{missing}")
        self.connect().upsert(collection_name=self._collection, data=data)
        return len(data)

    def flush(self) -> None:
        """把内存中的数据落盘。

        不 flush 时 ``get_collection_stats`` 的 row_count 会是 0，
        看起来像"什么都没写进去"，实际数据已在。排查时极易误判。
        """
        self.connect().flush(self._collection)
        log.info("集合 {} 已 flush", self._collection)

    def delete_by_doc(self, doc_id: str) -> int:
        """按文档删除。旧版本清理与回滚用。"""
        client = self.connect()
        result = client.delete(
            collection_name=self._collection,
            filter=f'doc_id == "{doc_id}"',
        )
        count = result.get("delete_count", 0) if isinstance(result, dict) else 0
        log.info("已从 Milvus 删除 doc_id={} 的 {} 条向量", doc_id, count)
        return count

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def count(self) -> int:
        """准确条数。

        不能用 ``get_collection_stats`` 的 ``row_count``：它把**已删除**的行也算进去
        （实测 upsert 30 条后 row_count 从 275 变成 305，而 ``count(*)`` 仍是 275）。
        排查"数据是否写进去"时这个差异会严重误导。
        """
        client = self.connect()
        if not client.has_collection(self._collection):
            return 0
        client.load_collection(self._collection)
        rows = client.query(
            collection_name=self._collection, filter="", output_fields=["count(*)"]
        )
        return int(rows[0]["count(*)"]) if rows else 0

    def describe(self) -> dict[str, Any]:
        client = self.connect()
        if not client.has_collection(self._collection):
            return {"exists": False}
        return {
            "exists": True,
            "name": self._collection,
            "row_count": self.count(),
            "fields": [f["name"] for f in client.describe_collection(self._collection)["fields"]],
        }
