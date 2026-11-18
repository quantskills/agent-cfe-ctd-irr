# 结构样例：冻结基线 run 的 L1–L3 输出

> 本文件只展示报告结构，不作为当前结论依据。当前证据状态见 `reports/AUDIT.md`。
> 样例数值取自冻结基线 run `20261114-223330`，`source_mode=degraded`。

## L1 · 一句话结论

在冻结声明式快照（`as_of=2026-11-13`，`data_version=degraded-ctd-irr-v1-64e0d9b08206`）
下，CTD=SCEN-C（IRR 2.197%，BNOC −0.0287），IRR−融资 34.737bp 小于全成本 49.778bp，
正向执行判定为否。该结论是条件化方法复现，不是市场套利判断。

## L2 · 证据表

| 项 | 数值 | 数据期 / 来源 |
|---|---|---|
| CTD（IRR 最高 / BNOC 最低一致） | SCEN-C | 2026-11-13 · `ctb_table.csv` |
| IRR | 2.197% | 同上 |
| IRR−融资 | 34.737bp | `executable_boundary.json` |
| 全成本带 | 49.778bp | `executable_boundary.json` |
| 正向可执行 | 否 | `executable_boundary.json` |

## L3 · IRR 排序（ASCII 降级）

```text
SCEN-C   2.197%  ████████████████████  CTD
SCEN-A   1.668%  ███████████████
SCEN-D  -6.319%  ░
SCEN-B  -8.642%  ░
```

## 事实与解读分离

- 事实：同一快照下 IRR 最高与 BNOC 最低均为 SCEN-C，与次优券相差 52.985bp。
- 解读：空间未覆盖全成本；CF 为声明式参考值，缺公布 CF 与同日现券价，
  因此不进入 live，也不给出交易指令。

## 数据来源与免责声明

来源：`reports/runtime_out/20261114-223330/`，门禁 33 项 `ok=true`。
本报告基于公开数据与规则化分析生成，仅供研究参考，不构成任何投资建议。
