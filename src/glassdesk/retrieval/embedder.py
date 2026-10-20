"""bge-m3 编码器封装（不依赖 FlagEmbedding）。

bge-m3 一次前向同时产出两种向量：

- **dense**：取 ``[CLS]`` 位置的隐藏态并做 L2 归一化，1024 维，管"语义相近"
- **sparse**：把隐藏态过 ``sparse_linear`` 头得到逐 token 权重，再按 token id
  做 max 池化，管"字面命中"（型号、标准号、色号这类精确串）

两者用途不同，检索时混合使用。只留稠密向量的话，``AS1288`` 和 ``AS 1288``
在向量空间里几乎一样，但业务上必须精确匹配。

**为什么不用 ``milvus_model.hybrid.BGEM3EmbeddingFunction``**：
它内部依赖 ``FlagEmbedding``，加载时会尝试访问 HuggingFace 并长时间挂住
（实测 >10 分钟无报错）。直接走 ``transformers`` 加载本地权重只要 0.8 秒，
而且本文件就是完整实现，没有黑箱。

**延迟加载**：模型只在第一次编码时载入，避免 import 就吃掉 2 GB 显存。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from glassdesk.logger import get_logger

log = get_logger("retrieval.embedder")

#: bge-m3 的稀疏头文件名（Linear(hidden, 1)）
SPARSE_HEAD_FILE = "sparse_linear.pt"


@dataclass(slots=True)
class EncodedVector:
    """一条文本的两种向量。"""

    dense: list[float]
    sparse: dict[int, float]


def _pick_device(preference: str = "auto") -> tuple[str, bool]:
    """返回 (device, use_fp16)。fp16 只在 CUDA 上开：CPU 上 fp16 又慢又不准。"""
    if preference != "auto":
        return preference, preference == "cuda"
    try:
        import torch
    except ImportError:  # 没装 ml extra
        return "cpu", False
    if torch.cuda.is_available():
        return "cuda", True
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps", False
    return "cpu", False


def max_pool_by_token(
    token_ids: list[int], weights: list[float], skip_ids: set[int]
) -> dict[int, float]:
    """按 token id 做 max 池化，得到稀疏向量。

    同一个词在句子里出现多次时取最大权重——这是 bge-m3 的官方做法，
    与"求和"相比更能突出最强的那个语义信号。
    """
    aggregated: dict[int, float] = {}
    for token_id, weight in zip(token_ids, weights):
        if token_id in skip_ids or weight <= 0:
            continue
        if weight > aggregated.get(token_id, 0.0):
            aggregated[token_id] = float(weight)
    return aggregated


class BGEEmbedder:
    """bge-m3 的薄封装。模型对象延迟到 ``_ensure_loaded`` 才创建。"""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        batch_size: int = 16,
        max_length: int = 1024,
    ) -> None:
        self.model_path = Path(model_path)
        self.device, self.use_fp16 = _pick_device(device)
        self.batch_size = batch_size
        self.max_length = max_length
        self._tokenizer = None
        self._model = None
        self._sparse_head = None
        self._dim: int | None = None
        self._skip_ids: set[int] = set()

    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if not self.model_path.is_dir():
            raise FileNotFoundError(f"找不到 bge-m3 模型目录：{self.model_path}")

        # 模型一律用本地目录，绝不允许联网。
        # 不设这两个变量时 transformers 可能尝试访问 huggingface.co 补齐文件，
        # 国内网络下会挂住十几分钟以上，而且没有任何报错。
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        import torch
        from transformers import AutoModel, AutoTokenizer

        log.info("加载 bge-m3：{}（device={}, fp16={}）",
                 self.model_path, self.device, self.use_fp16)
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(self.model_path), local_files_only=True
        )
        dtype = torch.float16 if self.use_fp16 else torch.float32
        self._model = AutoModel.from_pretrained(
            str(self.model_path), local_files_only=True, dtype=dtype
        ).to(self.device).eval()

        head_path = self.model_path / SPARSE_HEAD_FILE
        if not head_path.is_file():
            raise FileNotFoundError(f"缺少稀疏头权重：{head_path}")
        state = torch.load(str(head_path), map_location="cpu")
        sparse_head = torch.nn.Linear(state["weight"].shape[1], 1)
        sparse_head.load_state_dict(
            {k: v.float() for k, v in state.items()}
        )
        self._sparse_head = sparse_head.to(self.device).eval()

        self._dim = int(self._model.config.hidden_size)
        self._skip_ids = set(self._tokenizer.all_special_ids)
        log.info("bge-m3 就绪，dense 维度 = {}，跳过特殊 token {}", self._dim, sorted(self._skip_ids))

    @property
    def dim(self) -> int:
        self._ensure_loaded()
        assert self._dim is not None
        return self._dim

    # ------------------------------------------------------------------
    def _encode_batch(self, texts: list[str]) -> list[EncodedVector]:
        import torch
        import torch.nn.functional as F

        self._ensure_loaded()
        assert self._tokenizer is not None and self._model is not None

        inputs = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            hidden = self._model(**inputs).last_hidden_state          # [B, L, H]
            dense = F.normalize(hidden[:, 0], p=2, dim=1)             # [CLS] + 归一化
            sparse_w = F.relu(self._sparse_head(hidden)).squeeze(-1)  # [B, L]

        dense_np = dense.float().cpu().tolist()
        sparse_np = sparse_w.float().cpu().tolist()
        ids_np = inputs["input_ids"].cpu().tolist()
        mask_np = inputs["attention_mask"].cpu().tolist()

        out: list[EncodedVector] = []
        for row in range(len(texts)):
            tokens = [t for t, m in zip(ids_np[row], mask_np[row]) if m]
            weights = sparse_np[row][: len(tokens)]
            out.append(
                EncodedVector(
                    dense=dense_np[row],
                    sparse=max_pool_by_token(tokens, weights, self._skip_ids),
                )
            )
        return out

    # ------------------------------------------------------------------
    def encode_documents(self, texts: list[str]) -> list[EncodedVector]:
        """批量编码。内部按 batch_size 分片，避免一次性占满显存。"""
        if not texts:
            return []
        self._ensure_loaded()
        out: list[EncodedVector] = []
        total = len(texts)
        for start in range(0, total, self.batch_size):
            batch = texts[start:start + self.batch_size]
            out.extend(self._encode_batch(batch))
            log.info("已编码 {}/{}", min(start + self.batch_size, total), total)
        return out

    def encode_query(self, text: str) -> EncodedVector:
        """单条查询编码。查询与文档必须用同一个模型，否则向量不在同一空间。"""
        return self.encode_documents([text])[0]

    # ------------------------------------------------------------------
    def release(self) -> None:
        """主动释放显存。

        入库阶段只加载 embedder，查询阶段再加载 reranker——
        8 GB 卡上两个模型同时常驻就会 OOM。
        """
        if self._model is None:
            return
        self._model = None
        self._tokenizer = None
        self._sparse_head = None
        self._dim = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        log.info("bge-m3 已释放")
