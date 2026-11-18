# -*- coding: utf-8 -*-
"""统一入口：python -m runtime <subcommand>。

规范 §6 子命令语义：
  (默认) run       研究态全链路
  validate         结构 / 时点 / 字段校验
  backtest         情景有效性指标（规范 §9.2 最低集）
  replay           期货模拟赛等价回放范围与账本
  publish          验收通过后写发布快照
  self-test        公式硬断言对拍
  clean            清理研究态运行产物
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.bonds import Bond, parse_date, hedge_ratio               # noqa: E402
from runtime.io_contract import attach_validation                    # noqa: E402
from runtime.scenarios import (                                      # noqa: E402
    evaluate_point, financing_scan, yield_shift_scan, strategy_comparison,
    executable_boundary, backtest_metrics,
)
from runtime.contest_replay import (                                  # noqa: E402
    probe_platform, build_scope, build_dry_run_plan, build_replay_ledger,
    as_flag, record_path,
)
from runtime.writers import (                                         # noqa: E402
    write_json, write_csv, write_manifest, write_report, publish_snapshot,
    new_run_id, sha256_file,
)
from runtime.validation import validate_run_dir                      # noqa: E402
from runtime.selftest import run_all as run_selftests                # noqa: E402
from runtime.robustness import run_robustness                        # noqa: E402


DEFAULT_INPUT = ROOT / "data" / "input_snapshot.json"
RUNTIME_OUT = ROOT / "reports" / "runtime_out"
PUBLISH_ROOT = ROOT / "reports" / "publish"


def _grid(lo: float, hi: float, step: float) -> list[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 10) for i in range(n + 1)]


def _load_snapshot(path: Path) -> dict:
    snap = json.loads(path.read_text(encoding="utf-8"))
    return attach_validation(snap)


def _core(snap: dict, r_grid: list[float], dy_grid: list[float]) -> dict:
    meta, costs = snap["meta"], snap["costs"]
    settle, delivery = parse_date(meta["settle_date"]), parse_date(meta["delivery_date"])
    bonds = [Bond.from_dict(b) for b in snap["deliverable_basket"]]
    f0, r = float(meta["futures_price"]), float(costs["repo_rate"])

    metrics, rank = evaluate_point(bonds, f0, settle, delivery, r)
    ctb_rows = [asdict(m) for m in metrics]
    fin_rows, fin_sw = financing_scan(bonds, f0, settle, delivery, r_grid)
    y_rows, y_sw = yield_shift_scan(bonds, f0, settle, delivery, r, dy_grid)
    switches = fin_sw + y_sw
    strat_rows = strategy_comparison(snap, bonds, settle, delivery, r_grid, dy_grid)
    boundary = executable_boundary(snap, bonds, settle, delivery, fin_rows, switches)
    bt = backtest_metrics(strat_rows, meta["data_version"])
    return {
        "bonds": bonds, "settle": settle, "delivery": delivery,
        "metrics": ctb_rows, "rank": rank, "fin_rows": fin_rows, "y_rows": y_rows,
        "switches": switches, "strat_rows": strat_rows, "boundary": boundary,
        "backtest": bt, "r_grid": r_grid, "dy_grid": dy_grid,
    }


def _stamp(rows: list[dict], snap: dict) -> list[dict]:
    """规范 §7 公共 Schema 最小集：所有 L5 表带锚定日/版本/来源/更新时间。"""
    meta = snap["meta"]
    stamp = {
        "as_of_date": meta["as_of_date"],
        "data_version": meta["data_version"],
        "source_mode": meta["source_mode"],
        "update_time": datetime.now().isoformat(timespec="seconds"),
        "symbol": meta["contract"],
        "asset_type": "future_cash_basis",
    }
    out = []
    for row in rows:
        merged = dict(stamp)
        merged.update(row)
        out.append(merged)
    return out


def _write_all(run_dir: Path, snap: dict, core: dict, command: str,
               input_path: Path, scope: dict | None = None,
               probe: dict | None = None) -> dict:
    write_json(run_dir / "input_snapshot.json", snap)
    write_csv(run_dir / "ctb_table.csv", _stamp(core["metrics"], snap))
    write_csv(run_dir / "financing_scan.csv", _stamp(core["fin_rows"], snap))
    write_csv(run_dir / "yield_shift_scan.csv", _stamp(core["y_rows"], snap))
    write_csv(run_dir / "strategy_comparison.csv", _stamp(core["strat_rows"], snap))
    write_json(run_dir / "switch_boundaries.json", {
        "count": len(core["switches"]), "boundaries": core["switches"],
        "note": "边界为网格中点估计，分辨率见各条 resolution；非精确定价解。",
    })
    write_json(run_dir / "executable_boundary.json", core["boundary"])
    write_json(run_dir / "backtest_metrics.json", core["backtest"])
    robustness = run_robustness(snap, core["bonds"], core["settle"],
                                core["delivery"], core["r_grid"], core["dy_grid"])
    write_json(run_dir / "robustness.json", robustness)

    # 审计修复：scope 由 cmd_run 在任何定价计算之前生成并落盘，此处只复用，
    # 不重新生成、不覆写，确保「事先声明」在时间顺序上真实成立。
    probe = probe if probe is not None else probe_platform()
    scope = scope if scope is not None else build_scope(snap, probe)
    ctd_bond = next(b for b in core["bonds"] if b.bond_id == core["rank"]["ctd_by_irr"])
    hedge_lots = hedge_ratio("dv01", ctd_bond, float(snap["meta"]["futures_price"]),
                             ctd_bond.modified_duration)
    plan = build_dry_run_plan(snap, core["rank"]["ctd_by_irr"], hedge_lots, 100.0)
    ledger = build_replay_ledger(core["strat_rows"], snap)
    replay_dir = run_dir / "contest_replay"
    if not (replay_dir / "contest_scope.json").exists():
        write_json(replay_dir / "contest_scope.json", scope)
    write_json(replay_dir / "dry_run_plan.json", plan)
    write_csv(replay_dir / "replay_ledger.csv", ledger)

    manifest = write_manifest(run_dir, snap, command, input_path, {
        "grids": {
            "repo_rate": [core["r_grid"][0], core["r_grid"][-1], len(core["r_grid"])],
            "yield_shift": [core["dy_grid"][0], core["dy_grid"][-1], len(core["dy_grid"])],
        },
        "outputs": [
            "ctb_table.csv", "financing_scan.csv", "yield_shift_scan.csv",
            "strategy_comparison.csv", "switch_boundaries.json",
            "executable_boundary.json", "backtest_metrics.json", "robustness.json",
            "contest_replay/contest_scope.json", "contest_replay/dry_run_plan.json",
            "contest_replay/replay_ledger.csv", "report.md",
        ],
        "contest_cli_available": probe["cli_available"],
        "live_order": False,
    })

    write_report(run_dir, snap, core["metrics"], core["rank"], core["boundary"],
                 core["switches"], core["metrics"], core["backtest"], robustness)
    validation = validate_run_dir(run_dir)
    write_json(run_dir / "validation.json", validation)
    write_json(run_dir / "run_index.json", {"manifest": manifest, "validation": validation})
    return validation


def cmd_run(args: argparse.Namespace) -> int:
    input_path = Path(args.input).resolve()
    snap = _load_snapshot(input_path)
    if not snap["validation"]["ok"]:
        print("[FAIL] input contract:", snap["validation"]["errors"])
        return 2

    # 融资网格默认围绕基准 repo_rate 展开，并保证基准点精确落在网格上
    r0 = float(snap["costs"]["repo_rate"])
    r_min = args.r_min if args.r_min is not None else round(r0 - 0.015, 10)
    r_max = args.r_max if args.r_max is not None else round(r0 + 0.030, 10)
    r_grid = _grid(r_min, r_max, args.r_step)
    dy_grid = _grid(args.dy_min, args.dy_max, args.dy_step)
    command = " ".join([str(Path(sys.executable).name), "-m runtime", "run",
                        f"--input {record_path(input_path)}",
                        f"--run-id {args.run_id or '<auto>'}"])
    run_dir = RUNTIME_OUT / (args.run_id or new_run_id())
    # 复用已有 run_id 会把新产物与旧产物混在一起（读者无法判断哪份是哪次跑出来的）。
    # 这里拒绝覆写并给出明确的下一步，而不是静默混入。
    if run_dir.exists() and any(run_dir.iterdir()):
        print(f"run 目录已存在且非空，拒绝覆写: {run_dir}", file=sys.stderr)
        print(f"请换一个 --run-id，或先执行: python -m runtime clean --run-id {run_dir.name}",
              file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)

    # 「事先明确等价回放范围」：必须在任何定价计算之前生成并落盘，
    # 供门禁做 scope_mtime <= ctb_table_mtime 的物理时序校验。
    probe = probe_platform()
    scope = build_scope(snap, probe)
    write_json(run_dir / "contest_replay" / "contest_scope.json", scope)

    core = _core(snap, r_grid, dy_grid)
    validation = _write_all(run_dir, snap, core, command, input_path,
                            scope=scope, probe=probe)

    print("=" * 68)
    print("  QuantStudio / CTD-IRR-BNOC research skill")
    print(f"  run_dir      : {run_dir}")
    print(f"  data_version : {snap['meta']['data_version']}")
    print(f"  source_mode  : {snap['meta']['source_mode']}  market_conclusion=false")
    print(f"  CTD (IRR)    : {core['rank']['ctd_by_irr']}  IRR={core['rank']['ctd_irr']*100:.3f}%")
    print(f"  CTD (BNOC)   : {core['rank']['ctd_by_bnoc']}  disagreement={core['rank']['ctd_disagreement']}")
    print(f"  switches     : {len(core['switches'])}  grid={len(core['strat_rows'])} scenarios")
    print(f"  forward exec : {core['boundary']['forward_executable']}  "
          f"IRR-r={core['boundary']['forward_spread_IRR_minus_repo_bp']:.2f}bp vs "
          f"cost={core['boundary']['all_in_cost_bp']:.2f}bp")
    print(f"  validation   : ok={validation['ok']} errors={len(validation['errors'])}")
    print("=" * 68)
    return 0 if validation["ok"] else 1


def _latest_run():
    """按运行时间取最近的 run；不能用目录名排序。

    目录名既有时间戳（20261114-223330）也有语义名（contract-smoke-20261118），
    按字典序排会让 20261119-xxxxxx 落在 contract-smoke-* 之前而选错目标。
    """
    if not RUNTIME_OUT.exists():
        return None
    candidates = [p for p in RUNTIME_OUT.iterdir()
                  if p.is_dir() and (p / "run_manifest.json").exists()]

    def _key(p: Path):
        try:
            manifest = json.loads((p / "run_manifest.json").read_text(encoding="utf-8"))
            stamp = str(manifest.get("generated_at") or "")
        except (OSError, ValueError):
            stamp = ""
        return (stamp, p.name)

    return max(candidates, key=_key) if candidates else None


def _resolve_run_dir(value: str) -> Path:
    """接受绝对路径 / 相对仓库根的路径 / 纯 run_id。"""
    p = Path(value)
    if p.is_absolute():
        return p
    if (ROOT / p).exists():
        return (ROOT / p).resolve()
    return (RUNTIME_OUT / p).resolve()


def cmd_validate(args: argparse.Namespace) -> int:
    if not args.run_dir:
        run_dir = _latest_run()
        if run_dir is None:
            print("[FAIL] no research run found; run `python -m runtime run` first")
            return 2
    else:
        run_dir = _resolve_run_dir(args.run_dir)
    result = validate_run_dir(run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def cmd_backtest(args: argparse.Namespace) -> int:
    if args.run_dir:
        run_dir = _resolve_run_dir(args.run_dir)
    else:
        run_dir = _latest_run()
        if run_dir is None:
            print("[FAIL] no research run found")
            return 2
    p = run_dir / "strategy_comparison.csv"
    if not p.exists():
        print(f"[FAIL] missing {p}; run `python -m runtime run` first")
        return 2
    import csv
    from .contest_replay import as_flag
    rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
    for r in rows:
        for k, v in list(r.items()):
            # 布尔列必须走 as_flag：CSV 里是字符串 "False"，直接 float 转换会失败，
            # 而字符串 "False" 在 Python 里为真值，会把「未切换」误判成「已切换」。
            if k in ("C_executed", "ctd_switched", "entry_ctd_disagreement"):
                r[k] = as_flag(v)
                continue
            try:
                r[k] = float(v)
            except (TypeError, ValueError):
                pass
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    result = backtest_metrics(rows, manifest["data_version"])
    write_json(run_dir / "backtest_metrics.json", result)
    print(json.dumps(result["overall"], ensure_ascii=False, indent=2))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    snap = _load_snapshot(Path(args.input).resolve())
    probe = probe_platform()
    scope = build_scope(snap, probe)
    out = Path(args.out).resolve()
    write_json(out / "contest_scope.json", scope)
    print(json.dumps({"cli_available": probe["cli_available"],
                      "treasury_in_catalog": [p["product"] for p in probe["treasury_products_in_catalog"]],
                      "live_order": False, "out": str(out)}, ensure_ascii=False, indent=2))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args.run_from)
    validation = validate_run_dir(run_dir)
    if not validation["ok"]:
        print("[FAIL] cannot publish an unvalidated run:", validation["errors"])
        return 1
    snap = json.loads((run_dir / "input_snapshot.json").read_text(encoding="utf-8"))
    if snap["meta"]["source_mode"] == "degraded" and not args.allow_degraded:
        print("[FAIL] degraded snapshot: pass --allow-degraded explicitly (research-only publish)")
        return 1
    dest = publish_snapshot(run_dir, PUBLISH_ROOT, snap)
    print(f"[OK] published -> {dest}")
    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    result = run_selftests()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def cmd_clean(args: argparse.Namespace) -> int:
    target = RUNTIME_OUT
    if not target.exists():
        print("[OK] nothing to clean")
        return 0
    removed = 0
    for child in target.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    print(f"[OK] removed {removed} research artifacts from {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m runtime")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="研究态全链路（默认）")
    r.add_argument("--input", default=str(DEFAULT_INPUT))
    r.add_argument("--run-id", default=None)
    r.add_argument("--r-min", type=float, default=None,
                   help="default: repo_rate - 0.015")
    r.add_argument("--r-max", type=float, default=None,
                   help="default: repo_rate + 0.030")
    r.add_argument("--r-step", type=float, default=0.0025)
    r.add_argument("--dy-min", type=float, default=-0.01)
    r.add_argument("--dy-max", type=float, default=0.01)
    r.add_argument("--dy-step", type=float, default=0.001)
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("validate")
    v.add_argument("run_dir", nargs="?", default="")
    v.set_defaults(func=cmd_validate)

    b = sub.add_parser("backtest")
    b.add_argument("run_dir", nargs="?", default="")
    b.set_defaults(func=cmd_backtest)

    rp = sub.add_parser("replay")
    rp.add_argument("--input", default=str(DEFAULT_INPUT))
    rp.add_argument("--out", default=str(ROOT / "reports" / "runtime_out" / "contest_scope"))
    rp.set_defaults(func=cmd_replay)

    pb = sub.add_parser("publish")
    pb.add_argument("--from", dest="run_from", required=True)
    pb.add_argument("--allow-degraded", action="store_true")
    pb.set_defaults(func=cmd_publish)

    st = sub.add_parser("self-test")
    st.set_defaults(func=cmd_self_test)

    c = sub.add_parser("clean")
    c.set_defaults(func=cmd_clean)
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        argv = ["run"] + (argv or sys.argv[1:])
        args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
