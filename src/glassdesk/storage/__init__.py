"""存储层：只负责连接与读写，不含业务逻辑。

依赖方向：``ingest / retrieval / api`` → ``storage``。storage 永不反向 import 上层。
"""

from __future__ import annotations

from glassdesk.storage.mysql import MySQLStore

__all__ = ["MySQLStore"]
