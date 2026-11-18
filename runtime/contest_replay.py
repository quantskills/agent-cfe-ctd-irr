# -*- coding: utf-8 -*-
"""期货模拟赛核验与「等价回放范围」事先声明。

题目条款：交易执行部分用期货模拟赛核验；品种或接口不支持时事先明确等价回放范围。
本机事实（2026-11-14 核验）：
  - QuantStudio 可由官方 launcher 启动（dsh web: 127.0.0.1:3097）。
  - 平台品种目录含中金所 t/tf/tl/ts（enabled=true），但「在目录中」不等于
    「该交割月份有行情或可交易」（docs/futures-instruments.md §行情与交易边界）。
  - 比赛 CLI 未安装/未授权：<DSH_HOME>/quantskills/contest/ 不存在，
    state.json / auth/ / runtimes/ 均无；不调用 CLI、不登录、不发出任何委托。
  - 期货模拟赛接口只覆盖期货腿，不覆盖现券交割、转换因子、应计利息、
    发票价格与债券借贷 → 这部分必须走等价回放。

因此本模块：
  1) 明确写出可核验范围与不可核验范围（事先声明，不是事后补）；
  2) 生成符合官方 CLI `order --dry-run --json` 参数形状的计划（不执行）；
  3) 用本地确定性账本回放期货腿，明确标注不是成交回执。
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI_PACKAGE = "@chongqingliangyunzhijing/contest-cli"
CLI_INVOCATION = "node <runtime>/node_modules/{pkg}/bin/panda.js order --symbol <contract> --direction <buy|sell> --offset open --volume <lots> --dry-run --json"
PRODUCTS_REL = Path("packages/quantskills-session/fly-runtime/fly/futures-products.json")
# 冻结快照与运行清单只记录这条配方。具体目录在运行时解析，不写入绝对路径。
PLATFORM_ENTRY_RECIPE = "<DSH_HOME>/launch-quantskills.ps1 -> launcher.mjs -> dsh web"
OUTSIDE_PROJECT = "<outside-project>"


def resolve_platform_paths(environ=None, project_root=None, max_depth: int = 3) -> dict:
    """解析平台路径，不硬编码任何机器专属绝对路径。

    优先用环境变量声明；否则从项目根目录向上有限层级发现 QSHome / QuantStudio。
    `max_depth=3` 覆盖本机布局 `<workspace>/<skills>/<skill>` → `<workspace>`，
    同时避免在上层目录误命中别的机器上的同名目录。
    """
    env = os.environ if environ is None else environ
    root = Path(project_root).resolve() if project_root is not None else ROOT
    parents = [root, *list(root.parents)[:max(0, max_depth - 1)]]

    dsh_home, dsh_source = None, "not_found"
    for key in ("DSH_HOME", "QSHOME", "QUANTSTUDIO_HOME"):
        value = env.get(key)
        if value:
            dsh_home, dsh_source = Path(value).expanduser(), f"env:{key}"
            break
    if dsh_home is None:
        for parent in parents:
            candidate = parent / "QSHome"
            if candidate.is_dir():
                dsh_home, dsh_source = candidate, "discovered:project_relative"
                break

    products, products_source = None, "not_found"
    override = env.get("QUANTSTUDIO_PRODUCTS_JSON")
    if override:
        products, products_source = Path(override).expanduser(), "env:QUANTSTUDIO_PRODUCTS_JSON"
    if products is None and dsh_home is not None:
        candidate = dsh_home.parent / "QuantStudio" / PRODUCTS_REL
        if candidate.is_file():
            products, products_source = candidate, "derived_from_dsh_home"
    if products is None:
        for parent in parents:
            candidate = parent / "QuantStudio" / PRODUCTS_REL
            if candidate.is_file():
                products, products_source = candidate, "discovered:project_relative"
                break

    return {
        "dsh_home": dsh_home,
        "products_json": products,
        "dsh_source": dsh_source,
        "products_source": products_source,
    }


def record_path(path: Path | str | None, *, root: Path | None = None) -> str | None:
    """把路径写成可随仓库移动的形式。

    项目内部用相对项目根的 posix 路径。项目外部不落绝对路径，只留
    ``<outside-project>``，避免把某台机器的盘符写进快照或 run 产物。
    """
    if path is None:
        return None
    base = (Path(root) if root is not None else ROOT).resolve()
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return OUTSIDE_PROJECT
    try:
        rel = resolved.relative_to(base)
    except ValueError:
        return OUTSIDE_PROJECT
    text = rel.as_posix()
    return text or "."


def resolve_recorded_path(raw: str | None, *, root: Path | None = None) -> Path | None:
    """读回 ``record_path`` 写下的路径。

    相对路径先按项目根解析，再按当前工作目录。绝对路径只有在该文件
    仍然存在时才使用。找不到的绝对路径不会按文件名改绑到仓库里的
    同名文件，否则旧 run 的输入哈希会悄悄对上新快照。
    """
    if not raw or raw == OUTSIDE_PROJECT:
        return None
    base = (Path(root) if root is not None else ROOT).resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for origin in (base, Path.cwd()):
        found = (origin / candidate).resolve()
        if found.is_file():
            return found
    return None


def probe_platform(paths: dict | None = None) -> dict:
    """只读探测，不发请求、不下单。

    `paths` 可注入以便测试；生产调用不传，使用当前环境的解析结果。
    任何机器专属路径都不会被写死：探测不到就如实报告 not_found。
    """
    paths = resolve_platform_paths() if paths is None else paths
    dsh_home = paths.get("dsh_home")
    products_json = paths.get("products_json")
    contest_dir = dsh_home / "quantskills" / "contest" if dsh_home is not None else None
    try:
        if products_json is None:
            raise FileNotFoundError("platform products catalog not found in this environment")
        products = json.loads(products_json.read_text(encoding="utf-8"))
    except Exception as exc:                      # noqa: BLE001
        products = []
        # 异常原文常带绝对路径，产物里只保留异常类型。
        product_error = f"{type(exc).__name__}: platform products catalog unreadable"
    else:
        product_error = None
    cfe = [p for p in products if isinstance(p, dict) and p.get("exchange") == "CFE"] if isinstance(products, list) else []
    treasury = [p for p in cfe if str(p.get("product", "")).lower() in ("t", "tf", "ts", "tl")]
    return {
        "platform_paths": {
            "dsh_home": record_path(dsh_home),
            "products_json": record_path(products_json),
            "dsh_home_source": paths.get("dsh_source", "not_found"),
            "products_json_source": paths.get("products_source", "not_found"),
            "note": "环境变量声明优先；否则从项目根向上有限层级发现。产物只记录相对路径或 <outside-project>。",
        },
        "contest_dir_exists": contest_dir.exists() if contest_dir is not None else False,
        "contest_state_exists": (contest_dir / "state.json").exists() if contest_dir is not None else False,
        "cli_runtime_exists": (contest_dir / "runtimes").exists() if contest_dir is not None else False,
        "cli_available": ((contest_dir / "runtimes").exists() and (contest_dir / "state.json").exists())
                         if contest_dir is not None else False,
        "products_catalog_readable": product_error is None,
        "products_catalog_error": product_error,
        "cfe_products": [p.get("product") for p in cfe],
        "treasury_products_in_catalog": treasury,
        "catalog_disclaimer": "目录仅表示品种覆盖，不保证具体交割月份有行情、开放交易或有 PandaData 历史授权。",
        "cli_package": CLI_PACKAGE,
        "cli_invocation_template": CLI_INVOCATION.replace("{pkg}", CLI_PACKAGE),
    }


def build_scope(snap: dict, probe: dict) -> dict:
    # 声明带可核对时间戳，且由 cli.cmd_run 在任何定价计算之前落盘；
    # independent_prior_declaration_available 显式披露未取得平台方独立签发文件。
    return {
        "declared_before_run": True,
        "declaration_scope": "in_run_before_computation",
        "declared_at": datetime.now().isoformat(timespec="seconds"),
        "independent_prior_declaration_available": False,
        "declaration_evidence_note": (
            "本声明由 cli.cmd_run 在任何定价计算之前生成并落盘（早于 _core 与 _write_all），"
            "declared_at 应早于或等于 run_manifest.generated_at，可由门禁核对；"
            "但它不等同于平台方在 run 之前签发的独立事先声明文件，后者本期未取得。"
            "题面「事先明确等价回放范围」在此按「同一 run 内先声明、后计算」解释，"
            "严格口径仍需平台方签发独立文件。"
        ),
        "as_of_date": snap["meta"]["as_of_date"],
        "contract": snap["meta"]["contract"],
        "platform_probe": probe,
        "verifiable_by_contest_cli": {
            "futures_leg": ["合约代码有效性", "最新报价", "委托预演(dry-run)", "持仓与成交回执"],
            "condition": "比赛 CLI 已安装、已授权、且该交割月份柜台返回新鲜报价",
            "status_here": "NOT_EXECUTED (CLI 未安装/未授权，见 platform_probe)",
        },
        "not_verifiable_by_contest_cli": {
            "cash_leg": ["可交割券清单与现券全价", "应计利息与票息现金流", "转换因子",
                       "发票价格与实物交割", "债券借贷/借券成本", "交割结算价形成规则"],
            "reason": "期货模拟赛接口不含现券、交割与借贷域；docs/contest.md 定义的是期货单腿委托/成交。",
        },
        "equivalent_replay_range": {
            "in_scope": [
                "按 CTD 与对冲比率生成期货腿的合约/方向/手数计划（dry-run 参数形状）",
                "按冻结输入快照的确定性账本回放期货腿 P&L",
                "融资变化、CTD 切换、成本覆盖与容量边界的情景复算",
            ],
            "out_of_scope": [
                "真实委托、真实成交回执、真实资金/持仓变动",
                "现券成交、实物交割、发票价结算、借券交割失败处理",
                "把等价回放账本称为成交记录或实盘核验",
            ],
        },
        "live_order": False,
        "market_conclusion": False,
        "source_mode": snap["meta"]["source_mode"],
    }


def _lots_for(notional_wan: float, hedge_lots_per_100wan: float,
              contract_notional_wan: float) -> tuple[int, float]:
    """把「每 100 万面值应卖出的手数」换算成整数手数。

    手数必须向上取整：向下截断会把 0.94 手变成 0 手，
    于是账本出现「已执行但手数为 0、方向 none」的自相矛盾记录。
    返回 (整数手数, 理论精确手数)；over-hedge 的部分在产物中留痕。
    """
    raw = notional_wan / contract_notional_wan * hedge_lots_per_100wan
    lots = max(0, int(math.ceil(raw - 1e-9)))
    return lots, raw


def build_dry_run_plan(snap: dict, ctd: str, hedge_lots_per_100wan: float,
                       notional_wan: float) -> dict:
    """生成官方 CLI 参数形状，但绝不执行。"""
    lots, raw = _lots_for(notional_wan, hedge_lots_per_100wan,
                           snap["meta"]["contract_notional_wan"])
    return {
        "status": "PLAN_ONLY_NOT_SUBMITTED",
        "cli": CLI_PACKAGE,
        "argv": ["order", "--symbol", snap["meta"]["contract"], "--direction", "sell",
                 "--offset", "open", "--volume", str(lots), "--dry-run", "--json"],
        "ctd": ctd,
        "hedge_lots_per_100wan": round(hedge_lots_per_100wan, 6),
        "hedge_lots_exact": round(raw, 6),
        "hedge_lots_rounding": "ceil_to_whole_lot",
        "notional_wan": notional_wan,
        "reason": "比赛 CLI 未安装/未授权；且现券交割不在模拟赛接口内，故只保留等价回放计划。",
        "note": "本对象不是委托、不是成交回执，不含账户或凭据信息。",
    }


def as_flag(value) -> bool:
    """统一布尔契约：内存 bool、CSV 'True'/'False'/'true'/'1'/'0' 均安全解析。

    禁止对字符串直接 ``if value``——Python 里非空字符串 ``\"False\"`` 为真。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


