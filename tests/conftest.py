"""pytest 公共夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from glassdesk.config import Settings, load_settings

MINIMAL_CONFIG = """
app:
  name: GlassDesk
  env: test
paths:
  models: E:/models
  data: data
  logs: logs
  outputs: outputs
models:
  embedder: E:/models/bge-m3
  reranker: E:/models/bge-reranker-v2-m3
  intent_backbone: E:/models/distilbert-base-uncased
services:
  milvus:
    host: 127.0.0.1
    port: 19530
    database: glassdesk
    database_test: glassdesk_test
    collection: chunks
  mysql:
    host: 127.0.0.1
    port: 3306
    user: root
    database: glassdesk
    database_test: glassdesk_test
    charset: utf8mb4
  redis:
    host: 127.0.0.1
    port: 6379
    db: 1
    db_test: 15
llm:
  provider: deepseek
  base_url: https://api.deepseek.com
  model: deepseek-chat
  temperature: 0.1
  max_tokens: 1024
  timeout_s: 60
  max_retries: 2
retrieval:
  parent_chunk_size: 1200
  child_chunk_size: 300
  chunk_overlap: 50
  top_k: 20
  rerank_candidates: 20
  final_k: 5
  min_rerank_score: 0.35
answer:
  phone_max_lines: 3
  phone_max_sentence_chars: 60
  low_confidence_threshold: 0.6
  force_human_intents:
    - after_sales
logging:
  level: DEBUG
  file: logs/app.log
  rotation: 10 MB
  retention: 14 days
"""

MINIMAL_ENV = """
MYSQL_PASSWORD=test-mysql-pw
REDIS_PASSWORD=test-redis-pw
DEEPSEEK_API_KEY=test-deepseek-key
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """一个临时的"项目根目录"，含 config.yaml 与 .env。"""
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    (tmp_path / ".env").write_text(MINIMAL_ENV, encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(project: Path) -> Settings:
    return load_settings(root=project)
