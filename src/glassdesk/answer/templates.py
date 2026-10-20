"""答案模板：把查到的数字渲染成"客服能照着念"的话。

**这里不调用大模型**，纯字符串拼接。原因见 PRD 的业务红线：
尺寸、价格、合规结论只能来自表，不能让模型生成。

两种模式（PRD D9，按渠道分而不是按角色分）：

- **电话模式**：≤3 行、单句 ≤60 字符、无表格、可直接朗读
- **邮件模式**：结构化、可整段复制、含出处
"""

from __future__ import annotations

from glassdesk.routing.structured import CapabilityVerdict, LeadTimeQuote, PriceQuote
from glassdesk.routing.slots import Slots

#: PRD I5：电话模式下单句最多这么长
PHONE_MAX_SENTENCE = 60
#: PRD I2：电话模式最多这么行
PHONE_MAX_LINES = 3


def _clip(sentence: str, limit: int = PHONE_MAX_SENTENCE) -> str:
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def capability_phone(slots: Slots, verdict: CapabilityVerdict) -> list[str]:
    """尺寸判定 → 电话模式回答。"""
    process = (slots.process or "toughened").replace("igu", "double glazed")
    thick = slots.thickness_mm
    size = ""
    if slots.width_mm is not None and slots.height_mm is not None:
        size = f" {slots.width_mm:.0f} by {slots.height_mm:.0f}"

    if verdict.can_make:
        lines = [_clip(f"Yes, {thick}mm {process} can be made at{size or ' that size'}.")]
        tol = verdict.limits.get("tolerance_mm")
        if tol:
            lines.append(_clip(f"Tolerance is plus or minus {tol}mm."))
        return lines[:PHONE_MAX_LINES]

    # 不行的时候，必须给出上限和替代方案——客服要的不是"不行"
    lines = [_clip(f"No, that cannot be made as one piece. {verdict.reason}.")]
    mx = verdict.limits.get("max_width_mm")
    my = verdict.limits.get("max_height_mm")
    if mx and my:
        lines.append(_clip(f"The maximum for this thickness is {mx} by {my}mm."))
    lines.append(_clip("We can split it into two panels, or use a thinner glass."))
    return lines[:PHONE_MAX_LINES]


def capability_email(slots: Slots, verdict: CapabilityVerdict) -> str:
    lines = [
        "**Size check**",
        "",
        f"- Requested: {slots.thickness_mm}mm {slots.process or 'toughened'}"
        f" {slots.width_mm or '-'} x {slots.height_mm or '-'} mm",
        f"- Result: {'within capability' if verdict.can_make else 'exceeds capability'}",
        f"- Detail: {verdict.reason}",
    ]
    if verdict.limits:
        lines.append(
            f"- Limits: max {verdict.limits['max_width_mm']} x "
            f"{verdict.limits['max_height_mm']}mm, max area "
            f"{verdict.limits['max_area_m2']}m2, tolerance "
            f"+/-{verdict.limits['tolerance_mm']}mm"
        )
    lines += ["- Source: Capability Matrix 2026", ""]
    return "\n".join(lines)


def price_phone(quote: PriceQuote, slots: Slots) -> list[str]:
    thick = slots.thickness_mm
    process = (slots.process or "toughened").replace("igu", "double glazed")
    lines = [_clip(f"{thick}mm {process} is {quote.unit_price_aud_m2:.0f} dollars per square metre.")]
    lines.append("That price includes GST.")
    if quote.min_charge_aud:
        lines.append(_clip(f"Minimum charge is {quote.min_charge_aud:.0f} dollars."))
    return lines[:PHONE_MAX_LINES]


def price_email(quote: PriceQuote, slots: Slots) -> str:
    return "\n".join([
        "**Price**",
        "",
        f"- Item: {slots.thickness_mm}mm {slots.process or 'toughened'}"
        + (f" ({quote.variant})" if quote.variant else ""),
        f"- Unit price: AUD {quote.unit_price_aud_m2:.2f} per m2 (incl. GST)",
        f"- Minimum charge: AUD {quote.min_charge_aud:.2f}",
        f"- MOQ: {quote.moq_m2} m2",
        f"- Source: Price List 2026 (valid to {quote.valid_to})",
        "",
    ])


def leadtime_phone(quote: LeadTimeQuote, slots: Slots) -> list[str]:
    process = (slots.process or "toughened").replace("igu", "double glazed")
    qty = f"{slots.quantity} panels of " if slots.quantity else ""
    lines = [_clip(f"{qty}{process} takes about {quote.business_days} business days.")]
    if quote.rush_available:
        lines.append(_clip(f"Rush is available with a {quote.rush_surcharge_pct:.0f}% surcharge."))
    else:
        lines.append("Rush is not available for this item.")
    if quote.note:
        lines.append(_clip(quote.note))
    return lines[:PHONE_MAX_LINES]


def leadtime_email(quote: LeadTimeQuote, slots: Slots) -> str:
    return "\n".join([
        "**Lead time**",
        "",
        f"- Item: {slots.thickness_mm}mm {slots.process or 'toughened'}"
        + (f", qty {slots.quantity}" if slots.quantity else ""),
        f"- Standard: {quote.business_days} business days from order confirmation",
        f"- Rush: {'available, ' + str(quote.rush_surcharge_pct) + '% surcharge' if quote.rush_available else 'not available'}",
        f"- Note: {quote.note or '-'}",
        "- Source: Standard Lead Times",
        "",
    ])


def ask_back(missing: list[str], mode: str = "phone") -> str:
    """缺槽位时的追问。一次问完，不要来回三轮。"""
    labels = {
        "thickness_mm": "thickness",
        "width_mm": "width",
        "height_mm": "height",
        "process": "glass type",
    }
    words = [labels.get(m, m) for m in missing]
    if len(words) == 1:
        question = f"What {words[0]} do you need?"
    else:
        question = "What " + ", ".join(words[:-1]) + " and " + words[-1] + " do you need?"
    if mode == "email":
        return f"To check this I need a bit more detail. {question}"
    return _clip(question)


def refuse_and_handoff(reason: str, mode: str = "phone") -> str:
    """查不到或超出能力时的兜底话术（对应 PRD 的能力边界承诺）。"""
    if mode == "email":
        return (
            f"This one needs an engineer to confirm. {reason}\n\n"
            "I have flagged it for the technical team and they will come back to you."
        )
    return _clip(f"I need to check this with our engineer. {reason}")
