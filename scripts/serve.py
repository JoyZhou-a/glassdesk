"""启动 Web 服务。

用法::

    .venv\\Scripts\\python.exe scripts/serve.py
    .venv\\Scripts\\python.exe scripts/serve.py --port 8010 --reload

启动后打开 http://127.0.0.1:8000

注意：首次启动会加载 bge-m3 和重排模型（约 5 秒），之后常驻内存。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 GlassDesk Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（调试用）")
    args = parser.parse_args()

    import uvicorn

    print(f"GlassDesk → http://{args.host}:{args.port}")
    uvicorn.run(
        "glassdesk.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
