"""路由：决定一个问题该走"查表"还是"查文档"。

**判据是槽位，不是意图。** 这一点来自 M2 的实测结论：尺寸类问题的答案只存在于
表里，而向量检索在"只有数字不同"的行之间原理上分不开。所以先看有没有抽出可
计算的参数，再决定走哪条路。

只有"该查哪张表"才用关键词判断——「how much」和「how long」没有歧义，
不值得为它训一个分类器。
"""

from __future__ import annotations

from dataclasses import dataclass

from glassdesk.routing import slots as S

#: 问价格的典型说法
PRICE_HINTS = ("how much", "price", "cost", "per square", "per m2", "quote", "charge")
#: 问交期的典型说法
LEADTIME_HINTS = ("how long", "lead time", "when will", "when can", "business days",
                  "delivery time", "turnaround")


@dataclass(slots=True)
class Route:
    """路由结果。``target`` 决定后续动作。"""

    target: str          # capability | price | leadtime | documents | ask | refuse
    reason: str = ""
    slots: S.Slots | None = None
    missing: list[str] | None = None


def decide(question: str) -> Route:
    """给一个问题定去向。"""
    extracted = S.extract(question)

    # 完全没有量化参数 → 只能查文档
    if extracted.is_empty:
        return Route("documents", "没有抽到尺寸参数", extracted)

    lowered = question.lower()
    wants_price = any(h in lowered for h in PRICE_HINTS)
    wants_time = any(h in lowered for h in LEADTIME_HINTS)

    if wants_price:
        need = ("thickness_mm",)
        validation = S.validate(extracted, need=need)
        if validation.can_query:
            return Route("price", "有厚度，可查报价", extracted)
        return Route("ask", validation.reason, extracted, validation.missing)

    if wants_time:
        need = ("thickness_mm",)
        validation = S.validate(extracted, need=need)
        if validation.can_query:
            return Route("leadtime", "有厚度，可查交期", extracted)
        return Route("ask", validation.reason, extracted, validation.missing)

    # 默认按"能不能做"处理，需要宽高
    need = ("thickness_mm", "width_mm", "height_mm")
    validation = S.validate(extracted, need=need)
    if validation.can_query:
        return Route("capability", "尺寸齐全，可查能力矩阵", extracted)
    if validation.status == "bad_value":
        return Route("unsupported", validation.reason, extracted)
    return Route("ask", validation.reason, extracted, validation.missing)
