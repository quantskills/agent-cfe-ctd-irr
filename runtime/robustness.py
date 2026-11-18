# -*- coding: utf-8 -*-
"""规范 §8.2 稳定性 / 微扰检验。

对成本、CF、期货价、融资利率做小幅微扰，检查 CTD、可执行判定与执行率
是否无解释地剧烈翻转。参数改动全部进入 data_version（见 build_snapshot.py）。
"""
from __future__ import annotations

import copy
from datetime import date
from typing import Sequence

from .bonds import Bond
from .scenarios import evaluate_point, cost_band, strategy_comparison


def _boundary(snap: dict, bonds: Sequence[Bond], settle: date, delivery: date):
    f0 = float(snap["meta"]["futures_price"])
    r = float(snap["costs"]["repo_rate"])
    _ms, rank = evaluate_point(bonds, f0, settle, delivery, r)
    ctd = next(b for b in bonds if b.bond_id == rank["ctd_by_irr"])
    band = cost_band(snap, ctd, settle, delivery)
    spread_bp = (rank["ctd_irr"] - r) * 10000.0
    return {
        "ctd": rank["ctd_by_irr"],
        "irr_pct": round(rank["ctd_irr"] * 100, 6),
        "top2_margin_bp": round(rank["top2_irr_margin_bp"], 4),
        "spread_bp": round(spread_bp, 4),
        "all_in_cost_bp": round(band["all_in_rate_bp"], 4),
        "forward_executable": bool(spread_bp > band["all_in_rate_bp"]),
        "margin_safe": bool(rank["top2_irr_margin_bp"] > snap["costs"]["ctd_switch_buffer_bp"]),
    }


