---
name: agent-cfe-ctd-irr
description: 当需要复现国债期货最便宜可交割券 CTD、隐含回购利率 IRR 与净基差 BNOC，并与买入持有/简单对冲对照、验证融资变化与 CTD 切换情景、给出可执行边界时使用。研究型 Agent，非 alpha 生产因子包。
---

# 国债期货 CTD / IRR / 净基差复现与可执行边界

## 定位边界

- **品类**：分析型 Agent（`agent-*`），遵循仓库旁路规范 `../.vscode/Agent开发规则.md` §3.1。
  不是 `alpha-*` 生产因子包，不产出可交易因子 parquet。
- **归属**：本地研究项目，非 QuantSkills 官方或社区收录项目；未经维护者审核，
  不代表 QuantSkills 官方背书。宿主声明入口为 `AGENTS.md`，方法正文以本文件为准。
- **能做**：从冻结输入快照复现应计利息、发票价格、隐含回购利率、净基差与 CTD；
  扫描融资利率与收益率曲线平移情景；定位 CTD 切换边界；与两条冻结基线对照；
  给出全成本覆盖、CTD 安全垫、容量与转换因子敏感性的可执行边界。
- **不能做**：不产生确定性交易指令；不把账面基差/净基差称为无风险套利；
  不把公式复算值当作交易所公布 CF（live 态 `conversion_factor` 须原样采用公布值，
  复算脚本的适用范围见 `references/methodology.md §11.1`）；
  本交付 degraded 快照里的 CF 是 3% 名义标准券折现的声明式参考值；
  不在缺正式数据时给出市场结论；不执行真实委托。
- **本题新增点**：原题未覆盖利率期现定价。核心是融资成本与可交割券约束，
  不是套用价差回归；交割期权使净基差不等于零，账面偏差不等于可锁定利润。

## 适用场景

- 需要复现 CTD / IRR / BNOC 三件套并做逐券对账时；
- 需要回答「融资变了 CTD 会不会切换」「曲线平移多少会换 CTD」时；
- 需要判断「这个净基差覆盖不覆盖成本、能不能执行、最大能做多大」时；
- 需要用冻结基线做价值验收、接受可靠负结果时。

## 输入

| 项 | 说明 |
|----|------|
| 官方数据库 | PandaData 官方数据服务；凭据在 `~/.pandadata/pandadata.env`（`DEFAULT_USERNAME` / `DEFAULT_PASSWORD` / `JAVA_SERVICE_BASE_URL`），接口契约见 `~/.claude/skills/pandadata-api/references/method-index.md` |
| 官方数据拉取 | `scripts/fetch_official.py` — 4 次单次调用，留痕 `data/official/official_anchor.json`；`--reuse` 可零网络复跑 |
| 交易所公开证据 | `data/official/cffex_sources.json` + `cffex_*`（清单/CF 与应计公式原图/交割细则 PDF）；来源 URL、SHA256、抓取时间逐项登记 |
| 当前可交割券清单 | `data/official/cffex_all_deliverable_current.csv`（T2612 共 17 只券）；`cffex_T2612_deliverable_basket.csv` 是上市时点的 13 只券快照，不作为 live 篮子；全量文件无数据期字段 |
| 冻结基线快照 | `data/input_snapshot.json`（`source_mode=degraded`，`input_origin=declared_scenario`） |
| 官方锚定混合快照 | `data/input_snapshot_official.json`（混合来源，**仍** `source_mode=degraded`） |
| 情景规格 | `data/scenario_spec.json`；由 `scripts/build_snapshot.py` 可复算生成快照 |
| `as_of` | `meta.as_of_date`；必须 <= `settle_date`（反未来函数门禁） |
| 关键参数 | `settle_date` / `delivery_date` / `futures_price` / `repo_rate` / `costs` / `capacity` |
| 数据前提字段 | 可交割券清单、现券净价/全价、应计利息、票息与付息日、转换因子、期货价格、融资与借券成本；逐券另需修正久期与可交割/可借券存量 |

