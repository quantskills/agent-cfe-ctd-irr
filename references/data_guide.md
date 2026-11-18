# 数据指南

规范依据：`Agent开发规则.md` §10。本文说明正式数据源、字段口径、时点对齐、
缺失降级与「哪些结果只能进研究态」。

## 1. 数据源与接口

| 层 | 正式来源 | 当前状态 |
|----|----------|----------|
| 可交割券清单 | 中金所合约公告 + 当前全量数据 | 证据已取得、尚未接入计算。当前接口 `all_6620.csv` 显示 T2612 共 17 只券（新增 260012/260019/260010/260016，最低 CF 0.8883）；中金所发〔2026〕8号附件2 是上市时点的 13 只券快照。见 `cffex_sources.json` 与 `cffex_all_deliverable_current.csv`（官方库本身无此域，见 `coverage_probe.json`） |
| 现券净价/全价/应计 | 成交行情或中债估值 | **仍缺失**（官方库无债券现券域；声明式收益率曲线折现生成）。免费快照源已实测可用但历史序列本次取数失败，见 §9 |
| 票息与付息日 | 发行条款 | 情景给定；**官方清单已给出票息与到期日**，付息日可按 `f=2` 由到期日推算，尚未接入 |
| 转换因子 CF | 交易所公布值 | 17 只券的公布值已取得。公式复算在上市清单上 13/13 一致、样本外 43/46 且有 3 个反例，故 CF 只采用公布值，不用复算值。当前快照仍用 `reference_cf()` 声明式参考值，尚未接入 |
| 期货价格 | `get_future_daily` 结算价 | **已接入**：`T2612.CFE` @20261113 `settlement=110.05`（`close=110.045`） |
| 融资利率 / 借券成本 | 回购与债券借贷市场 | **仍缺失**：`get_macro_ir` 仅返回 `symbol` / `period_date` / `data_value`；接口文档写「完整指标码表见文件下载」，该码表文件本机未取得且本期未下载。GC001 可作有出处的代理，债券借贷费率无免费公开源，见 §9 |
| 交割日历 / 合约规格 | `get_future_detail` + 中金所交割细则 | **已接入**：`T2612.CFE` 交割窗口 2026-12-14~16、乘数 10000、保证金率 0.02、`is_trading=1`；**交割结算价/发票价规则原文已落盘**（`cffex_delivery_rules.pdf`） |

> 表中「已取得」仅指证据落盘，不等于已接入计算。两个 run 的 `source_mode`
> 均为 `degraded`；交易所公开材料属研究态证据层，不参与已发布批次的判定。

**实测调用**（`scripts/fetch_official.py`，每个接口单次调用，失败不重试）：
`get_trade_cal`（23 行）、`get_future_detail(exchange=['CFE'])`（704 行，其中 Government 159 行）、
`get_future_daily(symbol=['T2612.CFE'])`（59 行）、`get_macro_ir`（829461 行，
只落盘最新数据期截面 3275 行）。SDK 共 213 个 `get_*` 方法。

**覆盖结论的依据**：`scripts/probe_coverage.py`（零网络、可复跑）按 `_` 分词做
词元精确匹配，213 个 `get_*` 方法无一命中债券/交割/CF/回购词元；8 个形近方法
逐条查 `api-docs.md` 排除：`get_repurchase` 是上市公司股份回购、`get_macro_cal*`
是经济日历事件、`get_future_basis` 是商品期现基差、`get_future_contract_pool` 是
多空合约池、`get_future_detail` 只给合约静态条款、`get_macro_ir` 数值无码表。
结论落盘 `data/official/coverage_probe.json`，方法名清单见 `sdk_methods.json`。
词元匹配是必要的：`get_fina_reports` / `get_stock_shareholder_report` 中的 `repo`
来自 rePORTS，子串匹配会把它误判为回购相关。

**交易日历口径**：`api-docs.md` 明确 `get_trade_cal` 的 `exchange` 只支持
`SH` / `HK` / `US`，不支持 `CFE`。因此本项目不以交易日历作为 CFE 日历证据：
锚定日是否交易日以 `get_future_daily` 该日是否有行情为准，交割日以中金所合约表
与交割细则为准。`coverage_probe.json` 记录 `cfe_supported=false` 与
`evidence_status=EVIDENCE_WITHDRAWN`。

另核查其余期货域接口：`get_future_basis` 给的是「现货价 − 期货价」的**商品基差**，
`get_future_term_structure` / `get_future_warehouse_receipt` 同样不含发票价、CF 或
可交割券清单，均**不能替代**国债期货的净基差 BNOC。故不再追加无效调用。

