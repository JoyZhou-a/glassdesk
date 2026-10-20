"""分源切分：Block 列表 → Chunk 列表（父块 + 子块）。

五种策略，对应不同文档结构（见 ``docs/06_语料生成规格.md`` §3）：

===========  ==========================================
strategy     适用
===========  ==========================================
manual       设备手册：按一级标题分节，节内打包成父块
section      标准摘要：一节一条，不切
entry        经验条目：一条一条，不切
record       工单 / 数据集：一条记录一块
table        能力矩阵 / 报价：一行一块，不切
===========  ==========================================

**表格行与警告永远不切。** 把它们切开就失去了"一行一个完整事实"的性质。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from glassdesk.ingest.models import Block, BlockKind, Chunk, sha256_text

_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """切分参数。``fingerprint()`` 会写进 documents.chunking_config_hash，
    参数一变就能看出"必须全量重建"。"""

    parent_chunk_size: int = 1200
    child_chunk_size: int = 300
    chunk_overlap: int = 50

    def fingerprint(self) -> str:
        return sha256_text(
            f"p{self.parent_chunk_size}|c{self.child_chunk_size}|o{self.chunk_overlap}"
        )


# ======================================================================
# 工具
# ======================================================================
def _join(blocks: list[Block]) -> str:
    return "\n".join(b.text for b in blocks if b.text.strip())


def _pack(blocks: list[Block], limit: int) -> list[list[Block]]:
    """贪心打包：每组总字符数不超过 limit。单块超长时自成一组（不切）。"""
    groups: list[list[Block]] = []
    current: list[Block] = []
    size = 0
    for block in blocks:
        length = len(block.text) + 1
        if current and size + length > limit:
            groups.append(current)
            current, size = [], 0
        current.append(block)
        size += length
    if current:
        groups.append(current)
    return groups


def _split_long(text: str, size: int, overlap: int) -> list[str]:
    """按句子边界把超长文本切成若干片，片间重叠 overlap 个字符。"""
    if len(text) <= size:
        return [text]

    sentences = [s for s in _SENTENCE_END.split(text) if s]
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) <= size:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
            tail = buffer[-overlap:] if overlap else ""
            buffer = f"{tail} {sentence}".strip()
        else:
            # 单句本身超长，硬切
            for i in range(0, len(sentence), size):
                pieces.append(sentence[i:i + size])
            buffer = ""
    if buffer:
        pieces.append(buffer)
    return pieces


def _make_section_id(doc_id: str, version: int, index: int) -> tuple[str, str]:
    parent = f"{doc_id}@{version}#parent{index}"
    return parent, parent


# ======================================================================
# 主入口
# ======================================================================
def chunk_blocks(
    blocks: list[Block],
    *,
    doc_id: str,
    version: int,
    strategy: str,
    cfg: ChunkConfig | None = None,
    default_clause: str | None = None,
) -> list[Chunk]:
    """按策略切分，返回父块在前、子块在后的顺序列表。"""
    config = cfg or ChunkConfig()
    handler = {
        "manual": _chunk_manual,
        "section": _chunk_by_heading,
        "entry": _chunk_by_heading,
        "record": _chunk_records,
        "table": _chunk_table,
    }.get(strategy)
    if handler is None:
        raise ValueError(f"未知切分策略：{strategy}")

    chunks = handler(blocks, doc_id, version, config, default_clause)
    return [c for c in chunks if c.is_valid()]


# ----------------------------------------------------------------------
# manual：按一级标题分节，节内先打包父块，再切子块
# ----------------------------------------------------------------------
def _chunk_manual(
    blocks: list[Block], doc_id: str, version: int, cfg: ChunkConfig,
    default_clause: str | None = None,
) -> list[Chunk]:
    sections = _split_sections(blocks)
    out: list[Chunk] = []
    parent_index = 0
    child_index = 0

    for heading, content in sections:
        if not heading and not content:
            continue
        # 只放标题 + 各内容块。**不要**再额外放一份"整节全文"，
        # 否则父块内容重复、子块数量翻倍。
        head = [Block(text=heading, kind=BlockKind.HEADING, level=1)] if heading else []
        group_blocks = [b for b in (*head, *content) if b.text.strip()]
        if not group_blocks:
            continue

        for group in _pack(group_blocks, cfg.parent_chunk_size):
            parent_id = f"{doc_id}@{version}#parent{parent_index}"
            out.append(
                Chunk(
                    chunk_id=parent_id,
                    doc_id=doc_id,
                    version=version,
                    kind="parent",
                    block_kind=BlockKind.PARAGRAPH.value,
                    ordinal=parent_index,
                    text=_join(group),
                    page_no=_first_page(group),
                    clause=_clip(heading) or None,
                )
            )
            parent_index += 1

            for child_block in _to_children(group, cfg):
                out.append(
                    Chunk(
                        chunk_id=f"{doc_id}@{version}#child{child_index}",
                        doc_id=doc_id,
                        version=version,
                        kind="child",
                        block_kind=child_block["kind"],
                        ordinal=child_index,
                        text=child_block["text"],
                        parent_id=parent_id,
                        page_no=child_block["page_no"],
                        clause=child_block["clause"] or _clip(heading) or None,
                    )
                )
                child_index += 1

    return out


def _split_sections(
    blocks: list[Block], boundary_level: int = 1
) -> list[tuple[str, list[Block]]]:
    """按标题分节。

    ``boundary_level`` 是"开新节"的标题层级：
    - manual 用 1（一级标题 = 一节）
    - section / entry 用 2（``#`` 是文档标题，``##`` 才是一节）

    比边界更浅的标题累积成标题前缀，更深的标题当正文保留其文字——
    条款式手册的 ``4.2.2.1`` 就是这种情况。
    """
    sections: list[tuple[str, list[Block]]] = []
    heading = ""
    content: list[Block] = []
    titles: list[str] = []

    def flush() -> None:
        nonlocal heading, content
        if heading or content:
            sections.append((heading, list(content)))
        heading, content = "", []

    for block in blocks:
        if block.kind is BlockKind.HEADING:
            if block.level < boundary_level:
                titles.append(block.text)
                continue
            if block.level == boundary_level:
                flush()
                heading = block.text
                continue
            content.append(block)  # 深层标题作为正文
            continue
        content.append(block)
    flush()

    title = " / ".join(titles)
    if title and sections:
        first_heading, first_content = sections[0]
        sections[0] = (f"{title} / {first_heading}" if first_heading else title, first_content)
    return sections


def _to_children(group: list[Block], cfg: ChunkConfig) -> list[dict]:
    """把一个父块内的块变成子块：表格行/警告独立成块，段落合并后按需再切。"""
    children: list[dict] = []
    buffer: list[Block] = []

    def flush() -> None:
        if not buffer:
            return
        text = _join(buffer)
        for piece in _split_long(text, cfg.child_chunk_size, cfg.chunk_overlap):
            children.append({
                "text": piece,
                "kind": buffer[0].kind.value,
                "page_no": _first_page(buffer),
                "clause": _first_clause(buffer),
            })
        buffer.clear()

    for block in group:
        if block.kind in (BlockKind.TABLE_ROW, BlockKind.WARNING):
            flush()
            children.append({
                "text": block.text,
                "kind": block.kind.value,
                "page_no": block.page_no,
                "clause": block.clause,
            })
        else:
            buffer.append(block)
    flush()
    return children


# ----------------------------------------------------------------------
# section / entry：一节一条，不切
# ----------------------------------------------------------------------
def _chunk_by_heading(
    blocks: list[Block], doc_id: str, version: int, cfg: ChunkConfig,
    default_clause: str | None = None,
) -> list[Chunk]:
    sections = [(h, c) for h, c in _split_sections(blocks, boundary_level=2) if h or c]
    out: list[Chunk] = []
    for index, (heading, content) in enumerate(sections):
        text = f"{heading}\n{_join(content)}".strip() if heading else _join(content)
        if not text:
            continue
        # 经验条目和标准条款都要求"整条"，但超长时仍按句子切开，避免超出上下文预算
        pieces = _split_long(text, cfg.parent_chunk_size, cfg.chunk_overlap)
        for piece_index, piece in enumerate(pieces):
            parent_id = f"{doc_id}@{version}#parent{index}"
            child_id = f"{doc_id}@{version}#child{index}" + (
                f"_{piece_index}" if len(pieces) > 1 else ""
            )
            out.append(
                Chunk(
                    chunk_id=parent_id if piece_index == 0 else parent_id + f"_{piece_index}",
                    doc_id=doc_id, version=version, kind="parent",
                    block_kind=BlockKind.PARAGRAPH.value, ordinal=index, text=piece,
                    clause=_clip(heading) or None,
                )
            )
            out.append(
                Chunk(
                    chunk_id=child_id, doc_id=doc_id, version=version, kind="child",
                    block_kind=BlockKind.PARAGRAPH.value, ordinal=index, text=piece,
                    parent_id=parent_id if piece_index == 0 else parent_id + f"_{piece_index}",
                    clause=_clip(heading) or None,
                )
            )
    return out


# ----------------------------------------------------------------------
# record：一条记录一块，无父块
# ----------------------------------------------------------------------
def _chunk_records(
    blocks: list[Block], doc_id: str, version: int, cfg: ChunkConfig,
    default_clause: str | None = None,
) -> list[Chunk]:
    out: list[Chunk] = []
    for index, block in enumerate(b for b in blocks if b.text.strip()):
        out.append(
            Chunk(
                chunk_id=f"{doc_id}@{version}#child{index}",
                doc_id=doc_id, version=version, kind="child",
                block_kind=block.kind.value, ordinal=index, text=block.text,
                clause=default_clause,
            )
        )
    return out


# ----------------------------------------------------------------------
# table：一行一块，按父块分组只为保留表标题
# ----------------------------------------------------------------------
def _chunk_table(
    blocks: list[Block], doc_id: str, version: int, cfg: ChunkConfig,
    default_clause: str | None = None,
) -> list[Chunk]:
    rows = [b for b in blocks if b.kind is BlockKind.TABLE_ROW and b.text.strip()]
    out: list[Chunk] = []
    child_index = 0
    for parent_index, group in enumerate(_pack(rows, cfg.parent_chunk_size)):
        parent_id = f"{doc_id}@{version}#parent{parent_index}"
        out.append(
            Chunk(
                chunk_id=parent_id, doc_id=doc_id, version=version, kind="parent",
                block_kind=BlockKind.TABLE_ROW.value, ordinal=parent_index,
                text=_join(group), clause=group[0].clause or default_clause,
            )
        )
        for block in group:
            out.append(
                Chunk(
                    chunk_id=f"{doc_id}@{version}#child{child_index}",
                    doc_id=doc_id, version=version, kind="child",
                    block_kind=BlockKind.TABLE_ROW.value, ordinal=child_index,
                    text=block.text, parent_id=parent_id,
                    page_no=block.page_no, clause=block.clause or default_clause,
                )
            )
            child_index += 1
    return out


# ----------------------------------------------------------------------
def _first_page(blocks: list[Block]) -> int | None:
    for block in blocks:
        if block.page_no is not None:
            return block.page_no
    return None


def _first_clause(blocks: list[Block]) -> str | None:
    for block in blocks:
        if block.clause:
            return block.clause
    return None


def _clip(text: str | None, limit: int = 64) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
