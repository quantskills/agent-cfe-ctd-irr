# -*- coding: utf-8 -*-
"""「产物是派生物」防回归：run 定位、页面重建、模板无硬编码、快照覆盖保护。

这些测试守的是一条形式上的硬约束：**网页与数据必须由 skill 生成**。
如果下游代码把某个 run 的目录名、或某次运行的研究数值写死，那么
「清掉缓存后重跑」就会失败，页面会退化成手工快照 —— 那正是要防的事。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run_py(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """子进程按 UTF-8 读写，避免中文 Windows 的 GBK 控制台把输出读崩。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        argv, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", env=env,
    )

from runtime import run_registry                                   # noqa: E402
from runtime.comparison import compare_dirs                       # noqa: E402

RUNS = ROOT / "reports" / "runtime_out"
TEMPLATE = ROOT / "delivery" / "visualization" / "dashboard.template.html"
DASHBOARD_SCRIPT = ROOT / "delivery" / "visualization" / "build_dashboard.py"

# 页面里**不允许**出现的写死研究数值。命中即说明模板退化成了手工快照。
FORBIDDEN_IN_TEMPLATE = [
    "399 个情景", "399 组", "399 情景", "19×21",
    "34.737", "49.778", "2.73 元价差", "21 档",
    "3019.41", "1259.09", "0.127906", "190/399",
]


def _have_any_run() -> bool:
    return any((p / "run_manifest.json").is_file() for p in RUNS.iterdir()) if RUNS.is_dir() else False


