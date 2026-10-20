"""路由：决定一个问题该走"查表"还是"查文档"。

**判据是槽位，不是意图。** 这一点来自 M2 的实测结论：尺寸类问题的答案只存在于
表里，而向量检索在"只有数字不同"的行之间原理上分不开。所以先看有没有抽出可
计算的参数，再决定走哪条路。

只有"该查哪张表"才用关键词判断——「how much」和「how long」没有歧义，
不值得为它训一个分类器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from glassdesk.routing import slots as S

#: 问价格的典型说法
PRICE_HINTS = ("how much", "price", "cost", "per square", "per m2", "quote", "charge")
#: 问交期的典型说法
LEADTIME_HINTS = ("how long", "lead time", "when will", "when can", "business days",
                  "delivery time", "turnaround")

#: 提到"大小"却没说数字的问法。
#:
#: 这类问题**必须追问**，不能送去检索文档。实测："Can you make a big one?"
#: 和 "Can you do it thicker?" 原本会走文档检索，模型从经验笔记里凑出一句
#: "通常不能做成整块"——听着像答案，其实没回答"多大算大"。
SIZE_HINTS = (
    "big", "large", "small", "tiny", "thick", "thin", "wide", "width",
    "height", "tall", "size", "pane", "sheet", "dimension",
    "area", "oversize", "square metre", "square meter",
)
# 注意：不包含 "panel" 和 "piece"。
# 它们太泛：``Can we join two panels invisibly?`` 和
# ``Customer insists on one piece`` 都是流程问题，却会被当成尺寸问题去追问。
# 带尺寸的 "panel" 问句本来就会被槽位抽到，不依赖这里。

#: 只有**在问"能不能做/多少钱/多久"**时，尺寸提示词才意味着"缺参数要追问"。
#:
#: 否则会误伤政策类问题：``Customer wants a panel bigger than the furnace,
#: what should we tell them?`` 含 ``panel`` 和 ``big``，但它问的是流程，
#: 文档里有答案。实测这一条误判让 after_sales 的答案正确率从 100% 掉到 78%。
CAPABILITY_PHRASES = (
    "can you", "can we", "could you", "is it possible", "are you able",
    "do you make", "do you do", "will it fit", "what is the maximum",
    "what's the maximum",
)


@dataclass(slots=True)
class Route:
    """路由结果。``target`` 决定后续动作。"""

    target: str          # capability | price | leadtime | documents | ask | refuse
    reason: str = ""
    slots: S.Slots | None = None
    missing: list[str] | None = None


def _matches(text: str, hints: tuple[str, ...]) -> bool:
    """按**完整词**匹配关键词，允许常见词形变化。

    踩过两次子串坑：
    - ``install`` 里含 ``tall`` —— "能上门安装吗" 被当成尺寸问题；
    - ``\\bpane`` 能匹配 ``panels`` —— "能不能把两块拼成一块" 被当成要报价。

    所以：两端都要词边界，只放行 s/es/er/ed/est 这类词尾变化，
    这样 ``thick`` 能匹配 ``thicker``，但 ``pane`` 不会匹配 ``panels``。
    """
    alternatives = "|".join(re.escape(h) for h in hints)
    pattern = rf"\b(?:{alternatives})(?:s|es|er|ed|est)?\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def decide(question: str) -> Route:
    """给一个问题定去向。"""
    extracted = S.extract(question)

    # 没有量化参数：先看是不是"在问能不能做，但没说尺寸"
    if extracted.is_empty:
        asking_capability = (
            _matches(question, CAPABILITY_PHRASES)
            or _matches(question, PRICE_HINTS)
            or _matches(question, LEADTIME_HINTS)
        )
        if asking_capability and _matches(question, SIZE_HINTS):
            return Route(
                "ask",
                "size question without numbers; must ask back",
                extracted,
                ["thickness_mm", "width_mm", "height_mm"],
            )
        return Route("documents", "no size parameters found", extracted)

    lowered = question.lower()
    wants_price = _matches(lowered, PRICE_HINTS)
    wants_time = _matches(lowered, LEADTIME_HINTS)

    if wants_price:
        need = ("thickness_mm",)
        validation = S.validate(extracted, need=need)
        if validation.can_query:
            return Route("price", "thickness present; price lookup", extracted)
        return Route("ask", validation.reason, extracted, validation.missing)

    if wants_time:
        need = ("thickness_mm",)
        validation = S.validate(extracted, need=need)
        if validation.can_query:
            return Route("leadtime", "thickness present; lead time lookup", extracted)
        return Route("ask", validation.reason, extracted, validation.missing)

    # 默认按"能不能做"处理，需要宽高
    need = ("thickness_mm", "width_mm", "height_mm")
    validation = S.validate(extracted, need=need)
    if validation.can_query:
        return Route("capability", "dimensions complete; capability lookup", extracted)
    if validation.status == "bad_value":
        return Route("unsupported", validation.reason, extracted)
    return Route("ask", validation.reason, extracted, validation.missing)
