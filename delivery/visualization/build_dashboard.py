"""生成 CTD/IRR/BNOC 项目的自包含动态可视化页面。

**页面是 skill 的派生产物，不是手工维护的快照。** 三条硬约束：

1. **不写死 run 目录名**。run 目录由时间戳生成，重跑必然换名；写死目录名会让
   「清掉缓存后重跑」在页面构建阶段直接崩溃。run 定位规则见
   `runtime/run_registry.py`：按「输入快照是否含官方锚定字段」判角色、按计算
   语义版本过滤、按生成时间取最新。`--base/--anchor` 可显式覆盖。
2. **不预先依赖对照文件**。对照结论现场从两个 run 的产物算（`runtime/comparison.py`），
   因此「先跑对照脚本、再建页面」的隐含顺序依赖被消除。
3. **页面上的每个研究数值只有一个来源**：run 产物 JSON/CSV。模板里的文案数字
   由 `dynamic` 段注入（情景数、收益率档数、空间 bp、成本带 bp、对照结论句），
   模板中不残留任何写死的研究结论数值。数据一改，页面随之更新。

用法::

    python delivery/visualization/build_dashboard.py
    python delivery/visualization/build_dashboard.py --base <run_id> --anchor <run_id>

产出::

    delivery/visualization/index.html   单文件页面，CSS/JS/数据全部内嵌
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.comparison import compare_dirs      # noqa: E402
from runtime.run_registry import (               # noqa: E402
    ROLE_ANCHOR, ROLE_BASE, describe_missing, resolve,
)

TEMPLATE = HERE / "dashboard.template.html"
OUTPUT = HERE / "index.html"
FONTS = HERE / "fonts"
RUNS = ROOT / "reports" / "runtime_out"

# 仅内嵌拉丁子集；中文继续走系统衬线/黑体回退，避免整页字库膨胀。
FONT_FILES = [
    ("IBM Plex Mono", 400, "plexmono400.woff2"),
    ("IBM Plex Mono", 500, "plexmono500.woff2"),
    ("Source Sans 3", 400, "sourcesans.woff2"),
    ("Source Sans 3", 600, "sourcesans.woff2"),
    ("Source Serif 4", 500, "sourceserif.woff2"),
    ("Source Serif 4", 600, "sourceserif.woff2"),
]
LATIN_RANGE = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
               "U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,"
               "U+2193,U+2212,U+2215,U+FEFF,U+FFFD")


def font_faces() -> str:
    """生成 @font-face 规则，使 index.html 无外部请求也能保持同一套字体。"""
    cache: dict[str, str] = {}
    rules = ["/* Fonts: IBM Plex / Source Sans / Source Serif, SIL OFL 1.1; latin subset inlined. */"]
    for family, weight, filename in FONT_FILES:
        if filename not in cache:
            cache[filename] = base64.b64encode((FONTS / filename).read_bytes()).decode("ascii")
        rules.append(
            "@font-face{font-family:'%s';font-style:normal;font-weight:%d;font-display:swap;"
            "src:url(data:font/woff2;base64,%s) format('woff2');unicode-range:%s;}"
            % (family, weight, cache[filename], LATIN_RANGE)
        )
    return "\n".join(rules)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_meta(base_dir: Path | None = None) -> dict:
    """页面元信息；as_of 取自 run 自带的输入快照，路径由脚本位置推导，跨环境自动适配。

    ``base_dir`` 缺省时按 run 自动发现取基线 run，因此调用方无需知道 run 目录名。
    """
    if base_dir is None:
        found = resolve(RUNS)
        base_dir = found.get(ROLE_BASE, {}).get("path")
    as_of = "unknown"
    if base_dir is not None and (base_dir / "input_snapshot.json").is_file():
        as_of = read_json(base_dir / "input_snapshot.json").get("meta", {}).get("as_of_date", as_of)
    return {
        "asOf": as_of,
        "builtAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def extract_run(run_id, label: str, futures_price: float,
                runs_dir: Path | None = None) -> dict:
    """把一个 run 目录的产物压成页面数据段。

    ``run_id`` 可以是 run 目录名，也可以是绝对/相对目录路径；
    ``runs_dir`` 缺省用默认的 `reports/runtime_out`。
    """
    folder = (Path(runs_dir) if runs_dir is not None else RUNS) / run_id
    ctb = []
    for row in read_csv(folder / "ctb_table.csv"):
        ctb.append(
            {
                "bond": row["bond_id"],
                "cf": num(row["conversion_factor"]),
                "clean": num(row["clean_price"]),
                "dirty": num(row["dirty_price"]),
                "grossBasis": num(row["gross_basis"]),
                "netCarry": num(row["net_carry"]),
                "bnoc": num(row["bnoc"]),
                "irrPct": num(row["irr_simple"]) * 100,
                "accruedSettle": num(row["accrued_settle"]),
                "accruedDelivery": num(row["accrued_delivery"]),
            }
        )

    yield_scan = []
    for row in read_csv(folder / "yield_shift_scan.csv"):
        yield_scan.append(
            {
                "bp": num(row["yield_shift_bp"]),
                "ctd": row["ctd_by_irr"],
                "ctdBnoc": row["ctd_by_bnoc"],
                "agree": row["ctd_disagreement"].strip().lower() not in ("true", "1", "yes"),
                "irrPct": num(row["ctd_irr"]) * 100,
                "bnoc": num(row["ctd_bnoc"]),
                "marginBp": num(row["top2_irr_margin_bp"]),
                "byBond": {
                    k: num(v) * 100
                    for k, v in json.loads(row["irr_by_bond"] or "{}").items()
                },
            }
        )

    financing = []
    for row in read_csv(folder / "financing_scan.csv"):
        financing.append(
            {
                "repoPct": num(row["repo_rate_pct"]),
                "ctd": row["ctd_by_irr"],
                "irrPct": num(row["ctd_irr"]) * 100,
                "marginBp": num(row["top2_irr_margin_bp"]),
                "byBond": {
                    k: num(v) * 100
                    for k, v in json.loads(row["irr_by_bond"] or "{}").items()
                },
            }
        )

    strategy = []
    for row in read_csv(folder / "strategy_comparison.csv"):
        strategy.append(
            [
                num(row["yield_shift_bp"]),
                num(row["repo_rate_pct"]),
                num(row["A_buy_hold_pnl_per_100"]),
                num(row["B_simple_hedge_pnl_per_100"]),
                num(row["C_research_pnl_per_100"]),
                1 if row["C_executed"].strip().lower() == "true" else 0,
                num(row["irr_minus_repo_bp"]),
                num(row["all_in_cost_bp"]),
                # v2 事前口径：持仓标的固定为结算日判定的 CTD；
                # 事后最优交割券只作诊断，不参与持仓选择。
                row.get("held_bond", ""),
                row.get("post_shock_ctd", ""),
                1 if row.get("ctd_switched", "").strip().lower() == "true" else 0,
                num(row.get("switch_opportunity_bp", 0.0)),
                num(row.get("hindsight_C_pnl_per_100", 0.0)),
                num(row.get("valuation_basis_gap_per_100", 0.0)),
            ]
        )

    boundary = read_json(folder / "executable_boundary.json")
    switches = read_json(folder / "switch_boundaries.json")
    backtest = read_json(folder / "backtest_metrics.json")
    manifest = read_json(folder / "run_manifest.json")
    snapshot = read_json(folder / "input_snapshot.json")

    # 融资假设与基准档 IRR 都从该 run 自己的快照取，避免在页面里写死常数。
    repo_rate_pct = round(num(snapshot.get("costs", {}).get("repo_rate")) * 100, 6)
    base_irr_pct = next(
        (row["irrPct"] for row in yield_scan if abs(row["bp"]) < 1e-9),
        yield_scan[0]["irrPct"] if yield_scan else 0.0,
    )

    return {
        "id": folder.name,
        "label": label,
        "repoRatePct": repo_rate_pct,
        "baseIrrPct": base_irr_pct,
        "dataVersion": manifest["data_version"],
        "semanticsVersion": manifest.get("semantics_version", ""),
        "sourceMode": manifest["source_mode"],
        "inputOrigin": manifest["input_origin"],
        "generatedAt": manifest.get("generated_at", ""),
        "futuresPrice": futures_price,
        "deliveryDate": manifest["delivery_date"],
        "boundary": {
            "ctd": boundary["ctd"],
            "spreadBp": boundary["forward_spread_IRR_minus_repo_bp"],
            "costBp": boundary["all_in_cost_bp"],
            "forwardExecutable": boundary["forward_executable"],
            "reverseExecutable": boundary["reverse_executable"],
            "verdictStatus": boundary["verdict_status"],
            "conclusionValid": boundary["conclusion_valid"],
            "usable": boundary["forward_executable_usable"],
            "invalidatedBy": boundary["invalidated_by"],
            "top2MarginBp": boundary["top2_irr_margin_bp"],
            "switchBufferBp": boundary["ctd_switch_buffer_bp"],
            "capacity": boundary["capacity"],
        },
        "switches": switches["boundaries"],
        "ctb": ctb,
        "yieldScan": yield_scan,
        "strategy": strategy,
        "switchExposure": {
            "available": bool(backtest.get("ctd_switch_exposure", {}).get("available")),
            "nSwitched": backtest.get("ctd_switch_exposure", {}).get("n_switched"),
            "share": backtest.get("ctd_switch_exposure", {}).get("share"),
            "meanBp": backtest.get("ctd_switch_exposure", {}).get("mean_switch_opportunity_bp"),
            "maxBp": backtest.get("ctd_switch_exposure", {}).get("max_switch_opportunity_bp"),
        },
        "hindsight": {
            "available": bool(backtest.get("hindsight_switch_counterfactual", {}).get("available")),
            "meanEdge": backtest.get("hindsight_switch_counterfactual", {}).get("mean_edge_per_100"),
            "minEdge": backtest.get("hindsight_switch_counterfactual", {}).get("min_edge_per_100"),
            "maxEdge": backtest.get("hindsight_switch_counterfactual", {}).get("max_edge_per_100"),
        },
        "backtest": {
            "overall": backtest["overall"],
            "gridSize": backtest["grid_size"],
            "turnover": backtest["turnover_ctd_switches"],
            "buckets": backtest["buckets_by_yield_shift"],
        },
    }
    # 说明：稳健性微扰明细、融资扫描表、门禁检查项属于过程证据，
    # 只保留在 reports/ 与归档中，不注入客户视图页面。


def build_dynamic(base: dict, anchor: dict | None, comparison: dict | None) -> dict:
    """页面上所有动态数值的**唯一来源**。

    模板里不写死任何研究数值；数据变更后这里的值随之改变，页面文案随之更新。
    """
    spread = float(base["boundary"]["spreadBp"])
    cost = float(base["boundary"]["costBp"])
    if anchor is not None and comparison is not None:
        delta = comparison["delta"]
        if delta.get("forward_executable_flipped"):
            cross = ("两组数据口径下的可执行判定不一致（官方锚定把正向判定翻转），"
                     "但该翻转源于声明式现券与真实期货价不在同一市场状态，不得当作套利机会。")
        else:
            cross = ("两组数据口径下的对照结论一致——成本闸门未被覆盖时，正向与反向均不可执行。")
    else:
        cross = "当前仅有一组数据口径，对照结论待锚定 run 补齐后给出。"
    repo_levels = len({row[1] for row in base["strategy"]})
    dy_bps = [float(row["bp"]) for row in base["yieldScan"]]
    dy_min = min(dy_bps) if dy_bps else 0.0
    dy_max = max(dy_bps) if dy_bps else 0.0
    price_gap = (float(anchor["futuresPrice"]) - float(base["futuresPrice"])
                 if anchor is not None else 0.0)
    return {
        "nScenarios": len(base["strategy"]),
        "nYieldBps": len(base["yieldScan"]),
        "nRepoLevels": repo_levels,
        "gridDims": f"{repo_levels}×{len(base['yieldScan'])}",
        "spreadBp": f"{spread:.3f}",
        "costBp": f"{cost:.3f}",
        "spaceCoversCost": "空间覆盖成本" if spread >= cost else "空间小于成本",
        "comparisonSentence": cross,
        "dyRange": f"−{abs(dy_min):.0f}bp 至 {dy_max:+.0f}bp",
        "priceGap": round(price_gap, 6),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成客户视图页面（run 自动发现，不写死目录名）")
    ap.add_argument("--runs-dir", default=str(ROOT / "reports" / "runtime_out"))
    ap.add_argument("--base", default=None, help="基线 run 目录或 run_id（缺省自动发现）")
    ap.add_argument("--anchor", default=None, help="官方锚定 run 目录或 run_id（缺省自动发现）")
    ap.add_argument("--semantics", default=None, help="只使用该计算语义版本下的 run")
    ap.add_argument("--template", default=str(TEMPLATE))
    ap.add_argument("--out", default=str(OUTPUT))
    args = ap.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    found = resolve(runs_dir, base=args.base, anchor=args.anchor, semantics=args.semantics)
    base_info = found.get(ROLE_BASE) or {}
    anchor_info = found.get(ROLE_ANCHOR) or {}
    if not base_info.get("path"):
        print(describe_missing(found), file=sys.stderr)
        return 2
    if not anchor_info.get("path"):
        print(describe_missing(found), file=sys.stderr)
        print("[FAIL] 客户视图需要 base + anchor 两个 run；"
              "请先执行 `python scripts/rebuild_all.py`。", file=sys.stderr)
        return 2

    base_dir = base_info["path"]
    anchor_dir = anchor_info["path"]

    # 对照结论现场算，不依赖是否先跑过 compare_runs.py。
    comparison_full = compare_dirs(base_dir, anchor_dir)
    futures_base = float(comparison_full["base"]["futures_price"])
    futures_anchor = float(comparison_full["official_anchored"]["futures_price"])

    base = extract_run(base_info["id"], "冻结基线", futures_base, runs_dir=runs_dir)
    anchor = extract_run(anchor_info["id"], "官方锚定", futures_anchor, runs_dir=runs_dir)
    dynamic = build_dynamic(base, anchor, comparison_full)

    payload = {
        "meta": build_meta(base_dir),
        "base": base,
        "anchor": anchor,
        "dynamic": dynamic,
        "provenance": {
            "baseRun": base_info["id"],
            "anchorRun": anchor_info["id"],
            "baseHow": base_info.get("how"),
            "anchorHow": anchor_info.get("how"),
            "semanticsVersion": base.get("semanticsVersion"),
            "dataVersion": base.get("dataVersion"),
            "generator": "delivery/visualization/build_dashboard.py",
        },
        "comparison": {
            "delta": comparison_full["delta"],
            "conclusion": comparison_full["conclusion"],
            "unchanged": comparison_full.get("unchanged_fields", []),
        },
    }

    template = Path(args.template).read_text(encoding="utf-8")
    for token in ("__PAYLOAD__", "__FONT_FACES__"):
        if token not in template:
            print(f"模板缺少 {token} 占位符", file=sys.stderr)
            return 2
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止产物文本里出现 </script> 之类字面量提前结束内联脚本块。
    data_json = data_json.replace("</", "<\\/")
    html = template.replace("__FONT_FACES__", font_faces()).replace("__PAYLOAD__", data_json)
    out_path = Path(args.out)
    out_path.write_text(html, encoding="utf-8")
    repo_levels = len({row[1] for row in base["strategy"]})
    print(
        f"已生成 {out_path}  |  "
        f"{out_path.stat().st_size} bytes  |  "
        f"base={base_info['id']}  anchor={anchor_info['id']}  |  "
        f"{len(base['strategy'])}×2 情景 / "
        f"{len(base['yieldScan'])}×2 收益率档 / "
        f"{repo_levels}×2 融资档"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
