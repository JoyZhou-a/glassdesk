"""请求 / 响应模型。

字段设计与 PRD 的"答案状态机"对齐：``status`` 是五种终态之一，
前端据此决定用什么颜色、要不要提示"转人工"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    mode: Literal["phone", "email"] = "phone"
    use_cache: bool = True


class Timing(BaseModel):
    retrieval_ms: float = 0.0
    total_ms: float = 0.0


class TokenUsage(BaseModel):
    calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class AskResponse(BaseModel):
    question: str
    mode: str
    #: 走哪条路：capability / price / leadtime / documents / ask / unsupported / refuse
    route: str
    #: 五种终态：answer / need_slot / refuse / escalate / service_error
    status: str
    answer: str
    citations: list[str] = Field(default_factory=list)
    authority: str | None = None
    evidence: int = 0
    notes: list[str] = Field(default_factory=list)
    timing: Timing = Field(default_factory=Timing)
    tokens: TokenUsage = Field(default_factory=TokenUsage)


class HealthResponse(BaseModel):
    ok: bool
    model_loaded: bool
    kb_version: str | None = None
    documents: int = 0
    chunks: int = 0
    vectors: int = 0
    detail: str = ""
