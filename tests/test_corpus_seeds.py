"""语料自洽规则的测试。

build_corpus.py 是脚本不是包，这里用 importlib 按路径加载，
这样可以单独测它的校验函数，而不必跑完整的渲染流程。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_corpus.py"


def load_build_corpus():
    spec = importlib.util.spec_from_file_location("build_corpus", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _cap_row(**over):
    base = {
        "process": "toughened",
        "shape": "flat",
        "thickness_mm": "6",
        "max_width_mm": "1000",
        "max_height_mm": "1000",
        "max_area_m2": "1.0",
        "min_width_mm": "100",
        "min_height_mm": "100",
        "tolerance_mm": "1.0",
        "min_radius_mm": "0",
        "igu_cavity_mm": "",
        "notes": "",
    }
    base.update(over)
    return base


def test_real_seeds_pass_all_checks():
    """仓库里的 seeds 必须始终通过数字自洽校验。"""
    mod = load_build_corpus()
    ck = mod.run_checks()
    assert ck.errors == [], "语料自洽校验失败：\n" + "\n".join(ck.errors)


def test_capability_rejects_illegal_thickness():
    mod = load_build_corpus()
    ck = mod.Checks()
    mod.check_capability([_cap_row(thickness_mm="7")], ck)
    assert any("不允许厚度" in e for e in ck.errors)


def test_capability_rejects_laminated_as_integer():
    """夹胶必须用公称厚度 6.38 等，不能写成整数 8。"""
    mod = load_build_corpus()
    ck = mod.Checks()
    mod.check_capability([_cap_row(process="laminated", thickness_mm="8")], ck)
    assert any("不允许厚度" in e for e in ck.errors)


def test_capability_rejects_inconsistent_area():
    mod = load_build_corpus()
    ck = mod.Checks()
    mod.check_capability([_cap_row(max_area_m2="99")], ck)
    assert any("面积不自洽" in e for e in ck.errors)


def test_capability_rejects_bent_without_radius():
    mod = load_build_corpus()
    ck = mod.Checks()
    mod.check_capability([_cap_row(shape="bent", min_radius_mm="0")], ck)
    assert any("bent 但 min_radius_mm=0" in e for e in ck.errors)


def test_capability_rejects_duplicate_key():
    mod = load_build_corpus()
    ck = mod.Checks()
    mod.check_capability([_cap_row(), _cap_row()], ck)
    assert any("唯一键重复" in e for e in ck.errors)


def test_leadtime_rejects_rush_fee_when_unavailable():
    mod = load_build_corpus()
    ck = mod.Checks()
    rows = [{
        "process": "toughened",
        "thickness_max_mm": "8",
        "qty_max": "10",
        "business_days": "10",
        "rush_available": "false",
        "rush_surcharge_pct": "25",
        "note": "",
    }]
    mod.check_leadtime(rows, ck)
    assert any("不可加急却有加急费" in e for e in ck.errors)


def test_experience_prefix_rule():
    mod = load_build_corpus()
    assert mod.starts_with_experience_prefix("Usually we cannot do this.")
    assert mod.starts_with_experience_prefix("In our experience this is the area limit.")
    assert mod.starts_with_experience_prefix("Typically a wider cavity helps.")
    assert not mod.starts_with_experience_prefix("This is a definite answer.")
    assert not mod.starts_with_experience_prefix("usually lower case fails")
