# -*- coding: utf-8 -*-
"""计算语义版本（semantics version）：口径漂移的显式标识。

背景：审计发现「代码改了、冻结产物没改」会让文档引用的数字与当前代码重算
的结果产生差异，而产物里没有任何字段能区分二者。解决办法是把**计算语义**
作为独立于 `data_version`（输入数据版本）的一条轴，写进每一份产物的
manifest / report / publish 元信息。

两条轴的分工：

* `data_version` —— 输入快照的版本（由 scenario_spec 哈希派生）。
  改了输入数据就必须变。
* `SEMANTICS_VERSION` —— 计算口径的版本。改了公式、闸门、选券规则、
  终值口径就必须变，**即使输入一个字节都没动**。

v2 相对 v1 的语义变更（详见 reports/AUDIT.md §11）：

1. **持仓标的改为严格事前（ex-ante）**。v1 用「冲击后」的 CTD 作为持仓
   标的，而闸门却用期初曲线判定 —— 同一个策略里混入了事后信息。
   v2 一律持有**结算日即可判定**的 CTD；交割时点若 CTD 已切换，那是
   持仓要承担的**风险**，不是可以事后追踪的免费收益。
2. **CTD 切换从「隐含 alpha」变成「显式风险 + 事后反事实」**。每行新增
   `post_shock_ctd` / `ctd_switched` / `switch_opportunity_bp`（事后机会成本）
   与 `hindsight_C_pnl_per_100`（事后换券反事实，明确不可实现）。
   注意后者**不是上界**：事后最优券按 IRR 选出，而发票价经转换因子缩放，
   IRR 更高的券其 CF 可能更低、绝对 P&L 反而更小，故该差额可正可负。
3. **A/B/C 的现券腿终值口径分离并可拆解**。买入持有 A 没有期货腿，
   没有交割义务，只能在现券市场卖出（盯市口径）；现金套利 B/C 有期货
   空头，交割现券（交割口径）。两者之差 `valuation_basis_gap` 必须显式
   拆出，否则 A/B 的差额会被误读成「对冲效果」。
   恒等式：`A − B = valuation_basis_gap + delivery_fee − hedge_leg_pnl_B`。
4. **期货腿久期代理与持仓标的一致**。期货交割价对收益率平移的敏感度用
   **期初 CTD 的修正久期**做代理（结算日即可观测，与持仓标的同一只券），
   v1 的基准 CTD 久期与 v2 的期初 CTD 久期在此快照下本就相同，
   但 v2 把这条绑定写成显式不变量，避免未来改动引入事后信息。
5. **C 的执行闸门与持仓标的同源**。闸门在结算日用期初曲线判定，
   持仓也取期初 CTD；两者不再分属两个不同的信息集。

本文件只声明版本与语义说明，不含计算逻辑；所有产物在写入时都会带上
`semantics_version` 字段。
"""
from __future__ import annotations

SEMANTICS_VERSION = "v2-exante-execution"

SEMANTICS_SHORT = "v2 事前执行口径"

SEMANTICS_DESCRIPTION = (
    "严格事前选券（持结算日判定的 CTD）、CTD 切换作为显式风险与事后反事实"
    "（不可实现，且不是上界）、"
    "A/B/C 现券腿终值口径分离并可拆解、期货腿久期代理事前可知。"
)

#: 旧产物（v1 语义）不适用 v2 专属门禁项；验证层据此跳过并显式记录，
#: 避免用新口径去判定旧产物，也避免旧产物被误认为已通过新口径检查。
LEGACY_SEMANTICS_VERSIONS = ("v1-legacy",)

#: 验证层认可的全部语义版本。旧 run 的 manifest 没有该字段（缺失），
#: 或显式带 v1-legacy，都属于「历史产物」而非「未知版本」——
#: 二者的区别是：未知版本要报错，历史版本要记 not_applicable。
KNOWN_SEMANTICS_VERSIONS = (SEMANTICS_VERSION,) + LEGACY_SEMANTICS_VERSIONS


def is_current(version: str | None) -> bool:
    """该 run 是否使用了当前语义版本。"""
    return version == SEMANTICS_VERSION


def stamp() -> dict:
    """写入产物元信息的语义标识块。"""
    return {
        "semantics_version": SEMANTICS_VERSION,
        "semantics_short": SEMANTICS_SHORT,
        "semantics_description": SEMANTICS_DESCRIPTION,
    }


def legacy_note(version: str | None) -> str | None:
    """对旧语义 run 给出一句可读的说明；当前语义返回 None。"""
    if is_current(version):
        return None
    return (
        f"该 run 的 semantics_version={version or '(缺失)'}，"
        f"低于当前语义 {SEMANTICS_VERSION}；其策略 P&L、闸门口径与选券规则"
        "属于旧口径，v2 专属门禁项对其不适用（记为 not_applicable 而非通过）。"
    )
