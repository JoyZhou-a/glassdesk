"""数据契约（DDL）的测试。

单元测试只检查 DDL 文本本身，不连数据库；
落库验证放在 ``@pytest.mark.integration`` 里，需要 ``-m integration`` 才跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glassdesk.ingest import schema
from glassdesk.storage.mysql import safe_ident


# ======================================================================
# 单元测试：DDL 文本
# ======================================================================
def test_ddl_has_four_tables_in_dependency_order():
    assert schema.EXPECTED_TABLES == (
        "documents", "chunks", "ingest_runs", "app_meta",
        "capability", "pricing", "leadtime",
    )
    assert len(schema.statements()) == 7


def test_retrieval_and_structured_tables_are_separated():
    """两类表职责不同：向量检索用 documents/chunks，槽位通道用三张结构化表。

    分开的意义：尺寸类问题在向量空间里分不开（M2 实测 Recall@5 只有 70%），
    必须走结构化查询；混在一起会让人误以为 chunks 就够了。
    """
    assert schema.RETRIEVAL_TABLES == ("documents", "chunks")
    assert schema.STRUCTURED_TABLES == ("capability", "pricing", "leadtime")
    assert set(schema.RETRIEVAL_TABLES).isdisjoint(schema.STRUCTURED_TABLES)


def test_price_table_has_cost_price_but_sql_never_selects_it():
    """成本价要入库（用于验证脱敏是否生效），但**查询 SQL 里绝不能出现**。

    只检查 SQL 语句块（三引号内的内容），不检查注释——注释里当然会提到这个词。
    """
    import re

    assert "cost_price_aud_m2" in schema.PRICING

    source = (Path(__file__).resolve().parents[1]
              / "src" / "glassdesk" / "routing" / "structured.py").read_text(encoding="utf-8")
    sql_blocks = re.findall(r'"""(.*?)"""', source, re.S)
    # 只挑真正的 SQL：以 SELECT 开头。模块说明也是三引号，不能用它评判。
    selects = [b.strip() for b in sql_blocks if b.strip().upper().startswith("SELECT")]
    assert selects, "没找到任何 SELECT 语句，测试本身失效了"
    for block in selects:
        assert "cost_price" not in block.lower(), f"SQL 里出现了成本价：{block[:120]}"


def test_all_ddl_are_idempotent():
    for sql in schema.statements():
        assert "CREATE TABLE IF NOT EXISTS" in sql, sql[:80]


def test_documents_uses_composite_primary_key():
    """doc_id 单独不唯一——同一份文档有多个版本。"""
    assert "PRIMARY KEY (doc_id, version)" in schema.DOCUMENTS


def test_chunks_has_identity_and_state_columns():
    for column in ("chunk_id", "doc_id", "version", "kind", "block_kind",
                   "parent_id", "ordinal", "content_hash", "status"):
        assert column in schema.CHUNKS, f"chunks 缺列 {column}"


def test_chunks_supports_block_kind_for_table_rows():
    """表格行要能和普通段落区分开，检索阶段处理方式不同。"""
    assert "block_kind" in schema.CHUNKS
    assert "table_row" in schema.CHUNKS


def test_ingest_runs_records_kb_version_around():
    for column in ("kb_version_before", "kb_version_after", "regression_passed", "status"):
        assert column in schema.INGEST_RUNS


def test_seed_meta_contains_kb_version():
    keys = dict(schema.seed_meta_statements())
    assert keys["kb_version"] == "1"


def test_authority_enum_matches_constants():
    for value in ("standard", "manual", "experience"):
        assert value in schema.DOCUMENTS


# ======================================================================
# 标识符白名单
# ======================================================================
def test_safe_ident_accepts_normal_names():
    assert safe_ident("glassdesk") == "glassdesk"
    assert safe_ident("glassdesk_test") == "glassdesk_test"
    assert safe_ident("T123") == "T123"


@pytest.mark.parametrize("bad", ["", "a-b", "a b", "a;DROP", "a`b", "x" * 65, "库名"])
def test_safe_ident_rejects_dangerous_names(bad):
    with pytest.raises(ValueError):
        safe_ident(bad)


# ======================================================================
# 集成测试：真正把表建到 glassdesk_test
# ======================================================================
@pytest.fixture(scope="module")
def test_db_settings():
    from glassdesk.config import PROJECT_ROOT, load_settings

    if not (PROJECT_ROOT / ".env").is_file():
        pytest.skip("项目根目录缺少 .env")
    return load_settings()


@pytest.mark.integration
def test_schema_applies_to_test_database(test_db_settings):
    from glassdesk.storage import MySQLStore

    cfg = test_db_settings.services.mysql
    MySQLStore.create_database(cfg, cfg.database_test)

    store = MySQLStore(cfg, database=cfg.database_test)
    try:
        store.executescript(schema.statements())
        present = set(store.list_tables())
        missing = [t for t in schema.EXPECTED_TABLES if t not in present]
        assert missing == [], f"缺少表：{missing}"
    finally:
        store.close()


@pytest.mark.integration
def test_schema_is_idempotent_on_real_db(test_db_settings):
    """重复执行不应报错，也不应清空已有数据。"""
    from glassdesk.storage import MySQLStore

    cfg = test_db_settings.services.mysql
    store = MySQLStore(cfg, database=cfg.database_test)
    try:
        store.execute(
            "INSERT IGNORE INTO app_meta (k, v, updated_at) VALUES ('probe', '1', NOW())"
        )
        store.connect().commit()
        store.executescript(schema.statements())  # 再跑一遍
        row = store.fetchone("SELECT v FROM app_meta WHERE k = 'probe'")
        assert row is not None and row["v"] == "1"
        store.execute("DELETE FROM app_meta WHERE k = 'probe'")
        store.connect().commit()
    finally:
        store.close()
