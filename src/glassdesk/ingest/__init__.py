"""入库管线：解析 → 切分 → 落库 → 索引。

依赖方向：``ingest`` → ``storage``；``ingest`` 不 import ``retrieval`` / ``api``。
"""

from __future__ import annotations

__all__: list[str] = []
