"""入库 CLI。

用法：
    uv run python scripts/ingest.py list                  # 清单与文件状态
    uv run python scripts/ingest.py prepare --all         # 全部切分落库（draft）
    uv run python scripts/ingest.py prepare --doc-id manual-tempering --dry-run
    uv run python scripts/ingest.py dump --doc-id capability-matrix --limit 5
    uv run python scripts/ingest.py activate --all        # draft → active
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from glassdesk.config import ConfigError, load_settings  # noqa: E402
from glassdesk.ingest import pipeline  # noqa: E402
from glassdesk.ingest.manifest import ManifestError, load_manifest  # noqa: E402
from glassdesk.logger import setup_logging  # noqa: E402
from glassdesk.storage import MySQLStore  # noqa: E402


def cmd_list(_args) -> int:
    specs = load_manifest()
    print("=" * 96)
    print(f"{'doc_id':<24}{'strategy':<10}{'authority':<12}{'file'}")
    print("=" * 96)
    for spec in specs.values():
        exists = "OK" if spec.path.exists() else "MISSING"
        print(f"{spec.doc_id:<24}{spec.strategy:<10}{spec.authority:<12}{spec.file}  [{exists}]")
    return 0


def cmd_prepare(args, store) -> int:
    specs = load_manifest()
    targets = list(specs.values()) if args.all else [specs[args.doc_id]]
    failures = 0
    for spec in targets:
        try:
            result = pipeline.prepare_document(
                store, spec,
                version=args.version,
                write=not args.dry_run,
                force=getattr(args, "force", False),
            )
            print(f"  [OK]   {result.summary()}")
            if args.dry_run:
                for chunk in result.chunks[: args.limit]:
                    _print_chunk(chunk)
        except pipeline.IngestError as exc:
            failures += 1
            print(f"  [SKIP] {spec.doc_id}: {exc}")
    return 1 if failures and not args.all else 0


def cmd_dump(args, store) -> int:
    specs = load_manifest()
    spec = specs[args.doc_id]
    result = pipeline.prepare_document(store, spec, version=args.version, write=False)
    print(f"{result.summary()}\n")
    shown = 0
    for chunk in result.chunks:
        if args.kind and chunk.kind != args.kind:
            continue
        _print_chunk(chunk)
        shown += 1
        if shown >= args.limit:
            break
    return 0


def cmd_activate(args, store) -> int:
    specs = load_manifest()
    targets = list(specs.values()) if args.all else [specs[args.doc_id]]
    for spec in targets:
        try:
            stats = pipeline.activate_document(store, spec.doc_id, args.version)
            print(f"  [OK]   {spec.doc_id}: {stats}")
        except pipeline.IngestError as exc:
            print(f"  [SKIP] {spec.doc_id}: {exc}")
    return 0


def _print_chunk(chunk) -> None:
    text = chunk.text.replace("\n", " ⏎ ")
    if len(text) > 150:
        text = text[:150] + "…"
    parent = f" → {chunk.parent_id}" if chunk.parent_id else ""
    print(f"\n  [{chunk.kind:<6}|{chunk.block_kind:<9}] {chunk.chunk_id}{parent}")
    print(f"   {text}")
    meta = []
    if chunk.page_no is not None:
        meta.append(f"page={chunk.page_no}")
    if chunk.clause:
        meta.append(f"clause={chunk.clause}")
    if meta:
        print(f"   ({', '.join(meta)})")


def main() -> int:
    parser = argparse.ArgumentParser(description="GlassDesk 入库")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出清单与文件状态")

    p_prepare = sub.add_parser("prepare", help="切分并写 draft")
    group = p_prepare.add_mutually_exclusive_group(required=True)
    group.add_argument("--doc-id")
    group.add_argument("--all", action="store_true")
    p_prepare.add_argument("--version", type=int, default=1)
    p_prepare.add_argument("--force", action="store_true",
                           help="重建已 active 的版本（会原地覆盖）")
    p_prepare.add_argument("--dry-run", action="store_true", help="只切分不写库")
    p_prepare.add_argument("--limit", type=int, default=10, help="dry-run 时打印前 N 条")

    p_dump = sub.add_parser("dump", help="打印切分结果，供人工核对")
    p_dump.add_argument("--doc-id", required=True)
    p_dump.add_argument("--version", type=int, default=1)
    p_dump.add_argument("--kind", choices=["parent", "child"])
    p_dump.add_argument("--limit", type=int, default=10)

    p_activate = sub.add_parser("activate", help="draft 切为 active")
    group2 = p_activate.add_mutually_exclusive_group(required=True)
    group2.add_argument("--doc-id")
    group2.add_argument("--all", action="store_true")
    p_activate.add_argument("--version", type=int, default=1)

    args = parser.parse_args()

    if args.command == "list":
        return cmd_list(args)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(settings, file=True)

    store = MySQLStore(settings.services.mysql, database=settings.services.mysql.database)
    try:
        handler = {"prepare": cmd_prepare, "dump": cmd_dump, "activate": cmd_activate}
        return handler[args.command](args, store)
    except ManifestError as exc:
        print(f"清单错误：{exc}")
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
