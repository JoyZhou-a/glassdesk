"""索引完整性检查：MySQL 里每个 active 子块，Milvus 里都该有对应向量。

为什么需要这个脚本：

向量库和 MySQL 是两套存储，**Milvus 丢数据是不会报错的**——它只是少返回几条。
表现是"某类问题检索不到东西"，但日志一切正常。

实测踩过一次：275 条向量里 `experience-fieldbook`（41 条）和 `tickets-2025`
（30 条）全部消失，总数掉到 204。如果没有这个检查，
售后类问题的召回率会直接变成 0，而你会以为是检索算法的问题。

用法::

    .venv\\Scripts\\python.exe scripts/check_index.py
    .venv\\Scripts\\python.exe scripts/check_index.py --fix   # 不对就重新索引
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.storage import MilvusStore, MySQLStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="校验向量索引完整性")
    parser.add_argument("--fix", action="store_true", help="不一致时自动重新索引")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=False)

    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    milvus = MilvusStore(settings.services.milvus)

    try:
        expected = store.fetchall(
            """
            SELECT doc_id, COUNT(*) AS n
            FROM chunks
            WHERE kind='child' AND status='active'
            GROUP BY doc_id ORDER BY doc_id
            """
        )
        if not expected:
            print("MySQL 里没有 active 子块，先跑 ingest")
            return 1

        client = milvus.client
        if not client.has_collection(milvus.collection):
            print(f"[FAIL] Milvus 里没有集合 {milvus.collection}，需要先 index")
            return 1
        client.load_collection(milvus.collection)

        print("=" * 62)
        print(f"{'doc_id':<24}{'MySQL':>8}{'Milvus':>8}  状态")
        print("=" * 62)

        problems = 0
        total_expected = total_actual = 0
        for row in expected:
            doc_id = row["doc_id"]
            want = int(row["n"])
            got_rows = client.query(
                collection_name=milvus.collection,
                filter=f'doc_id == "{doc_id}"',
                output_fields=["count(*)"],
            )
            got = int(got_rows[0]["count(*)"]) if got_rows else 0
            total_expected += want
            total_actual += got

            status = "OK" if got == want else f"[缺失 {want - got}]"
            if got != want:
                problems += 1
            print(f"{doc_id:<24}{want:>8}{got:>8}  {status}")

        print("=" * 62)
        print(f"{'合计':<24}{total_expected:>8}{total_actual:>8}")
        client.close()

        if problems:
            print(f"\n[FAIL] {problems} 份文档的向量数与 MySQL 不一致")
            if args.fix:
                print("正在重新索引…")
                subprocess.run(
                    [sys.executable, str(Path(__file__).parent / "ingest.py"),
                     "index", "--all", "--recreate"],
                    check=False,
                )
                return 0
            print("加 --fix 可自动修复")
            return 1

        print("\n[OK] 索引完整")
        return 0
    finally:
        milvus.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