**凭据**：`~/.pandadata/pandadata.env`（`DEFAULT_USERNAME` / `DEFAULT_PASSWORD` /
`JAVA_SERVICE_BASE_URL`），只读入进程内存，不写入任何产物；
`.env.example` 仅保留占位符。

**依赖 Skill 调用契约**：`pandadata-api` 是本链路唯一外部 Skill 依赖。调用前先读
`~/.claude/skills/pandadata-api/SKILL.md`，再查 `references/method-index.md` 与 `api-docs.md`；
引用须写明方法名、关键参数、数据期、单位与来源 Skill，不凭记忆猜参数、字段、symbol 或指标码含义。
任一环节失败即标注缺口并降级，不得静默填造；`--reuse` 路径不触发该依赖。

## 2. 输入快照字段契约（`data/input_snapshot.json`）

| 字段 | 类型 | 单位 / 口径 |
|------|------|-------------|
| `meta.as_of_date` | date | 结论锚定日，必须 <= `settle_date` |
| `meta.settle_date` | date | 现券结算日，须落在 `[last_coupon, next_coupon)` |
| `meta.delivery_date` | date | 交割日，须 > settle |
| `meta.futures_price` | float | 每百元面值净价报价（T 合约 tick 0.005） |
| `meta.data_version` | str | 冻结 `degraded-ctd-irr-v1-<spec hash>`；混合 `hybrid-ctd-irr-v1-<spec+anchor hash>`；规格或锚定改动必变 |
| `meta.source_mode` | str | 由 provenance 判定：`live` / `degraded`（混合锚定仍为 `degraded`）。`live` 需同时满足 `input_origin ∈ {pandadata,user_verified}` + `source_ref` + `data_asof` + `FORMAL_DATA_ITEMS` 七项全为真，缺一即降级；门禁逐项复核 |
| `meta.input_origin` | str | `declared_scenario` / `hybrid_declared_plus_pandadata` / `pandadata` / `user_verified` |
| `meta.official_anchor` | dict | 混合快照专有：官方字段清单、声明字段清单、合约规格、覆盖评估、数据缺口、调用方法名 |
| `meta.futures_market_quote` / `.official_delivery_calendar` | bool | 已由官方库锚定时为 true，契约据此筛掉对应 `gap_notes` |
| `meta.source_ref` / `meta.data_asof` | str | 正式输入必须提供，否则强制降级 |
| `deliverable_basket[].coupon` | float | 年票息率小数，如 0.0235 |
| `.freq` | int | 年付息次数（本题 1） |
| `.last_coupon` / `.next_coupon` / `.maturity` | date | 付息与到期日 |
| `.clean_price` | float | 每百元面值净价 |
| `.accrued_interest` | float | 结算日应计，每百元面值 |
| `.dirty_price` | float | = clean + accrued（契约校验，误差 <= 1e-5） |
| `.conversion_factor` | float | 交易所公布 CF；0 < CF < 1.3 |
| `.modified_duration` | float | 修正久期，一阶情景重定价用 |
| `.accrual_basis` | str | `ACT/ACT`（ICMA）或 `ACT/365` |
| `.available_notional_wan` / `.lendable_notional_wan` | float | 可交割 / 可借券存量，万元面值 |
| `costs.repo_rate` | float | 年化单利 ACT/365 融资利率 |
| `costs.bond_lend_fee` | float | 年化借券成本 |
| `costs.transaction_cost_bp` / `.slippage_bp` / `.ctd_switch_buffer_bp` | float | bp |
| `costs.delivery_fee_per_100` | float | 每百元面值交割费 |
| `capacity.max_position_lots_per_account` | int | 情景限仓手数（非交易所规则） |

**主键**：`as_of_date` + `bond_id` + `data_version`（逐券表）；
`grid_index`（情景表）。发布前校验唯一。

## 3. 时点对齐与滞后

- `as_of_date` 是结论锚定日；不得使用当日尚未发布/不可得数据（规范 §8.1）。
- 交割结算价由交易所按交割月规则产生；本 skill 用「当前期货价代理」
  是**明确的情景假设**，不等价于真实交割结算价，不得据此宣称已锁定利润。
- 付息日与应计按债券条款日历，不按交易日；`accrual_period()` 会把付息期滚动到覆盖目标日。

## 4. 代理标的与定码

- 合约标的情景代号 `T2612`（`exchange=CFE`, `product=t`）。平台品种目录
  `float/.../futures-products.json` 实测含 `t/tf/ts/tl` 且 `enabled=true`；
  但 `docs/futures-instruments.md` 明确「目录表示品种覆盖，不保证该交割月份有行情或开放交易」。
