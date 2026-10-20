"""统一编排：一个问题从文本走到最终答案。

**这是唯一的入口逻辑。** ``scripts/ask.py`` 和所有评测脚本都必须调它——
否则会出现"评测跑的和线上跑的不是同一条路"的情况（踩过一次：
答案评测绕过了路由，把所有题都塞进向量检索，于是本该追问的题被答了，
negative 只有 45%，看着像系统坏了，其实是评测错了）。

路线::

    抽槽位
      ├ 齐了 → 查表 + 模板（零 token）
      ├ 不齐 → 追问（零 token）
      ├ 规格不存在 → 直接说清楚（零 token）
      └ 没抽出东西 → 向量检索 + 大模型（花 token）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from glassdesk.answer import templates
from glassdesk.constants import OutputMode
from glassdesk.logger import get_logger
from glassdesk.routing.router import decide
from glassdesk.routing.structured import StructuredLookup

log = get_logger("answer.flow")


@dataclass(slots=True)
class FlowResult:
    """一次问答的结果。状态取值与 PRD 的五种终态对齐。"""

    route: str
    status: str            # answer | need_slot | refuse | escalate | service_error
    text: str
    citations: list[str] = field(default_factory=list)
    authority: str | None = None
    evidence: int = 0
    notes: list[str] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    retrieval_ms: float = 0.0

    @property
    def is_answer(self) -> bool:
        return self.status == "answer"

    @property
    def is_safe(self) -> bool:
        """没给客户一个可能错的数字——追问、拒答、转人工都算安全。"""
        return self.status in ("need_slot", "refuse", "escalate")


def run(
    question: str,
    *,
    store,
    lookup_factory=StructuredLookup,
    retriever=None,
    generator_factory=None,
    mode: OutputMode | str = OutputMode.PHONE,
) -> FlowResult:
    """统一入口。``retriever`` / ``generator_factory`` 只在文档路线需要。

    延迟构造：结构化路线完全不碰嵌入模型和 LLM，调用方可以传 None。
    """
    mode_enum = mode if isinstance(mode, OutputMode) else OutputMode(str(mode))
    route = decide(question)

    # ---------- 零 token 的三条路 ----------
    if route.target in ("capability", "price", "leadtime"):
        lookup = lookup_factory(store)
        slots = route.slots
        if route.target == "capability":
            verdict = lookup.check_capability(slots)
            render = templates.capability_phone if mode_enum is OutputMode.PHONE \
                else templates.capability_email
            return FlowResult(
                route=route.target,
                status="answer" if verdict.can_make else "refuse",
                text=render(slots, verdict),
                authority="manual",
            )
        if route.target == "price":
            quote = lookup.price(slots)
            if quote is None:
                return FlowResult(route=route.target, status="refuse",
                                  text=templates.refuse_and_handoff(
                                      "No price on file for that specification.", mode_enum.value))
            render = templates.price_phone if mode_enum is OutputMode.PHONE \
                else templates.price_email
            return FlowResult(route=route.target, status="answer",
                              text=render(quote, slots), authority="manual")
        quote = lookup.leadtime(slots)
        if quote is None:
            return FlowResult(route=route.target, status="refuse",
                              text=templates.refuse_and_handoff(
                                  "No lead time on file for that specification.", mode_enum.value))
        render = templates.leadtime_phone if mode_enum is OutputMode.PHONE \
            else templates.leadtime_email
        return FlowResult(route=route.target, status="answer",
                          text=render(quote, slots), authority="manual")

    if route.target == "unsupported":
        return FlowResult(route=route.target, status="refuse",
                          text=templates.unsupported(route.reason, mode_enum.value))

    if route.target == "ask":
        return FlowResult(route=route.target, status="need_slot",
                          text=templates.ask_back(route.missing or [], mode_enum.value),
                          notes=[route.reason])

    if route.target == "refuse":
        return FlowResult(route=route.target, status="escalate",
                          text=templates.refuse_and_handoff(route.reason, mode_enum.value))

    # ---------- 文档路线：检索 + 生成 ----------
    if retriever is None or generator_factory is None:
        raise ValueError("文档路线需要 retriever 和 generator_factory")

    retrieval = retriever.retrieve(question)
    generator = generator_factory()
    result = generator.generate(question, retrieval.chunks, mode_enum)
    return FlowResult(
        route="documents",
        status="answer" if result.is_answerable else result.status.value,
        text=result.render(),
        citations=result.citations,
        authority=result.authority,
        evidence=result.evidence_count,
        notes=result.notes,
        tokens=result.usage,
        retrieval_ms=retrieval.total_ms,
    )
