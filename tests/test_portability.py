# -*- coding: utf-8 -*-
"""跨环境适应性硬断言：路径发现不得依赖任何机器专属绝对路径。"""
from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runtime import contest_replay
from runtime import writers
from runtime.contest_replay import (
    OUTSIDE_PROJECT, PLATFORM_ENTRY_RECIPE, as_flag, record_path, resolve_recorded_path,
)

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS_REL = contest_replay.PRODUCTS_REL
MACHINE_PATHS = ("F:/ProjectAGI", "F:\\ProjectAGI",
                 "C:/Users/Administrator", "C:\\Users\\Administrator",
                 "D:/env/anaconda3", "D:\\env\\anaconda3")


class PathDiscoveryTests(unittest.TestCase):
    def test_no_hardcoded_machine_paths_in_runtime(self):
        for rel in ("runtime/contest_replay.py", "runtime/writers.py",
                    "runtime/cli.py", "runtime/validation.py", "runtime/comparison.py",
                    "scripts/build_snapshot.py", "scripts/fetch_official.py",
                    "scripts/probe_coverage.py",
                    "delivery/visualization/build_dashboard.py",
                    "data/input_snapshot.json", "data/input_snapshot_official.json"):
            source = (ROOT / rel).read_text(encoding="utf-8")
            for bad in MACHINE_PATHS:
                self.assertNotIn(bad, source, f"{rel} still hardcodes {bad}")

    def test_environment_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skill_root = base / "somewhere" / "skill"
            skill_root.mkdir(parents=True)
            dsh = base / "QSHome"
            dsh.mkdir()
            products = base / "QuantStudio" / PRODUCTS_REL
            products.parent.mkdir(parents=True)
            products.write_text("[]", encoding="utf-8")
            paths = contest_replay.resolve_platform_paths(
                environ={"DSH_HOME": str(dsh),
                         "QUANTSTUDIO_PRODUCTS_JSON": str(products)},
                project_root=skill_root,
            )
            self.assertEqual(paths["dsh_home"], dsh)
            self.assertEqual(paths["products_json"], products)
            probe = contest_replay.probe_platform(paths)
            self.assertTrue(probe["products_catalog_readable"])
            self.assertEqual(probe["platform_paths"]["dsh_home_source"], "env:DSH_HOME")
            self.assertEqual(probe["platform_paths"]["products_json_source"],
                             "env:QUANTSTUDIO_PRODUCTS_JSON")
            dumped = json.dumps(probe)
            self.assertNotIn(str(base), dumped)
            self.assertEqual(probe["platform_paths"]["dsh_home"], OUTSIDE_PROJECT)
            self.assertEqual(probe["platform_paths"]["products_json"], OUTSIDE_PROJECT)

    def test_project_relative_discovery_finds_sibling_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skill_root = base / "skills" / "agent-x"
            skill_root.mkdir(parents=True)
            dsh = base / "QSHome"
            dsh.mkdir()
            products = base / "QuantStudio" / PRODUCTS_REL
            products.parent.mkdir(parents=True)
            products.write_text("[]", encoding="utf-8")
            paths = contest_replay.resolve_platform_paths(environ={}, project_root=skill_root)
            self.assertEqual(paths["dsh_home"], dsh.resolve())
            self.assertEqual(paths["products_json"], products.resolve())
            self.assertEqual(paths["dsh_source"], "discovered:project_relative")
            self.assertEqual(contest_replay.probe_platform(paths)["contest_dir_exists"], False)

    def test_missing_platform_is_reported_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_root = Path(tmp) / "isolated" / "skill"
            skill_root.mkdir(parents=True)
            paths = contest_replay.resolve_platform_paths(environ={}, project_root=skill_root)
            self.assertIsNone(paths["dsh_home"])
            self.assertIsNone(paths["products_json"])
            probe = contest_replay.probe_platform(paths)
            self.assertFalse(probe["products_catalog_readable"])
            self.assertFalse(probe["cli_available"])
            self.assertIsNone(probe["platform_paths"]["dsh_home"])

    def test_manifest_entry_falls_back_explicitly(self):
        with mock.patch.object(writers, "resolve_platform_paths",
                               return_value={"dsh_home": None}):
            label = writers.platform_entry_label()
            self.assertTrue(label.startswith("not_detected"))
            self.assertIn("<DSH_HOME>", label)

    def test_manifest_entry_never_embeds_resolved_home(self):
        home = Path("F:/ProjectAGI/QSHome")
        with mock.patch.object(writers, "resolve_platform_paths",
                               return_value={"dsh_home": home, "dsh_source": "env:DSH_HOME"}):
            label = writers.platform_entry_label()
        self.assertNotIn("ProjectAGI", label)
        self.assertNotIn("F:", label)
        self.assertIn("env:DSH_HOME", label)
        self.assertIn(PLATFORM_ENTRY_RECIPE, label)

    def test_recorded_project_path_roundtrips_from_another_cwd(self):
        target = ROOT / "data" / "input_snapshot.json"
        recorded = record_path(target)
        self.assertEqual(recorded, "data/input_snapshot.json")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("runtime.contest_replay.Path.cwd", return_value=Path(tmp)):
                found = resolve_recorded_path(recorded)
        self.assertEqual(found, target.resolve())

    def test_frozen_snapshots_use_the_stable_entry_recipe(self):
        for name in ("input_snapshot.json", "input_snapshot_official.json"):
            snap = json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
            self.assertEqual(snap["contest_scope"]["entry"], PLATFORM_ENTRY_RECIPE)


