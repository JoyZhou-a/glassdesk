"""生成 120 条检索评测集，**全程不调用大模型**。

为什么不用大模型出题：
1. 我们只需要"检索对不对"，题面用模板拼就足够；
2. 题目必须绑定到具体 chunk_id 作为 ground truth，模板比模型更可控；
3. 调一次 API 就是钱，而这件事一分钱不用花。

产出 ``data/eval/eval_set.jsonl``，**入库提交**——评测集必须固定，
否则每次跑出来的数不可比。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.storage import MySQLStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "data" / "seeds"
OUT = ROOT / "data" / "eval" / "eval_set.jsonl"

COATING_WORD = {
    "none": "",
    "low_e_soft": "with low-e soft coat",
    "low_e_hard": "with low-e hard coat",
    "tinted": "tinted",
}

PROCESS_WORD = {
    "toughened": "toughened",
    "annealed": "annealed",
    "laminated": "laminated",
    "igu": "double glazed",
}


@dataclass(slots=True)
class Item:
    item_id: str
    category: str          # spec | config | selection | after_sales | logistics | negative
    channel: str           # phone | email
    question: str
    ground_truth: str
    evidence_chunk_ids: list[str] = field(default_factory=list)
    expected_state: str = "ok"      # ok | need_slot | escalate
    note: str = ""


# ======================================================================
# 解析 chunk 文本成字段字典
# ======================================================================
def parse_pairs(text: str) -> dict[str, str]:
    """把 ``A=1 | B=2`` 解析成字典。用于按字段内容绑定 chunk。"""
    out: dict[str, str] = {}
    for part in text.split(" | "):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成评测集（不调大模型）")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=False)

    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    try:
        rows = store.fetchall(
            """
            SELECT chunk_id, doc_id, text, clause
            FROM chunks
            WHERE kind='child' AND status='active'
            ORDER BY doc_id, ordinal
            """
        )
    finally:
        store.close()

    if not rows:
        print("没有可用的 active 子块，先跑 ingest pipeline")
        return 1

    by_doc: dict[str, list[dict]] = {}
    for row in rows:
        by_doc.setdefault(row["doc_id"], []).append(row)

    items: list[Item] = []
    items += _spec_questions(by_doc.get("capability-matrix", []))
    items += _config_questions(by_doc.get("price-list", []))
    items += _logistics_questions(by_doc.get("lead-time", []))
    items += _experience_questions(by_doc.get("experience-fieldbook", []))
    items += _standard_questions(by_doc.get("std-summary", []))
    items += _negative_questions()

    # 重新编号，保证 id 连续
    for index, item in enumerate(items, start=1):
        item.item_id = f"Q{index:03d}"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        for item in items:
            fh.write(json.dumps({
                "id": item.item_id,
                "category": item.category,
                "channel": item.channel,
                "question": item.question,
                "ground_truth": item.ground_truth,
                "evidence_chunk_ids": item.evidence_chunk_ids,
                "expected_state": item.expected_state,
                "note": item.note,
            }, ensure_ascii=False) + "\n")

    _report(items, out_path)
    return 0


# ======================================================================
# 各类题目
# ======================================================================
def _spec_questions(chunks: list[dict]) -> list[Item]:
    """尺寸与能力：每题直接绑到能力矩阵的一行。"""
    items: list[Item] = []
    for chunk in chunks:
        f = parse_pairs(chunk["text"])
        if "Thickness (mm)" not in f or "Process" not in f:
            continue
        process = f["Process"]
        shape = f.get("Shape", "flat")
        thickness = f["Thickness (mm)"]
        max_w = float(f["Max width (mm)"])
        max_h = float(f["Max height (mm)"])
        # 取上限的 80% / 60%，保证在允许域内，也避免贴边
        width = int(round(max_w * 0.8 / 10) * 10)
        height = int(round(max_h * 0.6 / 10) * 10)
        word = PROCESS_WORD.get(process, process)
        shape_word = "bent " if shape == "bent" else ""
        items.append(
            Item(
                item_id="",
                category="spec",
                channel="phone",
                question=(
                    f"Can you make {thickness}mm {word} {shape_word}glass "
                    f"{width} wide by {height} high?"
                ),
                ground_truth=(
                    f"Yes - within the maximum size for {process} {shape} "
                    f"{thickness}mm glass."
                ),
                evidence_chunk_ids=[chunk["chunk_id"]],
                note=f"capability row: {process}/{shape}/{thickness}",
            )
        )
    return items


def _config_questions(chunks: list[dict]) -> list[Item]:
    """配置与价格：每题绑到报价表一行，答案是单价。

    **题面必须包含 shape / variant / coating**：同一厚度下平板与异形、无膜与有膜
    是不同的报价行（8mm 平板 145、8mm 异形 310）。题面不写清楚就会生成多个文字
    完全相同、答案却不同的题目，系统无法区分，评出来的分是假的。
    """
    items: list[Item] = []
    for chunk in chunks:
        f = parse_pairs(chunk["text"])
        if "Unit price (AUD/m2, incl GST)" not in f:
            continue
        process = f.get("Process", "")
        shape = f.get("Shape", "flat")
        thickness = f.get("Thickness (mm)", "")
        variant = f.get("Variant")
        coating = f.get("Coating", "none")
        price = f["Unit price (AUD/m2, incl GST)"]

        label = f"{thickness}mm "
        if shape == "bent":
            label += "bent "
        label += PROCESS_WORD.get(process, process)
        if variant:
            label += f" ({variant})"
        coat_word = COATING_WORD.get(coating, "")
        if coat_word:
            label += f" {coat_word}"
        label += " glass"

        items.append(
            Item(
                item_id="",
                category="config",
                channel="email",
                question=f"How much per square metre for {label}, including GST?",
                ground_truth=f"AUD {price} per m2 including GST.",
                evidence_chunk_ids=[chunk["chunk_id"]],
                note=f"price row: {process}/{shape}/{thickness}/{variant or '-'}/{coating}",
            )
        )
    return items


def _logistics_questions(chunks: list[dict]) -> list[Item]:
    """交期：每题绑到交期表一行，答案是工作日数。"""
    items: list[Item] = []
    for chunk in chunks:
        f = parse_pairs(chunk["text"])
        if "Business days" not in f:
            continue
        process = f.get("Process", "")
        thickness_max = f.get("Thickness up to (mm)", "")
        qty_max = int(float(f.get("Quantity up to", "10")))
        days = f["Business days"]
        qty = 6 if qty_max <= 10 else 20
        word = PROCESS_WORD.get(process, process)
        items.append(
            Item(
                item_id="",
                category="logistics",
                channel="phone",
                question=(
                    f"How long for {qty} panels of {word} glass "
                    f"up to {thickness_max}mm thick?"
                ),
                ground_truth=f"{days} business days.",
                evidence_chunk_ids=[chunk["chunk_id"]],
                note="lead time row",
            )
        )
    return items


def _experience_questions(chunks: list[dict]) -> list[Item]:
    """售后/经验：直接用 seeds 里写好的客户问法，绑到对应条目。"""
    data = yaml.safe_load((SEEDS / "experience.yaml").read_text(encoding="utf-8"))
    entries = data["entries"]
    items: list[Item] = []

    # chunk 的 clause 就是 "## EXP-001 - topic" 里的标题
    by_entry: dict[str, str] = {}
    for chunk in chunks:
        clause = chunk["clause"] or ""
        entry_id = clause.split(" - ")[0].strip()
        if entry_id:
            by_entry[entry_id] = chunk["chunk_id"]

    for entry in entries:
        chunk_id = by_entry.get(entry["entry_id"])
        if not chunk_id:
            continue
        tags = entry.get("tags") or ["after_sales"]
        category = tags[0] if tags[0] in ("after_sales", "selection") else "after_sales"
        items.append(
            Item(
                item_id="",
                category=category,
                channel="phone",
                question=entry["question"],
                ground_truth=" ".join(entry["answer_en"].split())[:200],
                evidence_chunk_ids=[chunk_id],
                # 题面里提到了尺寸但没给全（比如"3000 x 3000 的板能做吗"没说厚度），
                # 正确答案就是**追问**，不是硬答。标签必须反映这一点，
                # 否则会把"正确追问"记成"答错"，让报告数字无故偏低。
                expected_state=_expected_state(entry["question"]),
                note=f"experience {entry['entry_id']}",
            )
        )
    return items


def _expected_state(question: str) -> str:
    """判断这类题应当"作答"还是"追问"。

    只用在经验/标准这两类**走向量检索**的题上：
    - 题面没有尺寸参数 → 应该能直接从文档答 → ``ok``
    - 题面提到尺寸但不全 → 正确行为是追问 → ``need_slot``
    """
    from glassdesk.routing import slots as S

    extracted = S.extract(question)
    if extracted.is_empty:
        return "ok"
    validation = S.validate(extracted, need=("thickness_mm", "width_mm", "height_mm"))
    return "ok" if validation.can_query else "need_slot"


#: 标准摘要的客户问法（人工撰写，因为要像客户在问，不能照抄标题）
STANDARD_QUESTIONS: dict[str, str] = {
    "Safety glazing in doors and side panels":
        "Does the glass next to my front door have to be safety glass?",
    "Glazing at low levels":
        "Do I need safety glass if the window goes down near the floor?",
    "Overhead glazing":
        "Can I use toughened glass for a skylight above a walkway?",
    "Marking of safety glazing":
        "How do I know the glass you supply is actually certified safety glass?",
    "Classification of safety glazing":
        "What do the different safety glass grades mean for where I can use it?",
    "Seal durability and unit integrity":
        "How long is a sealed unit supposed to keep its seal?",
    "Condensation inside the cavity":
        "There is moisture inside my double glazing - is that covered?",
    "Glazing used as a pool barrier":
        "What glass can I use for a pool fence?",
    "Glazing in bushfire prone areas":
        "My block is in a bushfire area - does the glass need to be different?",
}


def _standard_questions(chunks: list[dict]) -> list[Item]:
    items: list[Item] = []
    for chunk in chunks:
        clause = chunk["clause"] or ""
        for topic, question in STANDARD_QUESTIONS.items():
            if topic in clause:
                items.append(
                    Item(
                        item_id="",
                        category="selection",
                        channel="phone",
                        question=question,
                        ground_truth=f"Answered by {topic}.",
                        evidence_chunk_ids=[chunk["chunk_id"]],
                        note=f"standard: {topic}",
                    )
                )
                break
    return items


def _negative_questions() -> list[Item]:
    """负面样例：系统**必须**拒绝回答或追问，而不是硬编一个答案。"""
    return [
        Item("", "negative", "phone",
             "Can you make a single panel 3000 wide by 6000 high?",
             "Cannot be made as one piece; offer a split or a smaller size.",
             [], "escalate", "oversize"),
        Item("", "negative", "email",
             "I need one panel 4000 x 4000 for a shopfront.",
             "Oversize for any single panel.",
             [], "escalate", "oversize"),
        Item("", "negative", "phone",
             "Can you do a 19mm panel 3000 by 5000?",
             "Exceeds the maximum area for 19mm.",
             [], "escalate", "oversize"),
        Item("", "negative", "phone",
             "Can you make a big one?",
             "Missing size and thickness - must ask, not guess.",
             [], "need_slot", "missing slot"),
        Item("", "negative", "phone",
             "How much for a pane?",
             "Missing size, thickness and process - must ask.",
             [], "need_slot", "missing slot"),
        Item("", "negative", "email",
             "Can you do it thicker?",
             "Thickness missing and unclear - must ask.",
             [], "need_slot", "missing slot"),
        Item("", "negative", "phone",
             "Do you supply glass for submarines?",
             "No such knowledge in the corpus.",
             [], "escalate", "no evidence"),
        Item("", "negative", "email",
             "What is the melting point of soda lime glass?",
             "Not covered by the corpus (only processing capability).",
             [], "escalate", "no evidence"),
        Item("", "negative", "phone",
             "What are your opening hours on Christmas Day?",
             "Out of scope.",
             [], "escalate", "out of scope"),
        Item("", "negative", "phone",
             "Can you install it at my house as well?",
             "Out of scope - supply only.",
             [], "escalate", "out of scope"),
        Item("", "negative", "email",
             "Is your toughened glass approved for aircraft use?",
             "No aviation certification in the corpus.",
             [], "escalate", "no evidence"),
    ]


# ======================================================================
def _report(items: list[Item], path: Path) -> None:
    from collections import Counter

    cats = Counter(i.category for i in items)
    states = Counter(i.expected_state for i in items)
    unbound = [i for i in items if i.expected_state == "ok" and not i.evidence_chunk_ids]

    print("=" * 64)
    print(f"已写出 {len(items)} 条 → {path}")
    print("=" * 64)
    print("分类分布：", dict(cats))
    print("期望状态：", dict(states))
    if unbound:
        print(f"[警告] {len(unbound)} 条正向题没有绑定 evidence：")
        for item in unbound[:5]:
            print(f"   {item.question[:60]}  ({item.note})")
    else:
        print("所有正向题均已绑定 evidence chunk")


if __name__ == "__main__":
    raise SystemExit(main())
