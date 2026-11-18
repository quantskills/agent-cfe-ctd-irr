# -*- coding: utf-8 -*-
"""从 data/scenario_spec.json 重建冻结输入快照 data/input_snapshot.json。

用法：
    python scripts/build_snapshot.py                         # 纯声明式冻结基线
    python scripts/build_snapshot.py --official-anchor data/official/official_anchor.json \
        --out data/input_snapshot_official.json              # 官方锚定混合快照

两种模式都产出 degraded 输入：官方库只覆盖期货行情与合约交割条款，
现券全价/应计、转换因子、可交割券清单、融资与借券成本仍是声明式。
混合模式只是把「期货价 / 交割日 / 合约规格」换成可核验的真实值，
用于量化数据缺口对结论的影响，绝不是市场结论（见 references/data_guide.md）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.pricing import build_bond  # noqa: E402
from runtime.io_contract import attach_validation  # noqa: E402
from runtime.contest_replay import PLATFORM_ENTRY_RECIPE  # noqa: E402


def _iso_market_date(value) -> str | None:
    """把行情日期收成 YYYY-MM-DD。认不出时返回 None，调用方保持原规格。"""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _apply_official_anchor(spec: dict, anchor: dict) -> dict:
    """只替换官方库真实覆盖的字段，其余保持声明式，绝不静默补造。"""
    out = json.loads(json.dumps(spec))
    contract = anchor["contract"]
    quote = anchor["quote"]
    value = quote.get("value")
    if not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("official anchor: quote.value 不是可用的正价格")
    if not contract.get("start_delivery_date"):
        raise ValueError("official anchor: 缺少 start_delivery_date")

    out["contract"] = str(contract.get("trading_code") or out["contract"])
    out["exchange"] = str(contract.get("exchange") or out["exchange"])
    out["delivery_date"] = str(contract["start_delivery_date"])
    out["delivery_date_origin"] = (
        "pandadata.get_future_detail.start_delivery_date@"
        f"{anchor['as_of_requested']}（真实交割窗口 "
        f"{contract.get('start_delivery_date')}~{contract.get('end_delivery_date')}，"
        "模型按最早可交割日单点计算，未建模窗口内择券权）"
    )
    out["futures_price"] = float(value)
    quote_day = _iso_market_date(quote.get("date"))
    if quote_day:
        out["as_of_date"] = quote_day
        out["settle_date"] = quote_day
    out["futures_price_origin"] = (
        f"pandadata.get_future_daily.{quote['field_used']}@{quote['date']} "
        f"symbol={quote['symbol']}"
    )
    out["delivery_settlement_price_rule"] = (
        "代理口径：以锚定日官方结算价代入发票价格公式；真实交割结算价由交易所按"
        "交割月规则另行产生，二者不等价，故本快照不得用于交割损益结算。"
    )
    out["official_anchor"] = {
        "source": "PandaData 官方数据服务（凭据见 ~/.pandadata/pandadata.env，未写入产物）",
        "service_base_url": anchor["service_base_url"],
        "as_of": anchor["as_of_requested"],
        "official_fields": ["futures_price", "delivery_date", "contract", "exchange",
                            "contract_multiplier", "margin_rate", "maturity_date"],
        "declared_fields": ["deliverable_basket", "clean_price", "accrued_interest",
                            "conversion_factor", "repo_rate", "bond_lend_fee",
                            "yield_curve", "capacity", "transaction_cost_bp",
                            "slippage_bp", "ctd_switch_buffer_bp"],
        "contract": contract,
        "quote": {k: quote[k] for k in ("symbol", "date", "field_used", "value",
                                        "settlement", "close", "price_origin")},
        "coverage": anchor["coverage"],
        "data_gaps": anchor["data_gaps"],
        "method_calls": [c["method"] for c in anchor["official_calls"]],
    }
    out["WARNING"] = (
        "混合来源快照：期货价、交割日与合约规格来自 PandaData 官方接口；"
        "现券全价/应计、转换因子、可交割券清单、融资与借券成本仍是声明式情景值。"
        "两类数据不在同一市场状态下，混用会产生虚假基差；本快照仅用于量化"
        "数据缺口对结论的影响，禁止据此形成市场结论或交易指令。"
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=str(ROOT / "data" / "scenario_spec.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "input_snapshot.json"))
    ap.add_argument("--official-anchor", default=None,
                    help="官方数据锚定文件；提供时生成「混合来源」快照")
    ap.add_argument("--check", action="store_true",
                    help="只重建并与目标文件比对，不写盘；不一致时退出码 3")
    ap.add_argument("--force", action="store_true",
                    help="允许改写内容不同的目标文件（冻结基线的显式覆盖开关）")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_hash = hashlib.sha256(spec_path.read_bytes()).hexdigest()[:12]

    anchor = None
    anchor_hash = None
    if args.official_anchor:
        anchor_path = Path(args.official_anchor)
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor_hash = hashlib.sha256(anchor_path.read_bytes()).hexdigest()[:12]
        spec = _apply_official_anchor(spec, anchor)
        data_version = f"hybrid-ctd-irr-v1-{spec_hash}{anchor_hash[:6]}"
    else:
        data_version = f"degraded-ctd-irr-v1-{spec_hash}"

    settle = date.fromisoformat(spec["settle_date"])
    delivery_date = spec["delivery_date"]
    curve = spec["yield_curve"]

    basket = []
    for item in spec["basket"]:
        enriched = dict(item)
        enriched["delivery_date"] = delivery_date
        basket.append(build_bond(enriched, settle, curve["level"], curve["slope_per_year"]))

    snap = {
        "meta": {
            "as_of_date": spec["as_of_date"],
            "settle_date": spec["settle_date"],
            "delivery_date": delivery_date,
            "contract": spec["contract"],
            "exchange": spec["exchange"],
            "product": spec["product"],
            "futures_price": spec["futures_price"],
            "futures_price_origin": spec.get("futures_price_origin"),
            "delivery_date_origin": spec.get("delivery_date_origin"),
            "delivery_settlement_price_rule": spec.get("delivery_settlement_price_rule"),
            "nominal_standard_coupon": spec["nominal_standard_coupon"],
            "tick_size": spec["tick_size"],
            "contract_notional_wan": spec["contract_notional_wan"],
            "eligible_rule_note": spec.get("eligible_rule_note"),
            "data_version": data_version,
            "source_mode": "degraded",
            "input_origin": ("hybrid_declared_plus_pandadata" if anchor
                             else "declared_scenario"),
            "source_ref": (
                "PandaData: get_future_detail / get_future_daily / get_macro_ir; "
                "see data/official/official_anchor.json" if anchor else None
            ),
            "data_asof": anchor["as_of_requested"] if anchor else None,
            "official_anchor": spec.get("official_anchor"),
            "official_anchor_sha256_12": anchor_hash,
            # 契约用它筛掉已由官方数据补齐的缺口项；其余仍逐条标注
            "futures_market_quote": bool(anchor),
            "official_delivery_calendar": bool(anchor),
            "scenario_spec": "data/scenario_spec.json",
            "scenario_spec_sha256_12": spec_hash,
            "generated_by": "scripts/build_snapshot.py",
            "market_conclusion": False,
            "WARNING": spec["WARNING"],
        },
        "contract_terms": {
            "settlement": "physical_delivery",
            "invoice_price_formula": "F_delivery * CF + AI_delivery",
            "conversion_factor_source": "declared_scenario_derived_from_3pct_standard_coupon_NOT_exchange_published",
            "accrual_basis": "ACT/ACT",
            "day_count": "ACT/365 for simple repo rate",
            "deliverable_basket_size": len(basket),
        },
        "costs": spec["costs"],
        "capacity": spec["capacity"],
        "yield_curve": curve,
        "deliverable_basket": basket,
        "frozen_baseline": {
            "baseline_id": "FB-2026-11-13-v1",
            "rules_frozen": True,
            "A_buy_and_hold": "按 repo_rate 融资买入 CTD 现券并持有到交割，不建期货腿。",
            "B_simple_hedge": "买入 CTD 现券 + 固定 1:1 面值卖出期货（h=1），不随 CTD 切换调整，不做成本覆盖判断。",
            "C_research": "按 IRR 选券 + DV01 比率对冲 + 仅在 IRR-r 覆盖全成本且 CTD 安全垫足够时执行。",
            "comparison_rule": "同一输入快照、同一情景网格、同一成本带；只改变决策规则，不改数据与参数。",
            "note": "冻结基线是本 skill 的对照基准，任何参数改动必须产生新的 baseline_id。",
        },
        "contest_scope": {
            "platform": "QuantStudio",
            "entry": PLATFORM_ENTRY_RECIPE,
            "futures_leg_verifiable": True,
            "cash_leg_verifiable": False,
            "reason": "期货模拟赛只提供期货合约的行情/委托/成交接口，不提供现券交割、转换因子、应计利息、发票价格与债券借贷。",
            "equivalent_replay_range": "期货腿以 order --dry-run 等价回放；现券腿、发票价与借券成本用本地确定性账本回放，不冒充成交回执。",
            "live_order": False,
        },
    }

    if not anchor:
        # 冻结模式必须与首次交付字节完全一致：新增的锚定字段只在混合模式出现，
        # 否则重建会改变 input_snapshot.json 的 SHA256，令已验收 run 的哈希门禁失败。
        for key in ("official_anchor", "official_anchor_sha256_12",
                    "futures_market_quote", "official_delivery_calendar"):
            snap["meta"].pop(key, None)

    attach_validation(snap)
    out = Path(args.out)
    payload = json.dumps(snap, ensure_ascii=False, indent=2) + "\n"

    # 冻结基线保护：目标已存在且内容不同时拒绝改写。
    # 快照是验收链的哈希锚点，被无意改写会让已验收 run 的哈希门禁失效；
    # 确实要更新时必须显式 --force，且那会产生新的 data_version（本就应如此）。
    if out.exists() and not args.force:
        current = out.read_text(encoding="utf-8")
        if current == payload:
            print(f"[build] [OK] 重建结果与现有文件字节一致，未改写: {out}")
            v = snap["validation"]
            print(f"[build] data_version={data_version}")
            print(f"[build] basket={len(basket)} source_mode={v['source_mode']} ok={v['ok']}")
            print(f"[build] gap_notes={len(v['gap_notes'])} scope_reduction={len(v['scope_reduction'])}")
            return 0 if v["ok"] else 1
        if args.check:
            print(f"[build] [FAIL] 重建结果与现有文件不一致: {out}", file=sys.stderr)
            print("        现有文件保留，未做任何改写。"
                  "若确需以新输入覆盖，请显式加 --force。", file=sys.stderr)
            return 3
        print(f"[build] [FAIL] 目标已存在且内容不同，拒绝改写: {out}", file=sys.stderr)
        print("        快照是验收链的哈希锚点。若确需更新冻结基线，"
              "请显式加 --force（新输入会产生新的 data_version）。", file=sys.stderr)
        return 3

    if args.check:
        print(f"[build] [FAIL] 目标不存在: {out}", file=sys.stderr)
        return 3

    out.parent.mkdir(parents=True, exist_ok=True)
    # newline 固定为 LF，避免 Windows 把换行写成 CRLF 后字节哈希随操作系统变化。
    out.write_text(payload, encoding="utf-8", newline="\n")

    v = snap["validation"]
    print(f"[build] data_version={data_version}")
    print(f"[build] basket={len(basket)} source_mode={v['source_mode']} ok={v['ok']}")
    print(f"[build] gap_notes={len(v['gap_notes'])} scope_reduction={len(v['scope_reduction'])}")
    print(f"[build] -> {out}")
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
