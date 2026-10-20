"""跑检索评测：Recall@k / MRR / 延迟，**不调用大模型**。

两个用途：
1. M2 的验收（检索质量与生成质量无关，必须独立度量）；
2. 用数据回答"权威分区到底值多少"——同一套题跑两遍：不过滤 vs 按类别过滤。

用法::

    .venv\\Scripts\\python.exe scripts/eval_retrieval.py
    .venv\\Scripts\\python.exe scripts/eval_retrieval.py --no-rerank   # 关闭重排做对照
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.constants import Authority  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.retrieval.embedder import BGEEmbedder  # noqa: E402
from glassdesk.retrieval.rerank import Reranker  # noqa: E402
from glassdesk.retrieval.service import RetrievalService  # noqa: E402
from glassdesk.storage import MilvusStore, MySQLStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVAL_SET = ROOT / "data" / "eval" / "eval_set.jsonl"
OUTPUT_DIR = ROOT / "outputs" / "eval"

#: 按类别限定权威来源。负面样例不限定（它们本来就该检索不到）。
AUTHORITY_BY_CATEGORY: dict[str, tuple[Authority, ...] | None] = {
    "spec": (Authority.MANUAL,),
    "config": (Authority.MANUAL,),
    "logistics": (Authority.MANUAL,),
    "selection": (Authority.STANDARD, Authority.MANUAL, Authority.EXPERIENCE),
    "after_sales": (Authority.EXPERIENCE, Authority.MANUAL),
    "negative": None,
}


def load_items(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def evaluate(service: RetrievalService, items: list[dict], *, use_rerank: bool,
             grouped: bool, k: int = 5) -> dict:
    per_category: dict[str, list[dict]] = defaultdict(list)
    latencies: list[float] = []

    for item in items:
        authorities = AUTHORITY_BY_CATEGORY.get(item["category"]) if grouped else None
        result = service.retrieve(
            item["question"], authorities=authorities, final_k=k, use_rerank=use_rerank
        )
        latencies.append(result.total_ms)

        returned = [c.chunk_id for c in result.chunks]
        evidence = set(item.get("evidence_chunk_ids") or [])

        if item["expected_state"] != "ok":
            # 负面样例：只有"没有可信结果"才算对。用重排后的最高分近似判断。
            top_score = max((c.score for c in result.chunks), default=0.0)
            per_category[item["category"]].append({
                "rejected": top_score < service.cfg.min_rerank_score,
                "rank": None,
            })
            continue

        rank = next(
            (i + 1 for i, cid in enumerate(returned) if cid in evidence), None
        )
        record: dict = {"rejected": None, "rank": rank}
        if rank is None:
            record["miss"] = {
                "question": item["question"],
                "expected": list(evidence),
                "got": returned,
                "note": item.get("note", ""),
            }
        per_category[item["category"]].append(record)

    misses = [r["miss"] for rows in per_category.values() for r in rows if "miss" in r]
    return {
        "per_category": {cat: _summarise(rows, k) for cat, rows in per_category.items()},
        "overall": _summarise([r for rows in per_category.values() for r in rows], k),
        "misses": misses,
        "latency": {
            "p50": statistics.median(latencies),
            "p95": _percentile(latencies, 95),
            "max": max(latencies),
        },
        "runs": len(items),
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def _summarise(rows: list[dict], k: int) -> dict:
    """正向题算 Recall/MRR；负面题算拒答率。

    区分方式：``rejected`` 为 None 表示正向题（它只会记 rank），
    非 None 表示负面题（记录是否被正确拒答）。
    """
    pos = [r for r in rows if r["rejected"] is None]
    neg = [r for r in rows if r["rejected"] is not None]
    pos_hits = [r for r in pos if r["rank"] is not None]

    return {
        "count": len(rows),
        "positive": len(pos),
        f"recall@{k}": (len(pos_hits) / len(pos)) if pos else 0.0,
        "mrr": (sum(1.0 / r["rank"] for r in pos_hits) / len(pos)) if pos else 0.0,
        "negative": len(neg),
        "rejection_rate": (sum(1 for r in neg if r["rejected"]) / len(neg)) if neg else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检索评测（不调大模型）")
    parser.add_argument("--eval-set", default=str(EVAL_SET))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--no-rerank", action="store_true", help="关闭重排做对照")
    parser.add_argument("--skip-grouped", action="store_true", help="跳过权威分区对照")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=False)

    items = load_items(Path(args.eval_set))
    print(f"评测集：{len(items)} 条")

    embedder = BGEEmbedder(settings.models.embedder, batch_size=1)
    reranker = None if args.no_rerank else Reranker(settings.models.reranker, device="auto")
    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    milvus = MilvusStore(settings.services.milvus)

    try:
        service = RetrievalService(settings, store, milvus, embedder, reranker)
        runs: dict[str, dict] = {}
        partial = OUTPUT_DIR / "retrieval_partial.json"

        print("\n[1/2] 不过滤（baseline）…")
        runs["baseline"] = evaluate(
            service, items, use_rerank=not args.no_rerank, grouped=False, k=args.k
        )
        _save_partial(runs, partial)

        if not args.skip_grouped:
            print("[2/2] 按类别限定权威来源（grouped）…")
            runs["grouped"] = evaluate(
                service, items, use_rerank=not args.no_rerank, grouped=True, k=args.k
            )
            _save_partial(runs, partial)
    finally:
        milvus.close()
        store.close()

    report = _render(runs, args.k, args.no_rerank)
    print(report)

    # 未命中的题是最有信息量的部分：说明是"检索不到"还是"排序错了"
    for name, data in runs.items():
        misses = data.get("misses") or []
        if not misses:
            continue
        print(f"\n=== {name} 未命中 {len(misses)} 条（前 6 条）===")
        for miss in misses[:6]:
            print(f"\n  Q: {miss['question']}")
            print(f"     期望 {miss['expected'][:1]}  ({miss['note']})")
            print(f"     实得 {miss['got'][:3]}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (OUTPUT_DIR / f"retrieval_{stamp}.md").write_text(report, encoding="utf-8")
    (OUTPUT_DIR / f"retrieval_{stamp}.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已保存到 {OUTPUT_DIR}/retrieval_{stamp}.md")
    return 0


def _save_partial(runs: dict[str, dict], path: Path) -> None:
    """每跑完一组就落盘，避免后半段失败时前面的结果白跑。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")


