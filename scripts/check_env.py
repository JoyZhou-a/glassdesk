"""环境自检：配置能否加载、依赖服务是否可达、模型是否就位。

用法：
    uv run python scripts/check_env.py
    uv run python scripts/check_env.py --skip-llm    # 不想花 token 时

退出码：0 全部通过；1 有失败项。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            self.failed += 1
        print(f"{OK if ok else FAIL} {name:<22} {detail}")

    def warn(self, name: str, detail: str = "") -> None:
        """警告：不计入失败，但必须显式说出来。"""
        self.warned += 1
        print(f"{WARN} {name:<22} {detail}")


def check_config(rep: Report):
    try:
        settings = load_settings()
    except ConfigError as exc:
        rep.add("配置加载", False, str(exc))
        return None
    rep.add("配置加载", True, f"root={settings.root}")
    return settings


def check_models(rep: Report, settings, do_download_hint: bool = True):
    missing = settings.models.missing()
    if missing:
        rep.add("模型目录", False, f"缺少 {len(missing)} 个：{', '.join(Path(m).name for m in missing)}")
        if do_download_hint:
            print("       提示：模型应放在 E:\\models\\，用 ModelScope 下载")
    else:
        rep.add("模型目录", True, "embedder / reranker / intent_backbone 均存在")


def check_mysql(rep: Report, settings):
    import pymysql

    cfg = settings.services.mysql
    try:
        conn = pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password.get_secret_value(),
            connect_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            cur.execute("SHOW DATABASES")
            dbs = {row[0] for row in cur.fetchall()}
        conn.close()
        need = {cfg.database, cfg.database_test}
        missing = need - dbs
        detail = f"v{version} @{cfg.host}:{cfg.port}"
        if missing:
            detail += f" | 待建库：{', '.join(sorted(missing))}"
        rep.add("MySQL", True, detail)
    except Exception as exc:  # noqa: BLE001
        rep.add("MySQL", False, f"{cfg.host}:{cfg.port} -> {exc}")


def check_redis(rep: Report, settings):
    import redis

    cfg = settings.services.redis
    try:
        client = redis.Redis(
            host=cfg.host,
            port=cfg.port,
            password=cfg.password.get_secret_value(),
            socket_timeout=5,
        )
        pong = client.ping()
        info = client.info("server")
        client.close()
        rep.add("Redis", bool(pong), f"v{info.get('redis_version')} @{cfg.host}:{cfg.port} db={cfg.db}")
    except Exception as exc:  # noqa: BLE001
        rep.add("Redis", False, f"{cfg.host}:{cfg.port} -> {exc}")


def check_milvus(rep: Report, settings):
    try:
        from pymilvus import MilvusClient

        cfg = settings.services.milvus
        client = MilvusClient(uri=f"http://{cfg.host}:{cfg.port}")
        dbs = set(client.list_databases())
        need = {cfg.database, cfg.database_test}
        missing = need - dbs
        has_hybrid = hasattr(client, "hybrid_search")
        detail = f"@{cfg.host}:{cfg.port} hybrid_search={has_hybrid}"
        if missing:
            detail += f" | 待建库：{', '.join(sorted(missing))}"
        rep.add("Milvus", has_hybrid, detail)
    except Exception as exc:  # noqa: BLE001
        rep.add("Milvus", False, f"{settings.services.milvus.host}:{settings.services.milvus.port} -> {exc}")


def check_llm(rep: Report, settings):
    from openai import OpenAI

    cfg = settings.llm
    try:
        client = OpenAI(api_key=cfg.api_key.get_secret_value(), base_url=cfg.base_url, timeout=30)
        resp = client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=8,
        )
        actual = resp.model
        note = "" if actual == cfg.model else f"（配置为 {cfg.model}，实际返回 {actual}）"
        rep.add("LLM", True, f"{cfg.base_url}{note}")
    except Exception as exc:  # noqa: BLE001
        rep.add("LLM", False, f"{cfg.base_url} -> {exc}")


def check_torch(rep: Report) -> None:
    """检查 torch 是 CUDA 版还是 CPU 版。

    为什么需要这一项：本项目的 torch 是从 conda 环境复制进 .venv 的
    （官方 CUDA 轮子在国内下不动），而 ``uv sync`` 会把它换回 PyPI 的 CPU 版。
    换回之后**不会报任何错**，只是编码从约 209 条/秒掉到 1.5 条/秒——
    表现是"怎么突然变慢了"，很难第一时间想到是 torch 被换了。
    """
    try:
        import torch
    except ImportError:
        rep.warn("torch", "未安装（M1a 之前的里程碑不需要）；索引/检索功能不可用")
        return

    version = torch.__version__
    if not torch.cuda.is_available():
        rep.warn(
            "torch",
            f"{version} 是 CPU 版 → GPU 不可用（编码会慢约 140 倍）。"
            "跑 pwsh scripts/use_conda_torch.ps1 修复",
        )
        return

    name = torch.cuda.get_device_name(0)
    arch = torch.cuda.get_arch_list()
    if "sm_120" not in arch:
        rep.warn("torch", f"{version} CUDA 可用，但架构表里没有 sm_120（BF16/FP16 可能退化）")
        return
    rep.add("torch", True, f"{version} | {name} | sm_120")


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassDesk 环境自检")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 LLM 调用（省 token）")
    args = parser.parse_args()

    print("=" * 72)
    print("GlassDesk 环境自检")
    print("=" * 72)

    settings = check_config(Report())
    if settings is None:
        return 1

    setup_logging(settings, file=False)

    rep = Report()
    check_models(rep, settings)
    check_torch(rep)
    check_mysql(rep, settings)
    check_redis(rep, settings)
    check_milvus(rep, settings)
    if not args.skip_llm:
        check_llm(rep, settings)

    print("=" * 72)
    if rep.failed:
        print(f"结果：{rep.failed} 项失败" + (f"，{rep.warned} 项警告" if rep.warned else ""))
        return 1
    if rep.warned:
        print(f"结果：全部通过，但有 {rep.warned} 项警告（见上）")
        return 0
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
