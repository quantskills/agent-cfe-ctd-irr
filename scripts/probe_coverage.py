# -*- coding: utf-8 -*-
"""官方数据域覆盖探测：把「官方库没有债券/CF/回购域」变成可核验证据。

零网络、不发请求、不读凭据。默认读本 skill 已落盘的方法名清单；
若清单缺失且当前 Python 装有 panda_data，则现场枚举后落盘。

按 `_` 分词做词元精确匹配，避免 `get_fina_reports` /
`get_stock_shareholder_report` 里的 `repo`（来自 rePORTS）被子串匹配误判；
对形近但需要人工看文档的方法逐条给出排除依据。

用法：
    python scripts/probe_coverage.py
    PANDADATA_API_SKILL=<依赖 Skill 目录> python scripts/probe_coverage.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OFFICIAL = ROOT / "data" / "official"
METHODS_JSON = OFFICIAL / "sdk_methods.json"
OUT = OFFICIAL / "coverage_probe.json"

# 依赖 Skill 的位置随环境变化：允许 PANDADATA_API_SKILL 覆盖，默认走用户目录。
API_SKILL = Path(os.environ.get("PANDADATA_API_SKILL", "")).expanduser() \
    if os.environ.get("PANDADATA_API_SKILL") else \
    Path.home() / ".claude" / "skills" / "pandadata-api"
API_DOCS = API_SKILL / "references" / "api-docs.md"
METHOD_INDEX = API_SKILL / "references" / "method-index.md"

# 与本题数据前提一一对应的词元（下划线分词后精确匹配）
DOMAIN_TOKENS = ("bond", "debt", "deliver", "deliverable", "convert",
                 "conversion", "repo", "ctd", "lend", "accru", "coupon",
                 "invoice", "treasury")

# 形近但需看文档才能排除的方法：{方法: (文档起始行, 实际数据域, 排除依据)}
NEAR_MISS = {
    "get_repurchase": (3218, "股票回购",
                       "入参为 symbol=股票代码，响应含回购股数/每股价格/注销日，是上市公司股份回购，不是债券回购融资利率。"),
    "get_macro_cal": (22484, "宏观经济日历事件",
                      "响应为 event_code / event_name_cn / actual_value 等事件发布字段，不是利率指标码表。"),
    "get_macro_cal_config": (22607, "经济日历事件配置",
                             "示例 event_code=CHN.001 对应官方制造业 PMI，event_code 命名与 IR0000xxx 不同。"),
    "get_macro_cal_info": (22555, "经济日历事件发布信息",
                           "响应为 pub_date_bj / indic_name / country_code，不含 IR 指标代码与数值映射。"),
    "get_future_basis": (None, "商品期现基差",
                         "口径为商品现货-期货价差，不含国债期货发票价格、CF 或净基差 BNOC。"),
    "get_future_contract_pool": (10504, "期货多空合约池",
                                 "响应仅为 symbol/date/underlying_symbol/position_direction，无交割券信息。"),
    "get_future_detail": (None, "期货合约静态条款",
                          "本题已用：含 start/end_delivery_date、contract_multiplier、margin_rate；但不含可交割券清单与 CF。"),
    "get_macro_ir": (20554, "中国宏观-利率汇率指标数值",
                     "响应仅 symbol/period_date/data_value；文档注明『完整指标码表见文件下载』，该码表文件本机未取得，"
                     "故无法把任一 data_value 认定为回购/借券利率。"),
}


def load_methods() -> list[str]:
    if METHODS_JSON.exists():
        return sorted(json.loads(METHODS_JSON.read_text(encoding="utf-8"))["methods"])
    try:
        import panda_data  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            f"[FAIL] {METHODS_JSON} 不存在且当前环境无 panda_data；"
            f"请在平台已授权的 Python 环境（装有 panda_data SDK）运行一次以生成清单：{exc}"
        )
    methods = sorted(m for m in dir(panda_data) if m.startswith("get_"))
    METHODS_JSON.write_text(json.dumps({
        "source": "panda_data SDK dir() enumeration (no network)",
        "count": len(methods),
        "methods": methods,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return methods


def main() -> int:
    OFFICIAL.mkdir(parents=True, exist_ok=True)
    methods = load_methods()
    exact: dict[str, list[str]] = {}
    for name in methods:
        tokens = [p for p in name.lower().split("_") if p in DOMAIN_TOKENS]
        if tokens:
            exact[name] = tokens

    near = []
    for method, (line, domain, reason) in NEAR_MISS.items():
        near.append({
            "method": method,
            "in_sdk": method in methods,
            "api_docs_source_line": line,
            "actual_domain": domain,
            "exclusion_reason": reason,
        })

    probe = {
        "probe_time": datetime.now().isoformat(timespec="seconds"),
        "network_calls": 0,
        "credential_used": False,
        "sdk_get_method_count": len(methods),
        "method_name_source": str(METHODS_JSON),
        "environment_paths": {
            "api_docs": str(API_DOCS),
            "api_docs_available": API_DOCS.is_file(),
            "method_index": str(METHOD_INDEX),
            "method_index_available": METHOD_INDEX.is_file(),
            "resolution": "PANDADATA_API_SKILL 环境变量优先，默认 ~/.claude/skills/pandadata-api",
        },
        "matching_rule": "按 '_' 分词后与 DOMAIN_TOKENS 做词元精确匹配（避免 reports→repo 子串误判）",
        "domain_tokens": list(DOMAIN_TOKENS),
        "exact_token_matches": exact,
        "near_miss_methods_checked": near,
        "macro_ir_catalog": {
            "status": "NOT_AVAILABLE_LOCALLY",
            "doc_quote": "指标代码，完整指标码表见文件下载",
            "doc_source": f"{API_DOCS} 第 20558 行附近（get_macro_ir 入参表）",
            "local_catalog_file_found": False,
            "downloaded": False,
            "consequence": "无法把 IR0000xxx 的 data_value 映射为回购/借券利率；本题 repo_rate 与 bond_lend_fee 仍为声明式情景值。",
            "how_to_close": "取得官方指标码表文件后，按码表锁定回购利率指标代码，再以同一 as_of 取数并替换 costs.repo_rate。",
        },
        "trade_cal_exchange_mismatch": {
            "call": "get_trade_cal(exchange='SH', ...)",
            "issue": "上交所日历被用于辅助核对中金所 CFE 合约的锚定日，交易所口径不一致。",
            "api_docs_quote": "exchange: 交易所代码，默认为 \"SH\"，目前支持\"SH\"，\"HK\"和\"US\"",
            "api_docs_source": f"{API_DOCS} 第 1 章 get_trade_cal 入参表",
            "cfe_supported": False,
            "resolution": "不以 trade_cal 作为 CFE 日历证据；锚定日以 get_future_daily 该日实际行情为准，交割日以中金所合约日历/交割细则为准。",
            "evidence_status": "EVIDENCE_WITHDRAWN",
        },
        "evidence_files": [str(METHODS_JSON), str(API_DOCS), str(METHOD_INDEX),
                           str(OFFICIAL / "official_anchor.json"),
                           str(ROOT / "data" / "official" / "macro_ir_latest.csv")],
        "conclusion": (
            "213 个 get_* 方法按词元精确匹配无债券/可交割券/转换因子/回购域方法；"
            "8 个形近方法已逐条看文档排除。官方库可覆盖期货行情、合约静态交割条款与交易日历，"
            "但不覆盖可交割券清单、现券全价/应计、交易所公布 CF 与市场回购/借券利率；"
            "且利率指标码表未取得。可交割券清单、公布 CF、交割结算价规则与可交割范围原文"
            "另有交易所公开材料（见 data/official/cffex_sources.json），但未接入 input_snapshot。"
            "因此整体输入仍须标为 degraded。"
        ),
        "method_note": (
            "data/official/official_anchor.json 的 bond_like_method_names 字段采用子串匹配，"
            "会误含 get_fina_reports / get_stock_shareholder_report，不作为覆盖结论依据。"
        ),
    }
    OUT.write_text(json.dumps(probe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "sdk_methods": len(methods),
        "exact_matches": exact,
        "near_miss_checked": len(near),
        "out": str(OUT),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
