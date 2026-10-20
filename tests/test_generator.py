"""答案生成测试：全部用假 LLM，**零 token**。"""

from __future__ import annotations

import pytest

from glassdesk.answer.generator import (
    PHONE_MAX_LINES,
    PHONE_MAX_SENTENCE,
    AnswerGenerator,
    _split_marker,
)
from glassdesk.answer.llm import LLMResponse
from glassdesk.constants import AnswerState, OutputMode
from glassdesk.retrieval.service import RetrievedChunk


class FakeLLM:
    """按预设文本返回，记录被调用了几次。"""

    def __init__(self, text: str = "ANSWER: It is fine.", *, raise_error: bool = False):
        self.text = text
        self.raise_error = raise_error
        self.calls = 0
        self.last_prompt = ""
        self.last_system = ""

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        self.calls += 1
        self.last_prompt = prompt
        self.last_system = system
        if self.raise_error:
            raise RuntimeError("network down")
        return LLMResponse(text=self.text, model="fake-model",
                           prompt_tokens=100, completion_tokens=20)


def chunk(authority: str = "manual", text: str = "12mm toughened allows 2440 x 4800.",
          clause: str = "Table 2-1", page: int | None = 12) -> RetrievedChunk:
    return RetrievedChunk(chunk_id="c1", text=text, score=0.9, doc_id="d",
                          authority=authority, clause=clause, page_no=page)


@pytest.fixture
def cfg(settings):
    return settings.answer


def gen(settings, text="ANSWER: Yes, 12mm toughened is available.", **kw):
    fake = FakeLLM(text, **kw)
    return AnswerGenerator(fake, settings.answer), fake


# ======================================================================
# 标记解析
# ======================================================================
def test_split_marker_ok():
    marker, body = _split_marker("ANSWER: Yes it can.")
    assert marker == "ok" and body == "Yes it can."


def test_split_marker_insufficient():
    marker, _ = _split_marker("INSUFFICIENT: no data")
    assert marker == "insufficient"


def test_split_marker_unknown_falls_back_to_unknown():
    """认不出标记就当"不确定"，由调用方按资料不足处理。"""
    marker, body = _split_marker("Sure! The answer is 42.")
    assert marker == "unknown"
    assert body.startswith("Sure")


# ======================================================================
# 正常路径
# ======================================================================
def test_answer_marker_returns_ok(settings):
    g, fake = gen(settings)
    result = g.generate("Can you do 12mm?", [chunk()])
    assert result.status is AnswerState.OK
    assert fake.calls == 1
    assert "12mm" in result.text


def test_citations_come_from_code_not_model(settings):
    """模型一个字都没写出处，出处仍必须存在，且来自检索结果。"""
    g, _ = gen(settings, "ANSWER: Yes, that size is fine.")
    result = g.generate("q", [chunk(clause="Table 2-1", page=12)])
    assert result.citations == ["(Table 2-1, p.12)"]
    assert "p.12" in result.render()
    # 模型输出里没有任何页码
    assert "p.12" not in result.text


def test_prompt_contains_numbered_sources(settings):
    g, fake = gen(settings)
    g.generate("q", [chunk(), chunk(text="Second source.")])
    assert "[1]" in fake.last_prompt and "[2]" in fake.last_prompt
    assert "INSUFFICIENT:" in fake.last_prompt


# ======================================================================
# 拒答
# ======================================================================
def test_insufficient_marker_escalates(settings):
    g, _ = gen(settings, "INSUFFICIENT: nothing on file about submarines.")
    result = g.generate("Do you make submarine glass?", [chunk()])
    assert result.status is AnswerState.ESCALATE
    assert not result.is_answerable


def test_unknown_marker_escalates_rather_than_guessing(settings):
    """模型没按格式输出时，宁可转人工也不能直接当答案。"""
    g, _ = gen(settings, "The answer is probably 12mm.")
    result = g.generate("q", [chunk()])
    assert result.status is AnswerState.ESCALATE


def test_no_chunks_skips_llm_entirely(settings):
    g, fake = gen(settings)
    result = g.generate("q", [])
    assert result.status is AnswerState.ESCALATE
    assert fake.calls == 0      # 不该为一个没证据的问题花钱


def test_llm_failure_returns_service_error(settings):
    g, _ = gen(settings, raise_error=True)
    result = g.generate("q", [chunk()])
    assert result.status is AnswerState.SERVICE_ERROR
    assert "engineering" in result.text.lower()


