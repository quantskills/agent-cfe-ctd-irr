# -*- coding: utf-8 -*-
"""一键重建：从输入规格到客户视图页面的完整链路。

这条脚本是「网页与数据必须由 skill 生成」这条要求的落地入口。清掉缓存
（run 目录、发布快照、页面）之后，执行它即可重新得到全部产物，且页面上的
数值随输入自动更新，不需要任何手工编辑。

链路（每一步都可单独复跑）::

    data/scenario_spec.json ──build_snapshot──> data/input_snapshot.json
    data/official/official_anchor.json ─┐
                                        ├─build_snapshot(锚定)─> input_snapshot_official.json
    ──runtime run──> reports/runtime_out/<run_id>/   （含门禁 validation.json）
    ──runtime publish──> reports/publish/<as_of>/<data_version>__<semantics_version>/
    ──compare_runs──> reports/official_anchor_comparison.{json,md}
    ──build_dashboard──> delivery/visualization/index.html

用法::

    python scripts/rebuild_all.py                    # 全量重建
    python scripts/rebuild_all.py --check            # 只校验输入快照可重现性，不写产物
    python scripts/rebuild_all.py --run-id-base X --run-id-anchor Y

设计约束：
1. **不删除既有 run 与发布快照**。历史 run 是口径对照与验收链的一部分，
   重跑只新建 run 目录。需要清理时显式执行 `python -m runtime clean`。
2. **不覆盖冻结输入快照**。输入快照由 `build_snapshot.py --check` 校验可重现性；
   内容不同则该步失败并保留原文件。
3. **零网络**。官方锚定数据取自已落盘的 `data/official/official_anchor.json`；
   需要重新抓取时显式执行 `python scripts/fetch_official.py`（单次可数请求）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNTIME_OUT = ROOT / "reports" / "runtime_out"
SNAPSHOT = ROOT / "data" / "input_snapshot.json"
SNAPSHOT_ANCHOR = ROOT / "data" / "input_snapshot_official.json"
ANCHOR_FILE = ROOT / "data" / "official" / "official_anchor.json"
DASHBOARD = ROOT / "delivery" / "visualization" / "index.html"


def _run(argv: list[str], title: str) -> int:
    print(f"\n=== {title} ===")
    print("$ " + " ".join(str(a) for a in argv))
    proc = subprocess.run([str(a) for a in argv], cwd=str(ROOT))
    if proc.returncode != 0:
        print(f"[FAIL] {title} 退出码 {proc.returncode}", file=sys.stderr)
    return proc.returncode


def _py(*args) -> list[str]:
    return [sys.executable, *args]


def _unique_run_id(stem: str, suffix: str) -> str:
    base = f"{stem}-{suffix}"
    candidate = base
    i = 1
    while (RUNTIME_OUT / candidate).exists():
        candidate = f"{base}-{i}"
        i += 1
    return candidate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一键重建全部产物（含客户视图页面）")
    ap.add_argument("--check", action="store_true",
                    help="只校验输入快照可重现性与输入契约，不写任何产物")
    ap.add_argument("--refresh-snapshot", action="store_true",
                    help="显式重建并覆盖输入快照（修正历史数据后使用；会产生新的 data_version）")
    ap.add_argument("--run-id-base", default=None)
    ap.add_argument("--run-id-anchor", default=None)
    ap.add_argument("--skip-publish", action="store_true")
    args = ap.parse_args(argv)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base_id = args.run_id_base or _unique_run_id(stamp, "base")
    anchor_id = args.run_id_anchor or _unique_run_id(stamp, "anchor")

    # ---- 步骤 1：输入快照（默认只校验不改写）----------------------------
    # 快照是验收链的哈希锚点，默认只校验不可重现性；只有显式 --refresh-snapshot
    # 才允许改写，且改写必然产生新的 data_version（版本轴随之推进）。
    extra = ["--force"] if args.refresh_snapshot else ["--check"]
    steps: list[tuple[str, list[str]]] = [
        ("1/6 校验冻结基线输入快照可重现",
         _py("scripts/build_snapshot.py", "--out", SNAPSHOT, *extra)),
        ("2/6 校验官方锚定输入快照可重现",
         _py("scripts/build_snapshot.py", "--official-anchor", ANCHOR_FILE,
             "--out", SNAPSHOT_ANCHOR, *extra)),
    ]
    for title, argv_ in steps:
        rc = _run(argv_, title)
        if rc != 0:
            print("[FAIL] 输入快照不可重现，已停止，未写任何产物。\n"
                  "        若这是有意修正历史数据，请加 --refresh-snapshot 显式重建"
                  "（会产生新的 data_version）。", file=sys.stderr)
            return rc

    if args.check:
        print("\n[OK] 输入快照可重现性校验通过（未写任何产物）。")
        return 0

    # ---- 步骤 2：两次研究态 run ------------------------------------------
    rc = _run(_py("-m", "runtime", "run", "--input", SNAPSHOT, "--run-id", base_id),
              f"3/6 运行冻结基线（run_id={base_id}）")
    if rc != 0:
        return rc
    rc = _run(_py("-m", "runtime", "run", "--input", SNAPSHOT_ANCHOR, "--run-id", anchor_id),
              f"4/6 运行官方锚定（run_id={anchor_id}）")
    if rc != 0:
        return rc

    # ---- 步骤 3：发布快照 ------------------------------------------------
    if not args.skip_publish:
        rc = _run(_py("-m", "runtime", "publish", "--from", f"reports/runtime_out/{base_id}",
                      "--allow-degraded"),
                  "5/6 发布冻结基线快照")
        if rc != 0:
            return rc
        rc = _run(_py("-m", "runtime", "publish", "--from", f"reports/runtime_out/{anchor_id}",
                      "--allow-degraded"),
                  "5/6 发布官方锚定快照")
        if rc != 0:
            return rc

    # ---- 步骤 4：对照结论（页面会现算，这里落盘留痕）--------------------
    rc = _run(_py("scripts/compare_runs.py",
                  "--base", f"reports/runtime_out/{base_id}",
                  "--official", f"reports/runtime_out/{anchor_id}"),
              "6/6 重算基线 ↔ 锚定对照结论")
    if rc != 0:
        return rc

    # ---- 步骤 5：客户视图页面（run 自动发现，不写死目录名）--------------
    rc = _run(_py("delivery/visualization/build_dashboard.py",
                  "--base", base_id, "--anchor", anchor_id),
              "6/6 生成客户视图页面")
    if rc != 0:
        return rc

    print("\n" + "=" * 68)
    print("  重建完成")
    print(f"  冻结基线 run : reports/runtime_out/{base_id}")
    print(f"  官方锚定 run : reports/runtime_out/{anchor_id}")
    print(f"  对照结论     : reports/official_anchor_comparison.{{json,md}}")
    print(f"  客户视图     : delivery/visualization/index.html"
          f"（{DASHBOARD.stat().st_size if DASHBOARD.exists() else 0} bytes）")
    print("  既有历史 run 与发布快照未被删除或改写。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
