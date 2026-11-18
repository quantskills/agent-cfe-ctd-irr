# -*- coding: utf-8 -*-
"""契约层防回归：bp 单位、布尔解析、事先闸门、robustness 可完成。"""
from __future__ import annotations

import csv
import json
import unittest
from datetime import date
from pathlib import Path

from runtime.bonds import Bond, parse_date
from runtime.cli import _grid, build_parser
from runtime.contest_replay import as_flag
from runtime.robustness import run_robustness, _boundary
from runtime.scenarios import backtest_metrics, strategy_comparison

ROOT = Path(__file__).resolve().parent.parent
SNAP = json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))


def _current_base_strategy_csv() -> Path | None:
    """定位当前口径基线 run 的策略表：按输入事实自动发现，不写死目录名。"""
    from runtime.run_registry import ROLE_BASE, resolve
    runs = ROOT / "reports" / "runtime_out"
    found = resolve(runs, semantics="v2-exante-execution")
    path = found.get(ROLE_BASE, {}).get("path")
    if not path:
        return None
    csv_path = Path(path) / "strategy_comparison.csv"
    return csv_path if csv_path.exists() else None

class FlagContractTests(unittest.TestCase):
    def test_string_false_is_false(self):
        self.assertFalse(as_flag("False"))
        self.assertFalse(as_flag("false"))
        self.assertFalse(as_flag("0"))
        self.assertTrue(as_flag("True"))
        self.assertTrue(as_flag(True))
        self.assertTrue(as_flag(1))

    def test_backtest_metrics_honours_csv_false(self):
        path = _current_base_strategy_csv()
        if path is None:
            self.skipTest("v2 runtime_out not present")
        with path.open(encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            for k, v in list(r.items()):
                if k in ("C_executed", "ctd_switched", "entry_ctd_disagreement"):
                    r[k] = as_flag(v)
                    continue
                try:
                    r[k] = float(v)
                except (TypeError, ValueError):
                    pass
        metrics = backtest_metrics(rows, "probe")
        self.assertGreater(metrics["overall"]["C_skipped_only"]["n"], 0)
        self.assertLess(metrics["overall"]["C_executed_only"]["n"], len(rows))
        # 布尔列必须是真正的布尔：CSV 里的字符串 "False" 不能被当成真值。
        self.assertTrue(metrics["ctd_switch_exposure"]["available"])
        self.assertGreater(metrics["ctd_switch_exposure"]["n_switched"], 0)

    def test_backtest_metrics_tolerates_legacy_csv(self):
        """v1 语义产物缺诊断列时必须返回 not_available，而不是抛 KeyError。"""
        legacy = [{
            "grid_index": i, "yield_shift_bp": 0.0, "repo_rate_pct": 1.85,
            "ctd": "SCEN-C", "C_executed": "False",
            "A_buy_hold_pnl_per_100": 0.1, "B_simple_hedge_pnl_per_100": 0.0,
            "C_research_pnl_per_100": 0.0,
        } for i in range(3)]
        for r in legacy:
            r["C_executed"] = as_flag(r["C_executed"])
        metrics = backtest_metrics(legacy, "probe")
        self.assertFalse(metrics["ctd_switch_exposure"]["available"])
        self.assertIsNone(metrics["ctd_switch_exposure"]["n_switched"])
        self.assertFalse(metrics["hindsight_switch_counterfactual"]["available"])
        self.assertEqual(metrics["overall"]["C"]["n"], 3)


class RobustnessContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = SNAP
        cls.bonds = [Bond.from_dict(b) for b in SNAP["deliverable_basket"]]
        cls.settle = parse_date(SNAP["meta"]["settle_date"])
        cls.delivery = parse_date(SNAP["meta"]["delivery_date"])
        args = build_parser().parse_args(["run"])
        r0 = float(SNAP["costs"]["repo_rate"])
        cls.r_grid = _grid(round(r0 - 0.015, 10), round(r0 + 0.030, 10), args.r_step)
        cls.dy_grid = _grid(args.dy_min, args.dy_max, args.dy_step)

    def test_boundary_exposes_spread_bp(self):
        base = _boundary(self.snap, self.bonds, self.settle, self.delivery)
        self.assertIn("spread_bp", base)
        self.assertNotIn("forward_spread_IRR_minus_repo_bp", base)

    def test_run_robustness_completes_without_keyerror(self):
        out = run_robustness(self.snap, self.bonds, self.settle, self.delivery,
                             self.r_grid, self.dy_grid)
        self.assertIn("cf_flip_found", out)
        self.assertIn("cost_coverage_gap_bp", out)
        # 缺口应在合理基点量级，绝不能是 *10000 后的六位数
        self.assertLess(abs(out["cost_coverage_gap_bp"]), 500)
        if not out["cf_flip_found"]:
            self.assertIsNone(out["cf_flip_threshold_abs"])
            self.assertIn("未找到", out["cf_flip_explanation"])


class ExAnteGateTests(unittest.TestCase):
    def test_execution_flag_independent_of_yield_shift(self):
        bonds = [Bond.from_dict(b) for b in SNAP["deliverable_basket"]]
        settle = date.fromisoformat(SNAP["meta"]["settle_date"])
        delivery = date.fromisoformat(SNAP["meta"]["delivery_date"])
        r = SNAP["costs"]["repo_rate"]
        rows = strategy_comparison(SNAP, bonds, settle, delivery, [r], [-0.01, 0.0, 0.01])
        flags = {round(r0["yield_shift_bp"]): r0["C_executed"] for r0 in rows}
        self.assertEqual(len(set(flags.values())), 1, "ex-ante gate must not depend on dy")
        self.assertTrue(all("entry_irr_minus_repo_bp" in r0 for r0 in rows))

    def test_holding_is_entry_ctd_not_post_shock_ctd(self):
        """事前口径：持仓标的必须是结算日判定的 CTD，不得取冲击后的 CTD。"""
        bonds = [Bond.from_dict(b) for b in SNAP["deliverable_basket"]]
        settle = date.fromisoformat(SNAP["meta"]["settle_date"])
        delivery = date.fromisoformat(SNAP["meta"]["delivery_date"])
        r = SNAP["costs"]["repo_rate"]
        rows = strategy_comparison(SNAP, bonds, settle, delivery, [r], [-0.01, 0.0, 0.01])
        entry_ctd = rows[0]["held_bond"]
        self.assertTrue(all(r0["held_bond"] == entry_ctd for r0 in rows),
                        "持仓券必须在整个 dy 轴上恒为期初 CTD")
        # 该快照确实存在 CTD 切换情景，否则这条断言会是空转
        switched = [r0 for r0 in rows if r0["ctd_switched"]]
        self.assertTrue(switched, "样本未覆盖 CTD 切换情景，事前断言未被真正检验")

    def test_entry_gate_and_holding_share_same_information_set(self):
        """闸门与持仓必须同源：都取期初曲线上判定的 CTD。"""
        from runtime.scenarios import evaluate_point
        bonds = [Bond.from_dict(b) for b in SNAP["deliverable_basket"]]
        settle = date.fromisoformat(SNAP["meta"]["settle_date"])
        delivery = date.fromisoformat(SNAP["meta"]["delivery_date"])
        f0 = SNAP["meta"]["futures_price"]
        r = SNAP["costs"]["repo_rate"]
        _m, rank = evaluate_point(bonds, f0, settle, delivery, r)
        rows = strategy_comparison(SNAP, bonds, settle, delivery, [r], [-0.01, 0.0, 0.01])
        self.assertTrue(all(r0["held_bond"] == rank["ctd_by_irr"] for r0 in rows))
        # entry_ctd_irr_pct 在 CSV 层保留 4 位小数，断言精度与之一致
        self.assertAlmostEqual(rows[0]["entry_ctd_irr_pct"],
                               round(rank["ctd_irr"] * 100, 4), places=4)


class SkillDocPortabilityTests(unittest.TestCase):
    def test_skill_md_has_no_machine_paths(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for bad in ("F:/ProjectAGI", "F:\\ProjectAGI", "D:/env/anaconda3",
                    "D:\\env\\anaconda3", "C:/Users/Administrator"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
