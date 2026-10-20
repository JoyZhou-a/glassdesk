"""数据契约（DDL）的测试。

单元测试只检查 DDL 文本本身，不连数据库；
落库验证放在 ``@pytest.mark.integration`` 里，需要 ``-m integration`` 才跑。
"""

from __future__ import annotations

import pytest

from glassdesk.ingest import schema
from glassdesk.storage.mysql import safe_ident


# ======================================================================
# 单元测试：DDL 文本
# ======================================================================
def test_ddl_has_four_tables_in_dependency_order():
    assert schema.EXPECTED_TABLES == ("documents", "chunks", "ingest_runs", "app_meta")
    assert len(schema.statements()) == 4


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