PandaData 官方库覆盖期货行情、合约交割条款与交易日历（`exchange=CFE` 下 159 个
国债期货合约，`get_future_detail` 含 `start/end_delivery_date`、`contract_multiplier`、
`margin_rate`；`get_future_daily` 含结算价），不覆盖可交割券清单、现券全价/应计、
交易所公布 CF、回购与借券利率（`get_macro_ir` 只给指标代码与数值，码表未随接口提供）。
中金所公开材料已补齐可交割券清单、公布 CF、CF/应计公式与交割细则，但现券价、
回购/借券利率仍未齐备，且这些证据尚未接入计算。

按题目「缺券价或交割条件时须缩小范围」，本 skill 收敛为方法复现 + 条件化情景结论：
价格由声明式收益率曲线与债券条款可复算生成，CF 是 3% 名义标准券折现的参考值，
所有产物 `source_mode=degraded`、`market_conclusion=false`。
证据清单与适用边界见 `references/data_guide.md §9` 和 `reports/AUDIT.md`，
演进过程见 `reports/CHANGELOG.md`。

另提供官方锚定复跑：只把期货价与交割日换成真实值，用来量化数据缺口的影响
（见 `reports/official_anchor_comparison.md`），不标为 live 或市场结论。

## 输出

| 层级 | 内容 | 形式 |
|------|------|------|
| L1 | 一句话条件化结论 | `report.md` / stdout |
| L2 | 逐券证据表、CTD 双判据、可执行边界、微扰稳定性 | `report.md` Markdown 表 |
| L3 | IRR 排序图（ASCII 降级） | `report.md` |
| L4 | 面向客户的研究仪表盘：CTD 象限、情景扫描、成本闸门、三策略对照、事前持有与 CTD 切换暴露、数据口径 | `delivery/visualization/index.html`；过程证据见 `reports/` |
| L5 | 结构化表与 JSON | `reports/runtime_out/<run_id>/` 下 CSV/JSON |

**论文锚定的方法增强（独立增量，不改变上方冻结产物）**：
`reports/convexity_enhancement/{report.md,verification.json}` 提供二阶凸性重定价的
L1–L5 验证产物（9 项硬断言、精度改善 33.9×、CTD 判定与切换边界不变）；
实现见 `runtime/convexity.py`，可复跑入口 `python scripts/convexity_report.py`。
方法与文献依据见 `references/methodology.md §12`。该增强为可选精度层，
默认不被 run 链路调用，冻结基线/verdict/门禁一律不动。

**交付形式约定**：分析型 skill 一律按 L1–L5 分层交付——
L1 条件化结论、L2 Markdown 证据表、L3 ASCII/PNG 图、L4 单文件 HTML 交互页、
L5 带公共最小字段集的 CSV/JSON。每次交付须在 `DELIVERY_INDEX.md` 索引到
对应层级的具体文件，并区分「结论性产物」与「只读研究视图」。

L4 是由两个 run 的既有产物生成的只读研究视图，不是回测净值图，也不是生产信号源；
本项目不产出 `reports/backtest/nav_curve.html` 一类净值页。

主要产物：`ctb_table.csv`、`financing_scan.csv`、`yield_shift_scan.csv`、
`strategy_comparison.csv`、`switch_boundaries.json`、`executable_boundary.json`、
`backtest_metrics.json`、`robustness.json`、`validation.json`、
`contest_replay/{contest_scope.json,dry_run_plan.json,replay_ledger.csv}`、
`run_manifest.json`、`report.md`、`IO_INDEX.md`。
所有 L5 表带公共最小集：`as_of_date` / `data_version` / `source_mode` /
`update_time` / `symbol` / `asset_type`，主键唯一。
发布态只读快照：`reports/publish/<as_of>/<data_version>__<semantics_version>/`。

**两条独立的版本轴**：`data_version` 是输入数据版本（由 scenario_spec 哈希派生），
`semantics_version` 是计算口径版本（公式、闸门、选券规则、终值口径）。改了口径
就必须改后者，**即使输入一个字节没动**。二者任意一条不同，两个 run 的数值就
不可直接比较。当前口径 `v2-exante-execution`（严格事前执行口径）。
v1 口径的历史产物保留在原路径，仅作对照，不再作为当前结论引用。

## 运行方式

对话触发：「用 CTD/IRR/净基差 skill 复跑冻结快照并给出可执行边界」。

