"""把 seeds 里的结构化数据装进 MySQL 表（M4 的前提）。

为什么不能只靠 chunks 里的文本：见 schema.py 的说明。
这三个 CSV 是**事实源**，chunks 里那份文本是给模糊检索兜底的副本。

用法::

    .venv\\Scripts\\python.exe scripts/load_tables.py
    .venv\\Scripts\\python.exe scripts/load_tables.py --check   # 只比对，不写库
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.ingest import schema  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.storage import MySQLStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "data" / "seeds"


def read_csv(name: str) -> list[dict[str, str]]:
    with (SEEDS / name).open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(value: str | None) -> float | None:
    return None if value in (None, "", "None") else float(value)


def _i(value: str | None) -> int | None:
    return None if value in (None, "", "None") else int(float(value))


def load_capability(store: MySQLStore) -> int:
    rows = read_csv("capability.csv")
    now = datetime.now().replace(microsecond=0)
    payload = [
        (
            r["process"], r["shape"], float(r["thickness_mm"]),
            int(r["max_width_mm"]), int(r["max_height_mm"]), float(r["max_area_m2"]),
            int(r["min_width_mm"]), int(r["min_height_mm"]), float(r["tolerance_mm"]),
            int(r["min_radius_mm"]), r["igu_cavity_mm"] or None, r["notes"] or None, now,
        )
        for r in rows
    ]
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM capability")
            cur.executemany(
                """
                INSERT INTO capability
                  (process, shape, thickness_mm, max_width_mm, max_height_mm, max_area_m2,
                   min_width_mm, min_height_mm, tolerance_mm, min_radius_mm,
                   igu_cavity_mm, notes, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                payload,
            )
    return len(payload)


def load_pricing(store: MySQLStore) -> int:
    rows = read_csv("pricing.csv")
    now = datetime.now().replace(microsecond=0)
    payload = [
        (
            r["process"], r["shape"], float(r["thickness_mm"]),
            r["variant"] or "", r["coating"],
            float(r["unit_price_aud_m2"]), float(r["min_charge_aud"]), float(r["moq_m2"]),
            r["valid_from"], r["valid_to"], float(r["cost_price_aud_m2"]), now,
        )
        for r in rows
    ]
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pricing")
            cur.executemany(
                """
                INSERT INTO pricing
                  (process, shape, thickness_mm, variant, coating, unit_price_aud_m2,
                   min_charge_aud, moq_m2, valid_from, valid_to, cost_price_aud_m2, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                payload,
            )
    return len(payload)


def load_leadtime(store: MySQLStore) -> int:
    rows = read_csv("leadtime.csv")
    now = datetime.now().replace(microsecond=0)
    payload = [
        (
            r["process"], float(r["thickness_max_mm"]), int(r["qty_max"]),
            int(r["business_days"]), r["rush_available"].lower() == "true",
            float(r["rush_surcharge_pct"]), r["note"] or None, now,
        )
        for r in rows
    ]
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM leadtime")
            cur.executemany(
                """
                INSERT INTO leadtime
                  (process, thickness_max_mm, qty_max, business_days,
                   rush_available, rush_surcharge_pct, note, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                payload,
            )
    return len(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="装载结构化表")
    parser.add_argument("--check", action="store_true", help="只读比对，不写库")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=False)

    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    try:
        # 表可能还不存在（老库），先补齐
        store.executescript(schema.statements())

        if not args.check:
            n_cap = load_capability(store)
            n_price = load_pricing(store)
            n_lead = load_leadtime(store)
            print(f"已装载：capability={n_cap} pricing={n_price} leadtime={n_lead}")

        print("\n" + "=" * 56)
        print(f"{'表':<14}{'行数':>8}")
        print("=" * 56)
        total = 0
        for table in schema.STRUCTURED_TABLES:
            row = store.fetchone(f"SELECT COUNT(*) AS n FROM `{table}`")
            n = int(row["n"]) if row else 0
            total += n
            print(f"{table:<14}{n:>8}")
        print("=" * 56)
        print(f"{'合计':<14}{total:>8}")

        # 关键校验：成本价必须存在（用于验证脱敏逻辑），但绝不能出现在接口返回里
        cost = store.fetchone(
            "SELECT COUNT(*) AS n FROM pricing WHERE cost_price_aud_m2 IS NOT NULL"
        )
        print(f"\n成本价已入库：{cost['n']} 行（用于验证脱敏，不对外返回）")

        if total == 0:
            print("\n[FAIL] 结构化表为空，M4 无法工作")
            return 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
