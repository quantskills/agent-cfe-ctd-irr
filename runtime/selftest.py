# -*- coding: utf-8 -*-
"""公式硬断言对拍（规范 §8 第二层：口径移植必须先逐点对拍）。

方法论坑（见项目长期记忆）：任何「两边全 0」的对拍都是假通过，
因此这里显式要求指标非退化，并使用与业务不同的独立推导路径交叉验证。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .bonds import (
    Bond, accrued_on, coupon_flows, evaluate_bond, invoice_price,
    reprice_bond, hedge_ratio, solve_irr_closed, reference_cf,
    days, ACT365,
)

ROOT = Path(__file__).resolve().parent.parent


def run_all() -> dict:
    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    snap = json.loads((ROOT / "data" / "input_snapshot.json").read_text(encoding="utf-8"))
    settle = date.fromisoformat(snap["meta"]["settle_date"])
    delivery = date.fromisoformat(snap["meta"]["delivery_date"])
    f0 = float(snap["meta"]["futures_price"])
    r = float(snap["costs"]["repo_rate"])
    bonds = [Bond.from_dict(b) for b in snap["deliverable_basket"]]

    # 1. 应计利息：付息日当天为 0，半期为半票息
    b = bonds[0]
    chk("accrued_zero_on_coupon_date",
        abs(accrued_on(b, b.last_coupon)) < 1e-12,
        f"value={accrued_on(b, b.last_coupon)}")
    period_start, period_end = b.last_coupon, b.next_coupon
    half = date.fromordinal((period_start.toordinal() + period_end.toordinal()) // 2)
    expected_half = 100.0 * b.coupon / b.freq / 2.0
    chk("accrued_half_period_act_act",
        abs(accrued_on(b, half) - expected_half) < 0.02,
        f"got={accrued_on(b, half):.6f} expected≈{expected_half:.6f}")

    # 2. 每券恒等式：BNOC_dirty = -(IRR-r)*D = GrossBasis - NetCarry
    for b in bonds:
        m = evaluate_bond(b, f0, settle, delivery, r)
        e1 = abs(m.bnoc_dirty - (-(m.irr_simple - r) * m.effective_base_D))
        e2 = abs(m.bnoc_dirty - (m.gross_basis - m.net_carry))
        chk(f"identity_{b.bond_id}", e1 < 1e-9 and e2 < 1e-9,
            f"err1={e1:.3e} err2={e2:.3e}")
        # r := IRR 时 BNOC 必须为 0（用独立调用路径验证）
        m0 = evaluate_bond(b, f0, settle, delivery, m.irr_simple)
        chk(f"bnoc_zero_at_irr_{b.bond_id}", abs(m0.bnoc_dirty) < 1e-9,
            f"bnoc_dirty={m0.bnoc_dirty:.3e}")

    # 3. 无票息券：IRR 闭式解与「总收入/成本」独立推导一致
    no_coupon = [b for b in bonds if not coupon_flows(b, settle, delivery)]
    chk("no_coupon_sample_exists", len(no_coupon) >= 1, f"n={len(no_coupon)}")
    for b in no_coupon:
        irr_s, parts = solve_irr_closed(b, f0, settle, delivery)
        d = days(settle, delivery)
        independent = (invoice_price(b, f0, delivery) - b.dirty_price) / b.dirty_price * ACT365 / d
        chk(f"no_coupon_irr_independent_{b.bond_id}",
            abs(irr_s - independent) < 1e-12,
            f"closed={irr_s:.10f} independent={independent:.10f}")

    # 4. 有票息券：票息再投资项必须非零（防止「两边全 0」假通过）
    with_coupon = [b for b in bonds if coupon_flows(b, settle, delivery)]
    chk("coupon_sample_exists", len(with_coupon) >= 1, f"n={len(with_coupon)}")
    for b in with_coupon:
        m = evaluate_bond(b, f0, settle, delivery, r)
        chk(f"coupon_reinvest_nonzero_{b.bond_id}",
            m.coupon_count > 0 and abs(m.irr_at_repo - m.irr_simple) > 1e-12,
            f"coupons={m.coupon_count} irr={m.irr_simple:.6f} irr_at_repo={m.irr_at_repo:.6f}")

    # 5. 一阶重定价与对冲比率
    rp = reprice_bond(bonds[0], 0.001)
    expected = bonds[0].clean_price * (1.0 - bonds[0].modified_duration * 0.001)
    chk("reprice_first_order", abs(rp.clean_price - expected) < 1e-12, "")
    chk("hedge_naive", hedge_ratio("naive_1to1", bonds[0], f0, 7.0) == 1.0, "")
    chk("hedge_cf", abs(hedge_ratio("cf_weighted", bonds[0], f0, 7.0) -
                        bonds[0].conversion_factor) < 1e-12, "")
    chk("hedge_dv01_positive", hedge_ratio("dv01", bonds[0], f0, 7.0) > 0, "")

    # 6. 非退化硬断言：毛基差、IRR、BNOC 不能全为 0
    ms = [evaluate_bond(x, f0, settle, delivery, r) for x in bonds]
    chk("non_degenerate_gross_basis",
        sum(1 for m in ms if abs(m.gross_basis) > 1e-6) == len(ms), "")
    chk("non_degenerate_irr_spread",
        max(m.irr_simple for m in ms) - min(m.irr_simple for m in ms) > 1e-6, "")
    chk("non_degenerate_bnoc",
        sum(1 for m in ms if abs(m.bnoc) > 1e-6) == len(ms), "")

    # 7. CF 参考值量级。degraded 快照的 conversion_factor 由 reference_cf()
    # 四舍五入写入，ref − CF ≈ 1e-7 只是舍入误差，属自反量，不作独立证据。
    # 这里只检查量级 plausibility，不把它伪装成 CF 有效性校验。
    for b in bonds:
        ref = reference_cf(b, delivery)
        chk(f"cf_reference_plausible_{b.bond_id}", 0.5 < ref < 1.2, f"ref={ref:.6f}")

    errors = [c for c in checks if not c["ok"]]
    return {
        "ok": not errors,
        "checks": checks,
        "n_checks": len(checks),
        "n_failed": len(errors),
        "data_version": snap["meta"]["data_version"],
        "cf_reference_gap_is_self_referential": True,
        "note": (
            "公式层对拍；业务结论另由 validation.json 门禁校验。"
            "cf_reference_gap 在 degraded 快照下为自反量（仅舍入误差），"
            "不得当作 CF 有效性的独立证据；只有接入交易所公布 CF 后才有校验意义。"
            "公式层共 27 条有效断言。"
        ),
    }
