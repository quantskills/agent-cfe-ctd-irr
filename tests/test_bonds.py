# -*- coding: utf-8 -*-
"""公式单元测试：python -m unittest discover -s tests -v

对应项目长期记忆「方法论坑」：逐行移植必须先写与独立推导逐点对拍的硬断言，
且测试不得出现「两边全 0」的假通过。
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.bonds import (  # noqa: E402
    Bond, accrued_on, coupon_flows, evaluate_bond, invoice_price,
    reprice_bond, solve_irr_closed, days, ACT365,
)
from runtime.selftest import run_all  # noqa: E402


class TestBondMath(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        snap = json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))
        cls.snap = snap
        cls.settle = date.fromisoformat(snap["meta"]["settle_date"])
        cls.delivery = date.fromisoformat(snap["meta"]["delivery_date"])
        cls.f0 = float(snap["meta"]["futures_price"])
        cls.r = float(snap["costs"]["repo_rate"])
        cls.bonds = [Bond.from_dict(b) for b in snap["deliverable_basket"]]

    def test_accrued_zero_on_coupon_date(self) -> None:
        for b in self.bonds:
            self.assertAlmostEqual(accrued_on(b, b.last_coupon), 0.0, places=12)

    def test_dirty_equals_clean_plus_accrued(self) -> None:
        for b in self.bonds:
            self.assertAlmostEqual(b.dirty_price, b.clean_price + b.accrued, places=9)

    def test_irr_matches_independent_derivation_for_no_coupon(self) -> None:
        sample = [b for b in self.bonds if not coupon_flows(b, self.settle, self.delivery)]
        self.assertTrue(sample, "must have at least one no-coupon bond")
        for b in sample:
            irr, _ = solve_irr_closed(b, self.f0, self.settle, self.delivery)
            independent = (invoice_price(b, self.f0, self.delivery) - b.dirty_price) \
                / b.dirty_price * ACT365 / days(self.settle, self.delivery)
            self.assertAlmostEqual(irr, independent, places=12)

    def test_identities_for_every_bond(self) -> None:
        for b in self.bonds:
            m = evaluate_bond(b, self.f0, self.settle, self.delivery, self.r)
            self.assertAlmostEqual(m.bnoc_dirty, m.gross_basis - m.net_carry, places=9)
            self.assertAlmostEqual(m.bnoc_dirty,
                                   -(m.irr_simple - self.r) * m.effective_base_D, places=9)
            m0 = evaluate_bond(b, self.f0, self.settle, self.delivery, m.irr_simple)
            self.assertAlmostEqual(m0.bnoc_dirty, 0.0, places=9)

    def test_no_false_pass_all_zero(self) -> None:
        ms = [evaluate_bond(b, self.f0, self.settle, self.delivery, self.r) for b in self.bonds]
        self.assertTrue(all(abs(m.gross_basis) > 1e-6 for m in ms))
        self.assertGreater(max(m.irr_simple for m in ms) - min(m.irr_simple for m in ms), 1e-6)
        self.assertTrue(all(abs(m.bnoc) > 1e-6 for m in ms))

    def test_reprice_is_first_order(self) -> None:
        b = self.bonds[0]
        rp = reprice_bond(b, 0.001)
        self.assertAlmostEqual(rp.clean_price,
                               b.clean_price * (1 - b.modified_duration * 0.001), places=12)

    def test_selftest_suite_passes(self) -> None:
        result = run_all()
        self.assertTrue(result["ok"], json.dumps(
            [c for c in result["checks"] if not c["ok"]], ensure_ascii=False))
        self.assertGreater(result["n_checks"], 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
