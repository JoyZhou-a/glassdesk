"""答案生成：检索结果 → 英文答案。

流程::

    证据（RetrievedChunk 列表）
      → 组提示词（含权威分级说明）
      → 调 LLM（带缓存）
      → 解析首行标记（ANSWER / INSUFFICIENT）
      → 代码侧校验（电话模式行数、句长、禁表格）
      → **代码拼出处**（模型永远不写引用）
      → 定状态：ok / low_confidence / escalate

关键点：**出处由代码按实际检索结果拼接**。让模型写引用，它一定会编页码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from glassdesk.answer import prompts
from glassdesk.answer.llm import CachedLLM, LLMResponse
from glassdesk.config import AnswerConfig
from glassdesk.constants import AnswerState, Authority, OutputMode
from glassdesk.logger import get_logger
from glassdesk.retrieval.service import RetrievedChunk

log = get_logger("answer.generator")

#: 电话模式单句上限（PRD I5）
PHONE_MAX_SENTENCE = 60
#: 电话模式最多行数（PRD I2）
PHONE_MAX_LINES = 3
#: 出现这些就说明模型在写富文本，电话模式不能用
_FORBIDDEN = ("|", "**", "###", "- ", "•")


@dataclass(slots=True)
class GeneratedAnswer:
    status: AnswerState
    mode: OutputMode
    text: str
    citations: list[str] = field(default_factory=list)
    authority: str | None = None
    evidence_count: int = 0
    cached: bool = False
    usage: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def is_answerable(self) -> bool:
        return self.status in (AnswerState.OK, AnswerState.LOW_CONFIDENCE)

    def render(self) -> str:
        """最终给客服看的文本：正文 + 出处。

        出处可能有多条（标准和经验都命中的情况），全部列出——客服被客户追问
        "依据是什么" 时要能说清出处。
        """
        if not self.citations:
            return self.text
        return f"{self.text}\n{' '.join(self.citations)}"


class AnswerGenerator:
    def __init__(self, llm: CachedLLM, cfg: AnswerConfig) -> None:
        self._llm = llm
        self._cfg = cfg

    # ------------------------------------------------------------------
    def generate(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        mode: OutputMode | str = OutputMode.PHONE,
    ) -> GeneratedAnswer:
        mode_enum = mode if isinstance(mode, OutputMode) else OutputMode(str(mode))

        # 1. 没有任何证据：直接转人工，不浪费一次调用
        if not chunks:
            return GeneratedAnswer(
                status=AnswerState.ESCALATE,
                mode=mode_enum,
                text=prompts.insufficient_hint(question),
                notes=["检索无结果"],
            )

        # 2. 调模型
        system, user_prompt = prompts.build(question, chunks, mode_enum)
        try:
            response = self._llm.complete(user_prompt, system=system)
        except Exception as exc:  # noqa: BLE001
            log.error("LLM 调用失败：{}", exc)
            return GeneratedAnswer(
                status=AnswerState.SERVICE_ERROR,
                mode=mode_enum,
                text="I cannot reach the knowledge service right now. Please check with engineering.",
                evidence_count=len(chunks),
                notes=[f"LLM 失败: {exc}"],
            )

        # 3. 解析标记
        status_marker, body = _split_marker(response.text)
        notes: list[str] = []

        if status_marker == "insufficient":
            return GeneratedAnswer(
                status=AnswerState.ESCALATE,
                mode=mode_enum,
                text=body or prompts.insufficient_hint(question),
                evidence_count=len(chunks),
                cached=response.cached,
                usage=_usage(response),
                notes=["模型判定资料不足"],
            )

        if status_marker == "unknown":
            # 模型没按约定的标记输出。**绝不能**把这种文本当答案——
            # 它可能是一段未经约束的自由发挥，而尺寸/价格答错是赔钱事故。
            return GeneratedAnswer(
                status=AnswerState.ESCALATE,
                mode=mode_enum,
                text=prompts.insufficient_hint(question),
                evidence_count=len(chunks),
                cached=response.cached,
                usage=_usage(response),
                notes=["模型未按格式输出，按资料不足处理"],
            )

        # 4. 代码侧格式校验：模型不一定听话
        body = _strip_markdown(body)
        if mode_enum is OutputMode.PHONE:
            body, violations = _enforce_phone(body)
            notes.extend(violations)

        # 5. 权威级别：有非经验来源就算权威，否则算经验
        authority = _dominant_authority(chunks)
        used_experience = any(c.authority == Authority.EXPERIENCE.value for c in chunks)
        if used_experience and not _has_hedge(body):
            # 证据里只要有经验来源，就不能让经验性说法冒充规范。
            # 只对"全是经验"加前缀不够——混合证据里那些经验性句子照样会漏出去。
            body = f"In our experience, {body[0].lower() + body[1:] if body else body}"
            notes.append("证据含 experience 来源，已加经验话术前缀")

        # 6. 出处由代码拼，且**按权威级别排序**，不是按相关度。
        #    实测踩过：池围栏问题里工单排第一，直接取 top1 会把出处引成工单，
        #    而不是真正的依据（AS 1926.1 摘要）。
        citations = _citations_by_authority(chunks)

        status = AnswerState.OK if not notes else AnswerState.LOW_CONFIDENCE
        return GeneratedAnswer(
            status=status,
            mode=mode_enum,
            text=body,
            citations=citations,
            authority=authority,
            evidence_count=len(chunks),
            cached=response.cached,
            usage=_usage(response),
            notes=notes,
        )


# ======================================================================
def _split_marker(text: str) -> tuple[str, str]:
    """读首行标记。认不出就按"资料不足"处理——宁可转人工，不可瞎答。"""
    stripped = text.lstrip()
    upper = stripped.upper()
    if upper.startswith(prompts.MARK_OK):
        return "ok", stripped[len(prompts.MARK_OK):].strip()
    if upper.startswith(prompts.MARK_INSUFFICIENT):
        return "insufficient", stripped[len(prompts.MARK_INSUFFICIENT):].strip()
    return "unknown", stripped


def _strip_markdown(text: str) -> str:
    """去掉模型自作主张加的 markdown（电话模式念不出来）。"""
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*•]\s+", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def _enforce_phone(text: str) -> tuple[str, list[str]]:
    """把文本压成符合电话模式的形态，返回 (新文本, 违规说明)。"""
    violations: list[str] = []
    if any(token in text for token in _FORBIDDEN):
        violations.append("模型输出了表格/列表，已清理")

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) > PHONE_MAX_LINES:
        sentences = sentences[:PHONE_MAX_LINES]
        violations.append(f"句子超过 {PHONE_MAX_LINES} 句，已截断")

    out: list[str] = []
    for sentence in sentences:
        if len(sentence) > PHONE_MAX_SENTENCE:
            sentence = sentence[: PHONE_MAX_SENTENCE - 1].rstrip() + "…"
            if "有句子超过 60 字符，已截断" not in violations:
                violations.append("有句子超过 60 字符，已截断")
        out.append(sentence)
    return " ".join(out), violations


def _dominant_authority(chunks: list[RetrievedChunk]) -> str:
    """有权威来源就算权威；全是经验才算经验。"""
    for chunk in chunks:
        if chunk.authority != Authority.EXPERIENCE.value:
            return chunk.authority
    return Authority.EXPERIENCE.value


#: 引用排序：标准 > 手册 > 经验。相关度高的往往是措辞最像的工单，
#: 但它不是"依据"——把依据列在工单前面，客服才敢对客户引用。
_AUTHORITY_RANK = {
    Authority.STANDARD.value: 0,
    Authority.MANUAL.value: 1,
    Authority.EXPERIENCE.value: 2,
}


def _citations_by_authority(chunks: list[RetrievedChunk], limit: int = 2) -> list[str]:
    """按权威级别挑出处，去重后最多 ``limit`` 条。"""
    ordered = sorted(
        chunks,
        key=lambda c: (_AUTHORITY_RANK.get(c.authority, 9), -(c.score or 0.0)),
    )
    seen: set[str] = set()
    out: list[str] = []
    for chunk in ordered:
        citation = chunk.citation()
        if citation in seen:
            continue
        seen.add(citation)
        out.append(citation)
        if len(out) >= limit:
            break
    return out


def _has_hedge(text: str) -> bool:
    lowered = text.lower()
    return any(lowered.startswith(p) or p in lowered[:40]
               for p in ("usually", "in our experience", "typically"))


def _usage(response: LLMResponse) -> dict:
    return {
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "actual_model": response.actual_model,
    }