平台真实入口（本机曾实际执行；留痕在工作区旁的 `QSLogs/<run_id>/`，不写入本仓库）：

```powershell
# 官方启动器：先设置 DSH_HOME，再从托管 bootstrap 启动（勿写死盘符）
$env:DSH_HOME = '<workspace>/QSHome'   # 或本机已配置的用户级 DSH_HOME
Set-Location (Join-Path $env:DSH_HOME 'quantskills/application/bootstrap')
node .\launcher.mjs -- --port 3097 --no-open
# 预期：dsh web: http://127.0.0.1:3097/
```

统一 CLI（主体纯标准库，Python 3.10+；解释器以 `PATH` 中的 `python` 为准，不写死本机路径）：

```bash
cd <path-to>/agent-cfe-ctd-irr
python scripts/rebuild_all.py            # ★ 一键重建全链路（含客户视图页面）
python scripts/build_snapshot.py --check  # 只校验冻结输入快照可重现性，不写盘
# 官方数据（需 panda_data SDK，见「依赖」）：
python scripts/fetch_official.py
python scripts/build_snapshot.py --official-anchor data/official/official_anchor.json \
    --out data/input_snapshot_official.json
python -m runtime run --input data/input_snapshot_official.json --run-id <new-id>
python scripts/compare_runs.py           # run 自动发现，无需指定目录名
python -m runtime run --run-id <id>         # 冻结基线研究态全链路（默认子命令）
python -m runtime validate                  # 时点/字段/结构/稳定性门禁
python -m runtime backtest                  # 规范 §9.2 最低指标集
python -m runtime replay                    # 期货模拟赛等价回放范围
python -m runtime publish --from <run_dir> --allow-degraded
python scripts/probe_coverage.py             # 官方数据域覆盖探测（零网络、可复跑）
python -m runtime self-test                 # 27 条有效公式硬断言
python -m unittest discover -s tests -v    # 单元测试
python scripts/replay_contest.py            # 只读探测，不安装 CLI、不下单
```

## 产物可重建性（清缓存 / 修正历史数据后如何重出）

**硬约束：网页与数据必须是 skill 的派生产物，不是手工维护的快照。**
清掉 `reports/runtime_out/`、发布快照与 `delivery/visualization/index.html`
之后重跑，产物必须能自动长回来，且页面上的每个研究数值随之更新。

| 环节 | 定位方式 | 说明 |
|---|---|---|
| 输入快照 | `data/scenario_spec.json` + `data/official/official_anchor.json` | 由 `build_snapshot.py` 重建，冻结基线**字节级可重现** |
| 研究态 run | `runtime run` 生成，目录名带时间戳 | 角色由**输入快照是否含官方锚定字段**判定，不靠目录名 |
| 发布快照 | `runtime publish` | 目录名含 `data_version` 与 `semantics_version` 两条版本轴 |
| 对照结论 | `runtime/comparison.py` 现算 | 页面不依赖预先生成的对照文件，无隐含顺序依赖 |
| 客户视图 | `delivery/visualization/build_dashboard.py` | run 自动发现；模板不含任何写死的研究数值 |

三条实现纪律：

1. **run 定位靠输入事实，不靠目录名**（`runtime/run_registry.py`）。
   目录名是时间戳，重跑必然换名；写死目录名会让「清缓存后重跑」直接崩溃。
2. **页面上的每个研究数值只有一个来源**：run 产物 JSON/CSV → `payload.dynamic`
   → 模板占位符。模板里不残留任何写死的研究结论数值
   （`tests/test_rebuild_pipeline.py` 静态扫描 + CDP 无头实测双向校验）。
3. **冻结输入快照不得被无意改写**：`build_snapshot.py` 在目标内容不同时
   拒绝写入并退出码 3；确需更新时显式 `--force`（或 `rebuild_all.py
   --refresh-snapshot`），并因此产生新的 `data_version`。

修正历史数据后的标准动作：

```bash
# 1) 改 data/scenario_spec.json（或重新抓官方锚定）
# 2) 显式重建：会产生新的 data_version，版本轴随之推进
python scripts/rebuild_all.py --refresh-snapshot
# 3) 页面、CSV、对照报告、发布快照全部自动更新，无需手工编辑
```

