"""数据模型 DDL —— 对应 ``docs/09_知识库更新.md`` §3。

约定：
- 每张表一个常量，顺序由 ``DDL`` 元组决定（documents → chunks → ingest_runs → app_meta）。
- 全部使用 ``CREATE TABLE IF NOT EXISTS``，可重复执行。
- ``chunks.kind`` 区分父块/子块；``block_kind`` 记录来源块类型（段落/表格行/警告/问答），
  便于检索阶段对表格行做不同处理——这是对 09 文档的小扩展，已在下方注明。
- ``experience_entries`` 不在 M1a 建，它属于经验闭环（M4）。
"""

from __future__ import annotations

DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id               VARCHAR(64)  NOT NULL COMMENT '稳定标识，不随内容/顺序变化',
  version              INT          NOT NULL COMMENT '从 1 递增',
  title                VARCHAR(255) NOT NULL,
  authority            ENUM('standard','manual','experience') NOT NULL,
  source_file          VARCHAR(512) NOT NULL,
  content_hash         CHAR(64)     NOT NULL COMMENT '整份文档 sha256',
  chunking_config_hash CHAR(64)     NOT NULL COMMENT '切分参数指纹',
  effective_from       DATE         NOT NULL,
  effective_to         DATE         NULL,
  status               ENUM('draft','active','superseded','archived') NOT NULL DEFAULT 'draft',
  chunk_count          INT          NOT NULL DEFAULT 0,
  ingested_at          DATETIME     NOT NULL,
  activated_at         DATETIME     NULL,
  PRIMARY KEY (doc_id, version),
  KEY idx_status (status),
  KEY idx_effective (effective_from, effective_to)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id     VARCHAR(160) NOT NULL COMMENT '{doc_id}@{version}#{kind}{ordinal}',
  doc_id       VARCHAR(64)  NOT NULL,
  version      INT          NOT NULL,
  kind         ENUM('parent','child') NOT NULL,
  block_kind   VARCHAR(16)  NOT NULL DEFAULT 'paragraph'
               COMMENT 'paragraph|table_row|warning|heading|qa|record',
  parent_id    VARCHAR(160) NULL COMMENT 'child 指向 parent；parent 为 NULL',
  ordinal      INT          NOT NULL COMMENT '文档内序号，稳定',
  text         MEDIUMTEXT   NOT NULL,
  content_hash CHAR(64)     NOT NULL,
  page_no      INT          NULL,
  clause       VARCHAR(64)  NULL COMMENT '如 AS1288 Section 5',
  status       ENUM('draft','active','superseded','archived') NOT NULL DEFAULT 'draft',
  created_at   DATETIME     NOT NULL,
  PRIMARY KEY (chunk_id),
  KEY idx_doc_version (doc_id, version),
  KEY idx_status (status),
  KEY idx_parent (parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

INGEST_RUNS = """
CREATE TABLE IF NOT EXISTS ingest_runs (
  run_id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  started_at        DATETIME NOT NULL,
  finished_at       DATETIME NULL,
  doc_id            VARCHAR(64) NOT NULL,
  version           INT NOT NULL,
  action            ENUM('add','update','archive','rebuild') NOT NULL,
  kb_version_before VARCHAR(32) NOT NULL,
  kb_version_after  VARCHAR(32) NULL,
  chunks_added      INT NOT NULL DEFAULT 0,
  chunks_deleted    INT NOT NULL DEFAULT 0,
  regression_passed TINYINT(1) NULL,
  status            ENUM('running','success','failed','rolled_back') NOT NULL,
  operator          VARCHAR(64) NOT NULL,
  note              TEXT NULL,
  KEY idx_doc (doc_id, version),
  KEY idx_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

APP_META = """
CREATE TABLE IF NOT EXISTS app_meta (
  k          VARCHAR(64) PRIMARY KEY,
  v          VARCHAR(255) NOT NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

# 建表顺序即依赖顺序（无外键，但便于阅读）
DDL: tuple[tuple[str, str], ...] = (
    ("documents", DOCUMENTS),
    ("chunks", CHUNKS),
    ("ingest_runs", INGEST_RUNS),
    ("app_meta", APP_META),
)

EXPECTED_TABLES: tuple[str, ...] = tuple(name for name, _ in DDL)

# 初始元数据（幂等插入）
SEED_META: tuple[tuple[str, str], ...] = (("kb_version", "1"),)


def statements() -> list[str]:
    """返回按顺序执行的 DDL 语句列表。"""
    return [sql for _, sql in DDL]


def seed_meta_statements() -> list[tuple[str, str]]:
    """返回需要幂等写入的 app_meta 初始键值。"""
    return list(SEED_META)
