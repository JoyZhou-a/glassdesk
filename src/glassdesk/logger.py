"""日志：控制台 + 文件双输出（CLAUDE.md §5 要求）。

约定：
- 业务代码一律 `from glassdesk.logger import get_logger`，不要直接 import loguru。
- 日志里**禁止**出现密钥；写连接串时只写 host:port。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from glassdesk.config import Settings

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{extra[module]}</cyan> | "
    "<level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {extra[module]} | "
    "{name}:{line} | {message}"
)

_configured = False
_default_file: Path | None = None


def setup_logging(settings: Settings, *, file: bool = True) -> Path | None:
    """按配置初始化日志。重复调用会重建 sink（幂等）。"""
    global _configured, _default_file

    logger.remove()
    level = settings.logging.level.upper()

    logger.add(
        sys.stderr,
        level=level,
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=False,
        diagnose=False,
    )

    path: Path | None = None
    if file:
        path = settings.log_file
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(path),
            level=level,
            format=_FILE_FORMAT,
            rotation=settings.logging.rotation,
            retention=settings.logging.retention,
            encoding="utf-8",
            enqueue=False,  # 测试里需要立刻落盘
            backtrace=True,
            diagnose=False,
        )
    _configured = True
    _default_file = path
    return path


def get_logger(module: str = "glassdesk"):
    """取带模块名的 logger。未初始化时自动挂一个控制台 sink，避免日志丢失。"""
    global _configured
    if not _configured:
        logger.remove()
        logger.add(sys.stderr, level="INFO", format=_CONSOLE_FORMAT, colorize=True)
        _configured = True
    return logger.bind(module=module)
