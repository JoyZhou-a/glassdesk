"""重排：用交叉编码器给"查询-候选"逐对打分。

bge-m3 是**双塔**模型（查询和文档各自编码再算余弦），快但精度有限；
reranker 是**交叉编码器**（把查询和候选拼在一起送进模型），慢但准。

实测（CPU）：20 个候选 739 ms（37 ms/条），可以承受。

**但实测也发现它并不可靠**：措辞几乎相同的工单能拿 0.99 分，
真正含答案的能力矩阵行只有 0.38 分。所以：

1. 不能靠它兜底——权威过滤必须在召回阶段做；
2. 它的作用是"在同类候选里排序"，而不是"跨来源做裁决"。
"""

from __future__ import annotations

import os
from pathlib import Path

from glassdesk.logger import get_logger

log = get_logger("retrieval.rerank")


class Reranker:
    """CrossEncoder 封装。延迟加载，可与 embedder 分时驻留显存。"""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        max_length: int = 512,
    ) -> None:
        self.model_path = Path(model_path)
        self.device = device if device != "auto" else self._detect_device()
        self.max_length = max_length
        self._model = None

    @staticmethod
    def _detect_device() -> str:
        try:
            import torch
        except ImportError:
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if not self.model_path.is_dir():
            raise FileNotFoundError(f"找不到 reranker 模型目录：{self.model_path}")

        # 与 embedder 同理：模型一律本地，禁止联网，否则会挂住且不报错
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        from sentence_transformers import CrossEncoder

        log.info("加载 reranker：{}（device={}）", self.model_path, self.device)
        self._model = CrossEncoder(
            str(self.model_path), device=self.device, max_length=self.max_length
        )

    # ------------------------------------------------------------------
    def score(self, query: str, texts: list[str]) -> list[float]:
        """返回每个候选的相关性分数，顺序与传入一致。"""
        if not texts:
            return []
        self._ensure_loaded()
        assert self._model is not None
        pairs = [[query, t] for t in texts]
        scores = self._model.predict(pairs)
        return [float(s) for s in scores]

    def rerank(self, query: str, items: list[tuple[str, object]]) -> list[tuple[object, float]]:
        """通用重排：``items`` 是 ``(文本, 任意载荷)``，按分数降序返回。"""
        if not items:
            return []
        scores = self.score(query, [text for text, _ in items])
        paired = [(payload, s) for (_, payload), s in zip(items, scores)]
        paired.sort(key=lambda p: p[1], reverse=True)
        return paired

    # ------------------------------------------------------------------
    def release(self) -> None:
        """释放模型与显存，供与 embedder 交替使用。"""
        if self._model is None:
            return
        self._model = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        log.info("reranker 已释放")