def run_robustness(snap: dict, bonds: Sequence[Bond], settle: date,
                   delivery: date, r_grid: Sequence[float],
                   dy_grid: Sequence[float]) -> dict:
    base = _boundary(snap, bonds, settle, delivery)
    # 9 点子网格的中间档必须落在基准融资假设（快照 costs.repo_rate）上，
    # 否则子网格执行率与全网格口径不可比（会系统性偏离）。
    # 仍保持 3×3=9 点，字段名 exec_rate_9grid 才成立。
    base_repo = float(snap["costs"]["repo_rate"])
    mid_r = min(r_grid, key=lambda x: abs(x - base_repo))
    small_r = sorted({r_grid[0], mid_r, r_grid[-1]})
    small_dy = [dy_grid[0], dy_grid[len(dy_grid) // 2], dy_grid[-1]]

    def exec_rate(s: dict, bs: Sequence[Bond]) -> float:
        rows = strategy_comparison(s, bs, settle, delivery, small_r, small_dy)
        if not rows:
            return 0.0
        from .contest_replay import as_flag
        return sum(1 for x in rows if as_flag(x["C_executed"])) / len(rows)

    base_rate = exec_rate(snap, bonds)
    cases: list[dict] = []

    def add(name: str, mutate) -> None:
        s = copy.deepcopy(snap)
        bs = [copy.copy(b) for b in bonds]
        mutate(s, bs)
        try:
            out = _boundary(s, bs, settle, delivery)
            out["exec_rate_9grid"] = round(exec_rate(s, bs), 4)
        except Exception as exc:                     # noqa: BLE001
            out = {"error": str(exc)}
        cases.append({"perturbation": name, "result": out})

    for pct in (-0.10, 0.10):
        add(f"all friction costs {pct:+.0%}", lambda s, b, p=pct: s["costs"].update({
            k: s["costs"][k] * (1 + p) for k in
            ("transaction_cost_bp", "slippage_bp", "delivery_fee_per_100")}))
    for delta in (-0.0002, 0.0002):
        add(f"repo_rate {delta:+.4f}", lambda s, b, d=delta:
            s["costs"].update({"repo_rate": s["costs"]["repo_rate"] + d}))
    for delta in (-2.0, 2.0):
        add(f"ctd_switch_buffer {delta:+.0f}bp", lambda s, b, d=delta:
            s["costs"].update({"ctd_switch_buffer_bp": max(0.0, s["costs"]["ctd_switch_buffer_bp"] + d)}))
    for delta in (-0.0005, 0.0005):
        def cf_mutate(s, b, d=delta):
            for x in b:
                x = x  # Bond is frozen; replace below
            s["_cf_shift"] = d
        # Bond 是 frozen dataclass，用 replace 生成新列表由外层处理
        def cf_case(s, b, d=delta):
            from dataclasses import replace as _replace
            for i, x in enumerate(b):
                b[i] = _replace(x, conversion_factor=x.conversion_factor + d)
        add(f"conversion_factor {delta:+.4f} all bonds", cf_case)
    for delta in (-0.01, 0.01):
        add(f"futures_price {delta:+.3f}", lambda s, b, d=delta:
            s["meta"].update({"futures_price": s["meta"]["futures_price"] + d}))

    # 崩溃的扰动必须显式计数：error case 没有 ctd / forward_executable /
    # exec_rate_9grid，若不单列会被三项聚合全部跳过，进而把「扰动崩了」
    # 误报成「结论稳定」。
    error_cases = [c["perturbation"] for c in cases if c["result"].get("error")]

    flips_ctd = [c["perturbation"] for c in cases
                 if c["result"].get("ctd") and c["result"]["ctd"] != base["ctd"]]
    all_exec_flips = [c["perturbation"] for c in cases
                      if "forward_executable" in c["result"] and
                      c["result"]["forward_executable"] != base["forward_executable"]]
    cf_flips = [x for x in all_exec_flips if "conversion_factor" in x]
    cost_flips = [x for x in all_exec_flips if "conversion_factor" not in x]
    max_rate_gap = max((abs(c["result"].get("exec_rate_9grid", base_rate) - base_rate)
                        for c in cases if "exec_rate_9grid" in c["result"]), default=0.0)

    # 二分求使正向可执行判定翻转的最小 CF 平移量（这是可执行边界的关键量）
    def shifted(delta: float):
        from dataclasses import replace as _replace
        s = copy.deepcopy(snap)
        bs = [_replace(b, conversion_factor=b.conversion_factor + delta) for b in bonds]
        return _boundary(s, bs, settle, delivery)

    # 双向搜索：正向 CF 抬高发票价、负向压低。搜不到翻转时如实标记，
    # 禁止把搜索上限当成「最小翻转阈值」。
    search_cap = 0.005
    cf_flip_found = False
    cf_threshold = None
    cf_flip_direction = None
    for direction, lo0, hi0 in (("up", 0.0, search_cap), ("down", -search_cap, 0.0)):
        lo, hi = lo0, hi0
        if shifted(hi if direction == "up" else lo)["forward_executable"] == base["forward_executable"]:
            continue
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if shifted(mid)["forward_executable"] == base["forward_executable"]:
                if direction == "up":
                    lo = mid
                else:
                    hi = mid
            else:
                if direction == "up":
                    hi = mid
                else:
                    lo = mid
        cf_flip_found = True
        cf_threshold = (lo + hi) / 2.0
        cf_flip_direction = direction
        break

    # spread_bp / all_in_cost_bp 已是基点，禁止再次 *10000。
    gap_bp = base["spread_bp"] - base["all_in_cost_bp"]
    if cf_flip_found:
        cf_explanation = (
            "CF 平移 delta 使发票价变化 F*delta（每百元面值），经 365/d 年化后推动 IRR；"
            f"本快照距成本覆盖约 {gap_bp:.1f}bp，"
            f"沿 {cf_flip_direction} 方向约 {abs(cf_threshold):.6f} 的 CF 变动即可翻转可执行判定。"
            "这正是『缺交易所公布转换因子时须缩小范围』的量化依据，也说明账面净基差"
            "不能被直接称为无风险套利。"
        )
    else:
        cf_explanation = (
            f"在 ±{search_cap} 搜索窗内未找到使 forward_executable 翻转的 CF 平移；"
            f"距成本覆盖约 {gap_bp:.1f}bp。未把搜索上限误记为翻转阈值。"
        )
    return {
        "baseline": base,
        "baseline_exec_rate_9grid": round(base_rate, 4),
        "n_perturbations": len(cases),
        "cases": cases,
        "ctd_flips": flips_ctd,
        "cost_and_price_perturbation_exec_flips": cost_flips,
        "cf_perturbation_exec_flips": cf_flips,
        "cf_flip_found": cf_flip_found,
        "cf_flip_direction": cf_flip_direction,
        "cf_flip_threshold_abs": (None if cf_threshold is None else round(abs(cf_threshold), 8)),
        "cf_flip_search_cap": search_cap,
        "cf_sensitivity_alert": bool(cf_flips) or cf_flip_found,
        "cf_flip_explanation": cf_explanation,
        "cost_coverage_gap_bp": round(gap_bp, 4),
        "max_exec_rate_gap": round(max_rate_gap, 4),
        "failed_perturbations": error_cases,
        "n_failed_perturbations": len(error_cases),
        "conclusion_stable": (not flips_ctd and not cost_flips and not error_cases
                              and max_rate_gap <= 0.34),
        "stability_rule": "CTD 不翻转、成本/价格类微扰下可执行判定不翻转、9 点网格执行率偏移 <= 0.34 视为稳定；"
                          "CF 类翻转必须单独标注并给出传导解释，不视为无解释的剧烈翻转；"
                          "任何扰动计算失败即判为不稳定，不允许把失败当成无变化。",
        "note": "微扰只覆盖给定参数；结论稳定性不等于真实市场稳健性。"
                 "baseline 使用 spread_bp（已是 bp），勿再乘 10000。",
    }
