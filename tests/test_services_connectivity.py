"""真实服务连通性测试。

默认**不执行**（pyproject 的 addopts 里有 `-m "not integration"`）。
需要真实环境时手动跑：

    uv run pytest -m integration -v
"""

from __future__ import annotations

import pytest

from glassdesk.config import PROJECT_ROOT, load_settings


@pytest.fixture(scope="module")
def real_settings():
    if not (PROJECT_ROOT / ".env").is_file():
        pytest.skip("项目根目录缺少 .env，跳过集成测试")
    return load_settings()


@pytest.mark.integration
def test_mysql_reachable(real_settings):
    import pymysql

    cfg = real_settings.services.mysql
    conn = pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password.get_secret_value(),
        connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        assert version.startswith("8.")
    finally:
        conn.close()


@pytest.mark.integration
def test_redis_reachable(real_settings):
    import redis

    cfg = real_settings.services.redis
    client = redis.Redis(
        host=cfg.host,
        port=cfg.port,
        password=cfg.password.get_secret_value(),
        socket_timeout=5,
    )
    try:
        assert client.ping() is True
    finally:
        client.close()


@pytest.mark.integration
def test_milvus_reachable(real_settings):
    from pymilvus import MilvusClient

    cfg = real_settings.services.milvus
    client = MilvusClient(uri=f"http://{cfg.host}:{cfg.port}")
    dbs = client.list_databases()
    assert "default" in dbs


@pytest.mark.integration
def test_deepseek_api_reachable(real_settings):
    from openai import OpenAI

    cfg = real_settings.llm
    client = OpenAI(api_key=cfg.api_key.get_secret_value(), base_url=cfg.base_url, timeout=30)
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        max_tokens=8,
    )
    assert resp.choices[0].message.content.strip()
