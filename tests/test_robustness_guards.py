# -*- coding: utf-8 -*-
"""防御性与一致性硬断言：畸形输入不得让校验层自身崩溃。

背景：一次独立审计发现，若干条路径会让「校验/门禁」自身抛异常中断，
而不是返回 ok=false —— 这会让下游拿到 traceback 而不是可机器读取的结论。
本文件把每一条固化为回归测试。
"""
from __future__ import annotations

import copy
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime import contest_replay, writers
from runtime.io_contract import validate_snapshot
from runtime.validation import validate_run_dir

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "reports" / "runtime_out"
# 历史 v1 run 是「历史产物仍全绿」的验收锚，刻意固定指向那一批产物。
FROZEN = RUNS / "20261114-223330"


def _discover(role: str, semantics: str | None = "v2-exante-execution") -> Path | None:
    """按输入事实定位当前口径的 run，不写死目录名。"""
    from runtime.run_registry import ROLE_BASE, ROLE_ANCHOR, resolve
    key = ROLE_BASE if role == "base" else ROLE_ANCHOR
    found = resolve(RUNS, semantics=semantics)
    path = found.get(key, {}).get("path")
    return Path(path) if path else None


def _discover_v2() -> Path | None:
    return _discover("base")


def _snapshot() -> dict:
    return json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))


class MalformedInputTests(unittest.TestCase):
    """契约层：畸形输入必须返回 ok=false，不得抛异常。"""

    def test_meta_invalid_date_returns_error_not_exception(self):
        snap = _snapshot()
        snap["meta"]["settle_date"] = "not-a-date"
        result = validate_snapshot(snap)
        self.assertFalse(result["ok"])
        self.assertTrue(any("invalid date" in e for e in result["errors"]), result["errors"])

    def test_meta_invalid_delivery_date_returns_error(self):
        snap = _snapshot()
        snap["meta"]["delivery_date"] = "2026-13-45"
        result = validate_snapshot(snap)
        self.assertFalse(result["ok"])

    def test_missing_capacity_is_rejected(self):
        """capacity 缺失会让下游 scenarios 直接 KeyError，契约层必须拦下。"""
        snap = _snapshot()
        snap.pop("capacity", None)
        result = validate_snapshot(snap)
        self.assertFalse(result["ok"])
        self.assertTrue(any("capacity" in e for e in result["errors"]), result["errors"])

    def test_missing_contract_notional_is_rejected(self):
        snap = _snapshot()
        snap["meta"].pop("contract_notional_wan", None)
        result = validate_snapshot(snap)
        self.assertFalse(result["ok"])

    def test_nonstandard_freq_is_rejected(self):
        """freq=0 会除零；freq=5 会静默改变应计与 IRR 且无告警。"""
        for bad_freq in (0, 5, 2.5):
            snap = _snapshot()
            snap["deliverable_basket"][0]["freq"] = bad_freq
            result = validate_snapshot(snap)
            self.assertFalse(result["ok"], f"freq={bad_freq} 未被拒绝")
            self.assertTrue(any("freq" in e for e in result["errors"]), result["errors"])

    def test_standard_freq_accepted(self):
        snap = _snapshot()
        snap["deliverable_basket"][0]["freq"] = 2
        result = validate_snapshot(snap)
        self.assertTrue(result["ok"], result["errors"])


