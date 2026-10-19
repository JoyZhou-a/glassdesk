"""日志模块测试：必须同时写控制台与文件。"""

from __future__ import annotations

from glassdesk.logger import get_logger, setup_logging


def test_setup_writes_to_file(settings):
    path = setup_logging(settings)
    assert path is not None and path.exists()

    get_logger("test.logger").info("hello-glassdesk")
    content = path.read_text(encoding="utf-8")
    assert "hello-glassdesk" in content
    assert "test.logger" in content


def test_log_file_is_utf8_and_supports_chinese(settings):
    path = setup_logging(settings)
    get_logger("test.logger").warning("中文日志：钢化炉上限 2440×5100")
    content = path.read_text(encoding="utf-8")
    assert "钢化炉上限" in content


def test_get_logger_without_setup_does_not_crash():
    logger = get_logger()
    logger.info("no-setup")
