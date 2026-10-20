"""读取 ``data/seeds/docs_manifest.yaml`` —— 文档清单。

清单是 ``doc_id`` 的唯一登记处：入库时按文件路径查表得到 ``doc_id``，
查不到就报错，不允许从文件名自动推导（文件名会变，id 不该变）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import re
import yaml

from glassdesk.config import PROJECT_ROOT

DEFAULT_MANIFEST = PROJECT_ROOT / "data" / "seeds" / "docs_manifest.yaml"

#: 切分策略。决定 chunker 怎么处理这份文档。
STRATEGIES = ("manual", "section", "entry", "record", "table")


@dataclass(frozen=True, slots=True)
class DocSpec:
    doc_id: str
    title: str
    authority: str          # standard | manual | experience
    kind: str               # document | table | dataset
    file: str               # 相对项目根的路径，可以是目录
    strategy: str           # 见 STRATEGIES
    effective_from: date
    effective_to: date | None = None

    @property
    def path(self) -> Path:
        p = Path(self.file)
        return p if p.is_absolute() else PROJECT_ROOT / p


class ManifestError(RuntimeError):
    """清单缺失或内容不合法。"""


def load_manifest(path: str | Path | None = None) -> dict[str, DocSpec]:
    """返回 ``{doc_id: DocSpec}``。任何不合法的字段都直接报错，不静默跳过。"""
    manifest_path = Path(path) if path else DEFAULT_MANIFEST
    if not manifest_path.is_file():
        raise ManifestError(f"找不到文档清单：{manifest_path}")

    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    entries = (raw or {}).get("documents")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"清单里没有 documents：{manifest_path}")

    specs: dict[str, DocSpec] = {}
    for index, item in enumerate(entries, start=1):
        missing = {"doc_id", "title", "authority", "kind", "file", "strategy"} - set(item)
        if missing:
            raise ManifestError(f"第 {index} 条缺字段：{sorted(missing)}")

        doc_id = item["doc_id"]
        if doc_id in specs:
            raise ManifestError(f"doc_id 重复：{doc_id}")
        if item["strategy"] not in STRATEGIES:
            raise ManifestError(f"{doc_id} 的 strategy 非法：{item['strategy']}")
        if item["authority"] not in ("standard", "manual", "experience"):
            raise ManifestError(f"{doc_id} 的 authority 非法：{item['authority']}")
        if re.search(r"(_v\d+|version)", Path(item["file"]).name, re.IGNORECASE):
            raise ManifestError(f"{doc_id} 的文件名不允许带版本号：{item['file']}")

        specs[doc_id] = DocSpec(
            doc_id=doc_id,
            title=item["title"],
            authority=item["authority"],
            kind=item["kind"],
            file=item["file"],
            strategy=item["strategy"],
            effective_from=_as_date(item.get("effective_from"), doc_id),
            effective_to=_as_date(item.get("effective_to"), doc_id) if item.get("effective_to") else None,
        )

    return specs


def _as_date(value: object, doc_id: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ManifestError(f"{doc_id} 的 effective_from 不是 ISO 日期：{value}") from exc
    raise ManifestError(f"{doc_id} 缺 effective_from")


def find_by_file(specs: dict[str, DocSpec], file_path: str | Path) -> DocSpec:
    """按文件路径反查清单条目，支持同一 doc 覆盖多个文件。"""
    target = Path(file_path).resolve()
    for spec in specs.values():
        if spec.path.resolve() == target:
            return spec
    raise ManifestError(
        f"文件未在清单中登记：{file_path}；请先在 data/seeds/docs_manifest.yaml 添加条目"
    )