class GateRobustnessTests(unittest.TestCase):
    """门禁层：畸形产物必须让检查失败，而不是让门禁自身抛异常。"""

    def _copy_run(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="ctd-gate-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dest = tmp / "run"
        shutil.copytree(FROZEN, dest)
        return dest

    def test_non_numeric_gross_basis_fails_check_without_crashing(self):
        run_dir = self._copy_run()
        table = run_dir / "ctb_table.csv"
        table.write_text(table.read_text(encoding="utf-8-sig").replace(
            ",0.0", ",abc", 1), encoding="utf-8-sig")
        result = validate_run_dir(run_dir)
        self.assertFalse(result["ok"])
        self.assertTrue(result["checks"], "门禁必须返回检查项而不是空结果")

    def test_frozen_run_still_passes_all_checks(self):
        """任何防御性改动都不得把既有冻结 run 的门禁打红。

        旧 run 生成于语义版本轴引入之前，v2 专属检查项对其记为 not_applicable
        （既不算通过也不算失败），因此这里只要求「没有失败项」。
        """
        result = validate_run_dir(FROZEN)
        self.assertTrue(result["ok"], [c for c in result["checks"] if not c.get("ok")])
        # 旧 run 不得因为缺少语义版本而被判失败，但必须留下明确 warning
        self.assertTrue(any("semantics_version" in w for w in result["warnings"]),
                        result["warnings"])

    def test_v2_run_passes_with_extended_checks(self):
        """v2 语义产物必须通过新增的策略 P&L 独立重算与事前持仓断言。"""
        v2_run = _discover_v2()
        if v2_run is None or not v2_run.exists():
            self.skipTest("v2 runtime_out not present")
        result = validate_run_dir(v2_run)
        self.assertTrue(result["ok"], [c for c in result["checks"] if not c.get("ok")])
        names = {c["check"] for c in result["checks"]}
        for required in ("independent_recompute_strategy_pnl",
                         "exante_holding_is_entry_ctd",
                         "terminal_basis_identity_holds",
                         "skipped_scenarios_have_zero_pnl",
                         "semantics_version_present"):
            self.assertIn(required, names)

    def test_gate_catches_ex_ante_holding_violation(self):
        """反向验证：把持仓券改成事后 CTD，门禁必须报红。

        这是门禁有效性的证明 —— 若门禁对此无反应，它就只是装饰。
        """
        v2_run = _discover_v2()
        if v2_run is None or not v2_run.exists():
            self.skipTest("v2 runtime_out not present")
        tmp = Path(tempfile.mkdtemp(prefix="ctd-exante-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dest = tmp / "run"
        shutil.copytree(v2_run, dest)
        table = dest / "strategy_comparison.csv"
        with table.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            r["held_bond"] = r["post_shock_ctd"]      # 注入 v1 的事后选券缺陷
        with table.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        result = validate_run_dir(dest)
        self.assertFalse(result["ok"])
        failed = {c["check"] for c in result["checks"] if not c.get("ok")}
        self.assertIn("exante_holding_is_entry_ctd", failed)
        self.assertIn("independent_recompute_strategy_pnl", failed)


class OutputValidityTests(unittest.TestCase):
    """写出层：产物必须是标准 JSON，且落盘是原子的。"""

    def test_nan_is_written_as_null_not_nan_literal(self):
        tmp = Path(tempfile.mkdtemp(prefix="ctd-json-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "boundary.json"
        writers.write_json(path, {"margin": float("nan"), "ok": True,
                                  "nested": [float("inf"), 1.5]})
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)
        data = json.loads(raw)           # 严格解析器必须能读
        self.assertIsNone(data["margin"])
        self.assertIsNone(data["nested"][0])

    def test_no_temp_file_left_behind(self):
        tmp = Path(tempfile.mkdtemp(prefix="ctd-atomic-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        writers.write_json(tmp / "a.json", {"x": 1})
        writers.write_csv(tmp / "b.csv", [{"k": 1}, {"k": 2}])
        self.assertEqual([p.name for p in tmp.iterdir()], ["a.json", "b.csv"])


class ReplayLotsTests(unittest.TestCase):
    """回放手数：向下取整会让「已执行」情景出现 0 手、无方向的矛盾记录。"""

    def test_partial_lot_rounds_up_to_one_lot(self):
        snap = {"meta": {"contract_notional_wan": 100}}
        lots, raw = contest_replay._lots_for(100.0, 0.9357, 100)
        self.assertEqual(lots, 1)
        self.assertAlmostEqual(raw, 0.9357, places=6)

    def test_zero_hedge_yields_zero_lots(self):
        snap_lots, _ = contest_replay._lots_for(100.0, 0.0, 100)
        self.assertEqual(snap_lots, 0)

    def test_ledger_never_marks_executed_with_zero_lots(self):
        snap = json.loads((FROZEN / "input_snapshot.json").read_text(encoding="utf-8"))
        rows = [{"grid_index": "0", "yield_shift_bp": "0", "repo_rate_pct": "2.6",
                 "ctd": "SCEN-C", "C_executed": "True", "C_hedge_ratio_lots": 0.9357,
                 "A_buy_hold_pnl_per_100": "0.0", "B_simple_hedge_pnl_per_100": "0.1",
                 "C_research_pnl_per_100": "0.2"}]
        ledger = contest_replay.build_replay_ledger(rows, snap)
        for row in ledger:
            if row["replay_executed"]:
                self.assertGreater(row["replay_lots"], 0)
                self.assertEqual(row["replay_direction"], "sell")
            else:
                self.assertEqual(row["replay_lots"], 0)


class DashboardPayloadTests(unittest.TestCase):
    """客户视图页面只承载研究结论，不夹带过程证据与机器专属路径。"""

    def _module(self):
        import importlib.util
        script = ROOT / "delivery" / "visualization" / "build_dashboard.py"
        spec = importlib.util.spec_from_file_location("_dash_payload_probe", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_payload_excludes_process_evidence(self):
        module = self._module()
        run = _discover_v2()
        if run is None:
            self.skipTest("no current-semantics base run present")
        payload = module.extract_run(run.name, "冻结基线", 107.32)
        for block in ("robustness", "financing", "validation", "fingerprint"):
            self.assertNotIn(block, payload, f"页面数据混入过程证据块：{block}")

    def test_payload_financing_assumption_is_data_driven(self):
        module = self._module()
        for role in ("base", "anchor"):
            folder = _discover(role)
            if folder is None or not folder.is_dir():
                continue
            snapshot = json.loads((folder / "input_snapshot.json").read_text(encoding="utf-8"))
            payload = module.extract_run(folder.name, "probe", 107.32)
            self.assertAlmostEqual(payload["repoRatePct"],
                                   round(float(snapshot["costs"]["repo_rate"]) * 100, 6),
                                   places=6)

    def test_built_page_has_no_machine_path(self):
        page = ROOT / "delivery" / "visualization" / "index.html"
        if not page.is_file():
            self.skipTest("页面尚未构建")
        raw = page.read_text(encoding="utf-8")
        for bad in ("F:/ProjectAGI", "F:\\ProjectAGI", "C:/Users/Administrator",
                    "C:\\Users\\Administrator", "D:/env"):
            self.assertNotIn(bad, raw)


if __name__ == "__main__":
    unittest.main()
