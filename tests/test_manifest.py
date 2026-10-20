"""文档清单的测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glassdesk.ingest.manifest import (
    STRATEGIES,
    ManifestError,
    find_by_file,
    load_manifest,
)

VALID_ENTRY = {
    "doc_id": "manual-x",
    "title": "X Manual",
    "authority": "manual",
    "kind": "document",
    "strategy": "manual",
    "file": "data/raw/manuals/x.docx",
    "effective_from": "2026-01-01",
}


def write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "docs_manifest.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "documents": entries},
                                   allow_unicode=True), encoding="utf-8")
    return path


# ======================================================================
# 真实清单
# ======================================================================
def test_real_manifest_loads():
    specs = load_manifest()
    assert len(specs) == 9
    assert "manual-tempering" in specs
    assert "capability-matrix" in specs


def test_real_manifest_strategies_are_known():
    for spec in load_manifest().values():
        assert spec.strategy in STRATEGIES, spec.doc_id


def test_real_manifest_files_exist():
    """data/raw 由 build_corpus.py 生成，未生成时跳过。"""
    specs = load_manifest()
    if not any(s.path.exists() for s in specs.values()):
        pytest.skip("data/raw 尚未生成，先运行 scripts/build_corpus.py")
    missing = [s.doc_id for s in specs.values() if not s.path.exists()]
    assert missing == [], f"清单登记了但文件不存在：{missing}"


def test_manifest_maps_table_docs_to_table_strategy():
    specs = load_manifest()
    for doc_id in ("capability-matrix", "price-list", "lead-time"):
        assert specs[doc_id].strategy == "table"


# ======================================================================
# 校验规则
# ======================================================================
def test_missing_field_raises(tmp_path: Path):
    entry = dict(VALID_ENTRY)
    del entry["strategy"]
    with pytest.raises(ManifestError, match="缺字段"):
        load_manifest(write_manifest(tmp_path, [entry]))


def test_unknown_strategy_raises(tmp_path: Path):
    entry = {**VALID_ENTRY, "strategy": "magic"}
    with pytest.raises(ManifestError, match="strategy 非法"):
        load_manifest(write_manifest(tmp_path, [entry]))


def test_unknown_authority_raises(tmp_path: Path):
    entry = {**VALID_ENTRY, "authority": "official"}
    with pytest.raises(ManifestError, match="authority 非法"):
        load_manifest(write_manifest(tmp_path, [entry]))


def test_duplicate_doc_id_raises(tmp_path: Path):
    with pytest.raises(ManifestError, match="doc_id 重复"):
        load_manifest(write_manifest(tmp_path, [VALID_ENTRY, dict(VALID_ENTRY)]))


def test_filename_with_version_is_rejected(tmp_path: Path):
    entry = {**VALID_ENTRY, "file": "data/raw/manuals/x_v2.docx"}
    with pytest.raises(ManifestError, match="不允许带版本号"):
        load_manifest(write_manifest(tmp_path, [entry]))


def test_bad_date_raises(tmp_path: Path):
    entry = {**VALID_ENTRY, "effective_from": "01/01/2026"}
    with pytest.raises(ManifestError, match="不是 ISO 日期"):
        load_manifest(write_manifest(tmp_path, [entry]))


def test_missing_manifest_raises(tmp_path: Path):
    with pytest.raises(ManifestError, match="找不到文档清单"):
        load_manifest(tmp_path / "nope.yaml")


# ======================================================================
# 按文件反查
# ======================================================================
def test_find_by_file_returns_spec(tmp_path: Path):
    specs = load_manifest(write_manifest(tmp_path, [VALID_ENTRY]))
    found = find_by_file(specs, "data/raw/manuals/x.docx")
    assert found.doc_id == "manual-x"


def test_find_by_file_rejects_unregistered(tmp_path: Path):
    specs = load_manifest(write_manifest(tmp_path, [VALID_ENTRY]))
    with pytest.raises(ManifestError, match="未在清单中登记"):
        find_by_file(specs, "data/raw/manuals/other.docx")