class RunRegistryTests(unittest.TestCase):
    """run 定位必须靠输入事实，不能靠目录名。"""

    def _make_run(self, tmp: Path, name: str, official: bool) -> Path:
        d = tmp / name
        d.mkdir(parents=True)
        (d / "run_manifest.json").write_text(json.dumps({
            "data_version": "hybrid-x" if official else "degraded-x",
            "semantics_version": "v2-exante-execution",
            "source_mode": "degraded",
            "generated_at": "2026-11-19T00:00:00",
        }), encoding="utf-8")
        (d / "input_snapshot.json").write_text(json.dumps({
            "meta": {"official_anchor": {"x": 1} if official else None,
                     "data_version": "hybrid-x" if official else "degraded-x"},
        }), encoding="utf-8")
        return d

    def test_role_is_decided_by_input_not_directory_name(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 目录名故意与角色相反：若实现依赖名字，这两条断言必挂。
            self._make_run(tmp, "zzz-anchor-lookalike", official=True)
            self._make_run(tmp, "aaa-base-lookalike", official=False)
            self.assertEqual(run_registry.classify(tmp / "zzz-anchor-lookalike"),
                             run_registry.ROLE_ANCHOR)
            self.assertEqual(run_registry.classify(tmp / "aaa-base-lookalike"),
                             run_registry.ROLE_BASE)

    def test_auto_discovery_survives_directory_rename(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._make_run(tmp, "old-name-a", official=False)
            self._make_run(tmp, "old-name-b", official=True)
            found = run_registry.resolve(tmp)
            self.assertIsNotNone(found[run_registry.ROLE_BASE]["path"])
            self.assertIsNotNone(found[run_registry.ROLE_ANCHOR]["path"])
            # 把两个目录改名后仍应能重新发现 —— 这正是「清缓存重跑」的形态。
            (tmp / "old-name-a").rename(tmp / "20261119-000001")
            (tmp / "old-name-b").rename(tmp / "20261119-000002")
            found2 = run_registry.resolve(tmp)
            self.assertEqual(found2[run_registry.ROLE_BASE]["id"], "20261119-000001")
            self.assertEqual(found2[run_registry.ROLE_ANCHOR]["id"], "20261119-000002")

    def test_missing_role_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._make_run(tmp, "only-base", official=False)
            found = run_registry.resolve(tmp)
            self.assertIsNone(found[run_registry.ROLE_ANCHOR]["path"])
            msg = run_registry.describe_missing(found)
            self.assertIn("anchor", msg)
            self.assertIn("only-base", msg)


class TemplateHardcodeTests(unittest.TestCase):
    """模板不得写死任何研究结论数值。"""

    def test_template_contains_no_hardcoded_research_numbers(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        hits = [p for p in FORBIDDEN_IN_TEMPLATE if p in text]
        self.assertEqual(hits, [], f"模板出现写死的研究数值: {hits}")

    def test_template_exposes_dynamic_placeholders(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("data-dyn", text)
        self.assertIn("dynText", text)
        self.assertIn("D.dynamic", text)


class DashboardRebuildTests(unittest.TestCase):
    """页面必须能从任意 run 名重建，且数值与 run 产物一致。"""

    def test_build_dashboard_succeeds_with_explicit_run_ids(self):
        if not _have_any_run():
            self.skipTest("no runtime run present")
        base, anchor = _pick_runs()
        if base is None or anchor is None:
            self.skipTest("need both base and anchor runs")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "index.html"
            rc = _run_py([sys.executable, str(DASHBOARD_SCRIPT),
                          "--base", base, "--anchor", anchor, "--out", str(out)])
            self.assertEqual(rc.returncode, 0, rc.stderr)
            self.assertTrue(out.is_file())
            payload = _read_payload(out)
            self.assertEqual(payload["base"]["id"], base)
            self.assertEqual(payload["anchor"]["id"], anchor)
            self.assertEqual(payload["dynamic"]["nScenarios"],
                             len(payload["base"]["strategy"]))
            self.assertEqual(payload["provenance"]["baseRun"], base)

    def test_page_numbers_match_run_artifacts(self):
        if not _have_any_run():
            self.skipTest("no runtime run present")
        base, anchor = _pick_runs()
        if base is None or anchor is None:
            self.skipTest("need both base and anchor runs")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "index.html"
            rc = _run_py([sys.executable, str(DASHBOARD_SCRIPT),
                          "--base", base, "--anchor", anchor, "--out", str(out)])
            self.assertEqual(rc.returncode, 0, rc.stderr)
            payload = _read_payload(out)
            boundary = json.loads(
                (RUNS / base / "executable_boundary.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(
                float(payload["dynamic"]["spreadBp"]),
                float(boundary["forward_spread_IRR_minus_repo_bp"]), places=3)
            self.assertAlmostEqual(
                float(payload["dynamic"]["costBp"]),
                float(boundary["all_in_cost_bp"]), places=3)

    def test_comparison_can_be_computed_without_prebuilt_file(self):
        """对照结论必须能现算，不能依赖「先跑过 compare_runs.py」。"""
        if not _have_any_run():
            self.skipTest("no runtime run present")
        base, anchor = _pick_runs()
        if base is None or anchor is None:
            self.skipTest("need both base and anchor runs")
        payload = compare_dirs(RUNS / base, RUNS / anchor)
        self.assertIn("delta", payload)
        self.assertIn("conclusion", payload)


class SnapshotProtectionTests(unittest.TestCase):
    """输入快照是验收链的哈希锚点，不得被无意改写。"""

    def test_check_mode_refuses_to_overwrite_different_content(self):
        script = ROOT / "scripts" / "build_snapshot.py"
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "snap.json"
            target.write_text('{"different": true}\n', encoding="utf-8")
            rc = _run_py([sys.executable, str(script), "--out", str(target), "--check"])
            self.assertNotEqual(rc.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"),
                             '{"different": true}\n', "check 模式不得改写目标文件")

    def test_frozen_snapshot_is_byte_reproducible(self):
        script = ROOT / "scripts" / "build_snapshot.py"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "snap.json"
            rc = _run_py([sys.executable, str(script), "--out", str(out)])
            self.assertEqual(rc.returncode, 0, rc.stderr)
            frozen = ROOT / "data" / "input_snapshot.json"
            self.assertTrue(frozen.is_file())
            self.assertEqual(out.read_bytes(), frozen.read_bytes(),
                             "冻结基线必须能由脚本字节级重建")


def _pick_runs() -> tuple[str | None, str | None]:
    """挑一对 base / anchor run；优先当前语义版本。"""
    if not RUNS.is_dir():
        return None, None
    found = run_registry.resolve(RUNS, semantics="v2-exante-execution")
    base = found[run_registry.ROLE_BASE]["id"]
    anchor = found[run_registry.ROLE_ANCHOR]["id"]
    return base, anchor


def _read_payload(html: Path) -> dict:
    text = html.read_text(encoding="utf-8")
    m = re.search(r"const D = (\{.*?\});\n", text, re.S)
    assert m, "页面缺少 payload"
    return json.loads(m.group(1).replace("<\\/", "</"))


if __name__ == "__main__":
    unittest.main()
