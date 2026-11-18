# -*- coding: utf-8 -*-
"""冻结基线 vs 官方锚定 run 的对照计算（纯读本地产物，零网络）。

原来这段逻辑只存在于 `scripts/compare_runs.py` 里，于是形成了一条隐藏的
顺序依赖：客户视图页面必须先手工跑一次对照脚本才能构建，而对照脚本的默认
run id 又写死在代码里。把计算下沉到 runtime 层后，两个消费者共用同一份实现，
页面可以直接从 run 产物现算对照，不需要任何中间文件。
"""
from __future__ import annotations

import csv
import json
import statistics as st
from datetime import date
from pathlib import Path

from .contest_replay import record_path


def _json(p: Path, name: str) -> dict:
    return json.loads((p / name).read_text(encoding="utf-8"))


def _rows(p: Path, name: str) -> list[dict]:
    with (p / name).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def summarize(run: Path) -> dict:
    """把一个 run 目录压成对照用的摘要。字段与历史 `official_anchor_comparison.json` 保持一致。"""
    eb = _json(run, "executable_boundary.json")
    bt = _json(run, "backtest_metrics.json")
    rb = _json(run, "robustness.json")
    sw = _json(run, "switch_boundaries.json")
    snap = _json(run, "input_snapshot.json")
    manifest = _json(run, "run_manifest.json")
    rows = _rows(run, "strategy_comparison.csv")
    skipped = [r for r in rows if r["C_executed"] == "False"]
    # v2 语义诊断块：缺失即为旧产物，必须如实标记，不得默认为 0。
    switch_exp = bt.get("ctd_switch_exposure", {})
    hindsight = bt.get("hindsight_switch_counterfactual", bt.get("hindsight_upper_bound", {}))
    return {
        "run_dir": record_path(run),
        "data_version": snap["meta"]["data_version"],
        "semantics_version": manifest.get("semantics_version"),
        "source_mode": snap["meta"]["source_mode"],
        "input_origin": snap["meta"]["input_origin"],
        "futures_price": snap["meta"]["futures_price"],
        "futures_price_origin": snap["meta"].get("futures_price_origin"),
        "delivery_date": snap["meta"]["delivery_date"],
        "delivery_date_origin": snap["meta"].get("delivery_date_origin"),
        "data_asof": snap["meta"].get("data_asof"),
        "official_anchor_present": bool(snap["meta"].get("official_anchor")),
        "ctd": eb["ctd"],
        "irr_minus_repo_bp": eb["forward_spread_IRR_minus_repo_bp"],
        "all_in_cost_bp": eb["all_in_cost_bp"],
        "forward_executable": eb["forward_executable"],
        "verdict_status": eb.get("verdict_status"),
        "conclusion_valid": eb.get("conclusion_valid"),
        "forward_executable_usable": eb.get("forward_executable_usable"),
        "reverse_executable": eb["reverse_executable"],
        "top2_irr_margin_bp": eb["top2_irr_margin_bp"],
        "capacity_binding": eb["capacity"]["binding_constraint"],
        "capacity_cap_wan": eb["capacity"]["binding_cap_wan"],
        "switch_boundaries": [
            {"axis": s["axis"], "boundary_bp": round(s["boundary_estimate"] * 10000, 2),
             "from": s["from_ctd"], "to": s["to_ctd"]} for s in sw["boundaries"]
        ],
        "grid_size": len(rows),
        "c_skipped": len(skipped),
        "c_skipped_pct": round(100.0 * len(skipped) / len(rows), 2) if rows else 0.0,
        "b_mean_pnl_in_skipped": (round(st.mean(float(r["B_simple_hedge_pnl_per_100"])
                                               for r in skipped), 6) if skipped else None),
        "c_mean_pnl_all": bt["overall"]["C"]["mean_pnl_per_100"],
        "ctd_switch_exposure": {
            "available": bool(switch_exp.get("available")),
            "n_switched": switch_exp.get("n_switched"),
            "share": switch_exp.get("share"),
            "mean_switch_opportunity_bp": switch_exp.get("mean_switch_opportunity_bp"),
        },
        "hindsight_switch_counterfactual": {
            "available": bool(hindsight.get("available")),
            "mean_edge_per_100": hindsight.get("mean_edge_per_100"),
            "min_edge_per_100": hindsight.get("min_edge_per_100"),
            "max_edge_per_100": hindsight.get("max_edge_per_100"),
        },
        "robustness_stable": rb["conclusion_stable"],
        "cf_sensitivity_alert": rb["cf_sensitivity_alert"],
        "cf_flip_threshold_abs": rb["cf_flip_threshold_abs"],
    }


def build_delta(base: dict, official: dict) -> dict:
    """两侧输入差异。数值口径与历史 `official_anchor_comparison.json` 保持一致。"""
    delta = {
        "futures_price": round(official["futures_price"] - base["futures_price"], 6),
        "delivery_date_days": None,
        "irr_minus_repo_bp": round(official["irr_minus_repo_bp"] - base["irr_minus_repo_bp"], 2),
        "all_in_cost_bp": round(official["all_in_cost_bp"] - base["all_in_cost_bp"], 2),
        "forward_executable_flipped": base["forward_executable"] != official["forward_executable"],
        "c_skipped": official["c_skipped"] - base["c_skipped"],
        "c_mean_pnl_all": round(official["c_mean_pnl_all"] - base["c_mean_pnl_all"], 6),
        "top2_irr_margin_bp": round(official["top2_irr_margin_bp"] - base["top2_irr_margin_bp"], 2),
    }
    delta["delivery_date_days"] = (date.fromisoformat(official["delivery_date"])
                                   - date.fromisoformat(base["delivery_date"])).days
    return delta


def build_comparison(base: dict, official: dict) -> dict:
    """完整对照载荷（与历史 JSON 结构一致，可直接落盘）。"""
    delta = build_delta(base, official)
    return {
        "purpose": "量化「真实期货价/交割日 + 声明式现券/CF」混合锚定对可执行边界的影响",
        "semantics_note": (
            "本次对照在计算语义 "
            f"{base.get('semantics_version')} / {official.get('semantics_version')} 下完成。"
            "两侧语义版本必须一致，否则数值不可直接比较。"
        ),
        "base": base,
        "official_anchored": official,
        "delta": delta,
        "conclusion": (
            "官方锚定把正向可执行判定从 False 翻转为 True、把风控闸门跳过率从 "
            f"{base['c_skipped_pct']}% 降到 {official['c_skipped_pct']}%；但翻转源于声明式现券与真实期货价"
            "不在同一市场状态下，属于虚假基差，不得解释为套利机会。可靠负结果："
            "缺官方可交割券清单/现券估值/转换因子时，即使期货行情真实，也不能给出可执行结论。"
        ),
        "unchanged_fields": ["deliverable_basket", "conversion_factor", "costs",
                             "yield_curve", "capacity", "cost_band_structure"],
        "note": "两侧使用同一份代码、同一情景网格，只替换输入快照中的官方锚定字段。",
    }


def compare_dirs(base_dir: Path, official_dir: Path) -> dict:
    """目录 → 完整对照载荷。页面与对照脚本共用这一条路径。"""
    return build_comparison(summarize(base_dir), summarize(official_dir))