`rebuild_all.py` **不删除**既有 run 与发布快照（它们是口径对照与验收链的一部分）；
需要清理时显式执行 `python -m runtime clean`。

## 依赖

- 主体计算 **纯标准库**（Python 3.10+，`requirements.txt` 无强制第三方包）。
- 官方数据拉取为可选链路，需 `panda_data` SDK + `pandas`；具体解释器由平台提供，
  不在仓库中写死路径（历史验证版本 panda_data 0.0.12 / pandas 2.3.3）；
  凭据 `~/.pandadata/pandadata.env` 只读入内存，绝不写入产物。
- 依赖 Skill：`~/.claude/skills/pandadata-api`（218 个方法索引，其中 213 个 `get_*`）。
  任何官方数据调用前先读其 `SKILL.md`；引用须写明方法名、关键参数、数据期、单位与来源 Skill，
  不得凭记忆猜方法或 symbol。`--reuse` 时完全离线，不触发该依赖。
- 平台侧只读探测：`QSHome/quantskills/contest/`、`QuantStudio/.../futures-products.json`。

## 验收与降级

- 门禁项数以各 run 的 `validation.json` 为准：文件完整、时点正确、原始输入与
  run 副本双哈希、live 七项门槛、source_mode 一致性、主键唯一、公共 Schema、
  BNOC 恒等式、非退化硬断言、从输入副本独立重算 CTD/IRR 与输出对拍、
  报告结构、禁用确定性用语、回放声明时间戳与限制披露、混合判定无效标记、
  微扰稳定性、CF 敏感性解释。
- **v2 语义门禁（2026-11-18 新增 8 项）**：语义版本存在且已知、
  **策略 P&L 独立重算**（不复用生成器自己的公式，容差 5e-6）、
  **事前持仓即期初 CTD**（逐行断言）、**终值口径恒等式**
  `A − B = valuation_basis_gap + delivery_fee − hedge_leg_pnl_B`、
  跳过情景零 P&L、事后反事实在跳过时为零、事后反事实确实取用换券标的。
  冻结基线 41 项 / 官方锚定 42 项，全部 `ok=true`。
- **反事实措辞纪律**：事后换券反事实**不是上界**。事后最优券按 IRR 选出，
  而发票价经转换因子缩放，IRR 更高的券其 CF 可能更低、绝对 P&L 反而更小，
  因此该差额可正可负（官方锚定 run 实测下界 −0.067883）。
- **混合锚定仍属 `degraded`**：`input_origin=hybrid_declared_plus_pandadata`
  不在 live 白名单；版本前缀 `hybrid-` 与 `degraded-`/live 均不共用；
  缺口项由 `official_anchor` 明细逐条标注，禁止借真实行情把结论标成 live。
- **公式层 27 条有效硬断言（`self-test`）+ 62 个单元测试**（2026-11-18），
  含「两边全 0 假通过」、可移植性、P&L 重复贴现、契约层与**产物可重建性**
  （run 定位靠输入事实、页面无写死研究数值、快照覆盖保护）防回归；
  门禁另有一层从 run 输入副本独立重算，不只依赖生成器自己的输出列。
- 当前证据强度与待补项见 `reports/AUDIT.md`，交付演进过程见 `reports/CHANGELOG.md`。
- **降级规则**：`source_mode` 只按 provenance 判定，`live` 需同时满足
  `input_origin ∈ {pandadata, user_verified}` + `source_ref` + `data_asof` +
  **七项 FORMAL_DATA_ITEMS 全为真**（缺任一项即降级，门禁逐项复核）；其余一律 `degraded`；
  `degraded` 版本前缀与 live 不共用；依赖失败标注 `gap_notes`，禁止静默填造；
  全部失败只输出框架、不编造数值。
- 发布态必须来自已通过门禁的同一套逻辑，写入 `data_version` 与 `update_time`。

## 禁止行为

- 确定性下单指令、「必涨 / 保证收益 / 仓位提到 xx%」等用语；
- 使用未来函数数据、凭记忆猜代码或数值、用会话内重算冒充发布态结果；
- 把净基差或账面偏差直接称为无风险套利；
- 用参考折现 CF 冒充交易所公布 CF；把等价回放账本称为成交回执；
- 在 `degraded` 下输出市场结论或真实交易建议。