def _render(runs: dict[str, dict], k: int, no_rerank: bool) -> str:
    lines = [
        "# 检索评测报告",
        "",
        f"- 指标：Recall@{k} / MRR",
        f"- 重排：{'关闭' if no_rerank else '开启（bge-reranker-v2-m3）'}",
        "",
    ]

    for name, data in runs.items():
        label = "不过滤" if name == "baseline" else "按类别限定权威"
        lines += [
            f"## {label}",
            "",
            f"运行 {data['runs']} 条",
            "",
            f"| 类别 | 条数 | Recall@{k} | MRR | 拒答率 |",
            "|---|---|---|---|---|",
        ]
        for cat, stats in sorted(data["per_category"].items()):
            recall = f"{stats[f'recall@{k}']*100:.1f}%"
            mrr = f"{stats['mrr']:.3f}"
            rej = (
                f"{stats['rejection_rate']*100:.0f}%"
                if stats["rejection_rate"] is not None else "-"
            )
            lines.append(f"| {cat} | {stats['count']} | {recall} | {mrr} | {rej} |")

        overall = data["overall"]
        lines += [
            f"| **合计** | {overall['count']} | "
            f"**{overall[f'recall@{k}']*100:.1f}%** | "
            f"**{overall['mrr']:.3f}** | - |",
            "",
            f"延迟：p50 {data['latency']['p50']:.0f} ms / "
            f"p95 {data['latency']['p95']:.0f} ms / max {data['latency']['max']:.0f} ms",
            "",
        ]

    if len(runs) == 2:
        base = runs["baseline"]["overall"]
        grouped = runs["grouped"]["overall"]
        delta = (grouped[f"recall@{k}"] - base[f"recall@{k}"]) * 100
        lines += [
            "## 权威分区带来多少提升",
            "",
            f"- Recall@{k}：{base[f'recall@{k}']*100:.1f}% → {grouped[f'recall@{k}']*100:.1f}%"
            f"（{delta:+.1f} 个百分点）",
            f"- MRR：{base['mrr']:.3f} → {grouped['mrr']:.3f}",
            "",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
