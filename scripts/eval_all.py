"""一键跑完三组评测，输出一份汇总报告。

三组指标**必须分开报**，不能加总成一个分数：

| 组 | 走哪条路 | 指标 | 花 token 吗 |
|---|---|---|---|
| 结构化（60 题） | 抽参数 → 查表 → 模板 | 答案数值正确率 | 否 |
| 检索（49 题） | 向量检索 + 重排 | Recall@5 / MRR | 否 |
| 答案（60 题） | 检索 + 大模型生成 | 状态正确率 / 引用覆盖 | 是（有缓存） |

原因：它们的失败模式不同——一个是数字算错，一个是没检索到，一个是生成跑偏。
加总会掩盖问题。

用法::

    .venv\\Scripts\\python.exe scripts/eval_all.py                    # 全部
    .venv\\Scripts\\python.exe scripts/eval_all.py --skip-retrieval    # 跳过慢的检索评测
    .venv\\Scripts\\python.exe scripts/eval_all.py --only structured   # 只跑一组
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
OUTPUT_DIR = ROOT / "outputs" / "eval"


def run(script: str, *extra: str) -> int:
    started = time.perf_counter()
    print(f"\n{'=' * 72}\n▶ {script}\n{'=' * 72}")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *extra],
        cwd=str(ROOT), check=False,
    )
    elapsed = time.perf_counter() - started
    print(f"◀ {script} 结束（{elapsed:.1f}s，exit={result.returncode}）")
    return result.returncode


def newest(pattern: str) -> Path | None:
    files = sorted(OUTPUT_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def summarise() -> str:
    lines = ["# GlassDesk 评测汇总", "", f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    lines += ["| 组 | 类别 | 条数 | 指标 |", "|---|---|---|---|"]
    rows = 0

    # 结构化
    path = OUTPUT_DIR / "structured_latest.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        total, correct = data.get("total", 0), data.get("correct", 0)
        for cat, n in sorted(data.get("positives", {}).items()):
            lines.append(f"| 结构化 | {cat} | {n} | — |")
        rate = correct / total * 100 if total else 0
        lines.append(f"| **结构化** | **合计** | **{total}** | **{rate:.0f}% 正确** |")
        safe = len(data.get("negative_safe", []))
        lines.append(f"| 结构化 | negative | {safe} | 负面样例安全处理 |")
        rows += 1

    # 检索
    path = newest("retrieval_2*.json")
    if path:
        data = json.loads(path.read_text(encoding="utf-8"))
        grouped = data.get("grouped") or data.get("baseline") or {}
        overall = grouped.get("overall", {})
        if overall:
            recall = next((v for k, v in overall.items() if k.startswith("recall@")), 0) * 100
            lines.append(
                f"| 检索 | 全部 | {overall.get('count', 0)} | "
                f"Recall@5 {recall:.1f}% / MRR {overall.get('mrr', 0):.3f} |"
            )
            for cat, stats in sorted(grouped.get("per_category", {}).items()):
                r = next((v for k, v in stats.items() if k.startswith("recall@")), 0) * 100
                lines.append(f"| 检索 | {cat} | {stats.get('count', 0)} | Recall@5 {r:.0f}% |")
            lat = grouped.get("latency", {})
            lines.append(
                f"| 检索 | 延迟 | — | p50 {lat.get('p50', 0):.0f}ms / p95 {lat.get('p95', 0):.0f}ms |"
            )
            rows += 1

    # 答案
    path = OUTPUT_DIR / "answer_latest.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        for cat, stats in sorted(data.get("summary", {}).items()):
            n = stats["n"]
            lines.append(
                f"| 答案 | {cat} | {n} | 状态正确 {stats['state_ok'] / n * 100:.0f}% / "
                f"引用覆盖 {stats['cited'] / n * 100:.0f}% |"
            )
        llm = data.get("llm", {})
        lines.append(
            f"| 答案 | token | — | 调用 {llm.get('calls', 0)} 次 / "
            f"缓存 {llm.get('cache_hits', 0)} 次 |"
        )
        rows += 1

    if rows == 0:
        lines.append("| — | 无数据 | — | 先跑评测 |")

    lines += [
        "",
        "## 口径说明",
        "",
        "- **结构化正确率** 的 ground truth 与查询表同源，严格说是自证：",
        "  它验证的是「抽取 + 查表 + 渲染」这条链路，不是独立验证数据本身。",
        "- **检索 Recall@5** 只对走向量检索的类别有意义（结构化类已分流）。",
        "- **答案状态正确率** 判的是「该答的答了、该问的问了、该拒的拒了」，",
        "  不是语义准确率；语义准确率需要人工评分或 LLM 评委，当前未做。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="一键评测")
    parser.add_argument("--only", choices=["structured", "retrieval", "answer"], default=None)
    parser.add_argument("--skip-retrieval", action="store_true", help="跳过较慢的检索评测")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0

    if args.only in (None, "structured"):
        failures += run("eval_structured.py") != 0
    if args.only in (None, "retrieval") and not args.skip_retrieval:
        failures += run("eval_retrieval.py") != 0
    if args.only in (None, "answer"):
        failures += run("eval_answer.py") != 0

    report = summarise()
    print("\n" + report)
    (OUTPUT_DIR / "summary.md").write_text(report, encoding="utf-8")
    print(f"\n汇总已保存 → {OUTPUT_DIR / 'summary.md'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
