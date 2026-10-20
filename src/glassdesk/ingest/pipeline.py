"""入库管线：prepare（切分落库为 draft）→ activate（切成 active）。

只做 M1a 的范围：**不写向量**。Milvus 索引在 M1b 补。

状态机（详见 ``docs/09_知识库更新.md`` §5）::

    draft --activate--> active
      ^                   |
      |                   v
      +------ superseded <+

M1a 简化了 09 文档的五步流程（prepare/verify/activate/regress/commit），
只实现 prepare 与 activate —— verify 的断言已内联在 prepare 里，
regress 需要检索能力，属 M1c。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from glassdesk.ingest.chunker import ChunkConfig, chunk_blocks
from glassdesk.ingest.loaders import SUPPORTED_SUFFIXES, load_file
from glassdesk.ingest.manifest import DocSpec, load_manifest
from glassdesk.ingest.models import Block, Chunk, BlockKind
from glassdesk.logger import get_logger
from glassdesk.storage import MySQLStore

log = get_logger("ingest.pipeline")


class IngestError(RuntimeError):
    """入库过程中的可预期错误（文件缺失、需要 OCR 等）。"""


@dataclass(slots=True)
class PrepareResult:
    doc_id: str
    version: int
    file_count: int = 0
    block_count: int = 0
    parent_count: int = 0
    child_count: int = 0
    needs_ocr: bool = False
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def summary(self) -> str:
        return (
            f"{self.doc_id} v{self.version}: "
            f"{self.file_count} 文件 / {self.block_count} 块 / "
            f"父块 {self.parent_count} / 子块 {self.child_count}"
        )


# ======================================================================
# 读取原文
# ======================================================================
def _files_of(spec: DocSpec) -> list[Path]:
    path = spec.path
    if path.is_dir():
        files = sorted(
            p for p in path.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            raise IngestError(f"{spec.doc_id} 的目录里没有可解析文件：{path}")
        return files
    if not path.is_file():
        raise IngestError(f"{spec.doc_id} 的文件不存在：{path}（先运行 build_corpus.py）")
    return [path]


def read_blocks(spec: DocSpec) -> tuple[list[Block], int, bool]:
    """把文档的（可能多个）文件读成统一 Block 列表。

    目录型文档（标准摘要）会把文件名写进 ``clause``，保证出处可追溯。
    """
    files = _files_of(spec)
    blocks: list[Block] = []
    needs_ocr = False

    for file_path in files:
        result = load_file(file_path)
        needs_ocr = needs_ocr or result.needs_ocr
        if spec.path.is_dir():
            stem = file_path.stem.upper().replace("-", "/").replace("AS", "AS ", 1)
            for block in result.blocks:
                tag = stem if not block.clause else f"{stem} / {block.clause}"
                blocks.append(
                    Block(
                        text=block.text,
                        kind=block.kind,
                        page_no=block.page_no,
                        clause=tag[:64],
                        level=block.level,
                        section_path=block.section_path,
                    )
                )
        else:
            blocks.extend(result.blocks)

    return blocks, len(files), needs_ocr


def source_hash(spec: DocSpec) -> str:
    """整份文档指纹：所有源文件字节的 sha256。文件没变就不必重建。"""
    import hashlib

    digest = hashlib.sha256()
    for file_path in _files_of(spec):
        digest.update(file_path.name.encode("utf-8"))
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


# ======================================================================
# prepare：切分 + 写 draft
# ======================================================================
def prepare_document(
    store: MySQLStore,
    spec: DocSpec,
    *,
    version: int = 1,
    cfg: ChunkConfig | None = None,
    write: bool = True,
    force: bool = False,
) -> PrepareResult:
    config = cfg or ChunkConfig()
    blocks, file_count, needs_ocr = read_blocks(spec)

    result = PrepareResult(
        doc_id=spec.doc_id,
        version=version,
        file_count=file_count,
        block_count=len(blocks),
        needs_ocr=needs_ocr,
    )

    if needs_ocr:
        raise IngestError(
            f"{spec.doc_id} 是扫描件，需要 OCR；M1a 阶段不处理（见 09 文档 M1c）"
        )
    if not blocks:
        raise IngestError(f"{spec.doc_id} 解析出 0 个块，检查源文件")

    chunks = chunk_blocks(
        blocks, doc_id=spec.doc_id, version=version, strategy=spec.strategy,
        cfg=config, default_clause=spec.title,
    )
    result.chunks = chunks
    result.parent_count = sum(1 for c in chunks if c.kind == "parent")
    result.child_count = sum(1 for c in chunks if c.kind == "child")

    # 内联 verify：结构断言，不通过就不写库
    _verify(result)

    if write:
        _guard_version(store, spec.doc_id, version, force=force)
        _write_draft(store, spec, result, config, force=force)
        log.info("prepare 完成 {}", result.summary())
    return result


def _guard_version(store: MySQLStore, doc_id: str, version: int, *, force: bool) -> None:
    """已 active 的版本不允许被静默重写。

    重写会把 chunks 打回 draft，而 documents 仍是 active —— 两者不一致，
    检索侧（M2）会看不到任何 chunk。要改内容必须升版本，或显式 --force。
    """
    row = store.fetchone(
        "SELECT status FROM documents WHERE doc_id=%s AND version=%s", (doc_id, version)
    )
    if row and row["status"] == "active" and not force:
        raise IngestError(
            f"{doc_id} v{version} 已处于 active 状态，不能覆盖。"
            f"请改用 --version {version + 1} 升版本，或加 --force 强制重建"
        )


def _verify(result: PrepareResult) -> None:
    child_ids = {c.chunk_id for c in result.chunks if c.kind == "child"}
    parent_ids = {c.chunk_id for c in result.chunks if c.kind == "parent"}
    if len(child_ids) != result.child_count or len(parent_ids) != result.parent_count:
        raise IngestError(f"{result.doc_id}: chunk_id 有重复，检查序号生成逻辑")
    for chunk in result.chunks:
        if chunk.kind == "child" and chunk.parent_id and chunk.parent_id not in parent_ids:
            raise IngestError(
                f"{result.doc_id}: 子块 {chunk.chunk_id} 指向不存在的父块 {chunk.parent_id}"
            )
    if result.child_count == 0:
        raise IngestError(f"{result.doc_id}: 没有产生任何子块")


def _write_draft(
    store: MySQLStore, spec: DocSpec, result: PrepareResult, cfg: ChunkConfig,
    *, force: bool = False,
) -> None:
    now = datetime.now().replace(microsecond=0)
    # --force 重建 active 版本时，chunk 直接写 active，避免状态错位
    chunk_status = "active" if store.fetchone(
        "SELECT status FROM documents WHERE doc_id=%s AND version=%s",
        (spec.doc_id, result.version),
    ) and force else "draft"
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                  (doc_id, version, title, authority, source_file, content_hash,
                   chunking_config_hash, effective_from, effective_to, status,
                   chunk_count, ingested_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s)
                ON DUPLICATE KEY UPDATE
                  title=VALUES(title), authority=VALUES(authority),
                  source_file=VALUES(source_file), content_hash=VALUES(content_hash),
                  chunking_config_hash=VALUES(chunking_config_hash),
                  effective_from=VALUES(effective_from), effective_to=VALUES(effective_to),
                  chunk_count=VALUES(chunk_count), ingested_at=VALUES(ingested_at)
                """,
                (
                    spec.doc_id, result.version, spec.title, spec.authority, spec.file,
                    source_hash(spec), cfg.fingerprint(),
                    spec.effective_from, spec.effective_to,
                    result.chunk_count, now,
                ),
            )
            # 同版本重跑：先清掉旧 chunk，保证幂等
            cur.execute(
                "DELETE FROM chunks WHERE doc_id=%s AND version=%s",
                (spec.doc_id, result.version),
            )
            cur.executemany(
                """
                INSERT INTO chunks
                  (chunk_id, doc_id, version, kind, block_kind, parent_id, ordinal,
                   text, content_hash, page_no, clause, status, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    (
                        c.chunk_id, c.doc_id, c.version, c.kind, c.block_kind,
                        c.parent_id, c.ordinal, c.text, c.content_hash,
                        c.page_no, c.clause, chunk_status, now,
                    )
                    for c in result.chunks
                ],
            )


# ======================================================================
# activate：draft → active，旧版本 superseded
# ======================================================================
def activate_document(store: MySQLStore, doc_id: str, version: int) -> dict[str, int]:
    now = datetime.now().replace(microsecond=0)
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM documents WHERE doc_id=%s AND version=%s",
                (doc_id, version),
            )
            row = cur.fetchone()
            if row is None:
                raise IngestError(f"{doc_id} v{version} 未 prepare，无法 activate")

            cur.execute(
                "UPDATE documents SET status='superseded' "
                "WHERE doc_id=%s AND version<>%s AND status='active'",
                (doc_id, version),
            )
            cur.execute(
                "UPDATE chunks SET status='superseded' "
                "WHERE doc_id=%s AND version<>%s AND status='active'",
                (doc_id, version),
            )
            cur.execute(
                "UPDATE documents SET status='active', activated_at=%s "
                "WHERE doc_id=%s AND version=%s",
                (now, doc_id, version),
            )
            active_chunks = cur.execute(
                "UPDATE chunks SET status='active' WHERE doc_id=%s AND version=%s",
                (doc_id, version),
            )

            cur.execute("SELECT v FROM app_meta WHERE k='kb_version'")
            kb = cur.fetchone()
            before = kb["v"] if kb else "0"
            after = str(int(before) + 1)
            cur.execute(
                "INSERT INTO app_meta (k, v, updated_at) VALUES ('kb_version', %s, %s) "
                "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at)",
                (after, now),
            )
            cur.execute(
                """
                INSERT INTO ingest_runs
                  (started_at, finished_at, doc_id, version, action,
                   kb_version_before, kb_version_after, chunks_added,
                   status, operator, note)
                VALUES (%s,%s,%s,%s,'add',%s,%s,%s,'success',%s,%s)
                """,
                (now, now, doc_id, version, before, after, active_chunks,
                 "cli", "M1a activate（未写向量）"),
            )

    log.info("activate 完成 {} v{}，kb_version {} → {}", doc_id, version, before, after)
    return {"chunks": active_chunks, "kb_version_before": int(before),
            "kb_version_after": int(after)}


# ======================================================================
# 批量
# ======================================================================
def prepare_all(store: MySQLStore, *, version: int = 1, write: bool = True) -> list[PrepareResult]:
    results: list[PrepareResult] = []
    for spec in load_manifest().values():
        results.append(prepare_document(store, spec, version=version, write=write))
    return results


# ======================================================================
# index：编码并写入 Milvus（M1b）
# ======================================================================
#: 只把 child chunk 写入向量库。父块不参与检索，只负责给模型提供上下文。
_ACTIVE_CHILD_SQL = """
SELECT chunk_id, text, page_no, clause
FROM chunks
WHERE doc_id = %s AND version = %s AND kind = 'child' AND status = 'active'
ORDER BY ordinal
"""


def index_document(
    store: MySQLStore,
    milvus,
    embedder,
    doc_id: str,
    *,
    version: int = 1,
    recreate: bool = False,
) -> dict[str, int]:
    """把某份文档的 active 子块编码并写入 Milvus。

    Milvus 只存 ``id + doc_id + authority + page_no + clause + 两种向量``，
    正文留在 MySQL —— 索引因此永远可以从 MySQL 重建。
    """
    doc = store.fetchone(
        "SELECT authority, status FROM documents WHERE doc_id=%s AND version=%s",
        (doc_id, version),
    )
    if doc is None:
        raise IngestError(f"{doc_id} v{version} 未入库，先执行 prepare + activate")
    if doc["status"] != "active":
        raise IngestError(
            f"{doc_id} v{version} 状态是 {doc['status']}，只有 active 才能建索引"
        )

    rows = store.fetchall(_ACTIVE_CHILD_SQL, (doc_id, version))
    if not rows:
        raise IngestError(f"{doc_id} v{version} 没有 active 子块，无法建索引")

    milvus.ensure_collection(embedder.dim, recreate=recreate)
    embeddings = embedder.encode_documents([r["text"] for r in rows])

    payload = []
    for row, vector in zip(rows, embeddings):
        payload.append(
            {
                "id": row["chunk_id"],
                "doc_id": doc_id,
                "authority": doc["authority"],
                "page_no": int(row["page_no"] or 0),
                "clause": (row["clause"] or "")[:64],
                "dense_vector": vector.dense,
                "sparse_vector": vector.sparse,
            }
        )

    written = milvus.upsert(payload)
    milvus.flush()
    log.info("index 完成 {} v{}：{} 条向量", doc_id, version, written)
    return {"chunks": len(rows), "vectors": written, "milvus_total": milvus.count()}


def index_all(store: MySQLStore, milvus, embedder, *, version: int = 1,
              recreate: bool = False) -> list[dict[str, int]]:
    results = []
    first = True
    for spec in load_manifest().values():
        try:
            results.append(
                index_document(
                    store, milvus, embedder, spec.doc_id,
                    version=version, recreate=recreate and first,
                )
            )
            first = False
        except IngestError as exc:
            log.warning("跳过 {}：{}", spec.doc_id, exc)
    return results
