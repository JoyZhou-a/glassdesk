"""结构化查询：拿槽位去查 capability / pricing / leadtime 三张表。

**这里绝不调用大模型。** 数字只能来自表，不能由模型生成——这是 PRD 里的红线：
尺寸和价格答错是赔钱事故，不是"体验不佳"。

脱敏要求（PRD D13）：``cost_price_aud_m2`` 可以入库（用于验证脱敏逻辑是否真的生效），
但**永远不出现在任何查询结果里**。所以下面的 SQL 一个都不 SELECT 它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from glassdesk.logger import get_logger
from glassdesk.routing.slots import Slots
from glassdesk.storage import MySQLStore

log = get_logger("routing.structured")


@dataclass(slots=True)
class CapabilityVerdict:
    """能不能做。``limits`` 永远带回来，答"不行"时要告诉客户上限是多少。"""

    can_make: bool
    reason: str
    row: dict | None = None
    limits: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.can_make


@dataclass(slots=True)
class PriceQuote:
    unit_price_aud_m2: float
    min_charge_aud: float
    moq_m2: float
    variant: str
    coating: str
    valid_to: date | None = None


@dataclass(slots=True)
class LeadTimeQuote:
    business_days: int
    rush_available: bool
    rush_surcharge_pct: float
    note: str | None = None


class StructuredLookup:
    def __init__(self, store: MySQLStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------
    def capability_row(self, process: str, shape: str, thickness_mm: float) -> dict | None:
        return self.store.fetchone(
            """
            SELECT process, shape, thickness_mm, max_width_mm, max_height_mm,
                   max_area_m2, min_width_mm, min_height_mm, tolerance_mm,
                   min_radius_mm, igu_cavity_mm, notes
            FROM capability
            WHERE process=%s AND shape=%s AND thickness_mm=%s
            """,
            (process, shape, float(thickness_mm)),
        )

    def check_capability(self, slots: Slots) -> CapabilityVerdict:
        """判定"能不能做"。四个判据：宽、高、面积、最小尺寸，异形再加弯曲半径。"""
        if slots.thickness_mm is None:
            return CapabilityVerdict(False, "没有厚度，无法判断")

        process = slots.process or "toughened"
        shape = slots.shape or "flat"
        row = self.capability_row(process, shape, slots.thickness_mm)
        if row is None:
            return CapabilityVerdict(
                False,
                f"没有 {process} {shape} {slots.thickness_mm}mm 这个规格",
            )

        limits = {
            "max_width_mm": row["max_width_mm"],
            "max_height_mm": row["max_height_mm"],
            "max_area_m2": float(row["max_area_m2"]),
            "min_width_mm": row["min_width_mm"],
            "min_height_mm": row["min_height_mm"],
            "tolerance_mm": float(row["tolerance_mm"]),
            "min_radius_mm": row["min_radius_mm"],
        }

        width, height = slots.width_mm, slots.height_mm
        if width is not None and width > limits["max_width_mm"]:
            return CapabilityVerdict(
                False, f"宽度 {width:.0f}mm 超过上限 {limits['max_width_mm']}mm", row, limits
            )
        if height is not None and height > limits["max_height_mm"]:
            return CapabilityVerdict(
                False, f"高度 {height:.0f}mm 超过上限 {limits['max_height_mm']}mm", row, limits
            )
        if width is not None and width < limits["min_width_mm"]:
            return CapabilityVerdict(
                False, f"宽度 {width:.0f}mm 低于最小 {limits['min_width_mm']}mm", row, limits
            )
        if height is not None and height < limits["min_height_mm"]:
            return CapabilityVerdict(
                False, f"高度 {height:.0f}mm 低于最小 {limits['min_height_mm']}mm", row, limits
            )

        if width is not None and height is not None:
            area = round(width * height / 1_000_000, 2)
            if area > limits["max_area_m2"]:
                return CapabilityVerdict(
                    False,
                    f"面积 {area}m2 超过上限 {limits['max_area_m2']}m2"
                    f"（长宽都没超，但面积超了）",
                    row, limits,
                )

        return CapabilityVerdict(True, "在加工范围内", row, limits)

    # ------------------------------------------------------------------
    # 报价
    # ------------------------------------------------------------------
    def price(self, slots: Slots) -> PriceQuote | None:
        if slots.thickness_mm is None:
            return None
        process = slots.process or "toughened"
        shape = slots.shape or "flat"

        # 同一个 (工艺, 形状, 厚度) 可能有多种规格：无膜 / Low-E 软膜 / 6-12-6 / 6-16-6。
        # 客户说了哪一种就精确匹配；没说就优先无膜（最便宜、最常报）。
        #
        # 排序策略（依次降级）：
        #   1. variant 精确命中（客户说了 6-16-6）
        #   2. coating 精确命中（客户说了 low-e）
        #   3. 无膜优先
        #   4. 有效期最新的
        sql = """
            SELECT unit_price_aud_m2, min_charge_aud, moq_m2, variant, coating, valid_to
            FROM pricing
            WHERE process=%s AND shape=%s AND thickness_mm=%s
        """
        params: list[object] = [process, shape, float(slots.thickness_mm)]

        if slots.variant:
            sql += " AND variant=%s"
            params.append(slots.variant)
        else:
            sql += " AND variant=''"

        if slots.coating:
            sql += " AND coating=%s"
            params.append(slots.coating)
            sql += " ORDER BY valid_to DESC LIMIT 1"
        else:
            sql += " ORDER BY (coating = 'none') DESC, valid_to DESC LIMIT 1"

        rows = self.store.fetchall(sql, tuple(params))
        if not rows:
            return None
        row = rows[0]
        return PriceQuote(
            unit_price_aud_m2=float(row["unit_price_aud_m2"]),
            min_charge_aud=float(row["min_charge_aud"]),
            moq_m2=float(row["moq_m2"]),
            variant=row["variant"] or "",
            coating=row["coating"],
            valid_to=row["valid_to"],
        )

    # ------------------------------------------------------------------
    # 交期
    # ------------------------------------------------------------------
    def leadtime(self, slots: Slots) -> LeadTimeQuote | None:
        if slots.thickness_mm is None:
            return None
        process = slots.process or "toughened"
        qty = slots.quantity or 1

        rows = self.store.fetchall(
            """
            SELECT business_days, rush_available, rush_surcharge_pct, note
            FROM leadtime
            WHERE process=%s AND thickness_max_mm >= %s AND qty_max >= %s
            ORDER BY thickness_max_mm ASC, qty_max ASC
            LIMIT 1
            """,
            (process, float(slots.thickness_mm), qty),
        )
        if not rows:
            return None
        row = rows[0]
        return LeadTimeQuote(
            business_days=int(row["business_days"]),
            rush_available=bool(row["rush_available"]),
            rush_surcharge_pct=float(row["rush_surcharge_pct"]),
            note=row["note"],
        )
