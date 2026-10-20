"""问一句话，拿到答案、出处和耗时。

这个脚本只是 ``glassdesk.answer.flow.run`` 的命令行外壳——**编排逻辑只有一份**，
评测脚本调的也是同一个函数。否则会出现"评测跑的路线和线上不一致"。

我踩过一次：答案评测绕过路由，把所有题都塞进向量检索，于是本该追问的题被答了，
negative 只有 45%，看着像系统坏了，其实是评测错了。

用法::

    .venv\\Scripts\\python.exe scripts/ask.py "What glass for a pool fence?"
    .venv\\Scripts\\python.exe scripts/ask.py --mode email "There is mist in my glazing"
    .venv\\Scripts\\python.exe scripts/ask.py --no-cache "How much for 12mm toughened?"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.answer import flow, templates  # noqa: E402
from glassdesk.answer.generator import AnswerGenerator  # noqa: E402
from glassdesk.answer.llm import CachedLLM  # noqa: E402
from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.constants import OutputMode  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.retrieval.embedder import BGEEmbedder  # noqa: E402
from glassdesk.retrieval.rerank import Reranker  # noqa: E402
from glassdesk.retrieval.service import RetrievalService  # noqa: E402
from glassdesk.storage import MilvusStore, MySQLStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="问 GlassDesk")
    parser.add_argument("question")
    parser.add_argument("--mode", choices=["phone", "email"], default="phone")
    parser.add_argument("--no-cache", action="store_true", help="不使用 LLM 缓存（调试用）")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本消费")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=False)

    mode = OutputMode(args.mode)
    templates.set_limits(settings.answer.phone_max_lines,
                         settings.answer.phone_max_sentence_chars)
    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    milvus = MilvusStore(settings.services.milvus)
    embedder = BGEEmbedder(settings.models.embedder, batch_size=1)
    reranker = None if args.no_rerank else Reranker(settings.models.reranker)
    llm = CachedLLM(settings.llm, settings.paths.outputs / "llm_cache",
                    use_cache=not args.no_cache)

    try:
        service = RetrievalService(settings, store, milvus, embedder, reranker)
        generator = AnswerGenerator(llm, settings.answer)
        result = flow.run(
            args.question,
            store=store,
            retriever=service,
            generator_factory=lambda: generator,
            mode=mode,
        )
        payload = {
            "question": args.question,
            "mode": mode.value,
            "route": result.route,
            "status": result.status,
            "authority": result.authority,
            "evidence": result.evidence,
            "citations": result.citations,
            "answer": result.text if isinstance(result.text, str) else result.text,
            "notes": result.notes,
            "retrieval_ms": round(result.retrieval_ms, 1),
            "tokens": llm.stats.as_dict(),
        }
    finally:
        milvus.close()
        store.close()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("=" * 70)
    print(f"Q: {payload['question']}")
    print(f"   路由: {payload['route']}   状态: {payload['status']}"
          f"   权威级: {payload['authority']}   证据: {payload['evidence']}")
    print("-" * 70)
    answer = payload["answer"]
    print(answer if isinstance(answer, str) else "\n".join(answer))
    print("-" * 70)
    tokens = payload["tokens"]
    if tokens.get("calls") or tokens.get("cache_hits"):
        print(f"token: 调用 {tokens['calls']} 次 / 缓存命中 {tokens['cache_hits']} 次 / "
              f"输入 {tokens['prompt_tokens']} / 输出 {tokens['completion_tokens']}")
    if payload["notes"]:
        print(f"备注: {'; '.join(payload['notes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
