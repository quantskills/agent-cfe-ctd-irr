# -*- coding: utf-8 -*-
"""输入快照契约：字段校验、口径一致性、source_mode 判定与缺口标注。

规范依据：Agent开发规则 §4（输入/验收降级）、§5（研究态/发布态）、
§7（公共 Schema 最小集）、§8.1（时点正确）、§14（不合格情况）。

核心原则：缺正式数据时**缩小范围并显式标注**，不静默补造、不把降级结果
当成市场结论。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .bonds import parse_date

REQUIRED_TOP = ("meta", "deliverable_basket", "costs", "capacity")
REQUIRED_META = ("as_of_date", "settle_date", "delivery_date", "futures_price",
                 "contract", "exchange", "data_version", "source_mode",
                 "contract_notional_wan")
REQUIRED_BOND = ("bond_id", "coupon", "freq", "maturity", "last_coupon",
                 "next_coupon", "clean_price", "accrued_interest",
                 "conversion_factor", "modified_duration")
REQUIRED_COSTS = ("repo_rate", "bond_lend_fee", "transaction_cost_bp",
                  "delivery_fee_per_100", "slippage_bp", "ctd_switch_buffer_bp")
# 合法付息频率（每年计息次数）；freq=0 会除零，freq=5 会静默改变应计与 IRR。
ALLOWED_FREQ = (1, 2, 4, 12)

FORMAL_DATA_ITEMS = (
    ("official_deliverable_list", "交易所公布的可交割券清单"),
    ("bond_dirty_quote", "现券全价/净价与应计利息（成交或中债估值）"),
    ("official_conversion_factor", "交易所公布的转换因子"),
    ("futures_market_quote", "期货实时/结算行情"),
    ("repo_market_rate", "市场回购融资利率"),
    ("bond_lend_market_rate", "债券借贷/借券成本"),
    ("official_delivery_calendar", "官方交割日历与交割结算价规则"),
)


def _missing(obj: dict, keys) -> list[str]:
    return [k for k in keys if k not in obj or obj[k] is None]


LIVE_ORIGINS = ("pandadata", "user_verified")


def live_requirements(meta: dict) -> dict:
    """逐项列出 live 门槛，供门禁与文档核验。

    除 input_origin / source_ref / data_asof 三项来源字段外，
    FORMAL_DATA_ITEMS 七项必须全部为真，缺一项即降级；
    避免纯声明式快照仅凭来源字段被标为 live。
    """
    items = {key: bool(meta.get(key)) for key, _ in FORMAL_DATA_ITEMS}
    return {
        "origin_allowed": str(meta.get("input_origin", "")) in LIVE_ORIGINS,
        "has_source_ref": bool(meta.get("source_ref")),
        "has_data_asof": bool(meta.get("data_asof")),
        "formal_items": items,
        "formal_items_present": sum(items.values()),
        "formal_items_required": len(items),
        "all_formal_items": all(items.values()),
        "satisfied": (
            str(meta.get("input_origin", "")) in LIVE_ORIGINS
            and bool(meta.get("source_ref"))
            and bool(meta.get("data_asof"))
            and all(items.values())
        ),
    }


def classify_source_mode(meta: dict) -> str:
    """只有带可核验来源、数据期、且七项正式数据齐备才允许标为 live。"""
    return "live" if live_requirements(meta)["satisfied"] else "degraded"


def validate_snapshot(snap: dict) -> dict:
    """返回 {ok, errors, warnings, gap_notes, scope_reduction, source_mode}。"""
    errors: list[str] = []
    warnings: list[str] = []
    gap_notes: list[str] = []

    for key in REQUIRED_TOP:
        if key not in snap:
            errors.append(f"missing top-level section: {key}")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings,
                "gap_notes": gap_notes, "scope_reduction": [], "source_mode": "unknown"}

    meta = snap["meta"]
    errors += [f"meta.{k} missing" for k in _missing(meta, REQUIRED_META)]
    errors += [f"costs.{k} missing" for k in _missing(snap["costs"], REQUIRED_COSTS)]

    basket = snap["deliverable_basket"]
    if not isinstance(basket, list) or not basket:
        errors.append("deliverable_basket must be a non-empty list")
        basket = []

    # 元层日期与券层同样要防畸形输入：解析失败不能让契约校验自身抛异常中断门禁。
    try:
        settle = parse_date(meta.get("settle_date", "2000-01-01"))
        delivery = parse_date(meta.get("delivery_date", "2000-01-01"))
        as_of = parse_date(meta.get("as_of_date", "2000-01-01"))
    except (TypeError, ValueError) as exc:
        errors.append(f"meta invalid date field: {exc}")
        settle = delivery = as_of = date.min
    if delivery <= settle:
        errors.append("delivery_date must be after settle_date")
    if as_of > settle:
        errors.append("as_of_date must not be after settle_date (no look-ahead)")

    for i, b in enumerate(basket):
        tag = f"basket[{i}]"
        missing_bond = _missing(b, REQUIRED_BOND)
        errors += [f"{tag}.{k} missing" for k in missing_bond]
        if missing_bond:
            continue
        try:
            last = parse_date(b["last_coupon"])
            nxt = parse_date(b["next_coupon"])
            mat = parse_date(b["maturity"])
            if not (last <= settle < nxt):
                errors.append(f"{tag} settle_date must lie in [last_coupon, next_coupon)")
            if mat <= delivery:
                errors.append(f"{tag} maturity must be after delivery_date")
            if not (0.0 < float(b["conversion_factor"]) < 1.3):
                errors.append(f"{tag} conversion_factor out of plausible range")
            if float(b["freq"]) not in ALLOWED_FREQ:
                # freq=0 会除零；freq=5 这类非标准频率会静默改变应计与 IRR 且无告警。
                errors.append(f"{tag} unsupported freq {b['freq']} (allowed {list(ALLOWED_FREQ)})")
            if float(b["clean_price"]) <= 0 or float(b["accrued_interest"]) < 0:
                errors.append(f"{tag} non-positive clean_price / negative accrued")
            dirty = float(b["clean_price"]) + float(b["accrued_interest"])
            if "dirty_price" in b and abs(float(b["dirty_price"]) - dirty) > 1e-5:
                errors.append(f"{tag} dirty_price != clean_price + accrued_interest")
        except (TypeError, ValueError) as exc:
            # 畸形输入必须返回 ok=False，不能让契约校验自身抛异常中断门禁。
            errors.append(f"{tag} non-numeric or invalid date field: {exc}")
            continue
        if b.get("accrual_basis", "ACT/ACT") not in ("ACT/ACT", "ACT/365"):
            errors.append(f"{tag} unsupported accrual_basis")

    source_mode = classify_source_mode(meta)
    if meta.get("source_mode") and meta["source_mode"] != source_mode:
        warnings.append(
            f"declared source_mode={meta['source_mode']} overridden to {source_mode} by provenance"
        )
    if source_mode != "live":
        if meta.get("official_anchor"):
            gap_notes.append(
                "混合来源：期货行情与合约交割条款已有 PandaData 官方锚定，"
                "但现券估值、转换因子、可交割券清单、融资/借券成本仍缺；"
                "按规范整体降级为 degraded，不得标为 live 或市场结论。"
            )
        else:
            gap_notes.append("输入缺少可核验的正式数据源/数据期，已按规范降级为 degraded。")
        for key, label in FORMAL_DATA_ITEMS:
            if not meta.get(key):
                gap_notes.append(f"缺失正式数据项 {key}（{label}）。")

    scope_reduction: list[str] = []
    if source_mode != "live":
        scope_reduction = [
            "结论仅在该冻结输入快照下成立，不外推到任何真实交易日。",
            "不输出确定性交易指令，不把账面基差/净基差称为无风险套利。",
            "所有数值标注 derived / declared_scenario 来源，market_conclusion=false。",
            "期货模拟赛无法核验现券交割、转换因子、应计利息与借券，须走等价回放（见 references/executable_boundary.md）。",
        ]

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "gap_notes": gap_notes,
        "scope_reduction": scope_reduction,
        "source_mode": source_mode,
        # live_requirements 不写进快照：冻结输入的字节哈希必须保持不变，
        # 门禁自行调用 live_requirements(meta) 计算，避免污染冻结基线。
    }


def attach_validation(snap: dict) -> dict:
    """把校验结果写回快照，统一 source_mode 与 market_conclusion 标记。"""
    result = validate_snapshot(snap)
    snap["meta"]["source_mode"] = result["source_mode"]
    snap["meta"]["market_conclusion"] = result["source_mode"] == "live"
    snap["validation"] = result
    return snap
