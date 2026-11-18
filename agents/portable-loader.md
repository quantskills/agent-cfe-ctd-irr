# 跨宿主加载说明

本 skill 是标准目录型 Agent Skill，正文唯一入口为 `SKILL.md`。

## 通用加载步骤

1. 读取本目录 `SKILL.md`（frontmatter 仅 `name` / `description`）。
2. 正式计算前读取 `references/data_guide.md` 确认数据口径与来源。
3. 需要公式细节时读 `references/methodology.md`；需要边界时读
   `references/executable_boundary.md`；需要验收证据时读 `references/acceptance.md`。
4. 运行时统一走 `python -m runtime <subcommand>`，不要绕过 CLI 直接调用单个函数
   充当「正式结果」。
5. 发布态只读 `reports/publish/<as_of>/<data_version>/`，禁止在会话内重算冒充发布。

## 平台路径解析（跨环境）

- 平台入口与品种目录**不硬编码盘符**：`runtime/contest_replay.py::resolve_platform_paths()`
  先读 `DSH_HOME` / `QUANTSTUDIO_HOME` / `QUANTSTUDIO_PRODUCTS_JSON`，
  否则从 skill 根目录向上有限层级发现同级的 `QSHome/` 与 `QuantStudio/`。
  探测不到时如实返回 `not_found`，不借用其他机器的路径。
- 留痕目录由平台运行环境决定，形如 `<workspace>/QSLogs/<run_id>/`；不写入仓库。
- 所有脚本以 `Path(__file__).resolve().parent.parent` 定位项目根，可在任意 CWD 运行；
  `--input` 可显式指定快照。L4 页脚的 `projectRoot` 同样由脚本位置推导。
- 依赖 Skill 目录用 `PANDADATA_API_SKILL` 覆盖，默认 `~/.claude/skills/pandadata-api`。
- 历史留痕文件（对话归档、平台采集、既有 run 的 manifest / scope）保留生成时的
  绝对路径作为证据，不代表当前环境配置。

## 前置条件

- 主体计算 Python 3.10+ 纯标准库；无需虚拟环境、无需网络。
- 官方数据链路（可选）：凭据 `~/.pandadata/pandadata.env`（`DEFAULT_USERNAME` /
  `DEFAULT_PASSWORD` / `JAVA_SERVICE_BASE_URL`）、SDK `panda_data`（本机 `skills-env`）、
  接口契约 `~/.claude/skills/pandadata-api`；`scripts/fetch_official.py --reuse` 可完全离线。
- 升级到 `live` 还需补齐交易所公布可交割券清单、现券全价/应计、公布 CF、
  市场回购与借券成本，以及可核验的 `source_ref` / `data_asof`；否则一律 `degraded`。
  官方库只有期货行情与合约交割条款，混合锚定 run 不得当作 live 结论。
- 期货模拟赛核验需要官方 CLI 已安装并授权；未满足时 `replay` 只输出等价回放范围，
  不产生委托。
