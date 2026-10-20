"""切分规则的测试。

全部用合成 Block，不依赖真实语料。
"""

from __future__ import annotations

import pytest

from glassdesk.ingest.chunker import ChunkConfig, chunk_blocks
from glassdesk.ingest.models import Block, BlockKind


def heading(text: str, level: int = 1) -> Block:
    return Block(text=text, kind=BlockKind.HEADING, level=level)


def para(text: str, page: int | None = None) -> Block:
    return Block(text=text, kind=BlockKind.PARAGRAPH, page_no=page)


def row(text: str, clause: str | None = None) -> Block:
    return Block(text=text, kind=BlockKind.TABLE_ROW, clause=clause)


def warning(text: str) -> Block:
    return Block(text=text, kind=BlockKind.WARNING)


def record(text: str) -> Block:
    return Block(text=text, kind=BlockKind.RECORD)


# ======================================================================
# 身份与命名
# ======================================================================
def test_chunk_ids_follow_naming_rule():
    blocks = [heading("1 Safety"), para("Only trained operators.")]
    chunks = chunk_blocks(blocks, doc_id="m", version=3, strategy="manual")
    ids = [c.chunk_id for c in chunks]
    assert f"m@3#parent0" in ids
    assert any(i.startswith("m@3#child") for i in ids)


def test_version_is_part_of_identity():
    """不同版本必须产生不同 id，否则新旧版本会互相覆盖。"""
    blocks = [heading("1 Safety"), para("text")]
    v1 = {c.chunk_id for c in chunk_blocks(blocks, doc_id="m", version=1, strategy="manual")}
    v2 = {c.chunk_id for c in chunk_blocks(blocks, doc_id="m", version=2, strategy="manual")}
    assert v1.isdisjoint(v2)


# ======================================================================
# manual
# ======================================================================
def test_manual_splits_by_top_level_heading():
    blocks = [
        heading("Doc Title", level=0),
        heading("1 Safety"),
        para("Only trained operators may run a cycle."),
        heading("2 Overview"),
        para("The furnace accepts flat glass."),
    ]
    chunks = chunk_blocks(blocks, doc_id="m", version=1, strategy="manual")
    parents = [c for c in chunks if c.kind == "parent"]
    assert len(parents) == 2
    # 文档标题并入第一节目录，便于出处显示
    assert "Doc Title" in parents[0].text
    assert "1 Safety" in parents[0].clause or "Safety" in parents[0].clause


def test_manual_keeps_table_row_as_standalone_child():
    """表格行不能被合并进段落——一行就是一个完整事实。"""
    blocks = [
        heading("4 Parameters"),
        para("Heating time is set per millimetre."),
        row("Thickness (mm)=6 | Quench=18", clause="Table 4-1"),
        para("Do not shorten heating time."),
    ]
    chunks = chunk_blocks(blocks, doc_id="m", version=1, strategy="manual")
    table_children = [c for c in chunks if c.kind == "child" and c.block_kind == "table_row"]
    assert len(table_children) == 1
    assert table_children[0].text.startswith("Thickness (mm)=6")
    assert table_children[0].clause == "Table 4-1"


def test_manual_keeps_warning_as_standalone_child():
    blocks = [
        heading("1 Safety"),
        para("Read this section first."),
        warning("DANGER: Never open the door while pressurised."),
    ]
    chunks = chunk_blocks(blocks, doc_id="m", version=1, strategy="manual")
    warns = [c for c in chunks if c.kind == "child" and c.block_kind == "warning"]
    assert len(warns) == 1
    assert warns[0].text.startswith("DANGER:")


def test_every_child_points_to_an_existing_parent():
    blocks = [heading("1 Safety"), para("a"), row("x=1"), warning("W: b")]
    chunks = chunk_blocks(blocks, doc_id="m", version=1, strategy="manual")
    parents = {c.chunk_id for c in chunks if c.kind == "parent"}
    for child in (c for c in chunks if c.kind == "child"):
        assert child.parent_id in parents


