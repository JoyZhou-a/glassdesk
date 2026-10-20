"""结构化通道评测：走"抽参数 → 查表 → 填模板"，**零 LLM 调用**。

评的是"答案里的关键数字对不对"，不是 Recall@5——因为这条路根本不走向量检索。
两类指标必须分开报，混在一起算总分是不诚实的。

用法::

    .venv\\Scripts\\python.exe scripts/eval_structured.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.answer import templates  # noqa: E402
from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.routing import slots as S  # noqa: E402
from glassdesk.routing.structured import StructuredLookup  # noqa: E402
from glassdesk.storage import MySQLStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVAL_SET = ROOT / "data" / "eval" / "eval_set.jsonl"
OUTPUT_DIR = ROOT / "outputs" / "eval"

#: 每类问题"必须有哪些槽位"才能查表
REQUIRED = {
    "spec": ("thickness_mm", "width_mm", "height_mm"),
    "config": ("thickness_mm",),
    "logistics": ("thickness_mm", "process"),
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    """把文本里的数字统一成"去掉多余 0"的形式，便于比较。

    ``42`` / ``42.0`` / ``42.00`` 视为同一个数。

    先剔除单位里的数字：``per m2``、``m²``、``per m3`` 里的 2/3 不是数据，
    否则期望集合会多出一个永远匹配不上的数字。
    """
    cleaned = re.sub(r"m\s*[²³23]\b", " ", text, flags=re.IGNORECASE)
    out = set()
    for raw in _NUMBER.findall(cleaned):
        out.add(f"{float(raw):g}")
    return out


def answer_for(item: dict, lookup: StructuredLookup, mode: str = "phone"):
    """返回 (渲染后的答案, 状态)。

    路由**不看类别**，只看槽位——这正是 PRD 里 ``need_structured`` 的定义：
    抽到可计算参数就走结构化，与"这是什么意图"无关。

    类别只用来决定"该查哪张表"和"需要哪些槽位"。
    """
    question = item["question"]
    category = item["category"]
    slots = S.extract(question)

    need = REQUIRED.get(category)
    if need is None:
        # 负面样例等未分类问题：有宽高就按尺寸校验，否则只要求厚度
        need = (("thickness_mm", "width_mm", "height_mm")
                if slots.width_mm and slots.height_mm else ("thickness_mm",))

    validation = S.validate(slots, need=need)
    if validation.status == "no_slots":
        return None, "no_slots"
    if validation.status == "missing":
        return templates.ask_back(validation.missing, mode), "ask"
    if validation.status == "bad_value":
        return templates.refuse_and_handoff(validation.reason, mode), "refuse"

    if category == "config":
        quote = lookup.price(slots)
        if quote is None:
            return templates.refuse_and_handoff("No price on file for that spec.", mode), "refuse"
        render = templates.price_phone if mode == "phone" else templates.price_email
        return render(quote, slots), "answer"

    if category == "logistics":
        quote = lookup.leadtime(slots)
        if quote is None:
            return templates.refuse_and_handoff("No lead time on file for that spec.", mode), "refuse"
        render = templates.leadtime_phone if mode == "phone" else templates.leadtime_email
        return render(quote, slots), "answer"

    # spec 与其它带尺寸的问题：查能力矩阵
    verdict = lookup.check_capability(slots)
    if not verdict.can_make and not verdict.limits:
        return templates.refuse_and_handoff(verdict.reason, mode), "refuse"
    render = templates.capability_phone if mode == "phone" else templates.capability_email
    return render(slots, verdict), ("answer" if verdict.can_make else "refuse")


def main() -> int:
    parser = argparse.ArgumentParser(description="结构化通道评测（不调大模型）")
    parser.add_argument("--eval-set", default=str(EVAL_SET))
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=False)

    items = [json.loads(line) for line in Path(args.eval_set).open(encoding="utf-8") if line.strip()]
    positives = [i for i in items if i["category"] in REQUIRED]
    negatives = [i for i in items if i["category"] == "negative"]

    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    try:
        lookup = StructuredLookup(store)
        stats: dict[str, list[dict]] = defaultdict(list)
        misses: list[dict] = []

        for item in positives:
            answer, status = answer_for(item, lookup)
            text = answer if isinstance(answer, str) else " ".join(answer or [])
            expected = numbers_in(item["ground_truth"])
            got = numbers_in(text)
            ok = status == "answer" and expected.issubset(got) if status != "refuse" else False

            # spec 类还要确认"能做/不能做"的结论正确（正向题应该都是能做）
            if item["category"] == "spec" and status == "answer":
                ok = ok and text.lower().startswith("yes")

            stats[item["category"]].append({"ok": ok, "status": status})
            if not ok:
                misses.append({
                    "category": item["category"],
                    "question": item["question"],
                    "status": status,
                    "expected_numbers": sorted(expected),
                    "got_numbers": sorted(got),
                    "answer": text[:160],
                })

        neg_stats = []
        for item in negatives:
            answer, status = answer_for(item, lookup)
            text = answer if isinstance(answer, str) else " ".join(answer or [])
            # 负面样例：必须是"追问"或"拒答"，不能给出一个像模像样的数字答案
            safe = status in ("ask", "refuse", "no_slots")
            neg_stats.append({"safe": safe, "status": status, "question": item["question"]})
    finally:
        store.close()

    # ------------------------------------------------------------------
    print("=" * 68)
    print("结构化通道评测（零 LLM 调用）")
    print("=" * 68)
    print(f"{'类别':<12}{'条数':>6}{'正确':>6}{'正确率':>8}")
    print("-" * 68)
    total = correct = 0
    for cat, rows in sorted(stats.items()):
        n = len(rows)
        c = sum(1 for r in rows if r["ok"])
        total += n
        correct += c
        print(f"{cat:<12}{n:>6}{c:>6}{c / n * 100:>7.1f}%")
    print("-" * 68)
    print(f"{'合计':<12}{total:>6}{correct:>6}{correct / total * 100 if total else 0:>7.1f}%")

    safe_neg = sum(1 for n in neg_stats if n["safe"])
    print(f"\n负面样例 {len(neg_stats)} 条：安全处理 {safe_neg} 条"
          f"（{'追问' if True else ''}或拒答，未编造数字）")
    for n in neg_stats:
        if not n["safe"]:
            print(f"  [危险] {n['question'][:60]} -> {n['status']}")

    if misses:
        print(f"\n未通过 {len(misses)} 条（前 5）：")
        for m in misses[:5]:
            print(f"\n  [{m['category']}] {m['question'][:72]}")
            print(f"     状态={m['status']} 期望数字={m['expected_numbers']} 实得={m['got_numbers']}")
            print(f"     答案: {m['answer']}")

    # 证明没有调用大模型：openai 模块根本没被导入
    llm_imported = "openai" in sys.modules
    print(f"\nLLM 模块是否被加载：{'是（异常！）' if llm_imported else '否 —— 全链路零 token'}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "positives": {k: len(v) for k, v in stats.items()},
        "correct": correct,
        "total": total,
        "negative_safe": [n for n in neg_stats if n["safe"]],
        "misses": misses,
        "llm_imported": llm_imported,
    }
    (OUTPUT_DIR / "structured_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if correct == total and safe_neg == len(neg_stats) and not llm_imported else 1


if __name__ == "__main__":
    raise SystemExit(main())
