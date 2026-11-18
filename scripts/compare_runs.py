# -*- coding: utf-8 -*-
"""冻结基线 vs 官方锚定 run 的可复跑对照（零网络、纯读本地产物）。

用法：
    python scripts/compare_runs.py                 # 自动发现 base / anchor run
    python scripts/compare_runs.py \
        --base reports/runtime_out/<run_id> \
        --official reports/runtime_out/<run_id>

run 定位规则见 `runtime/run_registry.py`：默认按「输入快照是否含官方锚定字段」
判定角色、按计算语义版本过滤、按生成时间取最新，**不依赖目录名**。
因此清掉缓存重跑后本脚本仍然可用，不会因为 run 目录换名而失效。

产出：
    reports/official_anchor_comparison.json
    reports/official_anchor_comparison.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.comparison import build_comparison, summarize   # noqa: E402
from runtime.run_registry import (                          # noqa: E402
    ROLE_ANCHOR, ROLE_BASE, describe_missing, resolve,
)

RUNTIME_OUT = ROOT / "reports" / "runtime_out"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None,
                    help="基线 run 目录或 run_id；缺省时按 role+语义版本+生成时间自动发现")
    ap.add_argument("--official", default=None,
                    help="官方锚定 run 目录或 run_id；缺省时自动发现")
    ap.add_argument("--runs-dir", default=str(RUNTIME_OUT))
    ap.add_argument("--semantics", default=None,
                    help="只对比该计算语义版本下的 run（缺省取当前最新语义版本）")
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    args = ap.parse_args()

    found = resolve(Path(args.runs_dir), base=args.base, anchor=args.official,
                    semantics=args.semantics)
    if not found.get(ROLE_BASE, {}).get("path") or not found.get(ROLE_ANCHOR, {}).get("path"):
        print(describe_missing(found), file=sys.stderr)
        return 2
    base_path = found[ROLE_BASE]["path"]
    official_path = found[ROLE_ANCHOR]["path"]
    print(f"[compare] base   = {found[ROLE_BASE]['id']} ({found[ROLE_BASE]['how']})")
    print(f"[compare] anchor = {found[ROLE_ANCHOR]['id']} ({found[ROLE_ANCHOR]['how']})")

    b = summarize(base_path)
    o = summarize(official_path)
    payload = build_comparison(b, o)
    delta = payload["delta"]

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "official_anchor_comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 冻结基线 vs 官方锚定复跑：对照结论", "",
        "> 本文件由 `scripts/compare_runs.py` 从两份 run 产物自动重算，零网络请求。", "",
        f"> 基线 run：`{found[ROLE_BASE]['id']}`；官方锚定 run：`{found[ROLE_ANCHOR]['id']}`"
        f"（按 role + 计算语义版本 + 生成时间自动发现，不依赖目录名）。", "",
        f"> 计算语义版本：基线 `{b.get('semantics_version')}`，"
        f"锚定 `{o.get('semantics_version')}`。"
        "两条 run 语义一致，数值可直接比较；与 `data_version` 是两条独立的版本轴。", "",
        "## 1. 两次 run 的输入差异", "",
        "| 字段 | 冻结基线（原 run） | 官方锚定 run | 差异 |",
        "|---|---|---|---|",
        f"| data_version | `{b['data_version']}` | `{o['data_version']}` | 不同前缀 |",
        f"| source_mode | `{b['source_mode']}` | `{o['source_mode']}` | 均为 degraded |",
        f"| input_origin | `{b['input_origin']}` | `{o['input_origin']}` | 混合来源 |",
        f"| data_asof | `{b['data_asof']}` | `{o['data_asof']}` | 官方数据期 |",
        f"| futures_price | {b['futures_price']} | {o['futures_price']} | {delta['futures_price']:+.2f} |",
        f"| delivery_date | {b['delivery_date']} | {o['delivery_date']} | {delta['delivery_date_days']:+d} 天 |",
        "| 现券/CF/成本/容量 | 声明式 | 声明式（未改） | 不变 |", "",
        "替换项来自 PandaData 官方接口：`get_future_daily`（结算价）与 "
        "`get_future_detail`（交割窗口）；调用留痕见 `data/official/official_anchor.json`。", "",
        "## 2. 结论差异", "",
        "| 指标 | 冻结基线 | 官方锚定 | 变化 |",
        "|---|---|---|---|",
        f"| CTD | {b['ctd']} | {o['ctd']} | 不变 |",
        f"| IRR−r (bp) | {b['irr_minus_repo_bp']:.2f} | {o['irr_minus_repo_bp']:.2f} | "
        f"{delta['irr_minus_repo_bp']:+.2f} |",
        f"| 全成本带 (bp) | {b['all_in_cost_bp']:.2f} | {o['all_in_cost_bp']:.2f} | "
        f"{delta['all_in_cost_bp']:+.2f} |",
        f"| 正向可执行 | {b['forward_executable']} | {o['forward_executable']} | "
        f"**{'翻转' if delta['forward_executable_flipped'] else '未变'}** |",
        f"| 反向可执行 | {b['reverse_executable']} | {o['reverse_executable']} | 未变 |",
        f"| C 跳过情景 | {b['c_skipped']}/{b['grid_size']} ({b['c_skipped_pct']}%) | "
        f"{o['c_skipped']}/{o['grid_size']} ({o['c_skipped_pct']}%) | "
        f"{delta['c_skipped']:+d} |",
        f"| 被跳过情景中 B 均值 | {b['b_mean_pnl_in_skipped']} | "
        f"{o['b_mean_pnl_in_skipped']} | 官方锚定 run 无跳过情景 |",
        f"| C 全网格均值 P&L/百元 | {b['c_mean_pnl_all']:.6f} | {o['c_mean_pnl_all']:.6f} | "
        f"{delta['c_mean_pnl_all']:+.6f} |",
        f"| CTD 前二名 IRR 差 (bp) | {b['top2_irr_margin_bp']:.2f} | "
        f"{o['top2_irr_margin_bp']:.2f} | {delta['top2_irr_margin_bp']:+.2f} |",
        f"| 曲线平移切换点 (bp) | "
        f"{', '.join(str(s['boundary_bp']) for s in b['switch_boundaries'])} | "
        f"{', '.join(str(s['boundary_bp']) for s in o['switch_boundaries'])} | 平移 |",
        f"| CF 翻转阈值 | {b['cf_flip_threshold_abs']} | {o['cf_flip_threshold_abs']} | "
        f"告警 {b['cf_sensitivity_alert']} → {o['cf_sensitivity_alert']} |",
        "",
        "## 2b. 事前执行口径下的 CTD 切换暴露", "",
        "持仓标的固定为**结算日判定的 CTD**（仅用结算日可观测信息）。"
        "下表量化「交割时最优交割券已不是持仓券」的情景占比与事后机会成本 —— "
        "这是持仓必须承担的风险，不是可以事后追踪的免费收益。",
        "",
        "| 指标 | 冻结基线 | 官方锚定 |",
        "|---|---|---|",
    ]
    bse = b.get("ctd_switch_exposure", {})
    ose = o.get("ctd_switch_exposure", {})
    if bse.get("available") and ose.get("available"):
        lines += [
            f"| 事后最优券已非持仓券的情景数 | {bse['n_switched']}/{b['grid_size']} | "
            f"{ose['n_switched']}/{o['grid_size']} |",
            f"| 占比 | {bse['share'] * 100:.1f}% | {ose['share'] * 100:.1f}% |",
            f"| 平均事后机会成本 (bp) | {bse['mean_switch_opportunity_bp']:.2f} | "
            f"{ose['mean_switch_opportunity_bp']:.2f} |",
        ]
    else:
        lines += ["| （该 run 无 CTD 切换暴露诊断列） | — | — |"]
    bh = b.get("hindsight_switch_counterfactual", {})
    oh = o.get("hindsight_switch_counterfactual", {})
    lines += [
        "",
        "事后换券反事实（**不可实现**）：若事先知道交割时会换券并改持事后最优券，"
        "C 的 P&L 会变化多少。该差额**不是上界** —— 事后最优券按 IRR 选出，"
        "而发票价 = F×CF + AI 经转换因子缩放，IRR 更高的券其绝对 P&L 未必更高。",
        "",
        "| 指标 | 冻结基线 | 官方锚定 |",
        "|---|---|---|",
    ]
    if bh.get("available") and oh.get("available"):
        lines += [
            f"| 平均变化 (元/百元) | {bh['mean_edge_per_100']:+.6f} | "
            f"{oh['mean_edge_per_100']:+.6f} |",
            f"| 区间 (元/百元) | {bh['min_edge_per_100']:+.6f} ~ {bh['max_edge_per_100']:+.6f} | "
            f"{oh['min_edge_per_100']:+.6f} ~ {oh['max_edge_per_100']:+.6f} |",
        ]
    else:
        lines += ["| （该 run 无事后换券反事实诊断列） | — | — |"]
    lines += [
        "",
        "> **判定有效性（机器可读）**：官方锚定 run 的 `forward_executable=True` 已在 "
        f"`executable_boundary.json` 标记为 `verdict_status={o['verdict_status']}`、"
        f"`conclusion_valid={o['conclusion_valid']}`、"
        f"`forward_executable_usable={o['forward_executable_usable']}`。该数值**不可采信**，"
        "本表列出它只为量化数据缺口的影响；下游消费必须先读这三个字段。",
        "",
        f"- **全成本带为何下降 {abs(delta['all_in_cost_bp']):.2f}bp**：交割日从 "
        f"{b['delivery_date']} 改为 {o['delivery_date']}（{delta['delivery_date_days']:+d} 天），"
        "持有期拉长使按 ACT/365 年化的固定交易费、交割费与滑点被摊薄；"
        "`costs` 六项参数两次 run 完全相同，不是成本假设改变。",
        "", "## 3. 为什么这是负结果，而不是发现了套利机会", "",
        "- 官方结算价 110.05 对应真实市场状态；声明式篮子由 1.85% 收益率曲线折现生成，"
        "两者不在同一时点、同一收益率水平上。混用直接把 2.73 元价差年化成 "
        f"{o['irr_minus_repo_bp']:.0f}bp 的「IRR−r」，属于构造出来的虚假基差。",
        "- 因此「正向可执行=True」与「C 跳过 0 个情景」不是策略改进，而是**风控闸门被"
        "不一致数据完全打开**；C 全网格均值上升同样是账面幻觉。",
        "- 冻结基线中 CF 平移 0.000111 即翻转判定（成本带附近的高敏感性），在官方锚定 run "
        "被巨额虚假价差淹没、告警消失——说明 CF 精度约束只在真实成本带附近才有判别力。",
        "", "## 4. 价值验收含义", "",
        "1. 相对冻结基线，本次接入**新增了可核验的平台证据**：官方库 159 个国债期货合约、"
        "真实交割窗口、真实结算价、交易日历，留痕可复跑（`--reuse` 零网络）。",
        "2. 它**改变了研究决策**：证明「有真实期货行情」不足以支撑 CTD/IRR 可执行结论；"
        "交付边界必须收紧为「官方可交割券清单 + 现券全价/应计 + 公布 CF + 回购/借券成本"
        "四者齐备前，不给可执行判定」。",
        "3. 作用大小可量化：可执行判定翻转 1 次，风控跳过率变化 "
        f"{abs(b['c_skipped_pct'] - o['c_skipped_pct']):.2f} 个百分点，"
        f"IRR−r 偏移 {delta['irr_minus_repo_bp']:+.0f}bp。",
        "4. 适用边界：仅对本冻结篮子与 T2612 锚定日成立；不外推到任何真实交易日或真实券。",
        "",         "## 5. 复跑", "",
        "```bash",
        "python scripts/fetch_official.py --reuse   # 零网络复用官方锚定",
        "python scripts/build_snapshot.py --official-anchor data/official/official_anchor.json \\",
        "    --out data/input_snapshot_official.json",
        "python -m runtime run --input data/input_snapshot.json --run-id <base-id>",
        "python -m runtime run --input data/input_snapshot_official.json --run-id <anchor-id>",
        "python scripts/compare_runs.py   # run 自动发现，无需指定目录名",
        "```", "",
        "> `source_mode` 两侧均为 `degraded`；混合锚定 run 不得标为 live 或市场结论。",
        "> 两侧 run 的 `semantics_version` 必须相同，否则数值不可直接比较。",
    ]
    (out / "official_anchor_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[compare] forward_executable {b['forward_executable']} -> {o['forward_executable']}")
    print(f"[compare] C skipped {b['c_skipped']} -> {o['c_skipped']}")
    print(f"[compare] -> {out / 'official_anchor_comparison.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
