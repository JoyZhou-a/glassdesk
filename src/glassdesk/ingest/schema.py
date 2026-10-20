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

# ======================================================================
# 结构化知识表（M4）
# ----------------------------------------------------------------------
# 这三张表是"能力矩阵 / 报价 / 交期"的**结构化形态**。
#
# 为什么还要单独建表，而不直接查 chunks 里的文本：
# 尺寸类问题在向量空间里根本分不开（"4mm" 和 "5mm" 语义几乎重合，实测
# Recall@5 只有 70%）。正确做法是抽出参数后 `WHERE thickness_mm = 4`。
# 把结构化数据放进真正的表，才能用 SQL 精确匹配。
#
# chunks 里仍保留这些内容的文本副本——那是给"模糊提问"做兜底检索用的。
# 两者职责不同，不是重复。
# ======================================================================
CAPABILITY = """
CREATE TABLE IF NOT EXISTS capability (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  process       ENUM('annealed','toughened','laminated','igu') NOT NULL,
  shape         ENUM('flat','bent') NOT NULL,
  thickness_mm  DECIMAL(6,2) NOT NULL COMMENT '夹胶用公称厚度，如 6.38',
  max_width_mm  INT NOT NULL,
  max_height_mm INT NOT NULL,
  max_area_m2   DECIMAL(6,2) NOT NULL,
  min_width_mm  INT NOT NULL,
  min_height_mm INT NOT NULL,
  tolerance_mm  DECIMAL(4,1) NOT NULL,
  min_radius_mm INT NOT NULL DEFAULT 0 COMMENT 'flat 为 0',
  igu_cavity_mm VARCHAR(32) NULL,
  notes         VARCHAR(255) NULL,
  updated_at    DATETIME NOT NULL,
  UNIQUE KEY uk_capability (process, shape, thickness_mm)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

PRICING = """
CREATE TABLE IF NOT EXISTS pricing (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  process           ENUM('annealed','toughened','laminated','igu') NOT NULL,
  shape             ENUM('flat','bent') NOT NULL,
  thickness_mm      DECIMAL(6,2) NOT NULL,
  variant           VARCHAR(32) NOT NULL DEFAULT '' COMMENT '如 6-12-6',
  coating           VARCHAR(16) NOT NULL DEFAULT 'none',
  unit_price_aud_m2 DECIMAL(8,2) NOT NULL COMMENT '含 GST',
  min_charge_aud    DECIMAL(8,2) NOT NULL,
  moq_m2            DECIMAL(5,2) NOT NULL,
  valid_from        DATE NOT NULL,
  valid_to          DATE NOT NULL,
  cost_price_aud_m2 DECIMAL(8,2) NOT NULL COMMENT '内部成本，**永不返回**',
  updated_at        DATETIME NOT NULL,
  UNIQUE KEY uk_pricing (process, shape, thickness_mm, variant, coating)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

LEADTIME = """
CREATE TABLE IF NOT EXISTS leadtime (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  process          ENUM('annealed','toughened','laminated','igu') NOT NULL,
  thickness_max_mm DECIMAL(6,2) NOT NULL,
  qty_max          INT NOT NULL,
  business_days    INT NOT NULL,
  rush_available   TINYINT(1) NOT NULL,
  rush_surcharge_pct DECIMAL(5,1) NOT NULL,
  note             VARCHAR(255) NULL,
  updated_at       DATETIME NOT NULL,
  UNIQUE KEY uk_leadtime (process, thickness_max_mm, qty_max)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

# 建表顺序即依赖顺序（无外键，但便于阅读）
DDL: tuple[tuple[str, str], ...] = (
    ("documents", DOCUMENTS),
    ("chunks", CHUNKS),
    ("ingest_runs", INGEST_RUNS),
    ("app_meta", APP_META),
    ("capability", CAPABILITY),
    ("pricing", PRICING),
    ("leadtime", LEADTIME),
)

#: 向量检索相关的表（build_eval / check_index 只关心这些）
RETRIEVAL_TABLES: tuple[str, ...] = ("documents", "chunks")

#: 结构化知识表（M4 的槽位通道查这些）
STRUCTURED_TABLES: tuple[str, ...] = ("capability", "pricing", "leadtime")

EXPECTED_TABLES: tuple[str, ...] = tuple(name for name, _ in DDL)

# 初始元数据（幂等插入）
SEED_META: tuple[tuple[str, str], ...] = (("kb_version", "1"),)


def statements() -> list[str]:
    """返回按顺序执行的 DDL 语句列表。"""
    return [sql for _, sql in DDL]


def seed_meta_statements() -> list[tuple[str, str]]:
    """返回需要幂等写入的 app_meta 初始键值。"""
    return list(SEED_META)
