"""MySQL 的最小封装：连接、事务、参数化执行。

设计取舍：
- 只做 pymysql 的薄封装，不引入 ORM。表结构简单，手写 SQL 更透明、更好排查。
- 所有查询强制走参数化（``%s``），不拼字符串。
- 事务用上下文管理器，失败自动回滚。
- 日志只写 ``host:port/db``，绝不输出密码。
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, Iterator, Sequence

import pymysql
from pymysql.cursors import DictCursor

from glassdesk.config import MySQLConfig
from glassdesk.logger import get_logger

log = get_logger("storage.mysql")

# 库名/表名无法用 %s 参数化，只能白名单校验后再拼接
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def safe_ident(name: str) -> str:
    """校验库名/表名，只允许字母、数字、下划线。

    标识符不能参数化，所以必须先校验再拼进 SQL，避免注入。
    """
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"非法的标识符：{name!r}（只允许字母、数字、下划线，≤64 字符）")
    return name


class MySQLStore:
    """一个连接 + 一组执行方法。不设计连接池：单实例、低并发，够用。"""

    def __init__(self, cfg: MySQLConfig, *, database: str | None = None) -> None:
        self._cfg = cfg
        # database=None 表示不选库（用于 CREATE DATABASE 这类操作）
        self._database = database
        self._conn: pymysql.connections.Connection | None = None

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------
    @property
    def database(self) -> str | None:
        return self._database

    def connect(self) -> pymysql.connections.Connection:
        if self._conn is not None:
            return self._conn

        password = self._cfg.password.get_secret_value()
        self._conn = pymysql.connect(
            host=self._cfg.host,
            port=self._cfg.port,
            user=self._cfg.user,
            password=password,
            database=self._database,
            charset=self._cfg.charset,
            autocommit=False,
            cursorclass=DictCursor,
        )
        log.info(
            "MySQL 已连接 {}:{}/{}",
            self._cfg.host,
            self._cfg.port,
            self._database or "(无库)",
        )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            log.info("MySQL 连接已关闭")

    def __enter__(self) -> "MySQLStore":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 事务
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator[pymysql.connections.Connection]:
        """成功提交，异常回滚。"""
        conn = self.connect()
        try:
            yield conn
        except Exception:
            conn.rollback()
            log.warning("事务回滚")
            raise
        else:
            conn.commit()

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """执行一条语句，返回受影响行数。需自行 commit 或包在 transaction 里。"""
        conn = self.connect()
        with conn.cursor() as cur:
            return cur.execute(sql, params)

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> int:
        conn = self.connect()
        with conn.cursor() as cur:
            return cur.executemany(sql, seq)

    def executescript(self, statements: Sequence[str]) -> None:
        """按顺序执行一组 DDL（不可回滚，DDL 在 MySQL 中隐式提交）。"""
        conn = self.connect()
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
        conn.commit()

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    def list_tables(self) -> list[str]:
        rows = self.fetchall("SHOW TABLES")
        return sorted(next(iter(row.values())) for row in rows)

    def table_exists(self, name: str) -> bool:
        row = self.fetchone(
            """
            SELECT COUNT(*) AS n
            FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = %s
            """,
            (name,),
        )
        return bool(row and row["n"])

    # ------------------------------------------------------------------
    # 建库
    # ------------------------------------------------------------------
    @classmethod
    def create_database(cls, cfg: MySQLConfig, name: str) -> bool:
        """幂等建库。返回 True 表示本次新建。"""
        name = safe_ident(name)
        store = cls(cfg, database=None)
        try:
            row = store.fetchone(
                "SELECT SCHEMA_NAME FROM information_schema.schemata WHERE SCHEMA_NAME = %s",
                (name,),
            )
            if row:
                log.info("库 {} 已存在，跳过", name)
                return False
            store.execute(
                f"CREATE DATABASE `{name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            store.connect().commit()
            log.info("库 {} 已创建", name)
            return True
        finally:
            store.close()
