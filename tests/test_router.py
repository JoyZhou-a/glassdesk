"""路由测试：这是"该查表还是该追问还是该查文档"的判据。

这两条规则都踩过子串匹配的坑，必须用测试锁住。
"""

from __future__ import annotations

import pytest

from glassdesk.routing.router import _matches, decide


# ======================================================================
# 关键词匹配
# ======================================================================
@pytest.mark.parametrize(
    "text,expected",
    [
        # 两次踩过的坑
        ("Can you install it at my house as well?", False),   # install 里含 tall
        ("Can we join two panels invisibly?", False),         # panels 不该匹配 pane
        # 正常命中
        ("Can you make a big one?", True),
        ("Can you do it thicker?", True),                     # thick + er
        ("How much for a pane?", True),
        ("Do you make larger sizes?", True),                  # large + r, size + s
        ("What glass for a pool fence?", False),
        ("Is your toughened glass approved for aircraft use?", False),
    ],
)
def test_size_hint_matching(text, expected):
    from glassdesk.routing.router import SIZE_HINTS

    assert _matches(text, SIZE_HINTS) is expected


# ======================================================================
# 路由判据
# ======================================================================
def test_complete_dimensions_route_to_capability():
    route = decide("Can you make 12mm toughened glass 2400 wide by 3000 high?")
    assert route.target == "capability"
    assert route.slots.thickness_mm == 12.0


def test_missing_thickness_asks():
    """有宽高没厚度：必须追问，不能猜厚度。"""
    route = decide("Can you make a single panel 3000 wide by 6000 high?")
    assert route.target == "ask"
    assert "thickness_mm" in (route.missing or [])


def test_size_question_without_numbers_asks():
    """问大小但一个数字都没有——追问比检索有用。"""
    assert decide("Can you make a big one?").target == "ask"
    assert decide("Can you do it thicker?").target == "ask"


def test_policy_question_with_size_words_goes_to_documents():
    """含 panel/big 但问的是流程，文档里有答案，不该追问。"""
    route = decide("Customer insists on one piece. Can we join two panels invisibly?")
    assert route.target == "documents"


def test_price_route_needs_only_thickness():
    route = decide("How much per square metre for 12mm toughened glass?")
    assert route.target == "price"


def test_leadtime_route():
    route = decide("How long for 20 panels of toughened glass up to 8mm thick?")
    assert route.target == "leadtime"


def test_price_without_thickness_asks():
    route = decide("How much for a pane?")
    assert route.target == "ask"


def test_bad_thickness_routes_to_unsupported():
    """7mm 钢化不存在——这是确定答案，不是"要问工程师"。"""
    route = decide("Can you make 7mm toughened glass 2000 wide by 3000 high?")
    assert route.target == "unsupported"


def test_plain_compliance_question_goes_to_documents():
    assert decide("Does the glass next to my front door have to be safety glass?").target == "documents"


def test_out_of_scope_goes_to_documents():
    """语料外的问题也走文档路线，由生成层判 INSUFFICIENT。"""
    assert decide("Do you supply glass for submarines?").target == "documents"
    assert decide("What are your opening hours on Christmas Day?").target == "documents"
