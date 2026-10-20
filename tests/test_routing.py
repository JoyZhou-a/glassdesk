"""M4 路由层测试：槽位抽取、校验、查表判定、模板渲染。

全部用假对象，不连数据库、不加载模型。
"""

from __future__ import annotations

import pytest

from glassdesk.answer import templates
from glassdesk.routing import slots as S
from glassdesk.routing.structured import (
    CapabilityVerdict,
    LeadTimeQuote,
    PriceQuote,
    StructuredLookup,
)


# ======================================================================
# 槽位抽取
# ======================================================================
@pytest.mark.parametrize(
    "text,width,height",
    [
        ("12mm toughened 2400 wide by 3000 high", 2400.0, 3000.0),
        ("12mm toughened 2400 by 3000", 2400.0, 3000.0),
        ("12mm toughened 2400x3000", 2400.0, 3000.0),
        ("12mm toughened 2.4m x 3m", 2400.0, 3000.0),
        ("12mm toughened 240cm by 300cm", 2400.0, 3000.0),
        ("12mm toughened 3000 high by 2400 wide", 2400.0, 3000.0),
    ],
)
def test_dimension_formats(text, width, height):
    s = S.extract(text)
    assert (s.width_mm, s.height_mm) == (width, height)


def test_thickness_not_confused_with_dimensions():
    """尺寸里的 2400 不能被当成厚度。"""
    s = S.extract("12mm toughened glass 2400 wide by 3000 high")
    assert s.thickness_mm == 12.0
    assert s.width_mm == 2400.0


def test_laminated_nominal_thickness_keeps_two_decimals():
    """夹胶 6.38mm 必须原样保留，四舍五入成 6.4 会导致查表失败。"""
    s = S.extract("6.38mm laminated glass 1950 wide by 2880 high")
    assert s.thickness_mm == 6.38
    assert s.width_mm == 1950.0


def test_unit_conversion_inch():
    s = S.extract('12mm toughened 94 inch by 118 inch')
    assert s.width_mm == pytest.approx(2387.6, abs=0.1)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("toughened glass", "toughened"),
        ("tempered glass", "toughened"),
        ("double glazed unit", "igu"),
        ("double glazing", "igu"),
        ("laminated safety glass", "laminated"),
        ("ordinary glass", "annealed"),
    ],
)
def test_process_synonyms(text, expected):
    assert S.extract(text).process == expected


def test_igu_variant_is_captured():
    """6-12-6 与 6-16-6 是不同报价行，规格串必须抽出来。"""
    s = S.extract("How much for 6mm double glazed (6-16-6) glass?")
    assert s.process == "igu"
    assert s.variant == "6-16-6"


def test_quantity_extraction():
    assert S.extract("How long for 20 panels of toughened glass?").quantity == 20
    assert S.extract("I need 3 sheets").quantity == 3


def test_shape_and_application():
    s = S.extract("What glass for a pool fence, curved?")
    assert s.application == "pool_fence"
    assert s.shape == "bent"


# ======================================================================
# 校验三态
# ======================================================================
def test_validate_no_slots():
    v = S.validate(S.extract("Can you make a big one?"))
    assert v.status == "no_slots"
    assert not v.can_query


def test_validate_missing_asks():
    v = S.validate(S.extract("Can you make 12mm toughened glass?"),
                   need=("thickness_mm", "width_mm", "height_mm"))
    assert v.status == "missing"
    assert set(v.missing) == {"width_mm", "height_mm"}


def test_validate_complete():
    v = S.validate(S.extract("12mm toughened 2400 by 3000"),
                   need=("thickness_mm", "width_mm", "height_mm"))
    assert v.can_query


def test_validate_rejects_impossible_thickness():
    v = S.validate(S.extract("7mm toughened glass 2400 by 3000"))
    assert v.status == "bad_value"
    assert "7" in v.reason


def test_validate_accepts_laminated_specials():
    v = S.validate(S.extract("6.38mm laminated 1950 by 2880"))
    assert v.can_query


# ======================================================================
# 查表判定（假 store）
# ======================================================================
CAP_ROW = {
    "process": "toughened", "shape": "flat", "thickness_mm": 12,
    "max_width_mm": 2440, "max_height_mm": 4800, "max_area_m2": 11.7,
    "min_width_mm": 300, "min_height_mm": 300, "tolerance_mm": 1.5,
    "min_radius_mm": 0, "igu_cavity_mm": None, "notes": None,
}


class FakeStore:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self, sql, params=None):
        return self.one

    def fetchall(self, sql, params=None):
        return self.many


def lookup_with(row):
    return StructuredLookup(FakeStore(one=row))


def test_capability_within_range():
    s = S.extract("12mm toughened 2400 wide by 3000 high")
    verdict = lookup_with(CAP_ROW).check_capability(s)
    assert verdict.can_make is True
    assert verdict.limits["max_width_mm"] == 2440


