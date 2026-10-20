"""存储层：只负责连接与读写，不含业务逻辑。

依赖方向：``ingest / retrieval / api`` → ``storage``。storage 永不反向 import 上层。

三个存储各司其职（见 ``docs/09_知识库更新.md`` §3.1）：

===========  ==========================================
MySQL        事实源：正文、状态、版本、出处
Milvus       派生索引：只有向量与过滤字段，没有正文
Redis        缓存（M3 才接入）
===========  ==========================================
"""

from __future__ import annotations

from glassdesk.storage.milvus import MilvusStore
from glassdesk.storage.mysql import MySQLStore, safe_ident

__all__ = ["MilvusStore", "MySQLStore", "safe_ident"]
