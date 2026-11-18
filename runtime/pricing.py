# -*- coding: utf-8 -*-
"""情景输入构造器：把「声明式收益率曲线 + 债券条款」变成可复现的价格面板。

本模块不产生市场数据，只按给定规则生成一组可复算的情景参数，用于复现
CTD / IRR / 净基差公式与情景逻辑。所有产物 source_mode=degraded、
market_conclusion=false。接入真实数据时直接替换 input_snapshot.json，
本模块不被调用。

口径说明（详见 references/data_guide.md 与 references/methodology.md §11）：

- `conversion_factor` 写入的是 `reference_cf()` 结果，属 degraded 态声明式参考值，
  不是交易所公布 CF，也不构成对输入 CF 的独立校验（见 bonds.reference_cf docstring）。
  live 态须原样采用公布值；`scripts/cffex_cf.py` 的公式复算有样本外反例，不作数据源。
- 情景券统一 `freq=1`（年付息），与记账式附息国债 `f=2` 的惯例不同；
  折现收益率按 ACT/365 年数加期限溢价，现金流折现按 ACT/ACT 期分数，
  属情景简化的混合口径，不等同于中债估值。
- 4 只券在交割月首日 2026-12-01 的剩余期限为 6.79~9.96 年。
  可交割范围以中金所合约表原文为准（`http://www.cffex.com/10tf/`），
  T2612 当前公布清单为 17 只券；冻结定稿里的 `eligible_rule_note`
  只作情景说明，不作规则依据。
"""
from __future__ import annotations

from datetime import date

from .bonds import add_months, days, accrued_on, reference_cf, Bond, ACT365


def coupon_dates(last_coupon: date, freq: int, maturity: date) -> list[date]:
    step = 12 // freq
    out: list[date] = []
    dt = add_months(last_coupon, step)
    guard = 0
    while dt <= maturity:
        out.append(dt)
        dt = add_months(dt, step)
        guard += 1
        if guard > 400:
            raise ValueError("coupon_dates did not converge")
    if not out:
        raise ValueError("bond has no remaining coupon")
    # 末期须落在到期日（scenario_spec 约定到期日与付息日对齐）
    out[-1] = min(out[-1], maturity)
    return out


def dirty_price_from_yield(coupon: float, freq: int, settle: date,
                           last_coupon: date, flow_dates: list[date],
                           y: float) -> float:
    """按到期收益率复利折现得到全价（每百元面值）。

    PV = sum_k cf_k / (1 + y/freq) ** (k + f)
    f = 结算日到下一付息日占当期实际天数的比例（ACT/ACT 期分数）。
    """
    f = days(settle, flow_dates[0]) / days(last_coupon, flow_dates[0])
    c = 100.0 * coupon / freq
    total = 0.0
    n = len(flow_dates)
    for k, _dt in enumerate(flow_dates):
        cf = c + (100.0 if k == n - 1 else 0.0)
        total += cf / (1.0 + y / freq) ** (k + f)
    return total


def modified_duration(coupon: float, freq: int, settle: date,
                      last_coupon: date, flow_dates: list[date],
                      y: float, eps: float = 1e-4) -> float:
    """数值微分求修正久期：D = -(1/P) dP/dy。"""
    p0 = dirty_price_from_yield(coupon, freq, settle, last_coupon, flow_dates, y)
    p_down = dirty_price_from_yield(coupon, freq, settle, last_coupon, flow_dates, y - eps)
    p_up = dirty_price_from_yield(coupon, freq, settle, last_coupon, flow_dates, y + eps)
    return (p_down - p_up) / (2.0 * eps * p0)


def last_coupon_before(settle: date, coupon_month: int, coupon_day: int,
                       freq: int = 1) -> date:
    """找出 settle 所在付息期的上一付息日。"""
    step = 12 // freq
    candidate = date(settle.year, coupon_month, coupon_day)
    if candidate > settle:
        candidate = add_months(candidate, -step)
    while add_months(candidate, step) <= settle:
        candidate = add_months(candidate, step)
    return candidate


def build_bond(spec: dict, settle: date, curve_level: float,
               slope_per_year: float) -> dict:
    freq = int(spec.get("freq", 1))
    maturity = date.fromisoformat(spec["maturity"])
    coupon = float(spec["coupon"])
    month, day = int(spec["coupon_month"]), int(spec["coupon_day"])
    last = last_coupon_before(settle, month, day, freq)
    nxt = add_months(last, 12 // freq)
    flows = coupon_dates(last, freq, maturity)

    years = days(settle, maturity) / ACT365
    y = curve_level + slope_per_year * years + float(spec.get("spread_bp", 0.0)) / 10000.0
    dirty = dirty_price_from_yield(coupon, freq, settle, last, flows, y)
    d_mod = modified_duration(coupon, freq, settle, last, flows, y)

    probe = Bond(
        bond_id=spec["bond_id"], coupon=coupon, freq=freq, maturity=maturity,
        last_coupon=last, next_coupon=nxt, clean_price=dirty, accrued=0.0,
        conversion_factor=1.0, modified_duration=d_mod,
        accrual_basis=spec.get("accrual_basis", "ACT/ACT"),
        available_notional_wan=float(spec.get("available_notional_wan", 0.0)),
        lendable_notional_wan=float(spec.get("lendable_notional_wan", 0.0)),
    )
    accrued = accrued_on(probe, settle)
    clean = dirty - accrued
    # 转换因子：按 3% 名义标准券折现的参考值，非交易所公布值
    cf = reference_cf(probe, date.fromisoformat(spec.get("cf_delivery_date", spec["delivery_date"]))) \
        if spec.get("delivery_date") else reference_cf(probe, flows[-1])

    return {
        "bond_id": spec["bond_id"],
        "name": spec.get("name", spec["bond_id"]),
        "coupon": coupon,
        "freq": freq,
        "maturity": maturity.isoformat(),
        "last_coupon": last.isoformat(),
        "next_coupon": nxt.isoformat(),
        "accrual_basis": spec.get("accrual_basis", "ACT/ACT"),
        "clean_price": round(clean, 6),
        "accrued_interest": round(accrued, 6),
        "dirty_price": round(dirty, 6),
        "conversion_factor": round(cf, 6),
        "modified_duration": round(d_mod, 6),
        "available_notional_wan": float(spec.get("available_notional_wan", 0.0)),
        "lendable_notional_wan": float(spec.get("lendable_notional_wan", 0.0)),
        "_derived": {
            "yield_to_maturity": round(y, 8),
            "remaining_years": round(years, 4),
            "remaining_coupons": len(flows),
            "dirty_price_origin": "derived_from_declared_yield_curve",
            "accrued_origin": "ACT/ACT from declared coupon schedule",
            "conversion_factor_origin": "derived_from_3pct_standard_coupon_discount_NOT_exchange_published",
        },
    }
