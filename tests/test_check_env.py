"""check_env 的 torch 检查测试。

用假 torch 模块模拟"CPU 版"和"CUDA 版"，不碰真实环境。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_env.py"


def load_check_env():
    spec = importlib.util.spec_from_file_location("check_env_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_fake_torch(version: str, cuda_ok: bool, arch: list[str]):
    module = types.ModuleType("torch")
    module.__version__ = version

    class _Cuda:
        @staticmethod
        def is_available():
            return cuda_ok

        @staticmethod
        def get_device_name(index=0):
            return "Fake GPU"

        @staticmethod
        def get_arch_list():
            return arch

    module.cuda = _Cuda()
    return module


def test_cpu_torch_produces_warning_not_failure(monkeypatch):
    """CPU 版 torch 是性能问题，不是错误——必须是警告，不能算失败。"""
    monkeypatch.setitem(sys.modules, "torch", make_fake_torch("2.14.1", False, []))
    mod = load_check_env()
    rep = mod.Report()

    mod.check_torch(rep)

    assert rep.warned == 1
    assert rep.failed == 0


def test_cuda_torch_passes_with_sm120(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "torch", make_fake_torch("2.13.0+cu130", True, ["sm_90", "sm_120"])
    )
    mod = load_check_env()
    rep = mod.Report()

    mod.check_torch(rep)

    assert rep.warned == 0
    assert rep.failed == 0


def test_cuda_without_sm120_warns(monkeypatch):
    """有 CUDA 但架构表里没有 sm_120，RTX 50 系列可能退化，要提示。"""
    monkeypatch.setitem(sys.modules, "torch", make_fake_torch("2.13.0+cu118", True, ["sm_90"]))
    mod = load_check_env()
    rep = mod.Report()

    mod.check_torch(rep)

    assert rep.warned == 1
    assert rep.failed == 0


def test_missing_torch_is_warning(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    mod = load_check_env()
    rep = mod.Report()

    mod.check_torch(rep)

    assert rep.warned == 1
    assert rep.failed == 0
