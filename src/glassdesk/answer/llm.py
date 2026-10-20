"""LLM 客户端 + 磁盘缓存。

**缓存是省钱的关键。** 同一个问题、同样的上下文、同样的参数，第二次调用直接读盘。
调试提示词时会反复跑同一批题，没有缓存每改一个字就重新烧一遍 token。

缓存键包含 model / temperature / max_tokens / 完整提示词——任何一项变了都应重算，
否则会拿到旧结果而不自知。

同时记录调用次数与 token 用量，评测报告要能说清"这次花了多少"。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from glassdesk.config import LLMConfig
from glassdesk.logger import get_logger

log = get_logger("answer.llm")


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    #: 服务端实际返回的模型名。实测配置 deepseek-chat 会返回 deepseek-flash，
    #: 记录下来便于核对到底用的哪个模型。
    actual_model: str | None = None
    latency_ms: float = 0.0


@dataclass
class LLMStats:
    calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class CachedLLM:
    """对 OpenAI 兼容接口的薄封装，带磁盘缓存与用量统计。"""

    def __init__(
        self,
        cfg: LLMConfig,
        cache_dir: str | Path,
        *,
        use_cache: bool = True,
    ) -> None:
        self._cfg = cfg
        self._cache_dir = Path(cache_dir)
        self._use_cache = use_cache
        self._client = None
        self.stats = LLMStats()
        if use_cache:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._cfg.api_key.get_secret_value(),
                base_url=self._cfg.base_url,
                timeout=self._cfg.timeout_s,
            )
        return self._client

    def cache_key(self, system: str, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self._cfg.model,
                "temperature": self._cfg.temperature,
                "max_tokens": self._cfg.max_tokens,
                "system": system,
                "prompt": prompt,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    # ------------------------------------------------------------------
    def complete(self, prompt: str, *, system: str = "You are a helpful assistant.") -> LLMResponse:
        key = self.cache_key(system, prompt)
        path = self._cache_path(key)

        if self._use_cache and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.stats.cache_hits += 1
            log.info("缓存命中 {}", key[:12])
            return LLMResponse(
                text=data["text"], model=data["model"],
                prompt_tokens=data.get("prompt_tokens", 0),
                completion_tokens=data.get("completion_tokens", 0),
                cached=True, actual_model=data.get("actual_model"),
            )

        started = time.perf_counter()
        client = self._ensure_client()
        self.stats.calls += 1
        completion = client.chat.completions.create(
            model=self._cfg.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=self._cfg.temperature,
            max_tokens=self._cfg.max_tokens,
        )
        latency = (time.perf_counter() - started) * 1000
        text = (completion.choices[0].message.content or "").strip()
        usage = completion.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        self.stats.prompt_tokens += prompt_tokens
        self.stats.completion_tokens += completion_tokens

        response = LLMResponse(
            text=text, model=self._cfg.model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            actual_model=getattr(completion, "model", None), latency_ms=latency,
        )

        if self._use_cache:
            path.write_text(
                json.dumps({
                    "text": text, "model": self._cfg.model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "actual_model": response.actual_model,
                    "saved_at": datetime.now().isoformat(timespec="seconds"),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        log.info(
            "LLM 调用完成 {}ms（prompt={} completion={}）",
            f"{latency:.0f}", prompt_tokens, completion_tokens,
        )
        return response
