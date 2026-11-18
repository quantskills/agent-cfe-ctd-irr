# -*- coding: utf-8 -*-
"""验收门禁：规范 §8 沙漏（时点 / 稳定性 / 结构）+ §7 公共 Schema 最小集。

结构校验通过不等于时点校验通过；两者都过才算 ok。
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .bonds import (
    Bond, evaluate_bond, parse_date, rank_basket,
    coupon_flows, accrued_on, days, ACT365, reprice_bond,
)
from .io_contract import live_requirements
from .contest_replay import as_flag, record_path, resolve_recorded_path
from .semantics import SEMANTICS_VERSION, KNOWN_SEMANTICS_VERSIONS

ROOT_TESTS = Path(__file__).resolve().parent.parent / "tests"


def _independent_recompute_strategy_pnl(snap: dict, strat: list[dict],
                                        meta: dict) -> tuple[float, str]:
    """从输入副本独立重算策略 P&L，与输出 CSV 对拍。

    **刻意不复用 `runtime/scenarios.py` 的实现**：若直接调用被测函数再与
    自己的输出比较，得到的是自反伪校验，零证据价值。这里的 P&L 公式
    按方法论文档独立重写一遍（现券终值 / 交割终值 / 期货腿 / 成本），
    两条路径只有输入与输出相同，实现互不引用。

    抽样重算（而非全量）以控制门禁耗时；抽样覆盖 dy 网格首/中/尾与
    r 网格首/中/尾，且强制包含执行与跳过两类情景。
    """
    from .scenarios import evaluate_point, cost_band
    bonds = [Bond.from_dict(b) for b in snap["deliverable_basket"]]
    settle = parse_date(meta["settle_date"])
    delivery = parse_date(meta["delivery_date"])
    f0 = float(meta["futures_price"])
    costs = snap["costs"]

    def pick_indices() -> list[int]:
        n = len(strat)
        if n == 0:
            return []
        cand = {0, n // 2, n - 1}
        # 覆盖不同 repo_rate 档：网格按 dy 外层、r 内层排列
        for frac in (0.05, 0.25, 0.45, 0.65, 0.85):
            cand.add(min(n - 1, int(n * frac)))
        executed = [i for i, r in enumerate(strat) if as_flag(r.get("C_executed"))]
        skipped = [i for i, r in enumerate(strat) if not as_flag(r.get("C_executed"))]
        if executed:
            cand.add(executed[0])
            cand.add(executed[len(executed) // 2])
        if skipped:
            cand.add(skipped[0])
            cand.add(skipped[len(skipped) // 2])
        return sorted(cand)

    def pnl_for(bond: Bond, dy: float, r: float, d_f: float,
                mode: str) -> dict:
        """独立实现的每百元面值 P&L（与 scenarios.strategy_pnl 无代码共享）。"""
        p = bond.dirty_price
        d = days(settle, delivery)
        flows = coupon_flows(bond, settle, delivery)
        ai_d = accrued_on(bond, delivery)
        c_sum = sum(c for _, c in flows)
        c_days = sum(c * days(dt, delivery) / ACT365 for dt, c in flows)
        f_del = f0 * (1.0 - d_f * dy)
        # 交割终值：发票价 + 票息及再投资
        delivery_terminal = (f_del * bond.conversion_factor + ai_d
                             + c_sum + r * c_days)
        # 现券市场终值：久期重定价净价 + 应计 + 票息及再投资
        market_terminal = (bond.clean_price * (1.0 - bond.modified_duration * dy)
                           + ai_d + c_sum + r * c_days)
        fin = p * r * d / ACT365
        fric = p * (costs["transaction_cost_bp"] + costs["slippage_bp"]) / 10000.0
        fee = costs["delivery_fee_per_100"]
        if mode == "naive_1to1":
            h = 1.0
        elif mode == "dv01":
            h = (bond.modified_duration * bond.clean_price
                 / (d_f * f0)) if (d_f > 0 and f0 > 0) else 0.0
        else:
            raise ValueError(f"unknown mode {mode}")
        hedge = -h * (f_del - f0)
        return {
            "A": market_terminal - p - fin - fric,
            "B": delivery_terminal - p - fin - fric - fee + hedge,
            "C": delivery_terminal - p - fin - fric - fee + hedge,
            "hedge": hedge,
            "basis": market_terminal - delivery_terminal,
        }

    max_err = 0.0
    worst = ""
    n_checked = 0
    for i in pick_indices():
        row = strat[i]
        dy = float(row["yield_shift_bp"]) / 10000.0
        r = float(row["repo_rate_pct"]) / 100.0
        held_id = row.get("held_bond")
        held = next((b for b in bonds if b.bond_id == held_id), None)
        if held is None:
            continue
        d_f = held.modified_duration
        vals = pnl_for(held, dy, r, d_f, "naive_1to1")
        dv = pnl_for(held, dy, r, d_f, "dv01")
        pairs = [
            ("A_buy_hold_pnl_per_100", vals["A"]),
            ("B_simple_hedge_pnl_per_100", vals["B"]),
        ]
        if as_flag(row.get("C_executed")):
            pairs.append(("C_research_pnl_per_100", dv["B"]))
        else:
            pairs.append(("C_research_pnl_per_100", 0.0))
        for key, expected in pairs:
            try:
                got = float(row[key])
            except (KeyError, TypeError, ValueError):
                max_err = float("inf")
                worst = f"row{i}.{key}=missing"
                continue
            err = abs(got - expected)
            if err > max_err:
                max_err = err
                worst = f"row{i}.{key} got={got:.8f} expected={expected:.8f}"
        n_checked += 1
    return max_err, f"rows_checked={n_checked} worst={worst or 'n/a'}"


def _exante_holding_violations(snap: dict, strat: list[dict],
                               meta: dict) -> list[dict]:
    """逐行断言持仓标的 == 该行融资利率下、期初曲线判定的 CTD。"""
    from .scenarios import evaluate_point
    bonds = [Bond.from_dict(b) for b in snap["deliverable_basket"]]
    settle = parse_date(meta["settle_date"])
    delivery = parse_date(meta["delivery_date"])
    f0 = float(meta["futures_price"])

    entry_ctd: dict[float, str] = {}
    violations = []
    for row in strat:
        r = float(row["repo_rate_pct"]) / 100.0
        key = round(r, 10)
        if key not in entry_ctd:
            _m, rank = evaluate_point(bonds, f0, settle, delivery, r)
            entry_ctd[key] = rank["ctd_by_irr"]
        expected = entry_ctd[key]
        got = row.get("held_bond")
        if got != expected:
            violations.append({"grid_index": row.get("grid_index"),
                               "repo_rate_pct": row.get("repo_rate_pct"),
                               "held_bond": got, "expected_entry_ctd": expected})
    return violations


def _terminal_basis_identity_error(strat: list[dict]) -> float:
    """校验 A − B = 估值基准差 + 交割费 − 期货腿损益。

    这条恒等式把「A 与 B 差多少」拆成三部分：终值口径差、交割费、期货腿。
    若实现里把 A/B 的终值口径混用（例如两者都走交割），该恒等式必然不成立。
    """
    max_err = 0.0
    for row in strat:
        try:
            a = float(row["A_buy_hold_pnl_per_100"])
            b = float(row["B_simple_hedge_pnl_per_100"])
            gap = float(row["valuation_basis_gap_per_100"])
            fee = float(row["delivery_fee_per_100"])
            hedge_b = float(row["hedge_leg_pnl_B"])
        except (KeyError, TypeError, ValueError):
            return float("inf")
        err = abs((a - b) - (gap + fee - hedge_b))
        max_err = max(max_err, err)
    return max_err


REQUIRED_FILES = (
    "run_manifest.json", "input_snapshot.json", "ctb_table.csv",
    "financing_scan.csv", "yield_shift_scan.csv", "strategy_comparison.csv",
    "switch_boundaries.json", "executable_boundary.json",
    "backtest_metrics.json", "robustness.json", "report.md",
    "contest_replay/contest_scope.json", "contest_replay/dry_run_plan.json",
    "contest_replay/replay_ledger.csv",
)
REQUIRED_REPORT_SECTIONS = ("## L1 结论", "## L2 证据表", "## L5 结构化产物索引",
                            "## 数据来源与方法", "## 缺口与范围缩小", "## 免责声明")
FORBIDDEN_PHRASES = ("必涨", "确定性买入", "仓位提到", "保证收益", "无风险套利机会",
                     "建议下单", "立即买入", "稳赚")
PUBLIC_FIELDS = ("as_of_date", "data_version", "source_mode", "update_time",
                 "symbol", "asset_type")


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def validate_run_dir(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            errors.append(f"{name}: {detail or 'failed'}")

    # 1. 文件完整性
    missing = [f for f in REQUIRED_FILES if not (run_dir / f).exists()]
    chk("required_files", not missing, f"missing={missing}")

    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "checks": checks, "errors": errors, "warnings": warnings}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snap = json.loads((run_dir / "input_snapshot.json").read_text(encoding="utf-8"))

    # 2. 时点正确（反未来函数）
    meta = snap["meta"]
    chk("as_of_not_after_settle", meta["as_of_date"] <= meta["settle_date"],
        f"as_of={meta['as_of_date']} settle={meta['settle_date']}")
    chk("delivery_after_settle", meta["delivery_date"] > meta["settle_date"],
        f"delivery={meta['delivery_date']}")
    chk("input_contract_ok", bool(snap.get("validation", {}).get("ok")),
        "input snapshot contract must pass")

    # 3. 输入冻结：同时校验「原始输入」与「run 目录里真正喂给计算的副本」，
    #    避免只校验源文件时副本被替换而不被发现。
    h_copy = hashlib.sha256((run_dir / "input_snapshot.json").read_bytes()).hexdigest()
    chk("input_copy_sha256_matches", h_copy == manifest.get("input_sha256"),
        f"manifest={manifest.get('input_sha256')} run_copy={h_copy}")
    raw_original = manifest.get("input_snapshot", "")
    original = resolve_recorded_path(raw_original)
    if original is not None:
        h_orig = hashlib.sha256(original.read_bytes()).hexdigest()
        chk("input_sha256_matches", h_orig == manifest.get("input_sha256"),
            f"manifest={manifest.get('input_sha256')} current={h_orig}")
        chk("input_copy_equals_original_bytes", h_copy == h_orig,
            f"run_copy={h_copy[:12]} original={h_orig[:12]}")
    else:
        # 空串会让 Path('') 退化为 '.'，进而对目录调用 read_bytes() 使门禁崩溃。
        warnings.append(f"original input not reachable: {raw_original or '(empty)'}")
    chk("data_version_present", bool(manifest.get("data_version")), "")

    # 3b. live 门槛：不能只靠 3 个自填字段，七项正式数据必须齐备
    lr = live_requirements(meta)
    if meta["source_mode"] == "live":
        chk("live_requires_all_formal_items", lr["satisfied"],
            f"present={lr['formal_items_present']}/{lr['formal_items_required']} "
            f"missing={[k for k, v in lr['formal_items'].items() if not v]}")
    else:
        # degraded 的定义是「live 门槛未全部满足」，用 satisfied 判定：
        # 仅看 all_formal_items 会把「七项数据齐备但缺来源字段」的合法降级误判为失败。
        chk("degraded_has_unmet_formal_items", not lr["satisfied"],
            f"satisfied={lr['satisfied']} "
            f"present={lr['formal_items_present']}/{lr['formal_items_required']} "
            f"origin_allowed={lr['origin_allowed']} "
            f"has_source_ref={lr['has_source_ref']} has_data_asof={lr['has_data_asof']}")

    # 4. source_mode / 结论标记一致
    degraded = meta["source_mode"] != "live"
    chk("degraded_not_marked_as_market_conclusion",
        (not degraded) or (meta.get("market_conclusion") is False),
        f"source_mode={meta['source_mode']} market_conclusion={meta.get('market_conclusion')}")
    if degraded:
        chk("gap_notes_present", len(snap["validation"].get("gap_notes", [])) > 0, "")
        chk("scope_reduction_present", len(snap["validation"].get("scope_reduction", [])) > 0, "")

    # 5. 主键唯一（§7）
    ctb = _read_csv(run_dir / "ctb_table.csv")
    ids = [r.get("bond_id") for r in ctb]
    chk("ctb_primary_key_unique", len(ids) == len(set(ids)) and len(ids) > 0,
        f"n={len(ids)} distinct={len(set(ids))}")
    strat = _read_csv(run_dir / "strategy_comparison.csv")
    gids = [r.get("grid_index") for r in strat]
    chk("strategy_primary_key_unique", len(gids) == len(set(gids)) and len(gids) > 0,
        f"n={len(gids)} distinct={len(set(gids))}")

    # 6. 公共 Schema 最小集（§7）
    for name in ("ctb_table.csv", "strategy_comparison.csv"):
        rows = _read_csv(run_dir / name)
        absent = [f for f in PUBLIC_FIELDS if rows and f not in rows[0]]
        chk(f"public_schema_{name}", not absent, f"absent={absent}")

    # 7. 数值恒等式：BNOC_dirty = GrossBasis - NetCarry = -(IRR-r)*D
    if ctb:
        max_err = 0.0
        for r in ctb:
            try:
                lhs = float(r["bnoc_dirty"])
                rhs = float(r["gross_basis"]) - float(r["net_carry"])
                max_err = max(max_err, abs(lhs - rhs))
            except (KeyError, ValueError):
                max_err = float("inf")
                break
        chk("bnoc_identity_from_output_csv", max_err < 1e-6, f"max_abs_err={max_err}")
        # 硬断言：不能是「两边全 0」的假通过
        try:
            nonzero = sum(1 for r in ctb if abs(float(r.get("gross_basis", 0))) > 1e-9)
        except (TypeError, ValueError):
            # 畸形数值必须让检查失败，而不是让门禁自身抛异常中断。
            nonzero = -1
        chk("non_degenerate_inputs", nonzero == len(ctb), f"nonzero_gross_basis={nonzero}/{len(ctb)}")

    # 8. 报告结构 + 禁用确定性投资建议
    report = (run_dir / "report.md").read_text(encoding="utf-8") if (run_dir / "report.md").exists() else ""
    absent_sec = [s for s in REQUIRED_REPORT_SECTIONS if s not in report]
    chk("report_sections", not absent_sec, f"absent={absent_sec}")
    hits = [w for w in FORBIDDEN_PHRASES if w in report]
    chk("no_deterministic_trading_language", not hits, f"hits={hits}")

    # 9. 等价回放范围事先声明且不下单
    scope_path = run_dir / "contest_replay" / "contest_scope.json"
    if scope_path.exists():
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        chk("replay_scope_declared_before_run", bool(scope.get("declared_before_run")), "")
        chk("no_live_order", scope.get("live_order") is False, "")
        chk("cash_leg_gap_declared", bool(scope.get("not_verifiable_by_contest_cli")), "")
        # 审计修复：布尔自述升级为可核对的时间戳，并显式承认缺独立事先声明
        decl, gen = scope.get("declared_at"), manifest.get("generated_at")
        chk("replay_declaration_timestamped",
            bool(decl) and bool(gen) and str(decl) <= str(gen),
            f"declared_at={decl} generated_at={gen}")
        chk("replay_declaration_limit_disclosed",
            bool(scope.get("declaration_evidence_note")), "")
        # 文件写入顺序是独立于布尔自述的物理证据：声明必须早于任何计算产物
        try:
            t_decl = scope_path.stat().st_mtime
            t_calc = (run_dir / "ctb_table.csv").stat().st_mtime
            chk("declaration_written_before_calculation_output", t_decl <= t_calc,
                f"scope_mtime={t_decl:.3f} ctb_table_mtime={t_calc:.3f}")
        except OSError as exc:
            chk("declaration_written_before_calculation_output", False, str(exc))

    # 10. 稳定性：CTD 双判据结果必须可解释（分歧留痕而非静默）
    boundary = json.loads((run_dir / "executable_boundary.json").read_text(encoding="utf-8")) \
        if (run_dir / "executable_boundary.json").exists() else {}
    if "all_in_cost_bp" in boundary:
        chk("cost_band_positive", boundary["all_in_cost_bp"] > 0, "")

    # 10b. 审计修复：从「本次 run 实际使用的输入副本」独立重算 CTD / IRR，
    #      与输出 CSV 对拍。旧门禁只用输出列互相验证，属于循环自证。
    try:
        bonds = [Bond.from_dict(b) for b in snap["deliverable_basket"]]
        settle = parse_date(meta["settle_date"])
        delivery = parse_date(meta["delivery_date"])
        f0 = float(meta["futures_price"])
        r0 = float(snap["costs"]["repo_rate"])
        recomputed = [evaluate_bond(b, f0, settle, delivery, r0) for b in bonds]
        rank = rank_basket(recomputed)
        by_id = {row["bond_id"]: row for row in ctb}
        max_irr_err = max(
            abs(m.irr_simple - float(by_id[m.bond_id]["irr_simple"]))
            for m in recomputed
        )
        chk("independent_recompute_ctd_irr",
            boundary.get("ctd") == rank["ctd_by_irr"] and max_irr_err < 1e-8,
            f"max_irr_err={max_irr_err:.3e} recomputed_ctd={rank['ctd_by_irr']} "
            f"output_ctd={boundary.get('ctd')}")
    except Exception as exc:  # noqa: BLE001
        chk("independent_recompute_ctd_irr", False, f"recompute failed: {exc}")

    # 10c. 审计修复：混合来源的 forward_executable 必须在机器可读层标为无效
    is_mixed = bool(meta.get("official_anchor")) and meta["source_mode"] != "live"
    chk("verdict_status_present", bool(boundary.get("verdict_status")),
        f"verdict_status={boundary.get('verdict_status')}")
    if is_mixed:
        chk("hybrid_verdict_explicitly_invalidated",
            boundary.get("conclusion_valid") is False
            and boundary.get("forward_executable_usable") is False
            and boundary.get("verdict_status") == "INVALID_MIXED_STATE"
            and len(boundary.get("invalidated_by", [])) > 0,
            f"status={boundary.get('verdict_status')} "
            f"valid={boundary.get('conclusion_valid')}")
        chk("hybrid_report_warns_invalid",
            ("INVALID_MIXED_STATE" in report) or ("判定无效" in report), "")
    else:
        chk("conditional_verdict_marked_valid",
            boundary.get("conclusion_valid") is True, "")

    # 10d. 语义版本登记：计算口径必须有独立版本号，否则跨 run 数值不可比。
    #      旧产物（v1 语义）生成时还没有这条轴，manifest 里没有该字段 ——
    #      这属于「历史产物」而非「未知版本」。判为不适用（记 warning）而不是
    #      失败，否则会把「语义轴是后加的」误写成「该 run 有错」。
    semantics_version = manifest.get("semantics_version")
    if semantics_version:
        chk("semantics_version_present", True, f"semantics_version={semantics_version}")
        chk("semantics_version_known", semantics_version in KNOWN_SEMANTICS_VERSIONS,
            f"known={sorted(KNOWN_SEMANTICS_VERSIONS)} got={semantics_version}")
    else:
        warnings.append(
            "manifest 缺少 semantics_version：该 run 生成于计算语义版本轴引入之前，"
            "其策略 P&L 与闸门口径属旧口径（v1-legacy），"
            "v2 专属门禁项对其记为 not_applicable 而非通过。")

    # 10e. 审计修复（v2 语义专属）：策略 P&L 独立重算。
    #      历史盲区：门禁只重算 CTD/IRR，从不重算策略 P&L，
    #      因此「P&L 重复贴现」这类缺陷能以全绿通过 33 项检查。
    #      这里从 run 输入副本重新实现 P&L 计算路径，与输出 CSV 对拍。
    #      v1 语义的旧 run 没有新列，记为 not_applicable 而不是「通过」。
    if semantics_version == SEMANTICS_VERSION and strat:
        try:
            pnl_err, pnl_detail = _independent_recompute_strategy_pnl(
                snap, strat, meta)
            # 阈值与 CSV 精度对齐（6 位小数，最坏舍入累积 ~2e-6）；
            # 用 1e-6 会把纯舍入误差误判成实现缺陷。
            chk("independent_recompute_strategy_pnl", pnl_err < 5e-6,
                f"max_abs_err={pnl_err:.3e} tol=5e-6 {pnl_detail}")
        except Exception as exc:  # noqa: BLE001
            chk("independent_recompute_strategy_pnl", False, f"recompute failed: {exc}")

        # 事前持仓断言：持仓标的必须等于「用该行融资利率在期初曲线上判定的 CTD」。
        # 这条直接堵死「闸门按期初判定、持仓却取冲击后 CTD」的事后选券缺陷。
        try:
            wrong_hold = _exante_holding_violations(snap, strat, meta)
            chk("exante_holding_is_entry_ctd", not wrong_hold,
                f"violations={len(wrong_hold)} "
                f"sample={wrong_hold[:3]}")
        except Exception as exc:  # noqa: BLE001
            chk("exante_holding_is_entry_ctd", False, f"check failed: {exc}")

        # 终值口径恒等式：A − B = 估值基准差 + 交割费 − 期货腿损益
        try:
            basis_err = _terminal_basis_identity_error(strat)
            # 阈值与 CSV 精度对齐：四列各保留 6 位小数，最坏舍入累积上界 2e-6。
            # 用 1e-6 会把纯舍入误差误判成口径错误。
            chk("terminal_basis_identity_holds", basis_err < 5e-6,
                f"max_abs_err={basis_err:.3e} tol=5e-6 (CSV 6 位小数舍入上界 2e-6)")
        except Exception as exc:  # noqa: BLE001
            chk("terminal_basis_identity_holds", False, f"check failed: {exc}")

        # 跳过的情景 P&L 必须恒为 0（否则「未执行」却仍有收益，是自相矛盾）
        try:
            bad_skip = [r.get("grid_index") for r in strat
                        if not as_flag(r.get("C_executed"))
                        and abs(float(r.get("C_research_pnl_per_100", 0.0))) > 1e-9]
            chk("skipped_scenarios_have_zero_pnl", not bad_skip,
                f"violations={len(bad_skip)} sample={bad_skip[:5]}")
        except Exception as exc:  # noqa: BLE001
            chk("skipped_scenarios_have_zero_pnl", False, f"check failed: {exc}")

        # 「事后换券」不必然优于「事前持仓」：IRR 高的券其转换因子可能更低，
        # 发票价 = F×CF + AI 经 CF 缩放后未必更高。这是一条**真实结论**，
        # 不是实现缺陷，因此此处只校验它与持仓券确实是两只不同的券，
        # 以及未执行情景下它必须为 0。
        try:
            bad_hs = [r.get("grid_index") for r in strat
                      if not as_flag(r.get("C_executed"))
                      and abs(float(r.get("hindsight_C_pnl_per_100", 0.0))) > 1e-9]
            chk("hindsight_zero_when_skipped", not bad_hs,
                f"violations={len(bad_hs)} sample={bad_hs[:5]}")
        except Exception as exc:  # noqa: BLE001
            chk("hindsight_zero_when_skipped", False, f"check failed: {exc}")

        # 事后换券口径必须真的换成了另一只券（否则该列毫无信息量）
        try:
            distinct = [r for r in strat
                        if as_flag(r.get("C_executed"))
                        and r.get("post_shock_ctd") != r.get("held_bond")]
            same_but_different = [r.get("grid_index") for r in distinct
                                  if abs(float(r.get("hindsight_C_pnl_per_100", 0.0))
                                         - float(r.get("C_research_pnl_per_100", 0.0))) < 1e-12]
            chk("hindsight_uses_switched_bond", not same_but_different,
                f"n_switched_executed={len(distinct)} "
                f"identical_to_actual={len(same_but_different)}")
        except Exception as exc:  # noqa: BLE001
            chk("hindsight_uses_switched_bond", False, f"check failed: {exc}")
    else:
        # 旧 run：不记为「通过」，只登记为不适用。
        warnings.append(
            f"semantics_version={semantics_version or '(缺失)'}：策略 P&L 独立重算、"
            "事前持仓断言与终值口径恒等式检查不适用于该 run（not_applicable），"
            "不得据此认为该 run 的策略 P&L 已被独立验证。")

    # 11. 规范 §8.2：关键阈值微扰后结论不得无解释地剧烈翻转
    rob_path = run_dir / "robustness.json"
    if rob_path.exists():
        rob = json.loads(rob_path.read_text(encoding="utf-8"))
        chk("perturbation_conclusion_stable", bool(rob.get("conclusion_stable")),
            f"ctd_flips={rob.get('ctd_flips')} cost_flips={rob.get('cost_and_price_perturbation_exec_flips')} "
            f"max_exec_rate_gap={rob.get('max_exec_rate_gap')}")
        # 新契约：搜到翻转须同时有阈值与解释；搜不到须显式 cf_flip_found=false，
        # 禁止把搜索上限冒充阈值。历史产物无 cf_flip_found 字段时回退旧检查。
        if "cf_flip_found" in rob:
            if rob.get("cf_flip_found"):
                chk("cf_flip_explicitly_explained",
                    bool(rob.get("cf_flip_explanation")) and rob.get("cf_flip_threshold_abs") is not None,
                    f"cf_flip_threshold={rob.get('cf_flip_threshold_abs')}")
                warnings.append(
                    "CF 敏感性告警：转换因子微小变动即可翻转可执行判定；"
                    "缺交易所公布 CF 时不得给出可执行结论。"
                )
            else:
                chk("cf_flip_absence_documented",
                    rob.get("cf_flip_threshold_abs") is None and bool(rob.get("cf_flip_explanation")),
                    "no-flip must keep threshold null and explain search window")
        elif rob.get("cf_sensitivity_alert"):
            chk("cf_flip_explicitly_explained",
                bool(rob.get("cf_flip_explanation")) and bool(rob.get("cf_flip_threshold_abs")),
                f"cf_flip_threshold={rob.get('cf_flip_threshold_abs')}")
            warnings.append(
                "CF 敏感性告警：转换因子微小变动即可翻转可执行判定；"
                "缺交易所公布 CF 时不得给出可执行结论。"
            )
        chk("unittest_selftest_hard_assertions_present",
            (ROOT_TESTS / "test_bonds.py").exists(), "")
    return {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "run_dir": record_path(run_dir),
        "data_version": manifest.get("data_version"),
        "source_mode": meta["source_mode"],
    }