## 官方数据锚定复跑（可靠负结果）

`run_id=v2-anchor-20261119`，`data_version=hybrid-ctd-irr-v1-64e0d9b0820681cf2a`，
`semantics_version=v2-exante-execution`：
把冻结基线的 `T2612.CFE` 结算价 107.32 换成真实值 110.05、交割日 12-11 换成
真实窗口 2026-12-14~16，现券/CF/成本/容量保持不变。结果 `IRR−r` 从 34.74bp
变成 3019.41bp，正向可执行由 `False` 变 `True`，C 的风控跳过情景从 273/399
变成 0/399。

> 真实期货价与声明式现券不在同一市场状态，2.73 元价差被年化成虚假基差，
> 风控闸门因此全部打开。这不是套利机会。即使期货行情真实，只要官方可交割券
> 清单、现券全价/应计、公布 CF 与融资/借券成本未齐备，就不得给出可执行判定。
> 对照明细见 `reports/official_anchor_comparison.md`。

## 使用与失败边界

- 适用：给定可交割券与成本参数的**条件化公式复现、情景扫描与执行边界判断**。
- 不适用：真实市场套利机会判断、实盘盈亏预测、跨期换月/交割月滚动、期权调整定价、
  借券失败与结算违约建模、时间序列样本外收益评估。
- 失败即停：输入契约不通过 → 退出码 2；缺任一必需字段 → 标注缺口并降级；
  比赛 CLI 未安装/未授权 → 只做等价回放，不发委托；
  公式恒等式被破坏 → 立即抛 `AssertionError`，不出报告。
- 情景是**参数网格，不是历史时间序列**；`backtest_metrics.json` 的方向准确率与
  最大回撤均为情景序列口径，不能解读为策略收益预期或实盘胜率。

## 目录

```
AGENTS.md                     Agent 声明与宿主行为入口（QuantSkills 兼容必填）
SKILL.md                      方法正文：输入输出、运行、验收与边界
README.md / README.en.md      仓库主页（中英文）
LICENSE                       MIT 许可
DELIVERY_INDEX.md             交付总索引（内容清单、校验状态、对话记录与平台留痕）
runtime/                     bonds / pricing / scenarios / robustness / contest_replay
                             io_contract / validation / selftest / writers / cli
                             semantics（计算语义版本轴）/ run_registry（run 自动发现）
                             comparison（对照结论现算）
scripts/                     rebuild_all（一键重建全链路）/ build_snapshot / fetch_official
                             probe_coverage / compare_runs /
                             validate / backtest / replay_contest / selftest
                             cffex_cf.py（公布 CF 的公式复算与适用范围检验）
references/                  data_guide / methodology / executable_boundary / acceptance
data/                        scenario_spec.json（情景规格） + input_snapshot.json（冻结快照）
                             + input_snapshot_official.json（混合锚定）
data/official/               官方数据落盘 CSV + official_anchor.json
                             + sdk_methods.json / coverage_probe.json（覆盖探测）
                             + cffex_*（交易所公开材料与 cffex_sources.json）
tests/                       test_bonds.py 硬断言
agents/                      skill-manifest.yaml / portable-loader.md
                             cursor-rule.mdc / openai.yaml（宿主适配）
reports/runtime_out/<run_id>/ 六个研究态 run：v1 冻结基线 + v1 官方锚定
                             + v2 事前口径基线（v2-base-20261119）
                             + v2 事前口径官方锚定（v2-anchor-20261119）
                             + 契约冒烟 / 探测子目录
reports/                     official_anchor_comparison.{json,md}
                             samples/（L1–L3 结构样例）
                             AUDIT.md（当前证据状态）/ CHANGELOG.md（演进记录）
reports/publish/<as_of>/<data_version>__<semantics_version>/ 发布态只读快照
delivery/conversation/       完整对话记录（结构化 + 可读版 + 元信息 + 生成脚本）
delivery/platform_capture/   QuantStudio 平台留痕（三个批次，逐字节副本）
delivery/visualization/      动态可视化页面（index.html 单文件 + 模板 + 离线字体 + 数据注入脚本）
```

> 本 skill 用于研究复现与验收，不构成投资建议。