def test_capability_rejects_oversize_width():
    s = S.extract("12mm toughened 3000 wide by 3000 high")
    verdict = lookup_with(CAP_ROW).check_capability(s)
    assert verdict.can_make is False
    assert "宽度" in verdict.reason
    # 不行的时候必须带回上限，客服要能说"最大只能做多少"
    assert verdict.limits["max_width_mm"] == 2440


def test_capability_rejects_area_even_when_dimensions_pass():
    """长宽都没超但面积超了——这是最容易被漏判的一类。"""
    row = {**CAP_ROW, "max_area_m2": 5.0}
    s = S.extract("12mm toughened 2400 wide by 3000 high")  # 7.2 m2 > 5.0
    verdict = lookup_with(row).check_capability(s)
    assert verdict.can_make is False
    assert "面积" in verdict.reason


def test_capability_unknown_spec_returns_no_limits():
    s = S.extract("12mm toughened 2400 by 3000")
    verdict = lookup_with(None).check_capability(s)
    assert verdict.can_make is False
    assert verdict.limits == {}


def test_capability_requires_thickness():
    verdict = lookup_with(CAP_ROW).check_capability(S.extract("big panel please"))
    assert verdict.can_make is False


# ======================================================================
# 报价：优先级与脱敏
# ======================================================================
def test_price_never_returns_cost_price():
    """成本价可以入库，但绝不能出现在查询结果里（PRD D13）。"""
    row = {
        "unit_price_aud_m2": 198, "min_charge_aud": 380, "moq_m2": 0.5,
        "variant": "", "coating": "none", "valid_to": None,
        "cost_price_aud_m2": 111,   # 就算被 SELECT 出来也不该进对象
    }
    quote = StructuredLookup(FakeStore(many=[row])).price(
        S.extract("12mm toughened glass")
    )
    assert quote is not None
    assert not hasattr(quote, "cost_price_aud_m2")
    assert "111" not in repr(quote)


def test_price_query_prefers_no_coating_when_unspecified():
    """客户没说涂膜时，SQL 里要优先选 none（最便宜、最常报）。"""
    captured = {}

    class RecordingStore(FakeStore):
        def fetchall(self, sql, params=None):
            captured["sql"] = sql
            return [{"unit_price_aud_m2": 198, "min_charge_aud": 380, "moq_m2": 0.5,
                     "variant": "", "coating": "none", "valid_to": None}]

    StructuredLookup(RecordingStore()).price(S.extract("12mm toughened glass"))
    assert "(coating = 'none') DESC" in captured["sql"]


def test_price_filters_variant_when_specified():
    captured = {}

    class RecordingStore(FakeStore):
        def fetchall(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return []

    StructuredLookup(RecordingStore()).price(
        S.extract("How much for 6mm double glazed (6-16-6)?")
    )
    assert "variant=%s" in captured["sql"]
    assert "6-16-6" in captured["params"]


# ======================================================================
# 交期
# ======================================================================
def test_leadtime_returns_days():
    row = {"business_days": 15, "rush_available": 1, "rush_surcharge_pct": 25.0, "note": None}
    quote = StructuredLookup(FakeStore(many=[row])).leadtime(
        S.extract("How long for 6 panels of toughened glass up to 8mm?")
    )
    assert quote.business_days == 15
    assert quote.rush_available is True


# ======================================================================
# 模板：电话模式硬约束（PRD I2/I4/I5）
# ======================================================================
def _slots():
    return S.extract("12mm toughened 2400 wide by 3000 high")


def test_phone_answer_at_most_three_lines():
    verdict = CapabilityVerdict(True, "ok", limits={"tolerance_mm": 1.5})
    lines = templates.capability_phone(_slots(), verdict)
    assert 1 <= len(lines) <= templates.PHONE_MAX_LINES


def test_phone_answer_sentences_within_limit():
    verdict = CapabilityVerdict(
        False, "宽度 3000mm 超过上限 2440mm",
        limits={"max_width_mm": 2440, "max_height_mm": 4800},
    )
    for line in templates.capability_phone(_slots(), verdict):
        assert len(line) <= templates.PHONE_MAX_SENTENCE + 1  # 允许省略号


def test_phone_refusal_mentions_limit_and_alternative():
    """答"不行"时必须给上限和替代方案——只说不行的话客服没法用。"""
    verdict = CapabilityVerdict(
        False, "宽度 3000mm 超过上限 2440mm",
        limits={"max_width_mm": 2440, "max_height_mm": 4800},
    )
    text = " ".join(templates.capability_phone(_slots(), verdict))
    assert "2440" in text
    assert "split" in text.lower() or "thinner" in text.lower()


def test_phone_answer_has_no_markdown_table():
    quote = PriceQuote(198, 380, 0.5, "", "none", None)
    text = " ".join(templates.price_phone(quote, _slots()))
    assert "|" not in text
    assert "**" not in text


def test_email_answer_includes_source():
    quote = LeadTimeQuote(15, True, 25.0, None)
    text = templates.leadtime_email(quote, _slots())
    assert "Source" in text
    assert "15" in text


def test_ask_back_lists_only_missing_fields():
    text = templates.ask_back(["width_mm", "height_mm"])
    assert "width" in text and "height" in text
    assert "thickness" not in text