def test_long_paragraph_is_split_but_keeps_overlap():
    sentence = "This is a fairly long sentence about glass processing."
    long_text = " ".join([sentence] * 20)
    chunks = chunk_blocks([heading("1 X"), para(long_text)],
                          doc_id="m", version=1, strategy="manual",
                          cfg=ChunkConfig(child_chunk_size=300, chunk_overlap=50))
    children = [c for c in chunks if c.kind == "child"]
    assert len(children) > 1
    assert all(len(c.text) <= 400 for c in children)


def test_parent_contains_child_text():
    blocks = [heading("1 X"), para("Alpha beta gamma."), row("k=v")]
    chunks = chunk_blocks(blocks, doc_id="m", version=1, strategy="manual")
    parent = next(c for c in chunks if c.kind == "parent")
    for child in (c for c in chunks if c.kind == "child"):
        assert child.text in parent.text


# ======================================================================
# table
# ======================================================================
def test_table_strategy_one_child_per_row():
    rows = [row(f"Thickness (mm)={t} | Max width=2440") for t in (4, 6, 8)]
    chunks = chunk_blocks(rows, doc_id="cap", version=1, strategy="table")
    children = [c for c in chunks if c.kind == "child"]
    assert len(children) == 3
    assert children[0].block_kind == "table_row"
    assert all(c.parent_id for c in children)


def test_table_row_never_split_even_if_huge():
    huge = "Thickness (mm)=19 | Notes=" + "x" * 2000
    chunks = chunk_blocks([row(huge)], doc_id="cap", version=1, strategy="table")
    children = [c for c in chunks if c.kind == "child"]
    assert len(children) == 1
    assert len(children[0].text) > 2000


# ======================================================================
# section / entry
# ======================================================================
def test_section_strategy_one_chunk_per_heading():
    blocks = [
        heading("AS 1288 - Glass in buildings", level=1),
        heading("Safety glazing in doors", level=2),
        para("Doors must be safety glazed."),
        heading("Overhead glazing", level=2),
        para("Overhead must be laminated."),
    ]
    chunks = chunk_blocks(blocks, doc_id="std", version=1, strategy="section")
    children = [c for c in chunks if c.kind == "child"]
    assert len(children) == 2
    assert "safety glazed" in children[0].text
    assert "laminated" in children[1].text


def test_entry_strategy_keeps_entry_whole():
    blocks = [
        heading("EXP-001 - cracking", level=2),
        para("Usually not a manufacturing defect. Ask for photos."),
        heading("EXP-002 - area limit", level=2),
        para("In our experience this is the area limit."),
    ]
    chunks = chunk_blocks(blocks, doc_id="exp", version=1, strategy="entry")
    children = [c for c in chunks if c.kind == "child"]
    assert len(children) == 2
    assert all(c.text.startswith("EXP-") for c in children)


# ======================================================================
# record
# ======================================================================
def test_record_strategy_no_parent():
    chunks = chunk_blocks([record("ticket_id=T-1 | intent=spec"),
                           record("ticket_id=T-2 | intent=logistics")],
                          doc_id="tk", version=1, strategy="record")
    assert all(c.kind == "child" for c in chunks)
    assert len(chunks) == 2
    assert all(c.parent_id is None for c in chunks)


# ======================================================================
# 过滤与异常
# ======================================================================
def test_invalid_blocks_are_dropped():
    blocks = [heading("1 X"), para("   |   |   "), para("real content")]
    chunks = chunk_blocks(blocks, doc_id="m", version=1, strategy="manual")
    assert all(c.is_valid() for c in chunks)
    assert any("real content" in c.text for c in chunks)


def test_unknown_strategy_raises():
    with pytest.raises(ValueError, match="未知切分策略"):
        chunk_blocks([para("x")], doc_id="m", version=1, strategy="nope")


# ======================================================================
# 配置指纹
# ======================================================================
def test_fingerprint_changes_with_params():
    a = ChunkConfig(parent_chunk_size=1200).fingerprint()
    b = ChunkConfig(parent_chunk_size=1000).fingerprint()
    assert a != b
    assert ChunkConfig().fingerprint() == ChunkConfig().fingerprint()
