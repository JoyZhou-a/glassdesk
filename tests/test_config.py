"""配置加载的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from glassdesk.config import ConfigError, load_settings, reset_settings_cache


def test_secrets_are_injected_from_env(settings):
    assert settings.services.mysql.password.get_secret_value() == "test-mysql-pw"
    assert settings.services.redis.password.get_secret_value() == "test-redis-pw"
    assert settings.llm.api_key.get_secret_value() == "test-deepseek-key"


def test_env_password_overrides_yaml(project: Path):
    """即使 yaml 里被误写了 password，也以 env 为准。"""
    cfg = (project / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("    charset: utf8mb4", "    charset: utf8mb4\n    password: leaked-in-yaml")
    (project / "config.yaml").write_text(cfg, encoding="utf-8")

    settings = load_settings(root=project)
    assert settings.services.mysql.password.get_secret_value() == "test-mysql-pw"


def test_missing_env_key_raises_with_key_name(project: Path):
    (project / ".env").write_text("MYSQL_PASSWORD=x\nREDIS_PASSWORD=y\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(root=project)
    assert "DEEPSEEK_API_KEY" in str(exc.value)


def test_missing_config_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        load_settings(config_path=tmp_path / "nope.yaml", env_path=tmp_path / ".env", root=tmp_path)
    assert "找不到配置文件" in str(exc.value)


def test_relative_paths_resolved_against_root(project: Path, settings):
    assert settings.paths.data == project / "data"
    assert settings.paths.logs == project / "logs"
    assert settings.paths.outputs == project / "outputs"
    assert settings.log_file == project / "logs" / "app.log"


def test_absolute_model_paths_preserved(settings):
    assert settings.models.embedder == Path("E:/models/bge-m3")


def test_models_missing_reports_absent_dirs(tmp_path: Path):
    """自包含：不依赖真实磁盘上 E:/models 是否存在（那属于集成范畴）。"""
    from glassdesk.config import ModelsConfig

    models = ModelsConfig(
        embedder=tmp_path / "a",
        reranker=tmp_path / "b",
        intent_backbone=tmp_path / "c",
    )
    assert len(models.missing()) == 3

    # 建了目录但没权重（模拟下载中途）——仍应判定为缺失
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.json").write_text("{}", encoding="utf-8")
    assert len(models.missing()) == 3

    # 只有 config.json + 0 字节权重，仍应判定为缺失
    (tmp_path / "a" / "pytorch_model.bin").write_bytes(b"")
    assert len(models.missing()) == 3

    # 有了非空权重才算完整
    (tmp_path / "a" / "pytorch_model.bin").write_bytes(b"x" * 1024)
    assert [Path(m).name for m in models.missing()] == ["b", "c"]

    # safetensors 形式同样认可
    (tmp_path / "b" / "model.safetensors").write_bytes(b"x" * 1024)
    assert [Path(m).name for m in models.missing()] == ["c"]


def test_models_missing_accepts_sharded_index(tmp_path: Path):
    from glassdesk.config import ModelsConfig

    d = tmp_path / "sharded"
    d.mkdir()
    (d / "config.json").write_text("{}", encoding="utf-8")
    (d / "model.safetensors.index.json").write_text("{}", encoding="utf-8")
    models = ModelsConfig(embedder=d, reranker=d, intent_backbone=d)
    assert models.missing() == []


def test_real_project_config_loads():
    """真实 config.yaml + .env 能被加载（只读本地文件，不联网）。"""
    from glassdesk.config import PROJECT_ROOT

    if not (PROJECT_ROOT / ".env").is_file():
        pytest.skip("项目根目录缺少 .env")

    real = load_settings()
    assert real.services.milvus.host
    assert real.services.mysql.password.get_secret_value()
    assert real.llm.model


def test_secret_is_masked_in_repr(settings):
    text = repr(settings)
    assert "test-mysql-pw" not in text
    assert "test-deepseek-key" not in text
    assert "test-redis-pw" not in text


def test_settings_are_cached():
    reset_settings_cache()
    from glassdesk.config import get_settings

    try:
        assert get_settings() is get_settings()
    finally:
        reset_settings_cache()