- 可交割券代号统一 `SCEN-A..D`，**刻意不使用真实债券代码**，避免把情景参数误读为市场数据。

## 5. 缺失、失败与降级

| 情况 | 处理 |
|------|------|
| 缺 `source_ref` / `data_asof` | 强制 `source_mode=degraded`，`market_conclusion=false` |
| 缺任一必需字段 | `io_contract.validate_snapshot` 返回 errors，`run` 退出码 2 |
| 券价 / CF / 交割日历缺失 | 缩小范围为方法复现 + 条件情景，写 `gap_notes` 与 `scope_reduction` |
| 比赛 CLI 未安装/未授权 | 只做等价回放，不发委托；`live_order=false` |
| 公式恒等式破坏 | 立即 `AssertionError`，不出报告 |
| 参数微扰使结论翻转 | 单独标注并给出传导解释（见 `robustness.json`），不静默通过 |

冻结基线 `gap_notes` 共 8 条（1 条降级说明 + 7 项正式数据缺失）；
官方锚定混合快照共 6 条（1 条混合来源说明 + 5 项仍缺失数据），
期货行情与交割日历两项已不再列为缺口。

混合快照把真实结算价 110.05 与声明式现券配在一起，会产生 3019bp 的虚假
`IRR−r` 并打开全部风控闸门。该 run 只用于量化数据缺口的影响，不得作为市场结论
（见 `reports/official_anchor_comparison.md`）。

## 6. 研究态 / 发布态

| 形态 | 路径 | 规则 |
|------|------|------|
| 研究态 | `reports/runtime_out/<run_id>/` | 允许重算、mock、降级、调参；必须标注版本与来源 |
| 发布态 | `reports/publish/<as_of>/<data_version>/` | 只读已验收定稿快照，写 `publish_meta.json`；禁止会话内重算冒充 |

`degraded` 版本前缀（`degraded-` / `hybrid-`）与 live 不共用。发布降级快照需显式
`--allow-degraded`，并仅供研究消费。

## 7. 官方数据接入与复跑（已实测）

```bash
# 1) 拉取（需 panda_data SDK 与 ~/.pandadata/pandadata.env）
python scripts/fetch_official.py   # 解释器由 PATH 提供，不写死本机路径
#    零网络复跑：加 --reuse，直接读 data/official/official_anchor.json
# 2) 生成混合快照（只替换 futures_price / delivery_date / contract 规格）
python scripts/build_snapshot.py --official-anchor data/official/official_anchor.json \
    --out data/input_snapshot_official.json
# 3) 复跑与对照
python -m runtime run --input data/input_snapshot_official.json --run-id <new-id>
python scripts/compare_runs.py --base reports/runtime_out/20261114-223330 \
    --official reports/runtime_out/<new-id>
```

产物：`data/official/{official_anchor.json,trade_cal.csv,future_detail_government.csv,
future_daily.csv,macro_ir_latest.csv}`、`reports/runtime_out/official_probe/fetch_log.json`
（方法、参数、行数、耗时，不含凭据）。

## 8. 如何升级到 `live`

PandaData 官方库与比赛 CLI 都不提供现券价、公布 CF、回购与借券利率；
现券价与借券费率仍是未闭环项（见 `reports/AUDIT.md §4`）。清单、票息、到期日、
公布 CF 与交割规则已由中金所公开材料取得（§9），但仍未接入计算。

1. 用当前全量清单的 **17 只券** 生成 `deliverable_basket`（上市清单的 13 只券
   不是完整篮子），`freq` 设为 **2**（半年付息），`last_coupon` / `next_coupon`
   按发行条款推算，每券真实 `clean_price` / `accrued_interest` / `modified_duration`
   与市场回购、借券成本一并补齐；`conversion_factor` **原样采用公布值**，
   `cffex_cf.py` 的公式复算有样本外反例，不作数据源。
2. 交割结算价/发票价规则见 §9。细则规定应计利息算到**第二交割日**：
   T2612 为 **2026-12-15**，而冻结/混合快照的 `delivery_date=2026-12-14`
   是交割窗口首日。live 快照须把发票价用的第二交割日单设为 12-15；
   冻结快照不改，以维持其 SHA256 与 33 项门禁结果。
3. 写新的 `input_snapshot.json`，设 `meta.input_origin = "pandadata"` 或
   `"user_verified"`，补齐 `source_ref` 与 `data_asof`，并让所有 `FORMAL_DATA_ITEMS` 为真。
