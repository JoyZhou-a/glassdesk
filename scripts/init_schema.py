"""建表脚本（幂等）。

用法：
    uv run python scripts/init_schema.py                # 作用于 glassdesk
    uv run python scripts/init_schema.py --database test # 作用于 glassdesk_test
    uv run python scripts/init_schema.py --drop          # 先删后建（仅限 test 库）

安全：``--drop`` 只允许配合 ``--database test``，防止误删生产库。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.ingest import schema  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.storage import MySQLStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 GlassDesk 表结构")
    parser.add_argument("--database", choices=["main", "test"], default="main")
    parser.add_argument("--drop", action="store_true", help="先删表再建（只允许 test 库）")
    args = parser.parse_args()

    if args.drop and args.database != "test":
        print("拒绝执行：--drop 只允许配合 --database test")
        return 2

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1

    setup_logging(settings, file=False)

    cfg = settings.services.mysql
    db_name = cfg.database if args.database == "main" else cfg.database_test

    print("=" * 64)
    print(f"初始化表结构：{db_name}")
    print("=" * 64)

    # 库不存在就先建（幂等）
    MySQLStore.create_database(cfg, db_name)

    store = MySQLStore(cfg, database=db_name)
    try:
        if args.drop:
            existing = store.list_tables()
            if existing:
                store.executescript([f"DROP TABLE IF EXISTS `{t}`" for t in existing])
                store.connect().commit()
                print(f"  已删除 {len(existing)} 张表：{', '.join(existing)}")

        store.executescript(schema.statements())
        print(f"  已建表：{', '.join(schema.EXPECTED_TABLES)}")

        for key, value in schema.seed_meta_statements():
            store.execute(
                "INSERT IGNORE INTO app_meta (k, v, updated_at) VALUES (%s, %s, NOW())",
                (key, value),
            )
        store.connect().commit()

        # 校验
        present = set(store.list_tables())
        missing = [t for t in schema.EXPECTED_TABLES if t not in present]
        if missing:
            print(f"  [FAIL] 缺少表：{missing}")
            return 1

        rows = store.fetchall("SELECT k, v FROM app_meta")
        print(f"  app_meta：{ {r['k']: r['v'] for r in rows} }")
        for table in schema.EXPECTED_TABLES:
            count = store.fetchone(f"SELECT COUNT(*) AS n FROM `{table}`")
            print(f"    {table:<12} rows={count['n']}")
    finally:
        store.close()

    print("=" * 64)
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
