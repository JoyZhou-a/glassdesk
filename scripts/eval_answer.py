"""答案级评测：走"检索 → 生成"这条路的题，看答案本身对不对。

只评检索类题目（after_sales / selection / negative），结构化类由
``eval_structured.py`` 单独评——两类指标必须分开报。

**不做 LLM 打分**（那要再花一倍的钱）。这里用三个零成本指标：

1. **状态正确率**：该答的答了、该拒的拒了
2. **引用覆盖率**：有答案的是否都带出处
3. **要点命中率**：ground truth 里的实词有多少出现在答案里（粗筛，非精确判分）

第 3 项是启发式，不能当"准确率"对外宣称——报告里会写明。

用法::

    .venv\\Scripts\\python.exe scripts/eval_answer.py
    .venv\\Scripts\\python.exe scripts/eval_answer.py --limit 5   # 小样本调试，省钱
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.answer import flow  # noqa: E402
from glassdesk.answer.generator import AnswerGenerator  # noqa: E402
from glassdesk.answer.llm import CachedLLM  # noqa: E402
from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.constants import AnswerState, OutputMode  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.retrieval.embedder import BGEEmbedder  # noqa: E402
from glassdesk.retrieval.rerank import Reranker  # noqa: E402
from glassdesk.retrieval.service import RetrievalService  # noqa: E402
from glassdesk.storage import MilvusStore, MySQLStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVAL_SET = ROOT / "data" / "eval" / "eval_set.jsonl"
OUTPUT_DIR = ROOT / "outputs" / "eval"

#: 只评走向量检索的类别
DOC_CATEGORIES = ("after_sales", "selection", "negative")

_STOPWORDS = {
    "this", "that", "with", "from", "have", "been", "they", "there", "their",
    "usually", "would", "should", "about", "which", "when", "what", "your",
}


def keywords(text: str, limit: int = 6) -> list[str]:
    """从 ground truth 里取实词，用于粗筛"答案有没有提到要点"。"""
    words = re.findall(r"[a-zA-Z]{5,}", text.lower())
    seen, out = set(), []
    for word in words:
        if word in _STOPWORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="答案级评测（检索路线）")
    parser.add_argument("--eval-set", default=str(EVAL_SET))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试省钱）")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=False)

    items = [json.loads(line) for line in Path(args.eval_set).open(encoding="utf-8") if line.strip()]
    items = [i for i in items if i["category"] in DOC_CATEGORIES]
    if args.limit:
        items = items[: args.limit]

    embedder = BGEEmbedder(settings.models.embedder, batch_size=1)
    reranker = Reranker(settings.models.reranker)
    llm = CachedLLM(settings.llm, settings.paths.outputs / "llm_cache",
                    use_cache=not args.no_cache)
    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    milvus = MilvusStore(settings.services.milvus)

    rows: list[dict] = []
    try:
        service = RetrievalService(settings, store, milvus, embedder, reranker)
        generator = AnswerGenerator(llm, settings.answer)

        for index, item in enumerate(items, start=1):
            # 走统一编排，和 ask.py 完全一致——否则评的不是真实链路
            result = flow.run(
                item["question"], store=store, retriever=service,
                generator_factory=lambda: generator, mode=OutputMode.PHONE,
            )
            answer_text = result.text if isinstance(result.text, str) else " ".join(result.text)
            expected_state_ok = (
                result.is_safe if item["expected_state"] != "ok" else result.is_answer
            )
            hit = keywords(item["ground_truth"])
            matched = [w for w in hit if w in answer_text.lower()]
            rows.append({
                "category": item["category"],
                "question": item["question"],
                "status": result.status,
                "state_ok": expected_state_ok,
                "has_citation": bool(result.citations),
                "keyword_rate": len(matched) / len(hit) if hit else None,
                "matched": matched,
                "answer": answer_text[:200],
                "cached": False,
                "notes": result.notes,
            })
            if index % 5 == 0:
                print(f"  已处理 {index}/{len(items)}")
    finally:
        milvus.close()
        store.close()

    report = _summarise(rows, llm.stats.as_dict())
    print(report["text"])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "answer_latest.json").write_text(
        json.dumps({"summary": report["summary"], "rows": rows, "llm": llm.stats.as_dict()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n已保存 → {OUTPUT_DIR}/answer_latest.json")
    return 0


def _summarise(rows: list[dict], llm_stats: dict) -> dict:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_cat[row["category"]].append(row)

    lines = [
        "=" * 72,
        "答案级评测（检索 → 生成，电话模式）",
        "=" * 72,
        f"{'类别':<12}{'条数':>6}{'状态正确':>9}{'引用覆盖':>9}{'要点命中':>9}",
        "-" * 72,
    ]
    summary = {}
    for cat, items in sorted(by_cat.items()):
        n = len(items)
        state_ok = sum(1 for i in items if i["state_ok"])
        # 引用覆盖率只在实际给出答案的题上统计：追问和拒答本来就不该有出处，
        # 把它们算进分母会把"正确地追问"误判成"缺引用"。
        answered = [i for i in items if i["status"] == "answer"]
        cited = sum(1 for i in answered if i["has_citation"])
        rates = [i["keyword_rate"] for i in items if i["keyword_rate"] is not None]
        kw = sum(rates) / len(rates) if rates else 0.0
        summary[cat] = {"n": n, "state_ok": state_ok, "cited": cited,
                         "answered": len(answered),
                         "cited_rate": (cited / len(answered)) if answered else 0.0,
                         "keyword_rate": kw}
        lines.append(
            f"{cat:<12}{n:>6}{state_ok / n * 100:>8.0f}%{(cited / len(answered) * 100) if answered else 0:>8.0f}%{kw * 100:>8.0f}%"
        )

    total = len(rows)
    ok = sum(1 for r in rows if r["state_ok"])
    all_answered = [r for r in rows if r["status"] == "answer"]
    cited = sum(1 for r in all_answered if r["has_citation"])
    rates = [r["keyword_rate"] for r in rows if r["keyword_rate"] is not None]
    kw = sum(rates) / len(rates) if rates else 0.0
    lines += [
        "-" * 72,
        f"{'合计':<12}{total:>6}{ok / total * 100:>8.0f}%{(cited / len(all_answered) * 100) if all_answered else 0:>8.0f}%{kw * 100:>8.0f}%",
        "",
        f"LLM：调用 {llm_stats['calls']} 次 / 缓存命中 {llm_stats['cache_hits']} 次 / "
        f"输入 {llm_stats['prompt_tokens']} tokens / 输出 {llm_stats['completion_tokens']} tokens",
        "",
        "说明：'要点命中' 是启发式粗筛（ground truth 里的实词有多少出现在答案中），",
        "      不等于答案正确率，不能对外当作准确率宣称。",
    ]

    failures = [r for r in rows if not r["state_ok"]]
    if failures:
        lines.append(f"\n状态不正确的 {len(failures)} 条（前 5）：")
        for f in failures[:5]:
            lines.append(f"  [{f['category']}] {f['question'][:64]}")
            lines.append(f"      状态={f['status']} 答案={f['answer'][:90]}")

    return {"text": "\n".join(lines), "summary": summary}


if __name__ == "__main__":
    raise SystemExit(main())