4. `python -m runtime run --input <path>`；门禁才会把 `source_mode` 判为 `live`、
   `market_conclusion=true`。此时计算逻辑、情景、对照与边界完全不变。
5. 在 live 之前，任何 `forward_executable=true` 都必须视为数据不一致的产物，不得采信。
   官方锚定 run 已在 `executable_boundary.json` 写 `verdict_status=INVALID_MIXED_STATE`、
   `conclusion_valid=false`、`forward_executable_usable=false`，门禁强制校验这三个字段。
6. **另需补的官方码表**：取得 `get_macro_ir` 的指标码表文件后，锁定回购利率指标代码，
   以同一 `as_of` 取数替换 `costs.repo_rate` 与 `costs.bond_lend_fee`；否则第 1 步仍不完整。
7. **规则原文**：`http://www.cffex.com/10tf/` 合约表写的是「发行期限不高于 10 年、
   合约到期月份首日剩余期限不低于 6.5 年的记账式附息国债」，没有 10.25 年上限。
   live 快照的 `eligible_rule_note` 应改用该原文并把 URL 写入 `source_ref`。
   冻结定稿中的 `eligible_rule_note` 保留原始情景表述（「6.5-10.25 年」），
   仅作情景说明、不作规则依据，不改写以维持字节哈希。

---

## 9. 中金所公开材料（证据层，未接入计算）

清单见 `data/official/cffex_sources.json`（含每个来源的 URL、文号、本地文件、
字节数与 SHA256）。全部为交易所公开材料，每个 URL 单次请求、无轮询/重试/凭据。

| 材料 | 本地文件 | 用途 |
|---|---|---|
| 中金所发〔2026〕8号 附件2 | `cffex_T2612_raw.csv`（GBK 原件）/ `cffex_T2612_deliverable_basket.csv`（UTF-8） | 13 只**上市时**可交割券、三市场代码、票息、到期日、公布 CF。**不完整，不可当 live 篮子** |
| 可交割国债信息全量接口 | `cffex_all_deliverable_current.csv`（UTF-8，133 行） | **T2612 当前 17 只券**及 T2703/T2706 的公布 CF；无数据期字段，抓取于 2026-11-17 |
| T 合约 CF 公式原图 | `cffex_cf_formula.jpg`（+ `_x4.png` 放大件） | 官方 CF 公式：指数 `x*f/12`、括号内 `n-1`、末项 `1-x*f/12`，4 位小数 |
| 应计利息公式原图 | `cffex_accrued_formula.png`（+ `_x4.png`） | ACT/ACT，算到**第二交割日**，7 位小数 |
| 10 年期合约表 | `http://www.cffex.com/10tf/`（网页） | 可交割范围原文、合约规格、公式说明 |
| 交割细则 PDF | `cffex_delivery_rules.pdf` | 第二十二/二十三/二十四条：交割结算价、交割货款、5 元/手交割费 |

**公式复算的适用范围**：`python scripts/cffex_cf.py --verify`（零网络）
在上市清单 13 只券上 13/13 一致；`--verify-all` 对当前全量清单跨 T2612/T2703/T2706
为 43/46，260012 / 260019 在 T2612、260019 在 T2703 出现 0.0005~0.0006 偏差，
穷举整数 (x, n) 均无法命中公布值。x/n 计数约定是在上市清单子集上反推的，
含拟合成分，对部分 2026 年新发券不适用。因此 CF 只采用交易所公布值，
公式复算仅作教学与有限子集对拍；`runtime/pricing.py::reference_cf()`
是声明式参考值，不是公布 CF。

**证据强度限制**：交割细则 PDF 已落盘并校验 SHA256，但本机无 `pdftotext`/`pypdf`，
CID 文本流未做本地抽取；条款文字来自交易所官方 PDF 的网页索引摘录，
升级 live 前需人工按原 PDF 页码复核，**不作为自动采集证据**。

**仍未补齐**（因此不得改判 live）：**带明确数据期**的官方清单（全量文件抓取于 11-17，
用于 `as_of=2026-11-13` 属回溯）、17 只券同一 `as_of` 的真实净价/全价、
市场回购利率的官方锚定、债券借贷费率。东财 `push2` 快照已实测可用
（`1.019845`、`1.204001`），但 `push2his` 历史日 K 本次 `curl 56` 连控制组
`1.510050` 也失败，且上交所国债现券常有价无成交，须逐券校验成交；
拿不到就换源或明确剔除该券（缩小范围），不得用声明式价格冒充行情。
