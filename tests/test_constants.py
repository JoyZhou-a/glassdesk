"""常量与枚举的契约测试：这些值一旦被改，测试必须失败。"""

from __future__ import annotations

from glassdesk.constants import (
    AUTHORITY_LABELS_ZH,
    EXPERIENCE_PREFIX,
    INTENT_LABELS_ZH,
    AnswerState,
    Authority,
    Channel,
    Intent,
    OutputMode,
)


def test_intent_has_exactly_six_members():
    """PRD §7.1：5 类 + other 兜底，刻意不穷举。"""
    assert len(Intent) == 6
    assert Intent.OTHER.value == "other"


def test_every_intent_has_chinese_label():
    assert set(INTENT_LABELS_ZH) == set(Intent)


def test_authority_classification():
    assert Authority.STANDARD.is_authoritative is True
    assert Authority.MANUAL.is_authoritative is True
    assert Authority.EXPERIENCE.is_authoritative is False
    assert set(AUTHORITY_LABELS_ZH) == set(Authority)


def test_answer_states_cover_prd_contract():
    """PRD §7.3 规定的五种终态，缺一不可。"""
    assert {s.value for s in AnswerState} == {
        "ok",
        "need_slot",
        "low_confidence",
        "escalate",
        "service_error",
    }


def test_channel_determines_output_mode():
    assert Channel.PHONE.output_mode is OutputMode.PHONE
    assert Channel.EMAIL.output_mode is OutputMode.EMAIL


def test_experience_prefix_is_non_empty():
    assert EXPERIENCE_PREFIX.strip()
