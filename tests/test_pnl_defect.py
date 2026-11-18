# -*- coding: utf-8 -*-
"""防回归：情景 P&L 不得对同一 dy 施加两次（买入成本固定在期初）。"""
from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from runtime.bonds import Bond, reprice_bond
from runtime.scenarios import strategy_comparison, strategy_pnl

ROOT = Path(__file__).resolve().parent.parent
SNAP = json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))


class NoDoubleDiscountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bonds = [Bond.from_dict(b) for b in SNAP["deliverable_basket"]]
        cls.settle = date.fromisoformat(SNAP["meta"]["settle_date"])
        cls.delivery = date.fromisoformat(SNAP["meta"]["delivery_date"])
        cls.f0 = SNAP["meta"]["futures_price"]
        cls.costs = SNAP["costs"]

    def test_entry_price_must_not_move_with_scenario(self):
        """同一 dy 重复贴现会让对冲 P&L 放大数倍；修正后应接近成本项量级。"""
        bond = next(b for b in self.bonds if b.bond_id == "SCEN-C")
        dy = -0.01
        wrong = strategy_pnl(reprice_bond(bond, dy), self.f0, self.settle,
                             self.delivery, self.costs["repo_rate"], dy,
                             bond.modified_duration, self.costs, "dv01",
                             bond.modified_duration)
        fixed = strategy_pnl(bond, self.f0, self.settle, self.delivery,
                             self.costs["repo_rate"], dy, bond.modified_duration,
                             self.costs, "dv01", bond.modified_duration)
        self.assertGreater(abs(wrong["pnl_hedged"]), 1.0)
        self.assertLess(abs(fixed["pnl_hedged"]), 0.1)

    def test_strategy_comparison_uses_entry_bond(self):
        rows = strategy_comparison(SNAP, self.bonds, self.settle, self.delivery,
                                   [self.costs["repo_rate"]], [-0.01, 0.0, 0.01])
        by_dy = {round(float(r["yield_shift_bp"])): r for r in rows}
        self.assertEqual(set(by_dy), {-100, 0, 100})
        # 修正后残差只来自久期近似与成本项（<1 元/百元）；旧口径在此量级为 6+。
        for bp in (-100, 100):
            self.assertLess(abs(by_dy[bp]["B_simple_hedge_pnl_per_100"]), 1.0)
        self.assertLess(abs(by_dy[0]["B_simple_hedge_pnl_per_100"]), 1.0)
        ids = {b.bond_id for b in self.bonds}
        self.assertTrue(all(r["held_bond"] in ids for r in rows))

    def test_terminal_value_basis_is_separable(self):
        """A 走现券市场退出、B/C 走实物交割，差额必须能拆成三项。"""
        rows = strategy_comparison(SNAP, self.bonds, self.settle, self.delivery,
                                   [self.costs["repo_rate"]], [-0.01, 0.0, 0.01])
        for r in rows:
            lhs = r["A_buy_hold_pnl_per_100"] - r["B_simple_hedge_pnl_per_100"]
            rhs = (r["valuation_basis_gap_per_100"] + r["delivery_fee_per_100"]
                   - r["hedge_leg_pnl_B"])
            self.assertAlmostEqual(lhs, rhs, places=5)
            # 两套终值口径必须真的不同（否则这条恒等式是恒真的假通过）
            self.assertNotAlmostEqual(r["cash_terminal_market_per_100"],
                                      r["cash_terminal_delivery_per_100"], places=3)

    def test_switch_exposure_is_quantified_not_hidden(self):
        """CTD 切换必须显式暴露为风险，不得从 P&L 里消失。"""
        rows = strategy_comparison(SNAP, self.bonds, self.settle, self.delivery,
                                   [self.costs["repo_rate"]], [-0.01, 0.0, 0.01])
        self.assertTrue(any(r["ctd_switched"] for r in rows))
        self.assertTrue(any(abs(r["switch_opportunity_bp"]) > 1e-9 for r in rows))
        for r in rows:
            if r["ctd_switched"]:
                self.assertNotEqual(r["held_bond"], r["post_shock_ctd"])
            else:
                self.assertEqual(r["held_bond"], r["post_shock_ctd"])


if __name__ == "__main__":
    unittest.main()
