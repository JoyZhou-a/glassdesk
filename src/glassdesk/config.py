"""配置加载：config.yaml（非密钥） + .env（密钥）。

设计约束（见 CLAUDE.md §6）：
1. 禁止绝对路径硬编码在代码里——模型路径等一律来自 config.yaml。
2. 密钥只从环境 / .env 读取，绝不写进 config.yaml，也绝不打印到日志。
3. 缺密钥时**立刻报错并说明缺哪个**，不允许用默认值静默兜底。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

_REQUIRED_ENV_KEYS = ("MYSQL_PASSWORD", "REDIS_PASSWORD", "DEEPSEEK_API_KEY")


class ConfigError(RuntimeError):
    """配置缺失或格式错误。"""


# ----------------------------------------------------------------------
# 分段配置模型
# ----------------------------------------------------------------------
class AppConfig(BaseModel):
    name: str = "GlassDesk"
    env: str = "dev"
    timezone: str = "Australia/Sydney"


class PathsConfig(BaseModel):
    models: Path
    data: Path = Path("data")
    logs: Path = Path("logs")
    outputs: Path = Path("outputs")

    def resolve(self, root: Path) -> "PathsConfig":
        """相对路径按项目根目录展开，绝对路径原样保留。"""
        return PathsConfig(
            models=self.models if self.models.is_absolute() else (root / self.models),
            data=self.data if self.data.is_absolute() else (root / self.data),
            logs=self.logs if self.logs.is_absolute() else (root / self.logs),
            outputs=self.outputs if self.outputs.is_absolute() else (root / self.outputs),
        )


_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")
_WEIGHT_INDEX_GLOBS = ("*.safetensors.index.json", "*.bin.index.json")


def _is_complete_model_dir(path: Path) -> bool:
    """判断目录里是否真的有可用的模型。

    只判断 `is_dir()` 会产生**误报**：ModelScope/HF 下载过程中会先建目录、
    写出 config.json，权重则以 `.incomplete` 存在。必须在下载中途就识别出来，
    否则会在真正加载模型时才炸。
    """
    if not path.is_dir():
        return False
    if not (path / "config.json").is_file():
        return False
    for name in _WEIGHT_FILES:
        weight = path / name
        if weight.is_file() and weight.stat().st_size > 0:
            return True
    return any(next(path.glob(pattern), None) is not None for pattern in _WEIGHT_INDEX_GLOBS)


class ModelsConfig(BaseModel):
    embedder: Path
    reranker: Path
    intent_backbone: Path

    def missing(self) -> list[str]:
        """返回不完整或不存在的模型目录列表（空列表表示都可用）。"""
        return [
            str(p)
            for p in (self.embedder, self.reranker, self.intent_backbone)
            if not _is_complete_model_dir(p)
        ]


class MilvusConfig(BaseModel):
    host: str
    port: int = 19530
    database: str
    database_test: str
    collection: str = "chunks"


class MySQLConfig(BaseModel):
    host: str
    port: int = 3306
    user: str
    database: str
    database_test: str
    charset: str = "utf8mb4"
    password: SecretStr


class RedisConfig(BaseModel):
    host: str
    port: int = 6379
    db: int = 0
    db_test: int = 15
    password: SecretStr


class ServicesConfig(BaseModel):
    milvus: MilvusConfig
    mysql: MySQLConfig
    redis: RedisConfig


class LLMConfig(BaseModel):
    provider: str = "deepseek"
    base_url: str
    model: str
    api_key: SecretStr
    temperature: float = 0.1
    max_tokens: int = 1024
    timeout_s: int = 60
    max_retries: int = 2


class HybridConfig(BaseModel):
    dense_weight: float = 0.7
    sparse_weight: float = 0.3


class RetrievalConfig(BaseModel):
    parent_chunk_size: int = 1200
    child_chunk_size: int = 300
    chunk_overlap: int = 50
    top_k: int = 20
    rerank_candidates: int = 20
    final_k: int = 5
    hybrid: HybridConfig = Field(default_factory=HybridConfig)
    min_rerank_score: float = 0.35


class AnswerConfig(BaseModel):
    phone_max_lines: int = 3
    phone_max_sentence_chars: int = 60
    low_confidence_threshold: float = 0.6
    force_human_intents: list[str] = Field(default_factory=list)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/app.log"
    rotation: str = "10 MB"
    retention: str = "14 days"


class Settings(BaseModel):
    """完整的运行时配置。"""

    app: AppConfig
    paths: PathsConfig
    models: ModelsConfig
    services: ServicesConfig
    llm: LLMConfig
    retrieval: RetrievalConfig
    answer: AnswerConfig
    logging: LoggingConfig

    root: Path = PROJECT_ROOT

    @property
    def log_file(self) -> Path:
        return self.root / self.logging.file


# ----------------------------------------------------------------------
# 加载
# ----------------------------------------------------------------------
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件内容不是映射（dict）：{path}")
    return data


def _read_env(path: Path) -> dict[str, str]:
    """读取 .env，优先级低于真实环境变量（便于 CI / 临时覆盖）。"""
    values: dict[str, str] = {}
    if path.is_file():
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    for key in _REQUIRED_ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def _require(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(
            f"缺少环境变量 {key}。请在项目根目录的 .env 中设置（可参考 .env.example）。"
        )
    return value


def _inject_secrets(raw: dict[str, Any], env: dict[str, str]) -> None:
    """把密钥从 env 注入到配置树的对应位置。"""
    try:
        raw["services"]["mysql"]["password"] = _require(env, "MYSQL_PASSWORD")
        raw["services"]["redis"]["password"] = _require(env, "REDIS_PASSWORD")
        raw["llm"]["api_key"] = _require(env, "DEEPSEEK_API_KEY")
    except KeyError as exc:
        raise ConfigError(f"config.yaml 结构缺少必需的段：{exc}") from exc


def load_settings(
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    root: str | Path | None = None,
) -> Settings:
    """加载配置。测试可传入临时 yaml / env 路径。"""
    root_path = Path(root) if root else PROJECT_ROOT
    cfg_path = Path(config_path) if config_path else root_path / "config.yaml"
    env_file = Path(env_path) if env_path else root_path / ".env"

    raw = _read_yaml(cfg_path)
    env = _read_env(env_file)
    _inject_secrets(raw, env)

    try:
        settings = Settings.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"配置校验失败：{exc}") from exc

    settings.root = root_path
    settings.paths = settings.paths.resolve(root_path)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中如需隔离，请直接调用 load_settings()。"""
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
