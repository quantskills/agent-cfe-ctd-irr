# -*- coding: utf-8 -*-
"""情景扫描、CTD 切换边界、三条策略对照与可执行边界。

题目要求的四类验证全部在这里落地：
  1) 融资变化扫描（r 网格）      -> 净基差判据下的 CTD 切换与可执行门槛
  2) 收益率曲线平移扫描（dy 网格）-> IRR 判据下的 CTD 切换边界
  3) 与买入持有 / 简单对冲对照  -> 冻结基线 A/B vs 研究方案 C
  4) 可执行边界与容量边界        -> 全成本覆盖、CTD 安全垫、券存量/借券/限仓

所有结论只在冻结输入快照内成立；情景是参数网格，不是历史时间序列。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Sequence

from .bonds import (
    Bond, BondMetrics, evaluate_bond, rank_basket, reprice_bond, hedge_ratio,
    coupon_flows, accrued_on, days, ACT365,
)


def evaluate_point(bonds: Sequence[Bond], futures_price: float, settle: date,
                   delivery: date, repo_rate: float) -> tuple[list[BondMetrics], dict]:
    metrics = [evaluate_bond(b, futures_price, settle, delivery, repo_rate) for b in bonds]
    return metrics, rank_basket(metrics)


def _find_switches(rows: list[dict], x_key: str, ctd_key: str) -> list[dict]:
    """在扫描网格上找 CTD 切换点，用线性插值给出边界估计。"""
    out = []
    for prev, cur in zip(rows, rows[1:]):
        if prev[ctd_key] != cur[ctd_key]:
            out.append({
                "axis": x_key,
                "from_ctd": prev[ctd_key],
                "to_ctd": cur[ctd_key],
                "bracket": [prev[x_key], cur[x_key]],
                "boundary_estimate": (prev[x_key] + cur[x_key]) / 2.0,
                "resolution": abs(cur[x_key] - prev[x_key]),
            })
    return out


def financing_scan(bonds: Sequence[Bond], futures_price: float, settle: date,
                   delivery: date, r_grid: Sequence[float]) -> tuple[list[dict], list[dict]]:
    rows = []
    for r in r_grid:
        metrics, rank = evaluate_point(bonds, futures_price, settle, delivery, r)
        rows.append({
            "repo_rate": round(r, 8),
            "repo_rate_pct": round(r * 100, 4),
            "ctd_by_irr": rank["ctd_by_irr"],
            "ctd_by_bnoc": rank["ctd_by_bnoc"],
            "ctd_by_irr_at_repo": rank["ctd_by_irr_at_repo"],
            "ctd_disagreement": rank["ctd_disagreement"],
            "ctd_irr": rank["ctd_irr"],
            "ctd_irr_at_repo": rank["ctd_irr_at_repo"],
            "ctd_bnoc": rank["ctd_bnoc"],
            "top2_irr_margin_bp": rank["top2_irr_margin_bp"],
            "top2_bnoc_margin_bp": rank["top2_bnoc_margin_bp"],
            "irr_by_bond": {m.bond_id: m.irr_simple for m in metrics},
            "bnoc_by_bond": {m.bond_id: m.bnoc for m in metrics},
        })
    switches_irr = _find_switches(rows, "repo_rate", "ctd_by_irr")
    switches_bnoc = _find_switches(rows, "repo_rate", "ctd_by_bnoc")
    return rows, switches_irr + switches_bnoc


def yield_shift_scan(bonds: Sequence[Bond], futures_price: float, settle: date,
                     delivery: date, repo_rate: float,
                     dy_grid: Sequence[float]) -> tuple[list[dict], list[dict]]:
    base_metrics, base_rank = evaluate_point(bonds, futures_price, settle, delivery, repo_rate)
    ctd_duration = next(m for m in base_metrics if m.bond_id == base_rank["ctd_by_irr"])
    d_f = next(b.modified_duration for b in bonds if b.bond_id == base_rank["ctd_by_irr"])

    rows = []
    for dy in dy_grid:
        repriced = [reprice_bond(b, dy) for b in bonds]
        f_del = futures_price * (1.0 - d_f * dy)
        metrics, rank = evaluate_point(repriced, f_del, settle, delivery, repo_rate)
        rows.append({
            "yield_shift": round(dy, 8),
            "yield_shift_bp": round(dy * 10000, 2),
            "futures_price_at_delivery_proxy": round(f_del, 6),
            "ctd_by_irr": rank["ctd_by_irr"],
            "ctd_by_bnoc": rank["ctd_by_bnoc"],
            "ctd_disagreement": rank["ctd_disagreement"],
            "ctd_irr": rank["ctd_irr"],
            "ctd_bnoc": rank["ctd_bnoc"],
            "top2_irr_margin_bp": rank["top2_irr_margin_bp"],
            "irr_by_bond": {m.bond_id: m.irr_simple for m in metrics},
        })
    switches = _find_switches(rows, "yield_shift", "ctd_by_irr")
    return rows, switches


def cost_band(snap: dict, bond: Bond, settle: date, delivery: date) -> dict:
    """把各项摩擦成本统一换算成年化利率带与每百元价格带。"""
    c = snap["costs"]
    d = days(settle, delivery)
    p = bond.dirty_price
    price_bp = (c["transaction_cost_bp"] + c["slippage_bp"]) / 10000.0
    price_cost = p * price_bp + c["delivery_fee_per_100"]
    rate_cost = price_cost / p * ACT365 / d
    buffer_rate = c["ctd_switch_buffer_bp"] / 10000.0
    return {
        "days": d,
        "price_cost_per_100": price_cost,
        "annualised_cost_rate": rate_cost,
        "ctd_switch_buffer_rate": buffer_rate,
        "all_in_rate": rate_cost + buffer_rate,
        "all_in_rate_bp": (rate_cost + buffer_rate) * 10000,
        "min_bnoc_to_cover_per_100": price_cost,
    }


def strategy_pnl(bond: Bond, futures_price: float, settle: date, delivery: date,
                 repo_rate: float, dy: float, d_f: float, costs: dict,
                 mode: str, ctd_duration: float) -> dict:
    """三条策略在单个 (dy, r) 情景下、每百元面值现券的到期 P&L。

    终值口径（三条策略**不相同**，差异被显式拆出，不允许混读）：

    * **A 买入持有**：没有期货腿，也没有交割义务，只能在**现券市场**退出。
      终值 M = 交割日净价（久期一阶重定价）+ 交割日应计 + 期间票息及再投资。
    * **B / C 现金套利**：有期货空头，到期**实物交割**。终值 I = 发票价
      = 交割结算价代理 × CF + 交割日应计 + 期间票息及再投资。
    * 因此 ``A − B = 估值基准差 (M − I) + 交割费 − 期货腿损益``。
      A 与 B 的差额**不是**纯粹的「对冲价值」，必须先扣除估值基准差。
      该恒等式由 runtime/validation.py 从输入独立重算对拍，不靠本文档自述。

    注意 ``bond`` 必须是**期初（结算日）**的那只券：买入成本固定在期初价，
    dy 只作用于交割日的价格与期货结算价代理。传入已按 dy 重定价的券会
    让同一冲击被施加两次。
    """
    d = days(settle, delivery)
    p = bond.dirty_price
    flows = coupon_flows(bond, settle, delivery)
    ai_del = accrued_on(bond, delivery)
    sum_c = sum(c for _, c in flows)
    sum_cd = sum(c * days(dt, delivery) / ACT365 for dt, c in flows)
    f_del = futures_price * (1.0 - d_f * dy)
    invoice = f_del * bond.conversion_factor + ai_del
    forward = invoice + sum_c + repo_rate * sum_cd
    financing = p * repo_rate * d / ACT365
    friction_bp = (costs["transaction_cost_bp"] + costs["slippage_bp"]) / 10000.0
    friction = p * friction_bp
    delivery_fee = costs["delivery_fee_per_100"]

    # 现券市场退出口径（仅 A 使用）：交割日净价按一阶久期重定价。
    clean_after = bond.clean_price * (1.0 - bond.modified_duration * dy)
    market_proceeds = clean_after + ai_del + sum_c + repo_rate * sum_cd
    pnl_a = market_proceeds - p - financing - friction

    # 期货腿：每 100 万面值现券卖出 h 手；期货价从 f0 变到 f_del。
    h = hedge_ratio(mode, bond, futures_price, ctd_duration)
    hedge_pnl = -h * (f_del - futures_price)

    # 交割退出口径（B / C 使用）：交割现券收发票价，并支付交割费。
    pnl_h = forward - p - financing - friction - delivery_fee + hedge_pnl
    # 同一现金腿、同一对冲，但改走现券市场退出：用于把 A−B 完整拆开。
    pnl_h_market = market_proceeds - p - financing - friction + hedge_pnl

    return {
        "pnl_buy_hold": pnl_a,
        "pnl_hedged": pnl_h,
        "pnl_hedged_market_exit": pnl_h_market,
        "hedge_ratio_lots": h,
        "hedge_leg_pnl": hedge_pnl,
        "futures_price_delivery_proxy": f_del,
        "forward_proceeds": forward,
        "market_proceeds": market_proceeds,
        "valuation_basis_gap": market_proceeds - forward,
        "financing_cost": financing,
        "friction_cost": friction + delivery_fee,
        "delivery_fee": delivery_fee,
    }


def strategy_comparison(snap: dict, bonds: Sequence[Bond], settle: date,
                        delivery: date, r_grid: Sequence[float],
                        dy_grid: Sequence[float]) -> list[dict]:
    """三策略对照网格。**严格事前执行语义**（v2）。

    事前 / 事后两层必须分清，否则会把不可实现的收益算进结论：

    * **事前（结算日可观测）**：期货价 f0、给定融资利率 r、期初曲线。
      据此确定 (1) 期初 CTD —— 也就是**实际持仓标的**，
      (2) 期初 IRR、期初前二名安全垫、(3) 全成本带，
      (4) C 的执行闸门。四者同源，决策与持仓不再分属两个信息集。
    * **事后（仅作诊断）**：收益率平移 dy 之后谁才是最优交割券
      （``post_shock_ctd``）、切换造成的机会成本
      （``switch_opportunity_bp``）、以及「若事先就知道会换券」
      的 P&L 反事实（``hindsight_C_pnl_per_100``，**不可实现**）。

    v1 的缺陷：闸门按期初曲线判定，持仓却取冲击后的 CTD —— 事后选券。
    v2 一律持有期初 CTD；CTD 切换因此成为持仓承担的风险，而非免费收益。

    期货腿久期代理 ``d_f`` 取**期初 CTD** 的修正久期，与持仓标的同源，
    且结算日即可知（事前可知），不含任何事后信息。
    """
    f0 = snap["meta"]["futures_price"]
    costs = snap["costs"]

    # ---------- 事前决策层：只用结算日可观测的信息 ----------
    entry_by_r: dict[float, dict] = {}
    for r in r_grid:
        _em, erank = evaluate_point(bonds, f0, settle, delivery, r)
        ectd = next(b for b in bonds if b.bond_id == erank["ctd_by_irr"])
        eband = cost_band(snap, ectd, settle, delivery)
        margin_ok = (not math.isnan(erank["top2_irr_margin_bp"])) and \
            erank["top2_irr_margin_bp"] > costs["ctd_switch_buffer_bp"]
        cover_ok = (erank["ctd_irr"] - r) > eband["all_in_rate"]
        entry_by_r[r] = {
            "ctd_id": erank["ctd_by_irr"],
            "ctd_bond": ectd,
            "ctd_irr": erank["ctd_irr"],
            "ctd_bnoc": erank["ctd_bnoc"],
            "ctd_disagreement": erank["ctd_disagreement"],
            "top2_irr_margin_bp": erank["top2_irr_margin_bp"],
            "band": eband,
            "cover_ok": bool(cover_ok),
            "margin_ok": bool(margin_ok),
            "executed": bool(margin_ok and cover_ok),
        }

    rows = []
    idx = 0
    for dy in dy_grid:
        repriced = [reprice_bond(b, dy) for b in bonds]
        for r in r_grid:
            entry = entry_by_r[r]
            # 持仓标的 = 期初 CTD（结算日可判定）。不随冲击改变。
            held = entry["ctd_bond"]
            # 期货腿久期代理 = 期初 CTD 的修正久期，与持仓同一只券，事前可知。
            d_f = held.modified_duration
            f_del_proxy = f0 * (1.0 - d_f * dy)

            # ---------- 事后诊断层：只用于量化 CTD 切换的风险 ----------
            post_metrics, post_rank = evaluate_point(repriced, f_del_proxy,
                                                     settle, delivery, r)
            post_ctd_id = post_rank["ctd_by_irr"]
            held_metric = next(m for m in post_metrics
                               if m.bond_id == held.bond_id)
            switch_gap_bp = (post_rank["ctd_irr"] - held_metric.irr_simple) * 10000.0

            a = strategy_pnl(held, f0, settle, delivery, r, dy, d_f, costs,
                             "naive_1to1", d_f)
            b = strategy_pnl(held, f0, settle, delivery, r, dy, d_f, costs,
                             "naive_1to1", d_f)
            c = strategy_pnl(held, f0, settle, delivery, r, dy, d_f, costs,
                             "dv01", d_f)

            executed = entry["executed"]
            if not executed:
                reason = []
                if not entry["cover_ok"]:
                    reason.append("IRR-r does not cover all-in cost (ex-ante entry)")
                if not entry["margin_ok"]:
                    reason.append("CTD switch margin below buffer (ex-ante entry)")
                c_pnl = 0.0
                hindsight_c = 0.0
            else:
                reason = []
                c_pnl = c["pnl_hedged"]
                # 事后反事实：若事先就知道交割时会换券并改持事后最优券。
                # **不可实现**。注意它不是上界：事后最优券按 IRR 选出，
                # 而发票价 = F×CF + AI 经 CF 缩放，IRR 更高的券其绝对 P&L
                # 未必更高，故该差额可正可负。
                post_bond = next(b0 for b0 in bonds if b0.bond_id == post_ctd_id)
                hs = strategy_pnl(post_bond, f0, settle, delivery, r, dy, d_f,
                                  costs, "dv01", d_f)
                hindsight_c = hs["pnl_hedged"]

            rows.append({
                "grid_index": idx,
                "yield_shift_bp": round(dy * 10000, 2),
                "repo_rate_pct": round(r * 100, 4),
                # 事前：持仓与决策
                "held_bond": held.bond_id,
                "entry_ctd_irr_pct": round(entry["ctd_irr"] * 100, 4),
                "entry_irr_minus_repo_bp": round((entry["ctd_irr"] - r) * 10000, 3),
                "entry_top2_irr_margin_bp": round(entry["top2_irr_margin_bp"], 3),
                "entry_ctd_disagreement": entry["ctd_disagreement"],
                "all_in_cost_bp": round(entry["band"]["all_in_rate_bp"], 3),
                # 事后：CTD 切换诊断
                "post_shock_ctd": post_ctd_id,
                "ctd_switched": post_ctd_id != held.bond_id,
                "switch_opportunity_bp": round(switch_gap_bp, 3),
                "held_irr_pct": round(held_metric.irr_simple * 100, 4),
                "held_irr_minus_repo_bp": round((held_metric.irr_simple - r) * 10000, 3),
                "post_shock_irr_pct": round(post_rank["ctd_irr"] * 100, 4),
                "post_shock_irr_minus_repo_bp": round((post_rank["ctd_irr"] - r) * 10000, 3),
                # 兼容旧列名：持仓券口径的 IRR−r（不再是事后最优券的）
                "irr_minus_repo_bp": round((held_metric.irr_simple - r) * 10000, 3),
                # P&L
                "A_buy_hold_pnl_per_100": round(a["pnl_buy_hold"], 6),
                "B_simple_hedge_pnl_per_100": round(b["pnl_hedged"], 6),
                "C_research_pnl_per_100": round(c_pnl, 6),
                "B_market_exit_pnl_per_100": round(b["pnl_hedged_market_exit"], 6),
                "hindsight_C_pnl_per_100": round(hindsight_c, 6),
                # 终值口径拆解
                "cash_terminal_market_per_100": round(a["market_proceeds"], 6),
                "cash_terminal_delivery_per_100": round(b["forward_proceeds"], 6),
                "valuation_basis_gap_per_100": round(b["valuation_basis_gap"], 6),
                "hedge_leg_pnl_B": round(b["hedge_leg_pnl"], 6),
                "hedge_leg_pnl_C": round(c["hedge_leg_pnl"], 6),
                "delivery_fee_per_100": round(b["delivery_fee"], 6),
                # 执行
                "C_executed": executed,
                "C_skip_reason": "; ".join(reason),
                "C_hedge_ratio_lots": round(c["hedge_ratio_lots"], 6),
                "B_hedge_ratio_lots": 1.0,
            })
            idx += 1
    return rows


def executable_boundary(snap: dict, bonds: Sequence[Bond], settle: date,
                        delivery: date, fin_rows: list[dict],
                        switches: list[dict]) -> dict:
    f0 = snap["meta"]["futures_price"]
    r = snap["costs"]["repo_rate"]
    metrics, rank = evaluate_point(bonds, f0, settle, delivery, r)
    ctd = next(b for b in bonds if b.bond_id == rank["ctd_by_irr"])
    band = cost_band(snap, ctd, settle, delivery)
    cap = snap["capacity"]

    # 容量边界：券存量 / 单账户限仓，二者取最小。
    # 正向 cash-and-carry 是「买现券 + 卖期货」，不占用借券额度；
    # 借券存量约束的是反向腿（卖空现券），而反向腿在本项目判定为不可执行，
    # 因此 lendable 不得进入正向 binding（否则会把 20 万压到 8.8 万）。
    deliverable_cap_wan = sum(b.available_notional_wan for b in bonds)
    lendable_cap_wan = sum(b.lendable_notional_wan for b in bonds)
    position_cap_wan = cap["max_position_lots_per_account"] * snap["meta"]["contract_notional_wan"]
    ctd_lendable_wan = ctd.lendable_notional_wan
    binding = min(deliverable_cap_wan, position_cap_wan)
    binding_name = ("deliverable_stock" if binding == deliverable_cap_wan
                    else "position_limit")

    # 反向套利（卖空现券 + 买期货）：借券是成本而非收益；
    # 且本机/模拟赛均无卖空现券与债券借贷接口，故判定为不可执行。
    forward_spread_bp = (rank["ctd_irr"] - r) * 10000.0
    reverse_spread_if_short_available_bp = \
        (r - rank["ctd_irr"]) * 10000.0 - snap["costs"]["bond_lend_fee"] * 10000.0
    reverse_executable = False

    # 审计修复（2026-11-15）：把「混合来源判定无效」写成机器可读字段。
    # 之前只有 report.md 的 L1 有文字告警，而 JSON / 发布态快照 /
    # strategy_comparison.csv 仍带 forward_executable=True，下游只读 JSON 会误用。
    meta = snap["meta"]
    is_mixed = bool(meta.get("official_anchor")) and meta.get("source_mode") != "live"
    is_live = meta.get("source_mode") == "live"
    if is_live:
        verdict_status = "LIVE_VALID"
    elif is_mixed:
        verdict_status = "INVALID_MIXED_STATE"
    else:
        verdict_status = "CONDITIONAL_VALID"
    conclusion_valid = not is_mixed
    invalidated_by = [
        "期货价/交割日来自官方行情，现券全价、应计、CF、融资与借券成本仍是声明式情景值；"
        "两类数据不在同一市场状态，价差被年化成虚假基差。",
        "缺交易所公布可交割券清单、现券估值、公布 CF 与市场回购/借券利率。",
        "本判定仅用于量化数据缺口的影响，不得作为市场结论或交易依据。",
    ] if is_mixed else []

    return {
        "as_of_date": meta["as_of_date"],
        "data_version": meta["data_version"],
        # ↓ 机器可读的判定有效性（下游必须先读这三个字段）
        "verdict_status": verdict_status,
        "conclusion_valid": conclusion_valid,
        "forward_executable_usable": conclusion_valid,
        "invalidated_by": invalidated_by,
        "ctd": rank["ctd_by_irr"],
        "forward_spread_IRR_minus_repo_bp": round(forward_spread_bp, 3),
        "all_in_cost_bp": round(band["all_in_rate_bp"], 3),
        "forward_executable": bool(forward_spread_bp > band["all_in_rate_bp"]),
        "reverse_executable": reverse_executable,
        "reverse_spread_if_short_available_bp": round(reverse_spread_if_short_available_bp, 3),
        "reverse_spread_note": "反向腿需借券（借券费是成本，已从价差扣除）；且比赛接口与本机数据均不支持卖空现券/债券借贷，故判定不可执行，不给出可套利结论。",
        "min_bnoc_to_cover_per_100": round(band["min_bnoc_to_cover_per_100"], 6),
        "ctd_switch_buffer_bp": snap["costs"]["ctd_switch_buffer_bp"],
        "top2_irr_margin_bp": round(rank["top2_irr_margin_bp"], 3),
        "ctd_margin_safe": bool(rank["top2_irr_margin_bp"] > snap["costs"]["ctd_switch_buffer_bp"]),
        "switch_boundaries_found": len(switches),
        "capacity": {
            "deliverable_stock_wan": deliverable_cap_wan,
            "lendable_stock_wan": lendable_cap_wan,
            "ctd_lendable_stock_wan": ctd_lendable_wan,
            "position_limit_wan": position_cap_wan,
            "binding_cap_wan": binding,
            "binding_constraint": binding_name,
            "assumed_participation_ratio": cap["assumed_participation_ratio"],
            "executable_notional_wan_at_participation": round(binding * cap["assumed_participation_ratio"], 2),
            "note": "容量参数同为情景给定，不是交易所限仓或真实存量；对标原 Q7 容量边界的『最高档』口径，仅给出口径与算法。",
        },
        "not_riskless_arbitrage_reasons": [
            "CTD 可切换：空头持有交割期权，净基差不等于零不等于可锁定利润。",
            "交割结算价由交易所规则产生，不能用当前期货价代理后宣称已锁定。",
            "现券买卖价差、借券可得性与回购利率浮动未在本快照中逐笔建模。",
            "转换因子为 3% 名义标准券折现的参考值，非交易所公布值；情景券为年付息，与我国国债半年付息惯例不同。",
            f"input_origin={meta.get('input_origin')}，source_mode={meta.get('source_mode')}，market_conclusion={meta.get('market_conclusion')}。",
        ] + (["本 run 为混合来源，上述 IRR / 可执行判定数值无效，见 verdict_status 与 invalidated_by。"]
             if is_mixed else []),
    }


def backtest_metrics(rows: list[dict], data_version: str) -> dict:
    """规范 §9.2 分析型最低集；明确标注这是参数网格，不是历史回测。"""
    from .contest_replay import as_flag

    def stats(subset: list[dict], key: str) -> dict:
        if not subset:
            return {"n": 0}
        # 旧版 CSV（v1 语义）没有新增列；缺列时返回 n=0 而不是抛 KeyError，
        # 让 backtest 子命令仍能对历史产物重算。
        if key not in subset[0]:
            return {"n": 0, "column_absent": True}
        vals = [r[key] for r in subset]
        n = len(vals)
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        win = sum(1 for v in vals if v > 0) / n
        equity, peak, mdd = 0.0, 0.0, 0.0
        for v in vals:
            equity += v
            peak = max(peak, equity)
            mdd = min(mdd, equity - peak)
        return {
            "n": n,
            "mean_pnl_per_100": round(mean, 6),
            "std_pnl_per_100": round(math.sqrt(var), 6),
            "direction_accuracy_pnl_gt_0": round(win, 4),
            "max_drawdown_of_cumulative_scenario_pnl": round(mdd, 6),
        }

    in_grid = [r for r in rows if r["grid_index"] % 2 == 0]
    holdout = [r for r in rows if r["grid_index"] % 2 == 1]
    # 换手代理：事后最优交割券在网格上的变化次数（市场侧属性）。
    switches = sum(
        1 for prev, cur in zip(rows, rows[1:])
        if prev.get("post_shock_ctd") != cur.get("post_shock_ctd")
    )
    executed = [r for r in rows if as_flag(r["C_executed"])]
    skipped = [r for r in rows if not as_flag(r["C_executed"])]
    switched = [r for r in rows if "ctd_switched" in r and as_flag(r["ctd_switched"])]
    buckets = {}
    for lo, hi, label in [(-1e9, -25, "dy<=-25bp"), (-25, 25, "-25bp<dy<25bp"), (25, 1e9, "dy>=25bp")]:
        sub = [r for r in rows if lo <= r["yield_shift_bp"] < hi]
        buckets[label] = {
            "A": stats(sub, "A_buy_hold_pnl_per_100"),
            "B": stats(sub, "B_simple_hedge_pnl_per_100"),
            "C": stats(sub, "C_research_pnl_per_100"),
        }
    # v1 语义的旧产物没有这三项诊断列；缺失时显式标注 not_available，
    # 不得静默给出 0（0 会被误读成「没有切换风险」）。
    # 守卫标志必须先算，再用于后续计算 —— 否则旧 CSV 会在这里抛 KeyError。
    has_switch_cols = bool(rows) and "switch_opportunity_bp" in rows[0]
    has_hindsight = bool(rows) and "hindsight_C_pnl_per_100" in rows[0]
    has_basis = bool(rows) and "valuation_basis_gap_per_100" in rows[0]

    basis_gap = stats(rows, "valuation_basis_gap_per_100")
    switch_gap = (round(sum(r["switch_opportunity_bp"] for r in switched) / len(switched), 4)
                  if switched else 0.0)
    hindsight_edge = (round(sum(r["hindsight_C_pnl_per_100"] - r["C_research_pnl_per_100"]
                                for r in rows) / len(rows), 6)
                      if has_hindsight and rows else 0.0)
    return {
        "sample_definition": "参数网格 (yield_shift x repo_rate) 的情景回放，不是历史时间序列。",
        "rebalance_rule": (
            "事前口径：每个情景都持结算日判定的 CTD（可见信息），"
            "按期初曲线判定执行闸门；C 的对冲比率用 DV01，B 固定 1:1。"
            "CTD 切换作为风险进入 P&L，不允许事后换券。"),
        "grid_size": len(rows),
        "data_version": data_version,
        "direction_accuracy_definition": "P&L(每百元面值) > 0 的情景占比。",
        "buckets_by_yield_shift": buckets,
        "overall": {
            "A": stats(rows, "A_buy_hold_pnl_per_100"),
            "B": stats(rows, "B_simple_hedge_pnl_per_100"),
            "C": stats(rows, "C_research_pnl_per_100"),
            "C_executed_only": stats(executed, "C_research_pnl_per_100"),
            "C_skipped_only": stats(skipped, "C_research_pnl_per_100"),
        },
        "parameter_grid_holdout": {
            "definition": "按 grid_index 奇偶切分的参数留出；不等价于时间序列样本外。",
            "in_grid": {"A": stats(in_grid, "A_buy_hold_pnl_per_100"),
                        "B": stats(in_grid, "B_simple_hedge_pnl_per_100"),
                        "C": stats(in_grid, "C_research_pnl_per_100")},
            "holdout": {"A": stats(holdout, "A_buy_hold_pnl_per_100"),
                        "B": stats(holdout, "B_simple_hedge_pnl_per_100"),
                        "C": stats(holdout, "C_research_pnl_per_100")},
        },
        "turnover_ctd_switches": switches,
        "turnover_definition": "事后最优交割券在相邻网格情景上的变化次数（市场侧切换频率代理）。",
        "ctd_switch_exposure": {
            "available": has_switch_cols,
            "n_switched": len(switched) if has_switch_cols else None,
            "share": (round(len(switched) / len(rows), 4) if (rows and has_switch_cols) else None),
            "mean_switch_opportunity_bp": switch_gap if has_switch_cols else None,
            "max_switch_opportunity_bp": (round(max((r["switch_opportunity_bp"] for r in switched),
                                                     default=0.0), 3) if has_switch_cols else None),
            "definition": (
                "持仓券在事后不再是最优交割券的情景占比，以及此时的事后机会成本。"
                "该机会成本不可实现，只能作为风险度量。"),
        } if has_switch_cols else {
            "available": False,
            "n_switched": None,
            "share": None,
            "mean_switch_opportunity_bp": None,
            "max_switch_opportunity_bp": None,
            "definition": "本产物为 v1 语义，无 CTD 切换暴露诊断列；不得据此断言无切换风险。",
        },
        "valuation_basis": {
            "available": has_basis,
            "mean_gap_per_100": basis_gap.get("mean_pnl_per_100"),
            "max_abs_gap_per_100": (round(max((abs(r["valuation_basis_gap_per_100"])
                                              for r in rows), default=0.0), 6)
                                    if has_basis else None),
            "definition": (
                "A 走现券市场退出、B/C 走实物交割，两者终值口径不同，差额即本项。"
                "读 A−B 时必须先扣除本项，再把剩余部分归因于对冲。"),
        },
        "hindsight_switch_counterfactual": {
            "available": has_hindsight,
            "mean_edge_per_100": hindsight_edge if has_hindsight else None,
            "min_edge_per_100": (round(min((r["hindsight_C_pnl_per_100"]
                                            - r["C_research_pnl_per_100"]
                                            for r in rows), default=0.0), 6)
                                  if has_hindsight else None),
            "max_edge_per_100": (round(max((r["hindsight_C_pnl_per_100"]
                                            - r["C_research_pnl_per_100"]
                                            for r in rows), default=0.0), 6)
                                  if has_hindsight else None),
            "definition": (
                "若事先知道交割时会换券并改持事后最优券，C 的 P&L 会变化多少。"
                "这是**不可实现**的事后反事实，仅用于量化 CTD 切换的影响。"
                "它**不是上界**：事后最优券按 IRR 选出，而发票价经转换因子缩放，"
                "IRR 更高的券其 CF 可能更低、绝对 P&L 反而更小，"
                "因此该差额可正可负。"),
        } if has_hindsight else {
            "available": False,
            "definition": "本产物为 v1 语义，无事后换券反事实诊断列。",
        },
        "caveat": "情景 P&L 为公式条件结果，不能解读为策略收益预期或实盘胜率。",
    }
