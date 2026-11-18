# -*- coding: utf-8 -*-
"""从 PandaData 官方数据服务拉取本题可核验的锚定数据（单次、可数请求）。

用法：
    <装有 panda_data SDK 的平台 Python> scripts/fetch_official.py        # 单次可数请求
    python scripts/fetch_official.py --reuse                              # 只读本地已落盘数据，零网络

设计约束：
1. 凭据只从 ~/.pandadata/pandadata.env 读入进程内存，绝不写进任何产物，
   也不出现在命令行参数或日志里。
2. 每个 get_* 接口只调用一次（symbol 兜底最多再加一次），不做轮询/批量。
3. 官方库没有现券、转换因子、可交割券清单、回购/借券利率域；
   本脚本只拉「期货行情 + 合约交割条款 + 宏观利率指标」，缺口显式记录。
4. --reuse 时直接读本地已落盘数据，零网络请求，保证可复跑。
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = Path.home() / ".pandadata" / "pandadata.env"
DEFAULT_OUT = ROOT / "data" / "official"
DEFAULT_LOG = ROOT / "reports" / "runtime_out" / "official_probe"


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line, posix=True)
        except ValueError:
            continue
        if parts and parts[0] == "export":
            parts = parts[1:]
        if not parts or "=" not in parts[0]:
            continue
        key, value = parts[0].split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _to_records(df) -> list[dict]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        return df.to_dict(orient="records")
    return list(df)


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    cols: list[str] = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in cols})


def _to_plain(value):
    """把 numpy/pandas 标量转成可 JSON 序列化的原生类型。"""
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return str(value)
    return value


def call(pd, calls: list[dict], method: str, **params) -> tuple[list[dict], bool]:
    """执行一次官方接口调用并留痕，不吞异常。"""
    entry: dict = {"method": method, "params": {k: _to_plain(v) for k, v in params.items()}}
    started = time.time()
    try:
        df = getattr(pd, method)(**params)
        rows = [{k: _to_plain(v) for k, v in r.items()} for r in _to_records(df)]
        entry.update(ok=True, rows=len(rows), seconds=round(time.time() - started, 2))
    except Exception as exc:  # 单次失败即记录，不重试
        rows = []
        entry.update(ok=False, rows=0, seconds=round(time.time() - started, 2),
                     error=f"{type(exc).__name__}: {exc}")
    calls.append(entry)
    print(f"[fetch] {method}: rows={entry['rows']} ok={entry['ok']} "
          f"{entry['seconds']}s", flush=True)
    return rows, bool(entry["ok"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=str(DEFAULT_ENV))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG))
    ap.add_argument("--as-of", default="2026-11-13",
                    help="锚定日 YYYY-MM-DD；不得晚于官方行情最新日")
    ap.add_argument("--contract", default="T2612", help="合约交易代码，如 T2612")
    ap.add_argument("--window-days", type=int, default=90)
    ap.add_argument("--reuse", action="store_true",
                    help="复用已落盘 official_anchor.json，零网络请求")
    args = ap.parse_args()

    out_dir, log_dir = Path(args.out_dir), Path(args.log_dir)
    anchor_path = out_dir / "official_anchor.json"
    if args.reuse:
        if not anchor_path.exists():
            print("[FAIL] --reuse 但 official_anchor.json 不存在", file=sys.stderr)
            return 2
        print(f"[fetch] reuse local anchor: {anchor_path}")
        return 0

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"[FAIL] 凭据文件不存在: {env_path}", file=sys.stderr)
        return 2
    env = _load_env(env_path)
    user, pwd, base = (env.get("DEFAULT_USERNAME", ""), env.get("DEFAULT_PASSWORD", ""),
                       env.get("JAVA_SERVICE_BASE_URL", ""))
    if not (user and pwd and base):
        print("[FAIL] 凭据文件缺少 DEFAULT_USERNAME / DEFAULT_PASSWORD / JAVA_SERVICE_BASE_URL",
              file=sys.stderr)
        return 2

    try:
        import panda_data
    except ModuleNotFoundError:
        print("[FAIL] 当前 Python 未安装 panda_data；请用 skills-env 的 python 运行本脚本",
              file=sys.stderr)
        return 2

    print("[fetch] login ...", flush=True)
    panda_data.init_token(username=user, password=pwd, base_url=base)
    print("[fetch] login OK", flush=True)

    as_of = date.fromisoformat(args.as_of)
    start = (as_of - timedelta(days=args.window_days)).strftime("%Y%m%d")
    end = as_of.strftime("%Y%m%d")
    cal_start = (as_of - timedelta(days=30)).strftime("%Y%m%d")
    calls: list[dict] = []

    # 1) 交易日历：确认锚定日是交易日（单次）
    cal_rows, _ = call(panda_data, calls, "get_trade_cal",
                       start_date=cal_start, end_date=end, exchange="SH", is_trading_day=1)
    _write_csv(out_dir / "trade_cal.csv", cal_rows)

    # 2) 中金所全部合约基本信息（单次）；只用 Government 域
    detail_rows, _ = call(panda_data, calls, "get_future_detail",
                          exchange=["CFE"], fields=[], is_trading=None)
    gov_rows = [r for r in detail_rows if str(r.get("product", "")) == "Government"]
    _write_csv(out_dir / "future_detail_government.csv", gov_rows)

    symbol = f"{args.contract}.CFE"
    picked = next((r for r in gov_rows if str(r.get("symbol")) == symbol), None)
    if picked is None:
        candidates = sorted(str(r.get("symbol", "")) for r in gov_rows
                            if str(r.get("symbol", "")).startswith(args.contract[:1]))
        print(f"[FAIL] 官方库未找到 {symbol}；同品种合约示例: {candidates[-8:]}", file=sys.stderr)
        return 3

    # 3) 目标合约日线（单次；仅当主代码无行情时才追加一次兜底）
    daily_rows, ok = call(panda_data, calls, "get_future_daily",
                          symbol=[symbol], start_date=start, end_date=end, fields=[])
    if not daily_rows:
        alt = f"{args.contract[:1]}_DOMINANT.CFE"
        daily_rows, ok = call(panda_data, calls, "get_future_daily",
                              symbol=[alt], start_date=start, end_date=end, fields=[])
    _write_csv(out_dir / "future_daily.csv", daily_rows)

    dated = [r for r in daily_rows if str(r.get("date", "")) <= end]
    dated.sort(key=lambda r: str(r.get("date", "")))
    latest = dated[-1] if dated else {}
    settlement = latest.get("settlement")
    close = latest.get("close")
    for k in ("settlement", "close", "open", "high", "low"):
        if k in latest:
            latest[k] = _to_plain(latest[k])
    quote_field = "settlement" if settlement not in (None, "", 0) else "close"
    quote_value = _to_plain(settlement if quote_field == "settlement" else close)

    # 4) 宏观利率指标（单次）。指标码表未随接口提供，不做含义猜测。
    ir_rows, _ = call(panda_data, calls, "get_macro_ir",
                      start_date=start, end_date=end)
    ir_symbols = sorted({str(r.get("symbol")) for r in ir_rows if r.get("symbol")})
    ir_latest = max((str(r.get("period_date", "")) for r in ir_rows), default="")
    # 接口一次返回全量历史（本题实测 82 万行）；只落盘最新数据期的截面，
    # 原始行数记入 anchor，避免把 20MB+ 无关历史塞进交付物。
    ir_latest_rows = [r for r in ir_rows if str(r.get("period_date", "")) == ir_latest]
    _write_csv(out_dir / "macro_ir_latest.csv", ir_latest_rows)

    sdk_methods = sorted(m for m in dir(panda_data) if m.startswith("get_"))
    bond_like = [m for m in sdk_methods
                 if any(x in m.lower() for x in
                        ("bond", "debt", "deliver", "convert", "repo", "ctd"))]

    anchor = {
        "fetch_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "credential_source": "~/.pandadata/pandadata.env（仅进程内存，未写入任何产物）",
        "service_base_url": base,
        "as_of_requested": args.as_of,
        "window_days": args.window_days,
        "official_calls": calls,
        "sdk_get_method_count": len(sdk_methods),
        "bond_like_method_names": bond_like,
        "calendar": {
            "rows": len(cal_rows),
            "as_of_is_trading_day": bool(latest) and str(latest.get("date", "")) == end,
            "note": "锚定日是否交易日以目标合约当日是否有行情为准；"
                    "get_trade_cal 仅作辅助核对，不据此推断交割日。",
        },
        "contract": {
            k: _to_plain(picked.get(k)) for k in
            ("symbol", "name", "product", "exchange", "underlying_symbol", "trading_code",
             "listed_date", "de_listed_date", "maturity_date", "start_delivery_date",
             "end_delivery_date", "contract_multiplier", "margin_rate", "is_trading",
             "trading_hours")
        },
        "quote": {
            "symbol": symbol,
            "date": latest.get("date"),
            "field_used": quote_field,
            "value": quote_value,
            "settlement": _to_plain(settlement),
            "close": _to_plain(close),
            "row": latest,
            "price_origin": "pandadata.get_future_daily settlement (fallback: close)",
        },
        "macro_ir": {
            "raw_rows_returned": len(ir_rows),
            "latest_period_rows_saved": len(ir_latest_rows),
            "distinct_symbols": len(ir_symbols),
            "latest_period_date": ir_latest,
            "symbol_sample": ir_symbols[:12],
            "note": "指标码表未随接口提供，无法把任一 data_value 认定为回购/借券利率；"
                    "本题 repo_rate 仍为声明式情景值。",
        },
        "coverage": {
            "government_contracts_in_CFE": len(gov_rows),
            "has_futures_quote": bool(daily_rows),
            "has_official_delivery_calendar": bool(picked.get("start_delivery_date")),
            "has_deliverable_bond_list": False,
            "has_bond_dirty_quote": False,
            "has_official_conversion_factor": False,
            "has_repo_or_lend_market_rate": False,
            "conclusion": "官方库覆盖期货行情与合约交割条款，不覆盖现券/CF/可交割券清单/融资借券成本；"
                          "整体输入仍须按规范标为 degraded，仅部分字段可官方锚定。",
        },
        "data_gaps": [
            "official_deliverable_list（交易所公布可交割券清单）",
            "bond_dirty_quote（现券全价/净价/应计利息）",
            "official_conversion_factor（交易所公布转换因子）",
            "repo_market_rate / bond_lend_market_rate（市场融资与借券成本）",
            "macro_ir 指标码表（无法把指标值映射成回购利率）",
        ],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    anchor_path.write_text(json.dumps(anchor, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "fetch_log.json").write_text(
        json.dumps({k: anchor[k] for k in
                    ("fetch_time", "service_base_url", "as_of_requested", "official_calls",
                     "sdk_get_method_count", "bond_like_method_names", "coverage")},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (log_dir / "README.md").write_text(
        "# 官方数据库探测留痕\n\n"
        "凭据：`~/.pandadata/pandadata.env`（只读入进程内存，本目录不含任何账密）。\n\n"
        "每个接口单次调用，失败不重试；`fetch_log.json` 记录方法、参数、行数与耗时。\n"
        "CSV 原始数据见 `data/official/`，结构化锚定见 `data/official/official_anchor.json`。\n\n"
        "复跑零网络：`python scripts/fetch_official.py --reuse`。\n",
        encoding="utf-8")

    print(f"[fetch] contract={symbol} delivery={picked.get('start_delivery_date')}.."
          f"{picked.get('end_delivery_date')}")
    print(f"[fetch] quote {quote_field}={quote_value} @ {latest.get('date')}")
    print(f"[fetch] government_contracts={len(gov_rows)} macro_ir_rows={len(ir_rows)}")
    print(f"[fetch] -> {anchor_path}")
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