# 兼容旧名
_as_flag = as_flag


def build_replay_ledger(rows: list[dict], snap: dict) -> list[dict]:
    """确定性回放账本：每情景一条，明确 replay 标记。"""
    unit = snap["meta"]["contract_notional_wan"]
    out = []
    for r in rows:
        executed = as_flag(r.get("C_executed"))
        lots, raw = _lots_for(100.0, float(r.get("C_hedge_ratio_lots", 1.0)), unit)
        if not executed:
            lots, raw = 0, 0.0
        # 账本记录的是**实际持仓标的**（v2 事前口径：结算日判定的 CTD）。
        # 旧版 CSV 只有 `ctd` 列；新列优先，缺失时回退旧列以兼容历史产物。
        held = r.get("held_bond") or r.get("ctd")
        out.append({
            "grid_index": r["grid_index"],
            "yield_shift_bp": r["yield_shift_bp"],
            "repo_rate_pct": r["repo_rate_pct"],
            "held_bond": held,
            "ctd": held,
            "replay_symbol": snap["meta"]["contract"],
            "replay_direction": "sell" if lots else "none",
            "replay_lots": lots,
            "replay_lots_exact": round(raw, 6),
            "replay_executed": executed,
            "replay_futures_pnl_per_100": r["B_simple_hedge_pnl_per_100"] if not executed else r["C_research_pnl_per_100"],
            "pnl_note": ("对照策略回放值，本情景 replay_executed=false，不代表建仓或成交"
                         if not executed else "研究方案回放值，非交易所成交回执"),
            "ledger_type": "deterministic_offline_replay_not_exchange_fill",
            "live_order": False,
        })
    return out
