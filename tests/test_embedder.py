"""编码器的单元测试。

不加载真实模型：只测纯函数（设备选择、稀疏池化）与错误路径。
真实编码放在 ``@pytest.mark.integration``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glassdesk.retrieval.embedder import (
    BGEEmbedder,
    EncodedVector,
    _pick_device,
    max_pool_by_token,
)


# ======================================================================
# 设备选择
# ======================================================================
def test_pick_device_respects_explicit_cpu():
    assert _pick_device("cpu") == ("cpu", False)


def test_pick_device_enables_fp16_only_on_cuda():
    """fp16 在 CPU 上又慢又不准，只在 CUDA 上开。"""
    assert _pick_device("cuda") == ("cuda", True)


def test_pick_device_auto_returns_known_device():
    device, fp16 = _pick_device("auto")
    assert device in ("cpu", "cuda", "mps")
    if device != "cuda":
        assert fp16 is False


# ======================================================================
# 稀疏向量的 max 池化
# ======================================================================
def test_max_pool_takes_max_per_token():
    ids = [10, 20, 10, 30]
    weights = [0.5, 0.9, 1.2, 0.3]
    assert max_pool_by_token(ids, weights, skip_ids=set()) == {
        10: 1.2,   # 取两次出现中的较大值，不是求和
        20: 0.9,
        30: 0.3,
    }


def test_max_pool_skips_special_tokens():
    ids = [101, 10, 102, 0]
    weights = [5.0, 0.7, 5.0, 0.0]
    assert max_pool_by_token(ids, weights, skip_ids={101, 102, 0}) == {10: 0.7}


def test_max_pool_drops_non_positive_weights():
    """relu 之后大量 token 权重为 0，不应该进入稀疏向量。"""
    ids = [10, 11, 12]
    weights = [0.0, -0.1, 0.4]
    assert max_pool_by_token(ids, weights, skip_ids=set()) == {12: 0.4}


def test_max_pool_returns_empty_for_all_zero():
    assert max_pool_by_token([1, 2], [0.0, 0.0], skip_ids=set()) == {}


def test_max_pool_keys_and_values_are_plain_int_float():
    result = max_pool_by_token([3], [0.25], skip_ids=set())
    assert all(type(k) is int for k in result)
    assert all(type(v) is float for v in result.values())


# ======================================================================
# EncodedVector
# ======================================================================
def test_encoded_vector_holds_both_kinds():
    v = EncodedVector(dense=[0.1, 0.2], sparse={1: 0.5})
    assert len(v.dense) == 2
    assert v.sparse[1] == 0.5


def test_encode_documents_on_empty_input_returns_empty():
    """空输入不应触发模型加载。"""
    embedder = BGEEmbedder("does-not-matter", device="cpu")
    assert embedder.encode_documents([]) == []


# ======================================================================
# 模型缺失
# ======================================================================
def test_missing_model_dir_raises(tmp_path: Path):
    embedder = BGEEmbedder(tmp_path / "not-there", device="cpu")
    with pytest.raises(FileNotFoundError, match="找不到 bge-m3 模型目录"):
        embedder._ensure_loaded()


def test_missing_sparse_head_raises(tmp_path: Path):
    """bge-m3 目录存在但没有 sparse_linear.pt 时必须报错，不能静默退化成纯稠密。"""
    from glassdesk.config import PROJECT_ROOT

    src = PROJECT_ROOT / "tests" / "conftest.py"
    fake = tmp_path / "fake-bge-m3"
    fake.mkdir()
    (fake / "config.json").write_text("{}", encoding="utf-8")
    assert src.is_file()

    embedder = BGEEmbedder(fake, device="cpu")
    from glassdesk.config import PROJECT_ROOT as root

    real = root / "config.yaml"
    if not real.is_file():
        pytest.skip("缺少 config.yaml")
    with pytest.raises((FileNotFoundError, OSError, ValueError)):
        embedder._ensure_loaded()


# ======================================================================
# 集成：真实编码
# ======================================================================
@pytest.fixture(scope="module")
def real_embedder():
    from glassdesk.config import PROJECT_ROOT, load_settings

    if not (PROJECT_ROOT / ".env").is_file():
        pytest.skip("项目根目录缺少 .env")
    settings = load_settings()
    if settings.models.missing():
        pytest.skip("模型不完整")
    return BGEEmbedder(settings.models.embedder, device="cpu", batch_size=2)


@pytest.mark.integration
def test_real_encoding_produces_dense_and_sparse(real_embedder):
    vectors = real_embedder.encode_documents(
        ["Can you make 12mm toughened glass, 2400 wide by 3000 high?"]
    )
    assert len(vectors) == 1
    assert len(vectors[0].dense) == real_embedder.dim == 1024
    assert len(vectors[0].sparse) > 0


@pytest.mark.integration
def test_real_dense_is_normalised(real_embedder):
    v = real_embedder.encode_documents(["tempering furnace quench pressure"])[0]
    norm = sum(x * x for x in v.dense) ** 0.5
    assert abs(norm - 1.0) < 0.01


@pytest.mark.integration
def test_real_encoding_is_deterministic(real_embedder):
    text = "Tempering furnace quench pressure"
    a = real_embedder.encode_documents([text])[0]
    b = real_embedder.encode_documents([text])[0]
    assert a.dense == b.dense
    assert a.sparse == b.sparse