# ======================================================================
# 电话模式硬约束
# ======================================================================
def test_phone_mode_truncates_extra_sentences(settings):
    g, _ = gen(settings, "ANSWER: One. Two. Three. Four. Five.")
    result = g.generate("q", [chunk()], OutputMode.PHONE)
    assert len(result.text.split(".")) - 1 <= PHONE_MAX_LINES
    assert result.status is AnswerState.LOW_CONFIDENCE
    assert any("截断" in n for n in result.notes)


def test_phone_mode_truncates_long_sentence(settings):
    long_sentence = "Yes " + "and " * 40 + "so on."
    g, _ = gen(settings, f"ANSWER: {long_sentence}")
    result = g.generate("q", [chunk()], OutputMode.PHONE)
    assert len(result.text) <= PHONE_MAX_SENTENCE + 20
    assert any("60 字符" in n for n in result.notes)


def test_markdown_is_stripped(settings):
    g, _ = gen(settings, "ANSWER: **Yes** it can.\n### Details\n- Size ok")
    result = g.generate("q", [chunk()], OutputMode.PHONE)
    assert "**" not in result.text
    assert "###" not in result.text


def test_email_mode_keeps_more_detail(settings):
    g, _ = gen(settings, "ANSWER: Yes. Tolerance is 1.5mm. Lead time applies.")
    result = g.generate("q", [chunk()], OutputMode.EMAIL)
    assert result.status is AnswerState.OK
    assert result.mode is OutputMode.EMAIL


# ======================================================================
# 权威分级
# ======================================================================
def test_experience_only_answer_gets_hedge(settings):
    """只有经验来源时，必须加对冲前缀（PRD 分级规则）。"""
    g, _ = gen(settings, "ANSWER: Cracks at the edge are installation damage.")
    result = g.generate("q", [chunk(authority="experience")])
    assert result.authority == "experience"
    assert result.text.lower().startswith(("usually", "in our experience", "typically"))


def test_mixed_evidence_still_gets_hedge(settings):
    """混合证据里经验性说法照样会漏出去，所以也要加前缀。

    实测踩过：池围栏问题同时命中标准和工单，模型把工单里那句
    "Never quote ordinary toughened glass" 直接写进了答案。
    """
    g, _ = gen(settings, "ANSWER: Use a laminated assembly.")
    result = g.generate("q", [chunk(authority="standard"), chunk(authority="experience")])
    assert result.text.lower().startswith(("usually", "in our experience", "typically"))
    assert any("experience" in n for n in result.notes)


def test_citations_are_ordered_by_authority(settings):
    """出处按权威级别排，不是按相关度。工单相关度最高但不是依据。"""
    g, _ = gen(settings, "ANSWER: Use laminated toughened.")
    experience = RetrievedChunk(chunk_id="e", text="t", score=0.99, doc_id="tickets",
                                authority="experience", clause="Service Tickets")
    standard = RetrievedChunk(chunk_id="s", text="t", score=0.40, doc_id="std",
                              authority="standard", clause="Glazing used as a pool barrier")
    result = g.generate("q", [experience, standard])
    assert result.citations[0].startswith("(Glazing used as a pool barrier")


def test_hedge_not_duplicated(settings):
    g, _ = gen(settings, "ANSWER: Usually this is installation damage.")
    result = g.generate("q", [chunk(authority="experience")])
    assert result.text.lower().count("usually") == 1


def test_authoritative_source_wins_over_experience(settings):
    g, _ = gen(settings, "ANSWER: Doors must be safety glazed.")
    result = g.generate("q", [chunk(authority="experience"), chunk(authority="standard")])
    assert result.authority == "standard"
    assert not result.text.lower().startswith("usually")


def test_experience_sources_are_tagged_in_prompt(settings):
    g, fake = gen(settings)
    g.generate("q", [chunk(authority="experience")])
    assert "unverified-practice" in fake.last_prompt
    assert "Usually" in fake.last_prompt


# ======================================================================
# 缓存标记
# ======================================================================
def test_cached_flag_propagates(settings):
    class CachedFake(FakeLLM):
        def complete(self, prompt, *, system=""):
            self.calls += 1
            return LLMResponse(text=self.text, model="m", cached=True)

    g = AnswerGenerator(CachedFake("ANSWER: Fine."), settings.answer)
    result = g.generate("q", [chunk()])
    assert result.cached is True
