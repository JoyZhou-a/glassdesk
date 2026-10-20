"""Milvus 标量过滤表达式构造。

为什么要在 Milvus 侧过滤而不是召回后再筛：

1. Milvus 支持标量过滤，且过滤与向量检索在同一个查询里完成，不损失 top_k。
2. 召回后再筛会把有效候选挤掉——本来要 20 条，筛完只剩 8 条。

**为什么必须有权威分区**（实测依据）：
同一个问题下，bge-reranker 给"措辞几乎相同的工单"打 0.99 分，
给"真正含答案的能力矩阵行"只打 0.38 分。也就是说重排器会主动把正确答案排到最后，
指望它兜底是错的——必须在召回阶段就把不该出现的来源挡掉。
"""

from __future__ import annotations

from collections.abc import Sequence

from glassdesk.constants import Authority


def _quote(value: str) -> str:
    """Milvus 表达式用双引号包字符串，内部双引号需转义。"""
    return '"' + value.replace('"', '\\"') + '"'


def authority_expr(authorities: Sequence[Authority | str] | None) -> str:
    """构造 ``authority in (...)`` 表达式。为空返回空串（不过滤）。"""
    if not authorities:
        return ""
    values = [a.value if isinstance(a, Authority) else str(a) for a in authorities]
    return "authority in [" + ", ".join(_quote(v) for v in values) + "]"


def doc_id_expr(doc_ids: Sequence[str] | None) -> str:
    if not doc_ids:
        return ""
    return "doc_id in [" + ", ".join(_quote(d) for d in doc_ids) + "]"


def chunk_id_expr(chunk_ids: Sequence[str] | None) -> str:
    if not chunk_ids:
        return ""
    return "id in [" + ", ".join(_quote(c) for c in chunk_ids) + "]"


def combine(*expressions: str) -> str:
    """用 ``and`` 连接非空表达式。"""
    parts = [e.strip() for e in expressions if e and e.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return " and ".join(f"({p})" for p in parts)


#: 常用组合：问尺寸/价格/交期时，只有权威来源算数
STRUCTURED_AUTHORITIES: tuple[Authority, ...] = (Authority.MANUAL,)

#: 问售后/经验时，经验来源优先，但手册也要能召回
AFTER_SALES_AUTHORITIES: tuple[Authority, ...] = (Authority.EXPERIENCE, Authority.MANUAL)

#: 问合规时，标准是第一优先
COMPLIANCE_AUTHORITIES: tuple[Authority, ...] = (Authority.STANDARD, Authority.MANUAL)
