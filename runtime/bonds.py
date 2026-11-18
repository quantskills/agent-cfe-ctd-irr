# -*- coding: utf-8 -*-
"""国债期货交割定价核心（纯标准库，无第三方依赖）。

覆盖：应计利息 / 发票价格 / 隐含回购利率 IRR / 净基差 BNOC / CTD 判定。
公式与单位口径逐条见 references/methodology.md。

三条可硬断言的恒等式（scripts/selftest.py 与 tests/test_bonds.py 校验）：

    D            = P_dirty * d/365 - sum(c_k * d_k/365)
    IRR          = (Invoice + sum(c_k) - P_dirty) / D
    BNOC_dirty(r)= -(IRR - r) * D
                 = GrossBasis - NetCarry(r)

其中 BNOC_dirty 是「每百元面值现券」口径，BNOC = BNOC_dirty / CF 是
「期货报价」口径。IRR 判据与 BNOC 判据可能因 D/CF 差异而分歧，本模块
显式报告该分歧，不静默取其一。
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

ACT365 = 365.0
STANDARD_COUPON = 0.03  # 中金所名义标准券票面利率，仅用于 CF 量级参考


def parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def days(a: date, b: date) -> int:
    """两个日期之间的实际天数（不含起始日，含结束日）。"""
    return (b - a).days


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) + months, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


@dataclass(frozen=True)
class Bond:
    bond_id: str
    coupon: float                 # 年票息率，小数，如 0.0235
    freq: int                     # 年付息次数
    maturity: date
    last_coupon: date             # 结算日所在付息期的上一付息日
    next_coupon: date             # 结算日所在付息期的下一付息日
    clean_price: float            # 每百元面值净价
    accrued: float                # 结算日应计利息，每百元面值
    conversion_factor: float      # 计算式输入字段；live 态须为交易所公布 CF，
                                  # degraded 声明态为 3% 名义标准券折现参考值
    modified_duration: float      # 修正久期，用于一阶情景重定价
    accrual_basis: str = "ACT/ACT"
    available_notional_wan: float = 0.0   # 可买/可交割存量，万元面值
    lendable_notional_wan: float = 0.0    # 可借券存量，万元面值

    @property
    def dirty_price(self) -> float:
        return self.clean_price + self.accrued

    @classmethod
    def from_dict(cls, d: dict) -> "Bond":
        return cls(
            bond_id=str(d["bond_id"]),
            coupon=float(d["coupon"]),
            freq=int(d["freq"]),
            maturity=parse_date(d["maturity"]),
            last_coupon=parse_date(d["last_coupon"]),
            next_coupon=parse_date(d["next_coupon"]),
            clean_price=float(d["clean_price"]),
            accrued=float(d["accrued_interest"]),
            conversion_factor=float(d["conversion_factor"]),
            modified_duration=float(d["modified_duration"]),
            accrual_basis=str(d.get("accrual_basis", "ACT/ACT")),
            available_notional_wan=float(d.get("available_notional_wan", 0.0)),
            lendable_notional_wan=float(d.get("lendable_notional_wan", 0.0)),
        )


def accrual_period(bond: Bond, on_date: date) -> tuple[date, date]:
    """把付息期滚动到覆盖 on_date 的区间。"""
    last, nxt = bond.last_coupon, bond.next_coupon
    step = 12 // bond.freq
    if step <= 0:
        raise ValueError("freq must be 1, 2, 3, 4, 6 or 12")
    guard = 0
    while on_date >= nxt:
        last, nxt = nxt, add_months(nxt, step)
        guard += 1
        if guard > 400:
            raise ValueError("accrual period did not converge forward")
    guard = 0
    while on_date < last:
        nxt, last = last, add_months(last, -step)
        guard += 1
        if guard > 400:
            raise ValueError("accrual period did not converge backward")
    return last, nxt


def accrued_on(bond: Bond, on_date: date) -> float:
    """每百元面值在 on_date 的应计利息。

    ACT/ACT(ICMA): c * days(last, on) / days(last, next)
    ACT/365      : c * days(last, on) / 365
    """
    if bond.accrual_basis not in ("ACT/ACT", "ACT/365"):
        raise ValueError(f"unsupported accrual_basis: {bond.accrual_basis}")
    last, nxt = accrual_period(bond, on_date)
    c = 100.0 * bond.coupon / bond.freq
    den = float(days(last, nxt)) if bond.accrual_basis == "ACT/ACT" else ACT365
    return c * days(last, on_date) / den


def coupon_flows(bond: Bond, settle: date, delivery: date) -> list[tuple[date, float]]:
    """(settle, delivery] 内的付息现金流，每笔为每百元面值票息。"""
    if delivery <= settle:
        raise ValueError("delivery must be after settle")
    step = 12 // bond.freq
    out: list[tuple[date, float]] = []
    dt = bond.next_coupon
    c = 100.0 * bond.coupon / bond.freq
    guard = 0
    while dt <= delivery and dt <= bond.maturity:
        if dt > settle:
            out.append((dt, c))
        dt = add_months(dt, step)
        guard += 1
        if guard > 400:
            raise ValueError("coupon schedule did not converge")
    return out


def invoice_price(bond: Bond, futures_price: float, delivery: date) -> float:
    """发票价格 = 期货交割结算价 * CF + 交割日应计利息（每百元面值）。"""
    return futures_price * bond.conversion_factor + accrued_on(bond, delivery)


def reference_cf(bond: Bond, delivery: date) -> float:
    """按 3% 名义标准券把交割日后现金流折现的「参考值」。

    两个用途必须分清（审计修复 2026-11-15，旧 docstring 与构造层实现矛盾）：
      1) degraded 声明式情景：`pricing.build_bond()` 把本函数结果写入
         input_snapshot 的 conversion_factor，作为可复算的参考值。
         此时 `cf_reference_gap` 只是 round(...,6) 的舍入误差，是**自反校验**，
         不构成 CF 有效性的独立证据。
      2) live 态：输入 CF 来自交易所公布值，本函数才真正能做量级交叉校验。

    本函数**不是**中金所 CF 公式：后者为 3% 名义票息、半年复利、
    按交割月首日折现、剩余期限取 3 个月的整数倍并剔除首期应计；
    本函数按 bond.freq 分期、以交割日的下一付息日折现，且情景券 freq=1。
    因此二者不要求相等，禁止把本函数结果称作「交易所公布 CF」。
    """
    step = 12 // bond.freq
    rate = STANDARD_COUPON / bond.freq
    c = 100.0 * bond.coupon / bond.freq
    dt = accrual_period(bond, delivery)[1]
    pv = 0.0
    k = 0
    guard = 0
    while dt <= bond.maturity:
        k += 1
        pv += (c + (100.0 if add_months(dt, step) > bond.maturity else 0.0)) / (1.0 + rate) ** k
        if add_months(dt, step) > bond.maturity:
            break
        dt = add_months(dt, step)
        guard += 1
        if guard > 400:
            break
    return pv / 100.0


def solve_irr_closed(bond: Bond, futures_price: float, settle: date,
                     delivery: date) -> tuple[float, dict]:
    """标准隐含回购利率：票息按 IRR 自身再投资，单利年化 ACT/365。

    P_dirty*(1+i*d/365) = Invoice + sum(c_k*(1+i*d_k/365))
    该方程为 i 的线性方程，可闭式求解；同时用二分法独立对拍。
    """
    p = bond.dirty_price
    d = days(settle, delivery)
    if d <= 0:
        raise ValueError("delivery must be after settle")
    flows = coupon_flows(bond, settle, delivery)
    inv = invoice_price(bond, futures_price, delivery)
    sum_c = sum(c for _, c in flows)
    sum_cd = sum(c * days(dt, delivery) / ACT365 for dt, c in flows)
    big_d = p * d / ACT365 - sum_cd
    if big_d <= 0:
        raise ValueError("non-positive effective financing base D")
    closed = (inv + sum_c - p) / big_d

    def residual(i: float) -> float:
        return p * (1.0 + i * d / ACT365) - inv - sum(
            c * (1.0 + i * days(dt, delivery) / ACT365) for dt, c in flows
        )

    lo, hi = -0.90, 2.0
    if residual(lo) * residual(hi) > 0:
        raise ValueError("IRR root is outside the bracket")
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if residual(mid) * residual(lo) <= 0:
            hi = mid
        else:
            lo = mid
    bisect = (lo + hi) / 2.0
    if abs(bisect - closed) > 1e-10:
        raise AssertionError("closed-form IRR disagrees with bisection")

    parts = {
        "days_to_delivery": d,
        "invoice_price": inv,
        "coupon_count": len(flows),
        "coupon_total": sum_c,
        "coupon_day_value": sum_cd,
        "effective_base_D": big_d,
        "forward_proceeds_no_reinvest": inv + sum_c,
        "irr_bisection_check": bisect,
    }
    return closed, parts


@dataclass(frozen=True)
class BondMetrics:
    bond_id: str
    conversion_factor: float
    clean_price: float
    dirty_price: float
    accrued_settle: float
    accrued_delivery: float
    invoice_price: float
    gross_basis: float
    net_carry: float
    bnoc_dirty: float
    bnoc: float
    irr_simple: float
    irr_compound: float
    irr_at_repo: float
    effective_base_D: float
    coupon_count: int
    days_to_delivery: int
    reference_cf_advisory: float
    cf_reference_gap: float


def evaluate_bond(bond: Bond, futures_price: float, settle: date,
                  delivery: date, repo_rate: float) -> BondMetrics:
    """在给定融资利率 r 下计算单券全套指标。"""
    p = bond.dirty_price
    d = days(settle, delivery)
    flows = coupon_flows(bond, settle, delivery)
    ai_del = accrued_on(bond, delivery)
    inv = invoice_price(bond, futures_price, delivery)
    sum_c = sum(c for _, c in flows)
    sum_cd = sum(c * days(dt, delivery) / ACT365 for dt, c in flows)

    irr_s, parts = solve_irr_closed(bond, futures_price, settle, delivery)
    big_d = parts["effective_base_D"]

    # 票息按市场回购利率 r 再投资的实务近似口径
    irr_at_r = (inv + sum_c + repo_rate * sum_cd - p) / (p * d / ACT365)
    # 复利年化换算（口径对照，非国内主口径）
    irr_compound = (1.0 + irr_s * d / ACT365) ** (ACT365 / d) - 1.0

    forward_at_r = inv + sum_c + repo_rate * sum_cd
    net_carry = (ai_del - bond.accrued) + sum_c + repo_rate * sum_cd - p * repo_rate * d / ACT365
    gross_basis = bond.clean_price - futures_price * bond.conversion_factor
    bnoc_dirty = p + p * repo_rate * d / ACT365 - forward_at_r
    bnoc = bnoc_dirty / bond.conversion_factor

    # 恒等式硬断言：任何移植/改动一旦破坏口径，立即失败
    if abs(bnoc_dirty - (gross_basis - net_carry)) > 1e-9:
        raise AssertionError("BNOC != GrossBasis - NetCarry")
    if abs(bnoc_dirty - (-(irr_s - repo_rate) * big_d)) > 1e-9:
        raise AssertionError("BNOC_dirty != -(IRR-r)*D")

    ref = reference_cf(bond, delivery)
    return BondMetrics(
        bond_id=bond.bond_id,
        conversion_factor=bond.conversion_factor,
        clean_price=bond.clean_price,
        dirty_price=p,
        accrued_settle=bond.accrued,
        accrued_delivery=ai_del,
        invoice_price=inv,
        gross_basis=gross_basis,
        net_carry=net_carry,
        bnoc_dirty=bnoc_dirty,
        bnoc=bnoc,
        irr_simple=irr_s,
        irr_compound=irr_compound,
        irr_at_repo=irr_at_r,
        effective_base_D=big_d,
        coupon_count=len(flows),
        days_to_delivery=d,
        reference_cf_advisory=ref,
        cf_reference_gap=ref - bond.conversion_factor,
    )


def rank_basket(metrics: Sequence[BondMetrics]) -> dict:
    """CTD 双判据 + 分歧检测 + 切换安全垫。"""
    if not metrics:
        raise ValueError("empty basket")
    by_irr = sorted(metrics, key=lambda m: m.irr_simple, reverse=True)
    by_bnoc = sorted(metrics, key=lambda m: m.bnoc)
    by_irr_repo = sorted(metrics, key=lambda m: m.irr_at_repo, reverse=True)
    ctd_irr, ctd_bnoc, ctd_repo = by_irr[0], by_bnoc[0], by_irr_repo[0]
    margin_bp = (by_irr[0].irr_simple - by_irr[1].irr_simple) * 10000.0 if len(by_irr) > 1 else float("nan")
    bnoc_margin_bp = (by_bnoc[1].bnoc - by_bnoc[0].bnoc) * 10000.0 if len(by_bnoc) > 1 else float("nan")
    return {
        "ctd_by_irr": ctd_irr.bond_id,
        "ctd_by_bnoc": ctd_bnoc.bond_id,
        "ctd_by_irr_at_repo": ctd_repo.bond_id,
        "ctd_disagreement": ctd_irr.bond_id != ctd_bnoc.bond_id,
        "irr_rank": [m.bond_id for m in by_irr],
        "bnoc_rank": [m.bond_id for m in by_bnoc],
        "top2_irr_margin_bp": margin_bp,
        "top2_bnoc_margin_bp": bnoc_margin_bp,
        "ctd_irr": ctd_irr.irr_simple,
        "ctd_irr_at_repo": ctd_repo.irr_at_repo,
        "ctd_bnoc": ctd_bnoc.bnoc,
    }


def reprice_bond(bond: Bond, yield_shift: float) -> Bond:
    """一阶久期重定价（情景专用，非真实估值）：dP/P = -D_mod * dy。"""
    new_clean = bond.clean_price * (1.0 - bond.modified_duration * yield_shift)
    return Bond(
        bond_id=bond.bond_id, coupon=bond.coupon, freq=bond.freq,
        maturity=bond.maturity, last_coupon=bond.last_coupon,
        next_coupon=bond.next_coupon, clean_price=new_clean,
        accrued=bond.accrued, conversion_factor=bond.conversion_factor,
        modified_duration=bond.modified_duration,
        accrual_basis=bond.accrual_basis,
        available_notional_wan=bond.available_notional_wan,
        lendable_notional_wan=bond.lendable_notional_wan,
    )


def hedge_ratio(mode: str, bond: Bond, futures_price: float,
                ctd_duration: float) -> float:
    """每 100 万面值现券应卖出的期货手数（面值 100 万/手）。

    naive_1to1 : 固定 1 手，不考虑 CF 与久期（冻结基线 B）
    cf_weighted: h = CF（发票价口径对冲）
    dv01       : h = D_bond * P_clean / (D_ctd * F)，一阶 DV01 中性近似
    """
    if mode == "naive_1to1":
        return 1.0
    if mode == "cf_weighted":
        return bond.conversion_factor
    if mode == "dv01":
        if ctd_duration <= 0 or futures_price <= 0:
            raise ValueError("invalid ctd_duration / futures_price")
        return bond.modified_duration * bond.clean_price / (ctd_duration * futures_price)
    raise ValueError(f"unknown hedge mode: {mode}")
