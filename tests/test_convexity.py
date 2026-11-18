# -*- coding: utf-8 -*-
"""凸性增强层的单元测试：硬断言，不用「两边全 0」的假通过。"""
import json
import unittest
from datetime import date
from pathlib import Path

from runtime.bonds import Bond, reprice_bond
from runtime.convexity import (
    effective_convexity, full_dirty_price, reprice_bond_convex, verify,
)

ROOT = Path(__file__).resolve().parent.parent
SNAP = json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))
SETTLE = date.fromisoformat(SNAP["meta"]["settle_date"])
BONDS = [Bond.from_dict(b) for b in SNAP["deliverable_basket"]]
YS = [float(b["_derived"]["yield_to_maturity"]) for b in SNAP["deliverable_basket"]]


class TestConvexity(unittest.TestCase):
    def test_convexity_positive_and_bounded(self):
        """普通债券凸性为正且量级合理（<剩余期限平方）。"""
        for b, y in zip(BONDS, YS):
            C = effective_convexity(b, SETTLE, y)
            years = (b.maturity - SETTLE).days / 365.0
            self.assertGreater(C, 0.0, b.bond_id)
            self.assertLess(C, years * years + 1.0, b.bond_id)

    def test_second_order_exact_at_zero_shift(self):
        """Δy=0 时二阶重定价必须回到原价（防止常数项污染）。"""
        for b, y in zip(BONDS, YS):
            C = effective_convexity(b, SETTLE, y)
            rp = reprice_bond_convex(b, 0.0, C)
            self.assertAlmostEqual(rp.clean_price, b.clean_price, places=9)

    def test_second_order_beats_first_order_at_large_shift(self):
        """±100bp 处二阶必须严格优于一阶，且误差显著小于。"""
        for b, y in zip(BONDS, YS):
            C = effective_convexity(b, SETTLE, y)
            for dy in (-0.01, 0.01):
                gt = full_dirty_price(b, SETTLE, y + dy) - b.accrued
                e1 = abs(reprice_bond(b, dy).clean_price - gt)
                e2 = abs(reprice_bond_convex(b, dy, C).clean_price - gt)
                self.assertGreater(e1, 1e-3, "一阶误差须非退化")
                self.assertLess(e2, e1, b.bond_id)
                self.assertLess(e2, e1 * 0.2, f"{b.bond_id} 二阶应至少好 5 倍")

    def test_full_price_matches_pricing_module(self):
        """本模块完整折现与 pricing 模块在同一口径下逐点一致。"""
        from runtime.pricing import coupon_dates, dirty_price_from_yield
        for b, s, y in zip(BONDS, SNAP["deliverable_basket"], YS):
            flows = coupon_dates(date.fromisoformat(s["last_coupon"]), b.freq, b.maturity)
            for dy in (-0.01, 0.0, 0.01):
                a = full_dirty_price(b, SETTLE, y + dy)
                c = dirty_price_from_yield(
                    b.coupon, b.freq, SETTLE, date.fromisoformat(s["last_coupon"]),
                    flows, y + dy)
                self.assertAlmostEqual(a, c, places=9)

    def test_verify_all_hard_assertions_pass(self):
        """集成验证：9 项硬断言全过，且结论不变性成立。"""
        res = verify(SNAP)
        self.assertTrue(res["ok"], str([c for c in res["checks"] if not c["ok"]]))
        self.assertEqual(res["n_failed"], 0)
        self.assertGreaterEqual(res["metrics"]["improvement_factor"], 2.0)
        self.assertEqual(res["consistency"]["switch_boundaries"],
                         ["0->10bp:SCEN-C→SCEN-A", "20->30bp:SCEN-A→SCEN-D"])


if __name__ == "__main__":
    unittest.main()
