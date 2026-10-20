"""从自然语言里抽"可查表的参数"（槽位）。

**为什么需要这一层**（M2 的实测结论）：

    Q: Can you make 4mm toughened glass 1950 wide by 2200 high?
    期望: 能力矩阵里 thickness=4 那一行
    实得: thickness=5 / 6 / 8 的行

能力矩阵 30 行结构完全一样、只有数字不同，而 "4" 和 "5" 在语义空间里几乎重合，
稠密向量**原理上分不开**。所以尺寸类问题不能靠向量检索，必须抽出参数后查表。

抽取原则：
1. **正则优先**。尺寸、数量是强模式，正则比模型准，而且是零延迟零成本。
2. **单位一律归一化到毫米**。客户会说 2.4m、2400mm、94 inch，表里只存 mm。
3. **抽不到就说抽不到**。绝不猜——猜错尺寸会导致报价错误。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

#: 原片/钢化/中空允许的整数厚度
INT_THICKNESS = (4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 19.0)
#: 夹胶的公称厚度（玻璃+胶层+玻璃）
LAMINATED_THICKNESS = (6.38, 8.76, 10.76, 12.76)

ALLOWED_THICKNESS: dict[str, tuple[float, ...]] = {
    "annealed": (4.0, 5.0, 6.0, 8.0, 10.0, 12.0),
    "toughened": INT_THICKNESS,
    "laminated": LAMINATED_THICKNESS,
    "igu": (4.0, 5.0, 6.0, 8.0, 10.0, 12.0),
}

#: 同义词表。客户不会用我们的术语，必须覆盖口语说法。
PROCESS_SYNONYMS: dict[str, tuple[str, ...]] = {
    "igu": ("igu", "double glazed", "double-glazed", "double glazing",
            "triple glazed", "triple glazing", "insulating glass", "sealed unit", "中空"),
    "laminated": ("laminated", "laminate", "夹胶", "safety laminate"),
    "toughened": ("toughened", "tempered", "钢化"),
    "annealed": ("annealed", "ordinary glass", "float glass", "原片"),
}

SHAPE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "bent": ("bent", "curved", "curved glass", "arched", "bowed", "弧形", "异形"),
    "flat": ("flat", "平板", "rectangular"),
}

COATING_SYNONYMS: dict[str, tuple[str, ...]] = {
    "low_e_soft": ("low-e soft", "soft coat", "low e soft"),
    "low_e_hard": ("low-e hard", "hard coat", "low e hard", "low-e", "low e", "low emissivity"),
    "tinted": ("tinted", "grey", "gray", "bronze", "green glass", "着色"),
    "none": ("clear glass", "clear"),
}

APPLICATION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "pool_fence": ("pool fence", "pool fencing", "pool barrier", "泳池围栏"),
    "door": ("door", "doors", "front door", "门"),
    "skylight": ("skylight", "roof glass", "overhead", "ceiling", "canopy", "天窗", "采光顶"),
    "shower": ("shower", "bathroom", "淋浴"),
    "balustrade": ("balustrade", "balcony", "railing", "栏杆"),
    "shopfront": ("shopfront", "shop front", "storefront", "shop window", "橱窗"),
    "window": ("window", "windows", "窗"),
}

LOCATIONS = ("ACT", "NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT")

#: 单位换算到毫米
UNIT_TO_MM = {
    "mm": 1.0, "millimetre": 1.0, "millimetres": 1.0, "millimeter": 1.0, "millimeters": 1.0,
    "cm": 10.0, "centimetre": 10.0, "centimetres": 10.0,
    "m": 1000.0, "metre": 1000.0, "metres": 1000.0, "meter": 1000.0, "meters": 1000.0,
    "inch": 25.4, "inches": 25.4, '"': 25.4,
}

_NUM = r"\d+(?:\.\d+)?"
_UNIT = r"(mm|millimetres?|millimeters?|cm|centimetres?|centimeters?|m|metres?|meters?|inch(?:es)?)"

#: "2400 wide by 3000 high" / "2400 by 3000"
_WIDE_BY = re.compile(
    rf"({_NUM})\s*{_UNIT}?\s*(?:wide|width)\s*(?:by|x|×)\s*({_NUM})\s*{_UNIT}?"
    rf"\s*(?:high|tall|height)?",
    re.IGNORECASE,
)
_HIGH_BY = re.compile(
    rf"({_NUM})\s*{_UNIT}?\s*(?:high|tall|height)\s*(?:by|x|×)\s*({_NUM})\s*{_UNIT}?"
    rf"\s*(?:wide|width)?",
    re.IGNORECASE,
)
#: "2400 x 3000" / "2400x3000" / "2.4m x 3m"
_GENERIC = re.compile(rf"({_NUM})\s*{_UNIT}?\s*(?:x|×|\*|by)\s*({_NUM})\s*{_UNIT}?", re.IGNORECASE)

_THICKNESS = re.compile(rf"({_NUM})\s*(?:mm|millimetres?)\s*(?:thick|thickness)?", re.IGNORECASE)
_THICKNESS_AFTER = re.compile(
    rf"({_NUM})\s*(?:mm)?\s*(?:thick|thickness|toughened|tempered|laminated|annealed)", re.IGNORECASE
)
_QUANTITY = re.compile(
    r"(\d+)\s*(?:panels?|sheets?|pieces?|pcs|units?|off)\b", re.IGNORECASE
)
_IGU_CONFIG = re.compile(rf"\b({_NUM})\s*-\s*({_NUM})\s*-\s*({_NUM})\b")
_CURVED_RADIUS = re.compile(rf"radius\s*(?:of\s*)?({_NUM})\s*(?:mm)?", re.IGNORECASE)


def _to_mm(value: str, unit: str | None) -> float:
    """把数值+单位换算成毫米。无单位时按毫米处理（客户说 2400 通常就是 mm）。

    保留 2 位小数：夹胶的公称厚度是 6.38 / 8.76 / 10.76 / 12.76，
    只保留 1 位会把它们四舍五入成 6.4 / 8.8 从而全部校验失败。
    """
    factor = UNIT_TO_MM.get((unit or "mm").lower().rstrip("."), 1.0)
    return round(float(value) * factor, 2)


@dataclass(slots=True)
class Slots:
    """一次提问里抽到的参数。值为 None 表示**没提到**，不是默认值。"""

    thickness_mm: float | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    quantity: int | None = None
    process: str | None = None
    shape: str | None = None
    coating: str | None = None
    application: str | None = None
    location: str | None = None
    igu_config: tuple[float, float, float] | None = None
    variant: str | None = None
    matched: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @property
    def is_empty(self) -> bool:
        """只有量化参数才算"抽到东西"，光有 application 还不足以查表。"""
        return self.thickness_mm is None and self.width_mm is None and self.height_mm is None


def _find_synonym(text: str, table: dict[str, tuple[str, ...]]) -> tuple[str | None, str | None]:
    """在文本里找同义词，返回 (规范值, 命中的原词)。长词优先，避免 "door" 抢在 "front door" 前面。"""
    lower = text.lower()
    best: tuple[str, str] | None = None
    for canonical, words in table.items():
        for word in words:
            if word in lower:
                if best is None or len(word) > len(best[1]):
                    best = (canonical, word)
    return (best[0], best[1]) if best else (None, None)


def _extract_dimensions(text: str, slots: Slots) -> None:
    """先抽宽高，并返回"已被占用的数值集合"，避免把尺寸当成厚度。"""
    for pattern, ordered in ((_WIDE_BY, True), (_HIGH_BY, False), (_GENERIC, True)):
        match = pattern.search(text)
        if not match:
            continue
        a_val, a_unit, b_val, b_unit = match.groups()
        a, b = _to_mm(a_val, a_unit), _to_mm(b_val, b_unit)
        if ordered:
            slots.width_mm, slots.height_mm = a, b
        else:
            slots.height_mm, slots.width_mm = a, b
        slots.matched["dimensions"] = match.group(0).strip()
        return


def _extract_thickness(text: str, slots: Slots) -> None:
    """厚度：优先"数字+mm"且不在尺寸表达式内的那个。"""
    dim = slots.matched.get("dimensions", "")
    candidates: list[float] = []
    for match in _THICKNESS.finditer(text):
        if match.group(0).strip() and match.group(0).strip() in dim:
            continue
        candidates.append(_to_mm(match.group(1), "mm"))
    for match in _THICKNESS_AFTER.finditer(text):
        if match.group(0).strip() in dim:
            continue
        candidates.append(_to_mm(match.group(1), "mm"))

    if not candidates:
        return
    # 优先取落在合法厚度集合里的值；否则取最小的（客户很少把厚度说得比尺寸大）
    allowed = set(INT_THICKNESS) | set(LAMINATED_THICKNESS)
    hit = [c for c in candidates if c in allowed]
    slots.thickness_mm = hit[0] if hit else min(candidates)
    slots.matched["thickness"] = str(slots.thickness_mm)


def extract(text: str) -> Slots:
    """从一句话里抽所有能抽到的槽位。抽不到就是 None，不猜。"""
    slots = Slots()
    if not text or not text.strip():
        return slots

    _extract_dimensions(text, slots)
    _extract_thickness(text, slots)

    qty = _QUANTITY.search(text)
    if qty:
        slots.quantity = int(qty.group(1))
        slots.matched["quantity"] = qty.group(0).strip()

    for attr, table in (
        ("process", PROCESS_SYNONYMS),
        ("shape", SHAPE_SYNONYMS),
        ("coating", COATING_SYNONYMS),
        ("application", APPLICATION_SYNONYMS),
    ):
        canonical, word = _find_synonym(text, table)
        if canonical:
            setattr(slots, attr, canonical)
            slots.matched[attr] = word or canonical

    upper = text.upper()
    for loc in LOCATIONS:
        if re.search(rf"\b{loc}\b", upper):
            slots.location = loc
            slots.matched["location"] = loc
            break

    igu = _IGU_CONFIG.search(text)
    if igu and slots.process in (None, "igu"):
        slots.igu_config = (float(igu.group(1)), float(igu.group(2)), float(igu.group(3)))
        # 规格串是报价表的 key 之一（6-12-6 与 6-16-6 价格不同），必须原样保留
        slots.variant = f"{float(igu.group(1)):g}-{float(igu.group(2)):g}-{float(igu.group(3)):g}"
        slots.process = "igu"
        slots.matched["igu_config"] = igu.group(0)

    return slots


# ======================================================================
# 校验：决定"能不能查表"
# ======================================================================
@dataclass(slots=True)
class Validation:
    status: str                 # complete | missing | no_slots | bad_value
    missing: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def can_query(self) -> bool:
        return self.status == "complete"


def validate(slots: Slots, *, need: tuple[str, ...] = ("thickness_mm",)) -> Validation:
    """检查槽位是否够查表。

    ``need`` 是该类问题**必须**有的字段：
    - 查能力矩阵：thickness + width + height
    - 查报价/交期：thickness（+ process 更好，但不强制）
    """
    if slots.is_empty:
        return Validation("no_slots", reason="没有抽到任何尺寸参数")

    missing = [f for f in need if getattr(slots, f) is None]
    if missing:
        return Validation("missing", missing=missing,
                          reason=f"缺少 {', '.join(missing)}，必须追问")

    if slots.thickness_mm is not None and slots.process is not None:
        allowed = ALLOWED_THICKNESS.get(slots.process, INT_THICKNESS)
        if slots.thickness_mm not in allowed:
            return Validation(
                "bad_value", missing=[],
                reason=f"{slots.process} 没有 {slots.thickness_mm}mm 这个规格"
                       f"（可选 {', '.join(str(a) for a in allowed)}）",
            )
    return Validation("complete")


def thickness_too_large(slots: Slots, max_mm: float) -> bool:
    return slots.thickness_mm is not None and slots.thickness_mm > max_mm
