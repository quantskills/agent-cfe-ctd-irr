# -*- coding: utf-8 -*-
"""产物落盘：结构化表、provenance、运行清单、L1-L3 报告、发布快照。

规范依据：Agent开发规则 §5（研究态/发布态）、§7（公共 Schema 最小集）、
§8（验收门禁）、§12（产物可复制、不含密钥）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .contest_replay import PLATFORM_ENTRY_RECIPE, record_path, resolve_platform_paths
from .semantics import SEMANTICS_VERSION, SEMANTICS_SHORT, SEMANTICS_DESCRIPTION


def platform_entry_label() -> str:
    """平台入口留痕：只写配方和解析来源，不写本机绝对路径。"""
    paths = resolve_platform_paths()
    if paths.get("dsh_home") is None:
        return (
            "not_detected ("
            f"{PLATFORM_ENTRY_RECIPE}; set DSH_HOME or place QSHome beside the workspace)"
        )
    source = paths.get("dsh_source") or "resolved"
    return f"resolved via {source}: {PLATFORM_ENTRY_RECIPE}"


def _json_safe(obj: Any) -> Any:
    """把 NaN / Inf 换成 null，保证写出的 JSON 是标准 JSON。

    单券篮子时 top2 margin 天然是 NaN；json.dumps 默认会写出 `NaN` 字面量，
    非 Python 解析器（JS JSON.parse、Go encoding/json）会直接报错。
    """
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _atomic_write(path: Path, text: str, encoding: str = "utf-8",
                  newline: str | None = "\n") -> None:
    """先写临时文件再原子替换，避免进程中断留下半截产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding=encoding, newline=newline) as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_json(path: Path, obj: Any) -> None:
    payload = json.dumps(_json_safe(obj), ensure_ascii=False, indent=2,
                         default=str, allow_nan=False) + "\n"
    _atomic_write(path, payload)


