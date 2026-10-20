"""入库管线的中间数据结构。

``Block`` 是 **loader 的输出**，只描述"原文里有什么"，不做切分。
``Chunk`` 是 **chunker 的输出**，描述"将要入库的一条知识单元"。

分开的理由：解析和切分是两件独立的事，混在一起会导致改切分策略时
必须动解析代码，反之亦然。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class BlockKind(str, Enum):
    """来源块类型。检索阶段要区别对待，所以必须保留。"""

    PARAGRAPH = "paragraph"
    TABLE_ROW = "table_row"
    WARNING = "warning"
    HEADING = "heading"
    QA = "qa"
    RECORD = "record"


@dataclass(slots=True, frozen=True)
class Block:
    """原文里的一个结构块。"""

    text: str
    kind: BlockKind = BlockKind.PARAGRAPH
    page_no: int | None = None
    clause: str | None = None
    level: int = 0
    section_path: str = ""


@dataclass(slots=True)
class LoadResult:
    """一个文件的解析结果。"""

    blocks: list[Block] = field(default_factory=list)
    #: 文本层为空（扫描件），需要 OCR；M1a 不处理，只标记
    needs_ocr: bool = False

    def __len__(self) -> int:
        return len(self.blocks)


def sha256_text(text: str) -> str:
    """内容指纹。用于判断 chunk 是否变化，不用于标识身份。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Chunk:
    """一条待入库的知识单元。"""

    chunk_id: str
    doc_id: str
    version: int
    kind: str           # parent | child
    block_kind: str     # 见 BlockKind
    ordinal: int
    text: str
    parent_id: str | None = None
    page_no: int | None = None
    clause: str | None = None

    @property
    def content_hash(self) -> str:
        return sha256_text(self.text)

    def is_valid(self) -> bool:
        """空文本或不含任何字母数字的块视为无效（表格分隔符、空行等）。"""
        return bool(self.text.strip()) and any(ch.isalnum() for ch in self.text)
