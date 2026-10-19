"""初始化存储：在 Milvus 与 MySQL 中创建本项目的 database / schema。

幂等：可重复执行。不会删除或修改任何已有库。

用法：
    uv run python scripts/init_storage.py
    uv run python scripts/init_storage.py --prod-only   # 只建生产库
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402


def init_mysql(settings, names: list[str]) -> list[str]:
    import pymysql

    cfg = settings.services.mysql
    conn = pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password.get_secret_value(),
        connect_timeout=5,
    )
    created: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            existing = {row[0] for row in cur.fetchall()}
            for name in names:
                if name in existing:
                    print(f"  MySQL  {name:<18} 已存在，跳过")
                    continue
                cur.execute(
                    f"CREATE DATABASE `{name}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
                created.append(name)
                print(f"  MySQL  {name:<18} 已创建")
        conn.commit()
    finally:
        conn.close()
    return created


def init_milvus(settings, names: list[str]) -> list[str]:
    from pymilvus import MilvusClient

    cfg = settings.services.milvus
    client = MilvusClient(uri=f"http://{cfg.host}:{cfg.port}")
    existing = set(client.list_databases())
    created: list[str] = []
    for name in names:
        if name in existing:
            print(f"  Milvus {name:<18} 已存在，跳过")
            continue
        client.create_database(name)
        created.append(name)
        print(f"  Milvus {name:<18} 已创建")
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 GlassDesk 存储")
    parser.add_argument("--prod-only", action="store_true", help="只创建生产库，不建测试库")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1

    setup_logging(settings, file=False)

    mysql_names = [settings.services.mysql.database]
    milvus_names = [settings.services.milvus.database]
    if not args.prod_only:
        mysql_names.append(settings.services.mysql.database_test)
        milvus_names.append(settings.services.milvus.database_test)

    print("=" * 60)
    print("初始化存储")
    print("=" * 60)
    init_mysql(settings, mysql_names)
    init_milvus(settings, milvus_names)
    print("=" * 60)
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
