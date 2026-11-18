---
name: agent-cfe-ctd-irr
description: "国债期货 CTD / IRR / BNOC 复现与可执行边界研究 Agent。Use when a research agent needs to reproduce cheapest-to-deliver, implied repo rate and net basis on a frozen snapshot, scan financing and CTD-switch scenarios, and state an evidence-bounded execution boundary without trading instructions."
metadata:
  organization: Independent (QuantStudio local workspace)
  project_type: agent
  category: fixed-income-research
  status: research
  validation_level: runnable
  license: MIT
  maintainer_type: local
  endorsement: none
quantSkills:
  project_type: agent
  category: fixed-income-research
  collection: fixed-income-research
  license: MIT
  language: zh
  platforms: [quantstudio, claude-code, codex, cursor]
  status: research
  validation_level: runnable
  maintainer_type: local
  endorsement: none
  requires: [pandadata-api]
  summary_zh: 复现国债期货最便宜可交割券、隐含回购利率与净基差，给出融资/曲线平移情景下的 CTD 切换与全成本可执行边界。研究型 Agent，非 alpha 生产因子包。
  summary_en: Reproduce CTD, implied repo rate and net basis for CGB futures, with financing and CTD-switch scenarios and an all-in-cost executable boundary. Research agent, not a production alpha factor.
---

# 国债期货 CTD / IRR / BNOC 复现与可执行边界

本文件是本仓库的 Agent 声明与行为入口。给人读的项目背景在
[`README.md`](README.md)；方法正文在 [`SKILL.md`](SKILL.md)；题目范围对照在
[`TASK_SCOPE.md`](TASK_SCOPE.md)；字段契约在
[`references/data_guide.md`](references/data_guide.md)；目录、对话归档与平台留痕在
[`DELIVERY_INDEX.md`](DELIVERY_INDEX.md)。

本项目为本地研究型 Agent，**不是** QuantSkills 官方或社区收录项目；
未经维护者审核，不代表 QuantSkills 官方背书。仅沿用分析型 Agent 的工程规范与
L4 Figure 的排版语言。

## 定位边界

- 品类：分析型 Agent（`agent-*`），非 `alpha-*` 生产因子包，不产出可交易 parquet。
- 能做：从冻结快照复现应计、发票价、IRR、BNOC 与 CTD 双判据；扫描融资与收益率平移；
  定位 CTD 切换边界；与两条冻结基线对照；给出全成本覆盖、容量与 CF 敏感性边界。
- 不能做：确定性交易指令；把账面净基差称为无风险套利；用参考折现 CF 冒充交易所公布 CF；
  在 `degraded` 下输出市场结论；缺正式数据时静默填造。

## 输出层级

| 层级 | 内容 | 形式 |
|------|------|------|
| L1 | 一句话条件化结论 | `report.md` / stdout |
| L2 | 逐券证据表、CTD 双判据、可执行边界 | `report.md` Markdown 表 |
| L3 | IRR 排序（ASCII 降级） | `report.md` |
| L4 | 研究仪表盘（本次交付） | `delivery/visualization/index.html` |
| L5 | 结构化表与 JSON | `reports/runtime_out/<run_id>/` |

L4 是从既有 run 产物生成的只读研究视图，不是回测净值图，也不是生产信号源。

## 使用方式

```bash
python -m runtime self-test
python -m unittest discover -s tests -v
python scripts/rebuild_all.py          # 一键重建全链路（含客户视图页面）
python -m runtime run --input data/input_snapshot.json --run-id <new-id>
python delivery/visualization/build_dashboard.py
```

需要官方数据时，先读依赖 Skill `~/.claude/skills/pandadata-api/SKILL.md`，
再按其接口契约单次调用；凭据只从本机 `~/.pandadata/pandadata.env` 读入内存。

## 验收与降级

- 当前口径两个 v2 run 各 **41 / 42** 项门禁 `ok=true`；公式层 27 条硬断言、**62 个单元测试**（2026-11-18）。
- `source_mode` 只按 provenance 判定；`live` 需七项正式数据项全为真，缺一即 `degraded`。
- 混合锚定 run 为 `INVALID_MIXED_STATE`，其“可执行=是”不得当市场结论。
- 依赖失败须标注 `gap_notes`；全部失败只输出框架，不编造数值。
- **两条独立版本轴**：`data_version`（输入数据）与 `semantics_version`（计算口径，
  当前 `v2-exante-execution`）分开登记；任一条不同，两个 run 的数值不可直接比较。
- **产物必须是派生物**：不得在下游写死某个 run 的目录名或某次运行的研究数值。
  run 按输入事实自动发现（`runtime/run_registry.py`），页面数值只来自 run 产物
  （`payload.dynamic`），模板不写死研究结论数值；冻结输入快照在内容不同时
  拒绝被改写（须显式 `--force`）。清缓存后 `scripts/rebuild_all.py` 必须能
  完整重出全部产物，数值与原产物一致。

## 边界声明

Research and education only. Outputs are not investment advice and do not promise returns.
本报告与仓库材料基于公开数据与规则化分析生成，仅供研究参考，不构成任何投资建议。
