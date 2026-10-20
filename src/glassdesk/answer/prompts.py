"""提示词：把检索到的证据变成"客服能照着念的英文答案"。

三条硬约束写进了提示词，并且**代码侧还会再校验一次**——模型不一定听话：

1. **只能用给定资料**。不许用外部知识，不许编数字/页码/条款号。
2. **出处由代码拼**。模型只写正文，否则它会自己编一个 "(p.42)"。
3. **权威分级**。``experience`` 来源必须说成 "Usually / In our experience"，
   不能当成标准要求来讲——这是合规要求，不是文字风格。

输出用一个首行标记来传达"能不能答"：

    ANSWER:       资料够，后面是答案
    INSUFFICIENT: 资料不够，后面是给客服的说明

用标记而不是 JSON，是因为解析简单、模型不容易写坏；代码只认前缀，认不出就按
"资料不足" 处理（宁可转人工，不可瞎答）。
"""

from __future__ import annotations

from glassdesk.constants import Authority, OutputMode
from glassdesk.retrieval.service import RetrievedChunk

SYSTEM = (
    "You are the English knowledge assistant for a glass manufacturer. "
    "You answer questions from customer service staff who may be on the phone "
    "with a customer while reading your answer. Be brief, factual and speakable. "
    "You never invent figures, page numbers or clause numbers."
)

MARK_OK = "ANSWER:"
MARK_INSUFFICIENT = "INSUFFICIENT:"


def render_sources(chunks: list[RetrievedChunk]) -> str:
    """把检索结果渲染成带编号的资料块，含权威级别与确认日期。"""
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        tags = [f"authority={chunk.authority}"]
        if chunk.clause:
            tags.append(f"section={chunk.clause}")
        if chunk.page_no:
            tags.append(f"page={chunk.page_no}")
        if chunk.authority == Authority.EXPERIENCE.value:
            tags.append("unverified-practice")
        body = " ".join(chunk.context.split())
        lines.append(f"[{index}] ({', '.join(tags)})\n{body}")
    return "\n\n".join(lines)


def _phone_style() -> str:
    return (
        "OUTPUT STYLE (phone):\n"
        "- At most 3 sentences in total.\n"
        "- Each sentence under 60 characters.\n"
        "- No tables, no bullet lists, no markdown, no headings.\n"
        "- The first sentence must answer the question directly and be readable aloud.\n"
        "- Do not write any citation or source reference; the system appends it."
    )


def _email_style() -> str:
    return (
        "OUTPUT STYLE (email):\n"
        "- One short paragraph (2-4 sentences) that answers the question.\n"
        "- Then at most 3 short lines of practical detail.\n"
        "- Plain text only: no markdown headings, no tables, no bold.\n"
        "- Do not write any citation or source reference; the system appends it."
    )


def build(
    question: str,
    chunks: list[RetrievedChunk],
    mode: OutputMode | str = OutputMode.PHONE,
) -> tuple[str, str]:
    """返回 ``(system, user_prompt)``。"""
    mode_value = mode.value if isinstance(mode, OutputMode) else str(mode)
    style = _phone_style() if mode_value == OutputMode.PHONE.value else _email_style()

    has_experience = any(c.authority == Authority.EXPERIENCE.value for c in chunks)
    has_authoritative = any(c.authority != Authority.EXPERIENCE.value for c in chunks)

    authority_note = []
    if has_experience:
        authority_note.append(
            "- Some sources are marked authority=experience. These are internal "
            "practice, not a standard. Phrase them as \"Usually ...\" or "
            "\"In our experience ...\" and never present them as a requirement."
        )
    if has_authoritative:
        authority_note.append(
            "- Sources with authority=standard or manual may be stated directly."
        )
    if has_experience and has_authoritative:
        authority_note.append(
            "- If a standard and an experience note disagree, follow the standard."
        )

    prompt = f"""Answer the customer service question using ONLY the sources below.

SOURCES
{render_sources(chunks)}

QUESTION
{question}

RULES
- Your first line must be exactly one of:
    {MARK_OK} <your answer>
    {MARK_INSUFFICIENT} <short reason for the agent>
- Never invent sizes, prices, lead times, page numbers, clause numbers or standard names.
- If the sources do not contain the answer, use {MARK_INSUFFICIENT} and say what
  a colleague would need to check. Do not guess.
- Answer in English, British/Australian spelling.
{chr(10).join(authority_note)}

{style}
"""
    return SYSTEM, prompt


def insufficient_hint(question: str) -> str:
    """给客服看的兜底说明，仅在模型不可用时使用。"""
    return f"Sources on file do not settle this question: {question.strip()}"