def write_csv(path: Path, rows: list[dict]) -> None:
    """把 dict 值展平成 JSON 字符串，保证 CSV 列稳定。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        _atomic_write(path, "")
        return
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    import io
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list))
                        else v) for k, v in r.items()})
    _atomic_write(path, buf.getvalue(), encoding="utf-8-sig", newline="")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def write_manifest(run_dir: Path, snap: dict, command: str,
                   input_path: Path, extra: dict | None = None) -> dict:
    """平台真实入口的输入/输出留痕清单。"""
    m = {
        "run_id": run_dir.name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "platform": "QuantStudio",
        "platform_entry": platform_entry_label(),
        "command": command,
        "python": platform.python_version(),
        "os": platform.platform(),
        "input_snapshot": record_path(input_path),
        "input_sha256": sha256_file(input_path),
        "data_version": snap["meta"]["data_version"],
        # 计算口径版本：与 data_version（输入数据版本）是两条独立的版本轴。
        # data_version 相同而 semantics_version 不同 ⇒ 数值不可直接比较。
        "semantics_version": SEMANTICS_VERSION,
        "semantics_short": SEMANTICS_SHORT,
        "source_mode": snap["meta"]["source_mode"],
        "input_origin": snap["meta"].get("input_origin"),
        "market_conclusion": snap["meta"].get("market_conclusion", False),
        "as_of_date": snap["meta"]["as_of_date"],
        "settle_date": snap["meta"]["settle_date"],
        "delivery_date": snap["meta"]["delivery_date"],
        "frozen_baseline_id": snap["frozen_baseline"]["baseline_id"],
        "validation": snap.get("validation", {}),
    }
    if extra:
        m.update(extra)
    write_json(run_dir / "run_manifest.json", m)
    return m


def _fmt(v: Any, nd: int = 4) -> str:
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def write_report(run_dir: Path, snap: dict, ctb_rows: list[dict],
                 rank: dict, boundary: dict, switches: list[dict],
                 metrics: dict, backtest: dict,
                 robustness: dict | None = None) -> str:
    """L1 结论 / L2 证据表 / L3 ASCII 降级图 / L5 索引 / 免责声明。"""
    meta = snap["meta"]
    degraded = meta["source_mode"] != "live"
    l1 = (
        f"在冻结输入快照（{meta['data_version']}，source_mode={meta['source_mode']}）下，"
        f"CTD={rank['ctd_by_irr']}，IRR={rank['ctd_irr']*100:.3f}%，"
        f"净基差={rank['ctd_bnoc']:.4f}（期货报价单位）；"
        f"IRR−r={boundary['forward_spread_IRR_minus_repo_bp']:.2f}bp，"
        f"全成本带={boundary['all_in_cost_bp']:.2f}bp，"
        f"正向可执行={boundary['forward_executable']}。"
    )
    if degraded:
        l1 += " 该结论是条件化的方法复现结果，不构成对真实市场套利机会的判断。"
    if boundary.get("verdict_status") == "INVALID_MIXED_STATE":
        l1 += (" **verdict_status=INVALID_MIXED_STATE，conclusion_valid=False；"
               "上述正向可执行数值判定无效、不可采信。**")

    hybrid_warning = ""
    # 判定条件与计算层保持一致（scenarios.py 的 is_mixed）：
    # official_anchor 且 source_mode 非 live 才算混合来源；否则会出现
    # 「JSON 判有效、报告判无效」的自相矛盾。
    if meta.get("official_anchor") and meta.get("source_mode") != "live":
        hybrid_warning = (
            "\n> **⚠️ 混合来源告警（该 run 的可执行判定无效）**：本 run 把 PandaData 官方真实"
            "结算价与交割日，与声明式现券全价/应计、参考折现 CF 混用；两者不在同一市场状态下，"
            "报告中的 `IRR−r` 与 `forward_executable=True` 是**虚假基差**的产物，不得采信、"
            "不得解释为套利机会或交易信号。缺官方可交割券清单、现券估值、公布 CF 与"
            "回购/借券成本时一律输出「不可判定」。"
            "见 `references/executable_boundary.md` §9 与 `reports/official_anchor_comparison.md`。\n"
        )

    lines = [
        f"# CTD / IRR / 净基差复现研究报告（{run_dir.name}）", "",
        "## L1 结论", l1,
        *([hybrid_warning] if hybrid_warning else []),
        "",
        "## L2 证据表：可交割券逐券指标", "",
        "| 券 | CF | 净价 | 全价 | 结算日应计 | 交割日应计 | 发票价 | 毛基差 | 净持有收益 | BNOC(券价) | BNOC(期货) | IRR | IRR@repo |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ctb_rows:
        lines.append("| " + " | ".join([
            r["bond_id"], _fmt(r["conversion_factor"], 6), _fmt(r["clean_price"], 3),
            _fmt(r["dirty_price"], 3), _fmt(r["accrued_settle"], 4),
            _fmt(r["accrued_delivery"], 4), _fmt(r["invoice_price"], 3),
            _fmt(r["gross_basis"], 4), _fmt(r["net_carry"], 4),
            _fmt(r["bnoc_dirty"], 4), _fmt(r["bnoc"], 4),
            f"{r['irr_simple']*100:.3f}%", f"{r['irr_at_repo']*100:.3f}%",
        ]) + " |")

    lines += [
        "", "## L2 证据表：CTD 双判据与切换安全垫", "",
        f"- IRR 判据 CTD：`{rank['ctd_by_irr']}`；BNOC 判据 CTD：`{rank['ctd_by_bnoc']}`；"
        f"分歧={rank['ctd_disagreement']}",
        f"- IRR 前二名安全垫：{rank['top2_irr_margin_bp']:.3f}bp；"
        f"BNOC 前二名安全垫：{rank['top2_bnoc_margin_bp']:.3f}bp",
        f"- 扫描共发现 {len(switches)} 个 CTD 切换边界（详见 switch_boundaries.json）",
        "",
        "## L2 证据表：可执行边界", "",
        f"- 正向价差 IRR−r = {boundary['forward_spread_IRR_minus_repo_bp']:.3f}bp；"
        f"全成本带 = {boundary['all_in_cost_bp']:.3f}bp；正向可执行 = {boundary['forward_executable']}",
        f"- 反向可执行 = {boundary['reverse_executable']}（卖空现券需借券）",
        f"- 容量约束项：`{boundary['capacity']['binding_constraint']}`，"
        f"上限 {boundary['capacity']['binding_cap_wan']:.0f} 万元面值",
        "",
        "## L2 证据表：策略口径与 CTD 切换暴露", "",
        f"- 计算口径版本：`{SEMANTICS_VERSION}`（{SEMANTICS_SHORT}）。"
        f"与 data_version 是两条独立版本轴，二者任意一条不同则数值不可直接比较。",
        "- 事前执行口径：三条策略均持有**结算日判定的 CTD**（仅用结算日可观测信息），"
        "冲击后谁是最优交割券只作事后诊断，不参与持仓选择。",
        "- 终值口径：A 走**现券市场**退出（无期货腿、无交割义务）；B/C 走**实物交割**退出。"
        "因此 `A − B = 估值基准差 + 交割费 − 期货腿损益`，"
        "A−B 的差额不是纯粹的「对冲价值」，必须先扣除估值基准差。",
    ]
    if backtest.get("ctd_switch_exposure", {}).get("available"):
        cse = backtest["ctd_switch_exposure"]
        lines += [
            f"- CTD 切换暴露：{cse['n_switched']}/{backtest['grid_size']} 个情景"
            f"（{cse['share'] * 100:.1f}%）的事后最优交割券已不是持仓券；"
            f"平均事后机会成本 {cse['mean_switch_opportunity_bp']:.2f}bp"
            f"（最大 {cse['max_switch_opportunity_bp']:.2f}bp），该值不可实现，仅作风险度量。",
        ]
    if backtest.get("valuation_basis", {}).get("available"):
        vb = backtest["valuation_basis"]
        lines += [
            f"- 终值口径差（估值基准差）均值 {vb['mean_gap_per_100']:+.6f} 元/百元，"
            f"最大绝对值 {vb['max_abs_gap_per_100']:.6f} 元/百元。",
        ]
    if backtest.get("hindsight_switch_counterfactual", {}).get("available"):
        hu = backtest["hindsight_switch_counterfactual"]
        lines += [
            f"- 事后换券反事实：若事先知道交割时会换券并改持事后最优券，"
            f"C 的 P&L 平均变化 {hu['mean_edge_per_100']:+.6f} 元/百元"
            f"（区间 {hu['min_edge_per_100']:+.6f} ~ {hu['max_edge_per_100']:+.6f}）。"
            "**该反事实不可实现**，且**不是上界**——事后最优券按 IRR 选出，"
            "而发票价经转换因子缩放，IRR 更高的券其绝对 P&L 未必更高。",
        ]
    lines += [
        "",
        "## L2 证据表：微扰稳定性与转换因子敏感性", "",
    ]
    if robustness:
        lines += [
            f"- 微扰次数 {robustness['n_perturbations']}，CTD 翻转 {len(robustness['ctd_flips'])} 次，"
            f"成本/价格类可执行判定翻转 {len(robustness['cost_and_price_perturbation_exec_flips'])} 次，"
            f"结论稳定={robustness['conclusion_stable']}",
            (
                f"- **转换因子敏感性**：在 ±{robustness.get('cf_flip_search_cap', 0.005)} "
                f"窗内{'找到' if robustness.get('cf_flip_found') else '未找到'}翻转；"
                + (
                    f"最小 |ΔCF| = {robustness['cf_flip_threshold_abs']:.8f}（{robustness.get('cf_flip_direction')}）。"
                    if robustness.get("cf_flip_found") and robustness.get("cf_flip_threshold_abs") is not None
                    else "阈值留空（未把搜索上限记为翻转点）。"
                )
                + f" {robustness.get('cf_flip_explanation', '')}"
            ),
        ]
    lines += [
        "",
        "## L3 图（ASCII 降级）", "",
        "```text",
        "IRR 排序（每券，%）",
    ]
    ordered = sorted(metrics, key=lambda x: x["irr_simple"], reverse=True)
    mx = max(abs(m["irr_simple"]) for m in ordered) or 1.0
    for m in ordered:
        n = int(abs(m["irr_simple"]) / mx * 34)
        lines.append(f"  {m['bond_id']:<8} {m['irr_simple']*100:7.3f}% | {'#' * n}")
    lines += [
        "```", "",
        "## L5 结构化产物索引", "",
        "| 文件 | 内容 |", "|---|---|",
        "| ctb_table.csv | 逐券 CTD/IRR/BNOC 指标 |",
        "| financing_scan.csv | 融资利率扫描（净基差判据切换） |",
        "| yield_shift_scan.csv | 曲线平移扫描（IRR 判据切换） |",
        "| strategy_comparison.csv | 冻结基线 A/B 与研究方案 C 对照 |",
        "| switch_boundaries.json | CTD 切换边界与安全垫 |",
        "| executable_boundary.json | 成本覆盖、CTD 安全垫、容量边界 |",
        "| backtest_metrics.json | 规范 §9.2 分析型最低指标集 |",
        "| validation.json | 时点/字段/结构校验与缺口标注 |",
        "| contest_replay/ | 期货模拟赛等价回放范围与账本 |",
        "", "## 数据来源与方法", "",
        f"- 数据源：`{meta.get('input_origin')}`，数据期 {meta['as_of_date']}，"
        f"data_version={meta['data_version']}",
        "- 方法：发票价 = F×CF + 交割日应计；IRR 为票息按自身再投资的单利年化解；"
        "BNOC = 毛基差 − 净持有收益 = −(IRR−r)×D；恒等式见 references/methodology.md。",
        "- 正式输入通道与字段口径见 references/data_guide.md；"
        "等价回放范围见 references/executable_boundary.md。",
        "", "## 缺口与范围缩小", "",
    ]
    for note in snap["validation"]["gap_notes"]:
        lines.append(f"- {note}")
    for note in snap["validation"]["scope_reduction"]:
        lines.append(f"- {note}")
    lines += [
        "", "## 免责声明", "",
        "本报告为研究态、条件化的方法复现结果，不是投资建议，不包含确定性交易指令，"
        "不把账面基差或净基差称为无风险套利。降级输入不得作为正式市场结论使用。",
        "",
    ]
    text = "\n".join(lines)
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    return text


def publish_snapshot(run_dir: Path, publish_root: Path, snap: dict) -> Path:
    """发布态：只读定稿快照，字段与版本齐全，禁止会话内重算冒充。

    目录名同时带 data_version 与 semantics_version：两条版本轴任意一条不同，
    数值就不可直接比较，混在一个目录里会被误读成「同一结论的两份拷贝」。
    """
    dest = (publish_root / snap["meta"]["as_of_date"]
            / f"{snap['meta']['data_version']}__{SEMANTICS_VERSION}")
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("ctb_table.csv", "financing_scan.csv", "yield_shift_scan.csv",
                 "strategy_comparison.csv", "switch_boundaries.json",
                 "executable_boundary.json", "backtest_metrics.json",
                 "robustness.json", "validation.json", "report.md",
                 "input_snapshot.json", "run_manifest.json", "IO_INDEX.md"):
        src = run_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)
    replay_src = run_dir / "contest_replay"
    if replay_src.is_dir():
        replay_dst = dest / "contest_replay"
        replay_dst.mkdir(parents=True, exist_ok=True)
        for f in replay_src.iterdir():
            if f.is_file():
                shutil.copy2(f, replay_dst / f.name)
    write_json(dest / "publish_meta.json", {
        "as_of_date": snap["meta"]["as_of_date"],
        "data_version": snap["meta"]["data_version"],
        "semantics_version": SEMANTICS_VERSION,
        "semantics_short": SEMANTICS_SHORT,
        "semantics_description": SEMANTICS_DESCRIPTION,
        "source_mode": snap["meta"]["source_mode"],
        "source_run_dir": record_path(run_dir),
        "update_time": datetime.now().isoformat(timespec="seconds"),
        "same_logic_as_research": True,
        "note": "发布态只读快照；mock/degraded 版本前缀与 live 不共用。",
    })
    return dest