class DefensiveInputTests(unittest.TestCase):
    def test_csv_flag_string_is_not_truthy(self):
        snap = json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))
        rows = [{
            "grid_index": 0, "yield_shift_bp": 0, "repo_rate_pct": 1.85, "ctd": "SCEN-C",
            "C_executed": "False", "C_hedge_ratio_lots": 2.0,
            "B_simple_hedge_pnl_per_100": 0.1, "C_research_pnl_per_100": 0.0,
        }]
        self.assertFalse(as_flag("False"))
        ledger = contest_replay.build_replay_ledger(rows, snap)
        self.assertFalse(ledger[0]["replay_executed"])
        self.assertEqual(ledger[0]["replay_lots"], 0)
        self.assertIn("不代表建仓", ledger[0]["pnl_note"])

    def test_malformed_basket_returns_contract_error(self):
        from runtime.io_contract import validate_snapshot

        snap = json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))
        snap = copy.deepcopy(snap)
        snap["deliverable_basket"][0]["conversion_factor"] = "not-a-number"
        result = validate_snapshot(snap)
        self.assertFalse(result["ok"])
        self.assertTrue(any("non-numeric" in e for e in result["errors"]))


class DashboardMetaTests(unittest.TestCase):
    def test_meta_carries_no_machine_specific_path(self):
        """页面元信息必须完全由数据与脚本位置推导，不得带本机绝对路径。

        旧实现注入 projectRoot=本机绝对路径，交付 HTML 会随机器变化，
        且属于机器专属信息残留；改为断言「元信息里不含任何机器专属路径」。
        """
        script = ROOT / "delivery" / "visualization" / "build_dashboard.py"
        spec = importlib.util.spec_from_file_location("_dashboard_meta_probe", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        meta = module.build_meta()
        self.assertIn("asOf", meta)
        for bad in MACHINE_PATHS:
            self.assertNotIn(bad, json.dumps(meta, ensure_ascii=False))
        for key, value in meta.items():
            self.assertNotIn("\\", str(value), f"meta.{key} 疑似含 Windows 绝对路径")
            self.assertNotIn("/", str(value), f"meta.{key} 疑似含路径分隔符")
        for bad in MACHINE_PATHS:
            self.assertNotIn(bad, inspect.getsource(module.build_meta))

    def test_financing_rate_is_injected_not_hardcoded(self):
        """融资假设必须来自各 run 自己的快照，不能写死在页面脚本里。"""
        template = (ROOT / "delivery" / "visualization" / "dashboard.template.html").read_text(
            encoding="utf-8")
        self.assertNotIn("REPO_PCT", template)
        script = ROOT / "delivery" / "visualization" / "build_dashboard.py"
        spec = importlib.util.spec_from_file_location("_dashboard_rate_probe", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from runtime.run_registry import ROLE_ANCHOR, ROLE_BASE, resolve
        found = resolve(module.RUNS, semantics="v2-exante-execution")
        for key in (ROLE_BASE, ROLE_ANCHOR):
            folder = found.get(key, {}).get("path")
            if not folder or not Path(folder).is_dir():
                continue
            run_id = Path(folder).name
            snapshot = json.loads((folder / "input_snapshot.json").read_text(encoding="utf-8"))
            expected = round(float(snapshot["costs"]["repo_rate"]) * 100, 6)
            extracted = module.extract_run(run_id, "probe", 0.0)
            self.assertAlmostEqual(extracted["repoRatePct"], expected, places=6)
            self.assertAlmostEqual(
                extracted["baseIrrPct"],
                module.extract_run(run_id, "probe", 0.0)["baseIrrPct"], places=9)


if __name__ == "__main__":
    unittest.main()
