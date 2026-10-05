<!-- dedicated-cft-start -->
## 独立 Chrome for Testing：行情采集与 OpenCLI

* 本项目所有 OpenCLI 浏览器操作及 Python → OpenCLI 调用使用独立 Chrome for Testing、专用 `user-data-dir`；操作前核验实例归属并显式传入已验证的 `--profile`，不依赖默认路由或固定临时 profile ID。
* 专用数据目录为 `%LOCALAPPDATA%\ChromeForTesting\market-collector`；先只读核验实际目录与运行实例，不与日常 Chrome 共用目录。
* 当前启动器和读取脚本分别为 `%LOCALAPPDATA%\Programs\ChromeForTesting\154.0.8037.92\launch_vix_collector.bat`、同目录的 `read_vix_quote.py`。版本号仅为当前定位参考，升级后重新发现并核验实际路径。
* 专用实例或调用失败时定位原因并明确报告；未经用户明确授权，不回退日常 Chrome，不静默降级或伪造成功。
* 复用现有专用会话及标签页，减少开窗、抢焦点、重启和重复登录；保留浏览器控制通知、安全提示和 sandbox。
* **用完即回收（用户 2026-10-02 明令）**：行情采集结束、不再需要该实例时，**必须运行 `stop_vix_collector.ps1` 回收专用实例**，不要让窗口和进程长期空转浪费资源。实测依据：连续运行 18 小时后单实例累积到 **90~95 个进程 / 8.4 GB 工作集**（而实际只有 1 个标签页），回收后回到 0，重启则只有 30 进程 / 2.96 GB。**长期挂着不回收 = 无谓浪费**，且是「资源被空壳 renderer 吃掉」的根源。回收脚本自带归属护栏（只碰 `ChromeForTesting` 可执行路径 + 规范化后精确匹配 `market-collector` 目录），实测回收前后**日常 Chrome 进程数不变**。
* 回收后若又要采集，重新跑启动器即可（会话/标签页由浏览器自身恢复，OpenCLI 侧会自动重连）。
* Python 包装器使用 `subprocess` 参数列表、`timeout`、退出码检查、JSON 解析，以及必要字段、类型和非空值校验。
* 行情核验标的、合约月份、来源、行情时间、时区和新鲜度，区分页面可读、当前报价和连续实时更新；只在必要登录、CAPTCHA、2FA 时请求用户完成一个动作，不保存秘密信息。

### 本机已验证事实（2026-10-02 升级 OpenCLI 1.8.8 后核验，使用前仍须重新核验连接归属）

| 项 | 已验证值 |
|---|---|
| 专用配置目录 | `%LOCALAPPDATA%\ChromeForTesting\market-collector`（实际 `C:\Users\super\AppData\Local\ChromeForTesting\market-collector`） |
| 浏览器可执行 | `%LOCALAPPDATA%\Programs\ChromeForTesting\154.0.8037.92\chrome-win64\chrome.exe`（Chrome for Testing 154.0.8037.92，独立于 `C:\Program Files\Google\Chrome`） |
| 启动器 | `%LOCALAPPDATA%\Programs\ChromeForTesting\154.0.8037.92\launch_vix_collector.bat`，已含 `--disable-field-trial-config --no-first-run --no-default-browser-check --user-data-dir=…\market-collector` |
| 回收器（⚠️ 用完必跑） | `%LOCALAPPDATA%\Programs\ChromeForTesting\154.0.8037.92\stop_vix_collector.ps1`（纯 ASCII，无 BOM 依赖）。`powershell -NoProfile -ExecutionPolicy Bypass -File stop_vix_collector.ps1`；`-Force` 跳过优雅关闭 |
| 读取脚本 | `%LOCALAPPDATA%\Programs\ChromeForTesting\154.0.8037.92\read_vix_quote.py`（2026-10-02 改造，见下「新鲜度语义」；旧版备份 `read_vix_quote.py.bak-20261002`） |
| 通用读取器 | 同目录 `read_investing_quote.py`（`--url` + `--marker`，任意 Investing.com 报价页；新鲜度语义与 VIX 版一致） |
| 契约测试 | [tests/test_vix_quote_freshness_contract.py](tests/test_vix_quote_freshness_contract.py)（6 项，锁死时间换算与陈旧判定；脚本缺失时整体 skip，不会把套件弄红） |
| OpenCLI profile（本次实测绑定） | `73wmd6ds` —— **本次会话实测它驱动的是专用 CFT**（见下方「归属确证」）；但这是**实测绑定，不是「该 ID 就等于 CFT」的结论**，更不得当常量写死 |
| OpenCLI 版本 | `@jackwener/opencli` **1.8.8**（全局 npm，2026-10-02 由 1.8.7 升级，`changed 17 packages`，Node v24.21.0 / npm 11.12.1） |
| OpenCLI 入口 | 全局 npm `%APPDATA%\npm\opencli.cmd`；实际模块 `<npm>\node_modules\@jackwener\opencli\dist\src\main.js`（脚本用 `node <main.js> --profile <id> browser cft_vix_collector …` 调用，`--profile` 为全局选项须前置） |
| 浏览器 bridge 扩展 | `%LOCALAPPDATA%\Programs\ChromeForTesting\opencli-bridge-1.0.24`（名 `OpenCLI`，v1.0.24，含 `key`） |

* **`browser` 必须带 session 名**：`opencli browser <session> <command>` 的 `<session>` 是**必填位置参数**（漏了会 `✖ <session> is a required positional argument`）。本项目沿用 `cft_vix_collector`。
* **`--profile` 不是可选项（本机已确证有多个 profile 同时在线）**：升级后 `opencli profile list` 同时列出 `73wmd6ds` 与 **`7pyv6fcs`** 两个 connected profile。省略 `--profile` 会落到 daemon 默认 profile —— **有连到日常 Chrome 的风险**，与「默认连接独立 CFT」的约定直接冲突。`7pyv6fcs` 归属**未确证**（其 `tab list` 为空；用 example.com 探针无法从 `chrome.exe` 命令行区分，因为 CDP 建标签页不改进程命令行），处置方式只能是**永远显式传 `--profile 73wmd6ds`**，不要试图猜默认值。
* **归属确证（比读归档文件更强）**：`73wmd6ds` 的 `browser open` 会驱动**已存在的 CFT 浏览器进程**（PID 95784，`Win32_Process` 命令行同时含 `…\ChromeForTesting\154.0.8037.92\chrome-win64\chrome.exe`（BROWSER-ROOT）与 `--user-data-dir="C:\Users\super\AppData\Local\ChromeForTesting\market-collector"`）。只读核验命令：
  `Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like '*ChromeForTesting*' -and $_.CommandLine -like '*user-data-dir*' } | Select ProcessId,CommandLine`
* **`opencli doctor` 的作用有限**：它只报 `Daemon: running on port 19825` / `Extension: connected` / profile 列表，**不报 profile 绑到哪个浏览器**。因此「有 profile 在线」≠「这个 profile 就是 CFT」；每次重启后都要按上一行的方法重新确证。
* **profile ID 的稳定性：daemon 重启实测是持久的，但不等于可写死**。2026-10-02 实测 `opencli daemon restart`（提示 `Restarting daemon will disconnect 2 browser profile(s)`）后，`73wmd6ds` 与 `7pyv6fcs` **两个 ID 都原样重新出现并自动重连** → ID 是按浏览器 profile 持久化的，**不是**「每次 daemon 启动重新随机回收」。（跨**浏览器**重启的持久性未测；不要据此外推。）但 ID 本身**不含任何「这是 CFT」的信息**，所以持久 ≠ 可信任。
* **硬编码 profile ID 违反设计意图**：本小节的立项文档 `.planning/quick/261001-n1m-persist-dedicated-chrome-for-testing-rul/261001-n1m-PLAN.md` 明确写的是「操作前从当前工具能力和已连接实例明确核验 `--profile` 及其浏览器归属，**不硬编码临时 profile ID**，不依赖默认路由」。因此 `73wmd6ds` 只能作为**「上次实测」的记录 + 读取脚本的默认值**，每次会话首次连接仍须按「归属确证」重新核验，不得因为 ID 稳定就省掉核验。
* daemon 与 CLI 都跑在全局 npm 路径下：`%APPDATA%\npm\node_modules\@jackwener\opencli\dist\src\daemon.js`（监听 `127.0.0.1:19825`）。**daemon 自带的 HTTP 端点在未带其自有鉴权时一律 403**，别直接 `Invoke-WebRequest` 探它。
* **1.8.8 升级结论：无破坏性变更**。`main.js` 仍是 `dist/src/main.js`（`package.json` 的 `bin` 未变），大小与 1.8.7 一致（8275 字节），三个 shim（`opencli` / `.cmd` / `.ps1`）已重装，`browser` 子命令与 `doctor` 均正常；`read_vix_quote.py` **无需改路径**，实测 exit 0 并取到实时报价。升级后无需迁移配置。
* **读取脚本的新鲜度语义（2026-10-02 改造后）**：输出含 `pageTimezone="America/New_York"`、`staleSeconds`、`attempts`、`maxStaleSeconds`。`staleSeconds = 采集时刻(ET) − 页面报价时间(ET)`，跨午夜会正确回绕、未来时间钳制为非负、按 IANA 规则自动处理夏令时。**默认超过 `--max-stale-sec`（60s）即重载重读，重试用尽仍陈旧则 exit 1 —— 绝不把陈旧行情当最新输出**；确需陈旧数据时显式加 `--allow-stale`，它仍会打印 stderr 告警并如实标注 `staleSeconds`。旧版本对陈旧度**毫无判定**，只标 `unverified`。
* **实测上游会长时间停更，且页面仍标 `Real-time Data`**：2026-10-02 实测 VIX 期货页面报价冻结在 `22:20:45`(ET) 达 **35 分钟以上**（`staleSeconds` 1357→1396→2147 持续增长），其间强制重载页面、多次复采均不变。**CME VIX 期货当时处于交易时段**，故这是 Investing.com 侧的数据滞后，不是页面未加载，也不是本地读取错误。**同期 BTC 页面却每约 10s 正常跳价** → 说明是**按标的的数据源问题，不是站点整体故障**。结论：**页面的 `Real-time Data` 字样不能作为新鲜度证据**，只能信 `staleSeconds`。
* **两条路径都已用实测数据验证通过（2026-10-02 用 BTC 补齐）**：`--max-stale-sec 60` 对 **BTC（7×24，页面每约 10s 更新一次）** 实测 `staleSeconds≈9.8`、`attempts=1`、**exit 0** —— 即「新鲜成功路径」真的能走通，且 `staleSeconds` 报的是**数据龄**（≈更新周期）而非加载时刻；同一闸门对 VIX 仍 `staleSeconds=2147`、**exit 1** 拒绝。**结论：该闸门有区分力，不是永远放行。** 阈值取 60s 是合适的（>10s 更新周期，<上游最长停更量级）。
* **通用读取器**：`read_investing_quote.py`（同目录）把标的参数化为 `--url` + `--marker`，同一套新鲜度语义、同一 session，可用于 VIX / BTC / 任意 Investing.com 报价页；`read_vix_quote.py` 保持不变（VIX 专用）。
* **Investing.com 部分页面有 Cloudflare 人机验证，会自行通过**：2026-10-02 首次打开 `/crypto/bitcoin` 落到「正在验证您是否是真人 / Just a moment」页（`document.title='请稍候…'`、无 `instrument-price-last`）。**等待约 30 秒后自行放行**，无需人工点击、无需 CAPTCHA 交互。**约定：遇到验证页先等待重试，不要尝试绕过**（不换 UA、不用其它通道规避、不自造数据）；确有硬验证再按约定请用户完成一个动作。读取脚本已把验证页作为显式错误报出（`page is behind an anti-bot challenge`），不会把它误当成报价失败或陈旧。
* **`--disable-field-trial-config` 已实测存在于运行实例**（不只写在 .bat 里）；CFT 程序目录 ACL 已含 `APPLICATION PACKAGE AUTHORITY\ALL APPLICATION PACKAGES` 与「所有受限制的应用程序包」的 `ReadAndExecute, Synchronize`（当年正是缺这条导致沙箱读取失败）。
* CFT 的 `chrome.exe` 版本信息为 `ProductName = Google Chrome for Testing` / `FileVersion = 154.0.8037.92`；**`Get-AuthenticodeSignature` 显示 `NotSigned` 属 Chrome for Testing 正常现象**（CfT 官方构建不做 Authenticode 签名），不要当成被篡改。
* **`opencli doctor` 重启后会 FAIL 并要求 `--profile`**：浏览器重启后 daemon 侧「默认 profile」未确立，`doctor` 会报 `Multiple Browser Bridge profiles are connected; choose one with --profile` 并 FAIL；**这不代表连接坏了** —— 显式 `--profile 73wmd6ds` 的操作照常可用（实测 `read_vix_quote.py` exit 0）。别被这条 FAIL 误导去「修」。
* 核验 Chrome 进程归属的只读方法：按 `Path`（或 `Win32_Process.CommandLine`）区分 CFT 与日常 Chrome —— 不要用窗口标题或端口猜测。

<!-- dedicated-cft-end -->

<!-- gsd-project-start source:PROJECT.md -->
## Project

**tushareindex — 股东参考表同步（GSD 增量里程碑）**

本仓库为 **A 股/ETF 的 Tushare → MySQL 数据工程** 与 **Flask 行情监控** 的棕地项目。本里程碑在现有 [fina_indicator.py](d:/cursor/tushareindex/fina_indicator.py) 中 **新增股东人数、前十大股东、前十大流通股东** 三表同步，与当前财务五表 **同进程、同增量季窗口、并行线程池** 对齐。

**Core Value:** **在不限爆 Tushare 的前提下，将三类股东参考数据可靠落库 `stock_tushare`，并与财务同步脚本共享同一套全量/增量与危险清表语义。**

### Constraints

- **API**：Tushare 500 次/分、`top10_*` 2000 积分起；`stk_holdernumber` 单次 3000 行。
- **工程**：单文件尽量扩展 `fina_indicator.py`，遵循 jinwq camelCase 与日志约定。
- **时间**：`top10` 全量按票循环可能极耗时，需日志进度与可中断。
<!-- gsd-project-end -->

<!-- gsd-stack-start source:codebase/STACK.md -->
## Technology Stack

## Languages
- Python 3.12 - 全部业务逻辑、数据同步、Web 服务
- SQL (MySQL 方言) - 数据查询与 DDL，散落于各 `.py` 文件和 `sql/` 目录
- HTML/CSS/JavaScript - `templates/` 和 `static/` 下的 Web 监控页
## Runtime
- Python 3.12（`pyproject.toml` 中 `requires-python = ">=3.12"`）
- Windows 11（开发与生产运行环境相同）
- `uv`（`uv.lock` 存在，`pyproject.toml` 使用 `[dependency-groups]`）
- 备用：pip（`requirements.txt` / `requirements_python_mysql.txt`）
- Lockfile: `uv.lock`（已提交）
## Frameworks
- Flask 3.1.3 - 行情监控 Web 服务（`rt_monitor_app.py`、`web_dashboard.py`）
- flask-socketio 5.3.x / 已安装 2.9.0.dev - 实时推送（`SocketIO(app, cors_allowed_origins="*")`，默认端口 5000，由 `RT_MONITOR_PORT` 控制）
- Werkzeug 3.0.x - WSGI 工具层（Flask 依赖）
- pandas 2.4.2 - 全量数据分析与批量写入，核心库
- numpy 2.4.2 - 向量化计算，与 pandas 配套使用
- SQLAlchemy 2.0.47 - ORM / `create_engine` 方式批量写入 DataFrame（`to_sql`），部分脚本直接用 `pymysql`
- pytest 9.0.2 - `pyproject.toml` dev 组
- ruff 0.14.14 - lint（dev 组）
- ty 0.0.14 - 类型检查（dev 组）
- 无 CI 管道，手动 `python script.py` 运行；批处理 `.bat` 和 PowerShell `.ps1` 做快捷启动
## Key Dependencies
- `tushare` 1.4.24 - Tushare Pro 行情接口，`ts.pro_api(token)`，几乎所有同步脚本依赖
- `pymysql` 1.4.6 - MySQL 连接驱动，主要连接方式
- `mysql-connector-python`（`mysql.connector`，部分脚本用）- 备用 MySQL 驱动
- `pandas` 2.4.2 - 数据处理核心
- `Flask` 3.1.3 + `flask-socketio` - Web 监控核心
- `requests` 2.32.5 - HTTP 请求（巨潮、GLM API）
- `pdfplumber` 0.11.x - PDF 表格抽取（`cninfo_ann_glm_digest.py`，可选）
- `openpyxl` - Excel 读写（含 Wind 数据导入）
- `xlrd` - 旧 `.xls` 格式读取（部分历史脚本）
- `reportlab` 4.4.4 - PDF 报告生成（ETF 基金分析报告、融资融券报告）
- `markdown` 3.4.x - Markdown 渲染（Web 模板）
- `scipy` - 统计与稀疏矩阵（特征工程脚本）
- `scikit-learn` 1.7.1 - 聚类（KMeans）、特征选择、StandardScaler、PCA（`08_*.py` 系列）
- `torch` 2.9.0.dev（CUDA 12.8）- 深度学习（`12_深度学习*.py`、`best_pytorch_model.pth`）
- `cupy` - GPU 加速计算（`08_数据库实时股票筛选_CuPy优化版.py`，可选）
- `tensorflow`/`keras` - 旧模型（`best_model.h5`，脚本已有 `fix_tensorflow_conflict.py`，非主流路径）
- `pytdx` 0.11.7 - 通达信本地文件读取（`V300C0_日线导出_pytdx_mootdx.py`）
- `mootdx` - 通达信本地 Reader（同上）
- `WindPy`（`from WindPy import w`）- Wind 金融终端 Python 接口（`wind1min.py`、`indexwind全A.py` 等，需 Wind 客户端安装）
## Configuration
- 主要通过**环境变量**传递密钥和连接参数
- `TUSHARE_TOKEN` - Tushare API Token（必填）
- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` - MySQL 连接（可选，脚本中有默认值 localhost/root/123456）
- `RT_MONITOR_PORT` - Web 服务端口（默认 5000）
- `RT_MONITOR_AUTO_KILL_PREVIOUS` - 是否自动清理旧进程（代码内默认 0；`run_rt_monitor.bat` 未预置环境变量时设为 1）
- `RT_MONITOR_API_PREFIX` - 反代前缀路径
- `ZHIPU_API_KEY` - 智谱 GLM API Key（`cninfo_ann_glm_digest.py` 使用）
- `jinwq.env` 文件保存本地开发默认值（**勿提交真实密码**）
- `config.json` - 部分脚本使用的 JSON 配置（API 重试、连接池参数）
- `pyproject.toml` - uv/ruff/ty 配置
- `uv.lock` - 锁定依赖版本
## Platform Requirements
- Windows 11，Python 3.12
- MySQL 8.4.5（本地），`innodb_buffer_pool_size=4096M`
- Wind 终端（仅 Wind 系列脚本需要）
- NVIDIA GPU + CUDA 12.x（仅 ML 脚本需要，主业务不依赖）
- 同开发环境（本地运行，无云端部署）
- 通过 `.bat` / `.ps1` 定时触发或手动执行
- Web 服务 `rt_monitor_app.py` 以 `socketio.run(host='0.0.0.0', port=5000)` 运行，可局域网访问
<!-- gsd-stack-end -->

<!-- gsd-conventions-start source:CONVENTIONS.md -->
## Conventions

## Source of truth
- 仓库细则：`.cursor/rules/jinwq.mdc`（camelCase 标识符、Python 3.12、MySQL 8.4、日期 DATE/`YYYYMMDD` 口径）。
- Tushare 入库生成规范：`.cursor/skills/tushare-to-mysql/SKILL.md`（分页、fields、限速 0.2s、NaN→NULL）。
## Python
- **命名**：变量/函数 **camelCase**（本项目约定，与常见 PEP8 snake_case 并存时需跟仓库）。
- **类型**：关键路径逐步类型提示；脚本偏工程化可读。
- **日志**：与 py 同名日志、轮转、Windows 安全 Handler（常驻脚本见 `python-daemon-exit-and-log.mdc`）。
- **API**：`_wait_tushare_api_slot` / 全局限流，避免多线程抢跑打满接口。
## 历史回补与并行（用户偏好）
- 按 **交易日**（或等价分片键）独立、可幂等的历史计算/回补，在**不限爆外部 API**且**无跨日强依赖**时，**默认采用多线程**（`ThreadPoolExecutor`），并暴露 `--*-workers` 或环境变量（如 `MARKETINFO_BACKFILL_WORKERS`）。
- 参考实现：`marketinfo_from_stk_factor_pro.py` 的 `--backfill-cols` + `--backfill-workers`（单进程内并行，每线程独立 `pymysql` 连接；建议 3~5，脚本内 cap 16）。
- **仍须遵守**：Tushare 拉取全局限流（如 `max_workers=5`、`time.sleep(0.2)`）；同脚本 `GET_LOCK` 单实例（勿多进程抢锁）；纯 MySQL 大表重扫时 workers 过大易打满磁盘/缓冲池。
## 本地数据回测验证（用户偏好）
- 基于已落库 MySQL 历史（如 `opt_daily`/`etf_daily`）的**方案 A 条件回测**，默认 **逐日全扫描**：每个 ETF **交易日**建仓采样一次（`SAMPLE_FREQ=D`，见 `opt_vertical_spread_backtest.iterSampleTradeDates`），**不用**周频 `W-MON` 除非显式 `--freq W-MON` 做快速抽样。
- 参考脚本：`opt_vertical_spread_backtest.py`、`opt_vertical_spread_tp_fine.py`。
## SQL
- 字符集 `utf8mb4`，排序 `utf8mb4_0900_ai_ci`（规则已载明）。
- 财务 PIT：`fina_indicator` 唯一键含 `ann_date`、`update_flag` 等（与现脚本一致）。
- **本机库校验**：核对 `stock_tushare` / `stock_wind` 等本地表数据或字段时，Agent 应在会话内直接执行查询（如 `mysql` CLI 或短时 `python -c` + pymysql），把关键结果写进回复；**不要**只把 SQL 丢给用户手工跑。
## Git / 规划
- GSD 产物置于 `.planning/`；若 `commit_docs: true` 则纳入版本控制。
<!-- gsd-conventions-end -->

<!-- gsd-architecture-start source:ARCHITECTURE.md -->
## Architecture

## Pattern
- **脚本型数据管线**：根目录大量独立 `.py` 脚本负责 Tushare → MySQL 同步（全量/增量、分页、限速、`REPLACE`/UPSERT）。
- **单文件 Web 服务**：`rt_monitor_app.py` 为行情监控主入口（Flask + 可选 SocketIO），读 `stock_tushare` / `stock_wind`，不写行情业务表。
- **数据库双库**：`stock_tushare`（Tushare 落地）、`stock_wind`（Wind/实时等）；脚本内 `DB_CONFIG` / 环境变量切换。
## Layers（逻辑分层）
## Data flow（财务线示例）
- `fina_indicator_vip` 等按 `period` 拉全市场 → 去重 → `fina_indicator` / `income` 等表。
- 股东三表扩展（计划）：`stk_holdernumber` 按公告区间；`top10_*` 按 `ts_code` 循环 + 报告期区间。
## Entry points
- 财务同步：`python fina_indicator.py`（多线程阶段1 + 可选 reconcile）。
- **成长股本地一条龙（通用，任意 ts_code）**：`python growth_stock_local/run_growth_pipeline.py --code {TS_CODE} --trade-date YYYY-MM-DD`（Layer1 → 舆情合并 → Agent A–E → 报告；入口见 `.cursor/skills/growth-stock-local/SKILL.md`；`600183.SH` 仅为文档示例）。
- 季报后刷新看板：`python growth_stock_local/refresh_tracking_board.py --code {TS_CODE} --re-export`。
- 监控 Web：`python rt_monitor_app.py`（端口 `RT_MONITOR_PORT`）。
- **rt_monitor 改代码后若浏览器/API 仍像旧版**：先排除「同一监听端口多个 `python … rt_monitor_app.py`」——核对响应头 `X-RT-Monitor-Build` 与仓库 `RT_MONITOR_STATIC_VER`（2026-10-04 起为 `static/`+`templates/` 内容哈希 `c`+10 位，改资源自动变、**不再手工递增**；期望值 `python -c "from rt_monitor.core.config import RT_MONITOR_STATIC_VER as v;print(v)"`，环境变量同名可手动覆盖；旧历史见 `rt_monitor/CHANGELOG-static-ver.md`）、`netstat -ano` 是否多个 PID 同端口 LISTEN，再查业务逻辑。清单：`docs/solutions/workflow-issues/rt-monitor-multi-instance-debug-checklist-20260426.md`。
## Key files
- `fina_indicator.py`：财务五表 + 全局限流、并行 `ThreadPoolExecutor`。
- `rt_monitor_app.py`：薄入口；**fina** 路由在 `fina_bp`，HTTP/预热在 `services/fina_arturn.py`，重查询仍 `monolith`。
- `.cursor/rules/jinwq.mdc`：项目 Python/MySQL 约定（camelCase、日志、日期口径）。
<!-- gsd-architecture-end -->

<!-- gsd-skills-start source:skills/ -->
## Project Skills

| Skill | Description | Path |
|-------|-------------|------|
| agent-browser | \| Browser automation for AI agents via inference.sh. Navigate web pages, interact with elements using @e refs, take screenshots, record video. Capabilities: web scraping, form filling, clicking, typing, drag-drop, file upload, JavaScript execution. Use for: web automation, data extraction, testing, agent browsing, research. Triggers: browser, web automation, scrape, navigate, click, fill form, screenshot, browse web, playwright, headless browser, web agent, surf internet, record video | `.agents/skills/agent-browser/SKILL.md` |
| pdf | Use this skill whenever the user wants to do anything with PDF files. This includes reading or extracting text/tables from PDFs, combining or merging multiple PDFs into one, splitting PDFs apart, rotating pages, adding watermarks, creating new PDFs, filling PDF forms, encrypting/decrypting PDFs, extracting images, and OCR on scanned PDFs to make them searchable. If the user mentions a .pdf file or asks to produce one, use this skill. | `.agents/skills/pdf/SKILL.md` |
| python-anti-patterns | Common Python anti-patterns to avoid. Use as a checklist when reviewing code, before finalizing implementations, or when debugging issues that might stem from known bad practices. | `.agents/skills/python-anti-patterns/SKILL.md` |
| python-code-review | Reviews Python code for type safety, async patterns, error handling, and common mistakes. Use when reviewing .py files, checking type hints, async/await usage, or exception handling. | `.agents/skills/python-code-review/SKILL.md` |
| python-performance-optimization | Profile and optimize Python code using cProfile, memory profilers, and performance best practices. Use when debugging slow Python code, optimizing bottlenecks, or improving application performance. | `.agents/skills/python-performance-optimization/SKILL.md` |
| python-testing-patterns | Implement comprehensive testing strategies with pytest, fixtures, mocking, and test-driven development. Use when writing Python tests, setting up test suites, or implementing testing best practices. | `.agents/skills/python-testing-patterns/SKILL.md` |
| vercel-react-best-practices | React and Next.js performance optimization guidelines from Vercel Engineering. This skill should be used when writing, reviewing, or refactoring React/Next.js code to ensure optimal performance patterns. Triggers on tasks involving React components, Next.js pages, data fetching, bundle optimization, or performance improvements. | `.agents/skills/vercel-react-best-practices/SKILL.md` |
| xlsx | "Use this skill any time a spreadsheet file is the primary input or output. This means any task where the user wants to: open, read, edit, or fix an existing .xlsx, .xlsm, .csv, or .tsv file (e.g., adding columns, computing formulas, formatting, charting, cleaning messy data); create a new spreadsheet from scratch or from other data sources; or convert between tabular file formats. Trigger especially when the user references a spreadsheet file by name or path — even casually (like \"the xlsx in my downloads\") — and wants something done to it or produced from it. Also trigger for cleaning or restructuring messy tabular data files (malformed rows, misplaced headers, junk data) into proper spreadsheets. The deliverable must be a spreadsheet file. Do NOT trigger when the primary deliverable is a Word document, HTML report, standalone Python script, database pipeline, or Google Sheets API integration, even if tabular data is involved." | `.agents/skills/xlsx/SKILL.md` |
| agent-native-architecture | Build applications where agents are first-class citizens. Use this skill when designing autonomous agents, creating MCP tools, implementing self-modifying systems, or building apps where features are outcomes achieved by agents operating in a loop. | `.cursor/skills/agent-native-architecture/SKILL.md` |
| andrew-kane-gem-writer | This skill should be used when writing Ruby gems following Andrew Kane's proven patterns and philosophy. It applies when creating new Ruby gems, refactoring existing gems, designing gem APIs, or when clean, minimal, production-ready Ruby library code is needed. Triggers on requests like "create a gem", "write a Ruby library", "design a gem API", or mentions of Andrew Kane's style. | `.cursor/skills/andrew-kane-gem-writer/SKILL.md` |
| compound-docs | Capture solved problems as categorized documentation with YAML frontmatter for fast lookup | `.cursor/skills/compound-docs/SKILL.md` |
| continual-learning | Incrementally extract recurring user corrections and durable workspace facts from transcript changes, then update AGENTS.md with plain bullet points only. Use when the user asks to mine previous chats, maintain AGENTS.md memory, or build a self-learning preference loop. | `.cursor/skills/continual-learning/SKILL.md` |
| create-agent-skills | Expert guidance for creating Claude Code skills and slash commands. Use when working with SKILL.md files, authoring new skills, improving existing skills, creating slash commands, or understanding skill structure and best practices. | `.cursor/skills/create-agent-skills/SKILL.md` |
| dev-environment-zh | 记录用户开发环境偏好（简体中文、Windows 11、Python 3.12、MySQL 8.4.5）与领域方向（股票数据分析、数据挖掘交易机会、后期机器学习），并推荐适用的 Cursor skills。在用户询问环境、推荐技能、或涉及量化/股票/ML 开发时使用。 | `.cursor/skills/dev-environment-zh/SKILL.md` |
| dhh-rails-style | This skill should be used when writing Ruby and Rails code in DHH's distinctive 37signals style. It applies when writing Ruby code, Rails applications, creating models, controllers, or any Ruby file. Triggers on Ruby/Rails code generation, refactoring requests, code review, or when the user mentions DHH, 37signals, Basecamp, HEY, or Campfire style. Embodies REST purity, fat models, thin controllers, Current attributes, Hotwire patterns, and the "clarity over cleverness" philosophy. | `.cursor/skills/dhh-rails-style/SKILL.md` |
| document-review | This skill should be used to refine brainstorm or plan documents before proceeding to the next workflow step. It applies when a brainstorm or plan document exists and the user wants to improve it. | `.cursor/skills/document-review/SKILL.md` |
| dspy-ruby | Build type-safe LLM applications with DSPy.rb — Ruby's programmatic prompt framework with signatures, modules, agents, and optimization. Use when implementing predictable AI features, creating LLM signatures and modules, configuring language model providers, building agent systems with tools, optimizing prompts, or testing LLM-powered functionality in Ruby applications. | `.cursor/skills/dspy-ruby/SKILL.md` |
| every-style-editor | This skill should be used when reviewing or editing copy to ensure adherence to Every's style guide. It provides a systematic line-by-line review process for grammar, punctuation, mechanics, and style guide compliance. | `.cursor/skills/every-style-editor/SKILL.md` |
| file-todos | This skill should be used when managing the file-based todo tracking system in the todos/ directory. It provides workflows for creating todos, managing status and dependencies, conducting triage, and integrating with slash commands and code review processes. | `.cursor/skills/file-todos/SKILL.md` |
| gemini-imagegen | This skill should be used when generating and editing images using the Gemini API (Nano Banana Pro). It applies when creating images from text prompts, editing existing images, applying style transfers, generating logos with text, creating stickers, product mockups, or any image generation/manipulation task. Supports text-to-image, image editing, multi-turn refinement, and composition from multiple reference images. | `.cursor/skills/gemini-imagegen/SKILL.md` |
| git-worktree | This skill manages Git worktrees for isolated parallel development. It handles creating, listing, switching, and cleaning up worktrees with a simple interactive interface, following KISS principles. | `.cursor/skills/git-worktree/SKILL.md` |
| hugging-face-datasets | Create and manage datasets on Hugging Face Hub. Supports initializing repos, defining configs/system prompts, streaming row updates, and SQL-based dataset querying/transformation. Designed to work alongside HF MCP server for comprehensive dataset workflows. | `.cursor/skills/hugging-face-datasets/SKILL.md` |
| install-trailofbits-modern-python | 将 Trail of Bits 的 modern-python skill 从 awesome-agent-skills 安装到 Cursor。在用户要求安装 trailofbits modern-python、安装 awesome-agent-skills 中的 modern-python、或配置 uv/ruff/ty 相关 skill 时使用。 | `.cursor/skills/install-trailofbits-modern-python/SKILL.md` |
| install-vercel-find-skills | 从 Vercel Labs 的 skills 仓库安装 find-skills 工具到 Cursor。使用场景：用户想要安装 find-skills、搜索可用的 skills、或从 Vercel Labs 仓库添加 skills 时使用。 | `.cursor/skills/install-vercel-find-skills/SKILL.md` |
| modern-python | Configures Python projects with modern tooling (uv, ruff, ty). Use when creating projects, writing standalone scripts, or migrating from pip/Poetry/mypy/black. | `.cursor/skills/modern-python/SKILL.md` |
| myetfdate | > Captures the user's core ETF universe and MySQL storage layout for daily and intraday ETF data. Use this skill whenever working with the user's ETF strategies, especially when querying or reasoning about stock_tushare.etf_daily, etf_stk_mins, etf_stk_mins_ma, etf_rt_min (today 15/60min, intraday refresh), and etf_bias_realtime (intraday BIAS, close usable as last price), or when constructing backtests/signals for the specified ETF codes. For multi-day interval return (T-5/T-20/T-60/T-120), use the Wind-style formula documented in this skill (rt_k.close + etf_daily qfq). | `.cursor/skills/myetfdate/SKILL.md` |
| mytushare | 本仓库 Tushare 项目约定摘要（与全局 tushare 技能区分）。入库/复权/指标口径约定；区间涨跌幅（Wind）与 myetfdate 一致。触发词：mytushare、tushare 项目约定、复权、入库、pro_api、分钟指标、区间涨跌幅。 | `.cursor/skills/mytushare/SKILL.md` |
| myuiux | 本项目的图表与弹窗 UI/UX 格式规范，用于行情监控页内图表、BIAS 弹窗、底部三张 Top30 紧凑表、应收账款/财报筛选表（含独立财报页）等。触发词：图表格式、弹窗样式、myuiux、监控页样式、Top30、股票筛选、应收账款、财报页、增列竖线、图标输出格式、固定层遮挡、重叠。 | `.cursor/skills/myuiux/SKILL.md` |
| orchestrating-swarms | This skill should be used when orchestrating multi-agent swarms using Claude Code's TeammateTool and Task system. It applies when coordinating multiple agents, running parallel code reviews, creating pipeline workflows with dependencies, building self-organizing task queues, or any task benefiting from divide-and-conquer patterns. | `.cursor/skills/orchestrating-swarms/SKILL.md` |
| rclone | Upload, sync, and manage files across cloud storage providers using rclone. Use when uploading files (images, videos, documents) to S3, Cloudflare R2, Backblaze B2, Google Drive, Dropbox, or any S3-compatible storage. Triggers on "upload to S3", "sync to cloud", "rclone", "backup files", "upload video/image to bucket", or requests to transfer files to remote storage. | `.cursor/skills/rclone/SKILL.md` |
| resolve_pr_parallel | Resolve all PR comments using parallel processing. Use when addressing PR review feedback, resolving review threads, or batch-fixing PR comments. | `.cursor/skills/resolve-pr-parallel/SKILL.md` |
| setup | Configure which review agents run for your project. Auto-detects stack and writes compound-engineering.local.md. | `.cursor/skills/setup/SKILL.md` |
| skill-creator | Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Claude's capabilities with specialized knowledge, workflows, or tool integrations. | `.cursor/skills/skill-creator/SKILL.md` |
| tushare-to-mysql | 根据 Tushare 接口文档自动生成 Python 数据入库脚本，将数据存储到本地 MySQL。支持全量导入和增量导入，自动建表（含字段注释），自动识别 VIP 接口。当用户提供 Tushare 接口链接（如 https://tushare.pro/document/2?doc_id=79）时使用。触发词：tushare入库、数据入库、接口导入、同步数据、下载数据到MySQL。ETF 区间涨跌幅展示口径见规则 O（与 myetfdate 一致）。 | `.cursor/skills/tushare-to-mysql/SKILL.md` |
| wechat-article-downloader | 微信公众号文章下载（HTML/PDF/MD 等）、单篇与合集、本地 MCP 批量与 CSV 导出流程；与 [qiye45/wechatDownload](https://github.com/qiye45/wechatDownload) 配套。触发词：微信公众号下载、公众号批量、appmsgalbum、wechat MCP。 | `.cursor/skills/wechat-article-downloader/SKILL.md` |
| growth-stock-local | A 股成长股本地通用一条龙（任意 ts_code）：`run_growth_pipeline.py`、Layer1、parallel 舆情、Agent 报告。触发：成长股分析、分析 XX 成长股、前置指标。详情见 `.cursor/skills/growth-stock-local/SKILL.md` → `growth_stock_local/skills/growth-orchestrator.md` | `.cursor/skills/growth-stock-local/SKILL.md` |
<!-- gsd-skills-end -->

<!-- gsd-workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- gsd-workflow-end -->

## Learned User Preferences

- **交付前必须逐项自检（2026-09-11 用户明令，长期有效）**：任何修改/优化在交付给用户之前，Agent **必须自己逐项核实**，确认无问题后才交付。要求：
  1. **每项改动都要有可复现的证据**（脚本输出/日志片段/接口返回），不能只凭推理或"测试通过"就宣称完成；
  2. **"测试全绿"≠"行为正确"** —— 本日两个真实 bug（陈旧判定无方向、补丁仅在值变化时写）都是 Agent 自己引入、且**旧测试全部通过**的，靠新写的取证脚本才抓出来；因此**新增/修改行为必须补针对性回归测试**；
  3. 自检要覆盖：**功能是否符合意图、边界与方向性假设、是否引入了回归、运行态是否真的生效**（不能只改代码不部署）；
  4. 交付说明中要**明确列出自检结论**与**未自检/无法自检的部分**（如需要浏览器目视的 UI 观感）；
  5. 有失败或不确定的，**如实报告并先修**，不要以"应该没问题"结题。

- **持续自我迭代机制（三层，2026-09-11 建立，用户认可）**：规则不能只写在文档里，必须固化为**可执行守卫**，否则会随时间腐烂。
  1. **`.planning/LESSONS.md`** = 教训库。**每次犯错（无论是否已交付）就追一条**，字段固定：`症状 / 错误写法 / 为什么错 / 正确写法 / 守卫 / 通用教训`。
  2. **`scripts/selfcheck_delivery.py`** = 守卫执行器。分组 `[code]`（实现是否仍在）、`[lessons]`（每条教训的守卫是否仍生效）、`[runtime]`（运行态是否真的对）、`[board]`（页面产出）、`[hygiene]`（脚本资产卫生/保留期是否在管）。**只写教训不加守卫 = 未完成**；守卫被重构删掉会导致 FAIL，逼迫重新评估该教训是否仍适用。
  3. **`scripts/selftest_guard_effective.py`** = 守卫的守卫。故意破坏一条守卫 → 断言自检确实 FAIL → 字节级还原并校验哈希。**只有"坏掉时会被发现"的守卫才算守卫**（LESSON-010）。
  - **会话收尾动作（每次都要做）**：① `python -X utf8 scripts/selfcheck_delivery.py` 必须全绿；② `python -m pytest tests/ -q` 全绿；③ `python -X utf8 scripts/audit_scratch_retention.py --sweep` 按 atime 自动续期本会话跑过的诊断脚本；④ 有新技术教训 → 写进 `LESSONS.md` **并**补一条守卫；⑤ 交付说明附自检结论与未能自检项。
  - **新增/修改行为时的固定动作**：先写能打印中间值的取证脚本或具名回归测试 → 再改代码 → 跑自检 → 部署 → 复测运行态。

- `stk_qfq_close_break` / 首次历史新高·850 新高：前端与同步落库**不做**「去脏 + 剔 A 类」过滤；选股时由用户自行心算。Agent 做因子回测、胜率/收益验证时必须自行去脏并剔 A，并与「全样本」结果分开报告。
- 去脏口径（回测默认）：`ABS(pct_chg)>32` 或 `list_age≤0`（含北交所换代码等脏点）剔除。
- 剔 A 口径（回测默认，组合条件）：`gap_to_peak_pct≥-0.5 AND list_age_days≤250`（次新贴着前高连板；单用 `list_age>250` 会误杀大量真突破）。B 类为真正跨越旧前高的样本。
- fina 突破区换手着色偏好（A 股红涨绿跌语义）：偏低换手（如 ≤7%）用**红**标关注，偏高（如 ≥14%）用**绿**标警惕；与「绿好红坏」国际习惯相反，以用户明示为准。
- 实时行情走银河**大 QMT 桥接**（MiniQMT 关停中）：RPC 主通道 ZMQ（`127.0.0.1:15573`），MySQL `bigqmt_rpc` 仅配置兜底；全推 ZMQ PUB `15574`。暂不加 Level2、不上 Redis。热路径 `subscribe_whole_quote(沪深A股+北交所+沪深ETF 代码列表)`，禁止盘中 `get_full_tick(['SH','SZ','BJ'])`（全市场约 6s 易卡死）。`get_full_tick` 仅启动/重连打底；`subscribe_quote` 留给自选股。全推/`get_full_tick` 卡应查 **交易中心** 站点，不是行情主站。`rt_k` 不替换。
- 有次数/时间窗限频的轮询：间隔从**开始发请求**起算，不是执行完成后再额外 sleep。50 次/分 → 基准 1.2s start-to-start；可自适应加间隔，但计时起点不变。

## Learned Workspace Facts

- **本机硬件（2026-09-11 实测）**：AMD Ryzen Threadripper 7980X，**64 核 / 128 逻辑线程**，基准 3201 MHz，L3 256 MB；物理内存 **255.5 GB**（常用可用 ~157 GB）；C:/D:/G: 各约 1.9 TB，可用 1.3~1.6 TB。→ 并发余量极大：在**不破坏稳定性**的前提下可放心用多线程提升数据新鲜度/覆盖面；但外部接口（QMT 桥单线程策略、通达信 TQ、Tushare 限频）与磁盘/缓冲池才是真瓶颈，**不要盲目加大对外并发**。
- **期权行情来源分工（2026-09-11 与用户确认）**：`510300 / 510500 / 588000` → **QMT**（`subscribe_quote` 订阅，配额约 418≤500；实测 tick 带完整五档 + `openInt`，滞后 0.2~3s）；`510050 / 159915` → **通达信** `tdx_opt_live.py`(:15581)（510050 不在订阅配额内；159915 深市 `.SZO` 无 tick 回调）。Tushare **没有期权实时行情**。对应常量：`OPT_QMT_TICK_UNDERLYINGS`、`QMT_LED_UNDLS`；`qmtLedChain()` **只在连续竞价时段为真**（午休/盘后让 TDX 兜底，否则整链无盘口）。
- **❌ pytdx 直连没有盘中实时行情（2026-09-14 用户明确指出，不要再尝试）**：`pytdx.hq.TdxHq_API`（服务器直取）**拿不到盘中实时报价**。2026-09-13 实测 16 台常见节点中 9 台能完成 TCP + 协议握手（`get_security_count(SH)=27920`），但 `get_security_quotes` / `get_security_bars` / `get_security_list` **一律返回空**（连 600519、510050 ETF 本身都是空）；2026-09-14 盘中复测仍然全空。取证脚本 `scripts/probe_pytdx_direct_feasibility.py`（已归档结论）。**因此"用 pytdx 绕开本机缓存取实时期权行情"这条路不成立**，不要再列为候选方案。
- **通达信期权快照只会被"强制刷新"打新，不会自行更新（2026-09-14 修正，推翻此前认知）**：`tq.get_market_snapshot` 读本地缓存，缓存**不自更新**；只有 `refresh_cache(force=True)` 才会让它跳到最新。此前记录的"自然脉冲周期 33s→135s"是**误判** —— 实测每次"脉冲"都精确发生在**我自己触发 refresh 的同一秒**（日志 11:04:04 / 11:08:07 / 11:12:10 / 11:16:14 / 11:20:17 与探测到的推进时刻一一对应），所谓"锯齿周期"就是**保险丝自己造出来的自我维持循环**（LESSON-017）。取证：`scripts/probe_tdx_cache_progress.py`、`scripts/probe_tdx_selfupdate_offline.py`（决定性排除实验：关刷 + 保险丝设 3600s → **420s 内推进 0 次**、滞后 8.8s→409.2s）。
  - **硬结论：「关闭刷新」与「保持新鲜」在本架构下不可兼得**，不是阈值调优问题，只能选刷新间隔。
  - **✅ 当前决策（2026-09-14 用户选择方案 A）：`TDX_OPT_REFRESH_SESSION_SEC=3`（默认已改回 3）** —— 新鲜度优先，**代价是约每 3s 弹一次通达信"数据刷新"进度条，用户已知悉并接受**（09-11 实测该配置当天触发 3116 次、99.2% 来自定时器）。
  - 随时可退回（**不必改代码**）：`curl "http://127.0.0.1:15581/set_refresh_sec?sec=N"`，`N=0` 即关闭（不弹框，但数据只在被强制刷新时才更新）；或设环境变量 `TDX_OPT_REFRESH_SESSION_SEC=0`。
  - 刷新时段：仅 `09:25–11:30` 与 `13:00–15:00`；**午休/盘后不刷，滞后会自然累积**（分析数据时必须排除这些时段，否则会把"午休累积"误判成"刷新失效"）。
  - 关联：五条链共用 daqmt 的合并快照，而快照时间戳与 TDX **逐位一致** → **TDX 的刷新节奏决定全部五条链的新鲜度**（含 QMT 订阅的三条链）。这是当前架构最大的新鲜度瓶颈。
  - **QMT 侧上游是健康的，别误判**：桥接推送缓存实测 **p50 仅 3.4s、424/424 合约全带完整五档**（`scripts/probe_bridge_tick_age.py`），全推心跳 `updates=1105万 unique=7213 silence_s=0.2`。所以问题不在 QMT 推送。
- **T 板缓存键必须与重算耗时解耦**：逐笔内容每 0.5~5s 真变；若把内容摘要放进缓存键，缓存永不命中 → 每请求同步全量重算 → API 被压到 3~5s（单次重算仅 ~0.5s）。用 `OPT_T_BOARD_REUSE_BUCKET_SEC`（默认 5s，桶内只用桶号作键）。**桶必须显著大于单次重算耗时**，否则形成自我维持的反馈循环。
- **T 板字段级灰阶陈旧度（A2）**：`最新/成交量/持仓量` 按各自字段时钟着色，仅**连续竞价时段**生效（午休/盘前/盘后不着色），只用灰阶不引入色相。阈值 **>30s 淡灰 `#9e9e9e` / >300s 深灰 `#c4c4c4`**（2026-09-11 由 10/60 调宽：实测系统投递中位 8.5s、P75 23s，10s 阈值会切在周期中位数上导致半数行变灰；30/300 正常占比 91%）。阈值见 `opt_t_board._OPT_T_AGE_*`，时段见 `optTAgeColoringActive()`，标定脚本 `scripts/tune_age_thresholds.py`。**规则：灰阶阈值必须明显大于系统正常投递周期。**
- **T 板刷新策略（2026-09-11 用户确认）**：**可见链 800ms**（`scheduleLiveRefresh` → `loadUnderlying(getActiveUnderlying())`）；**其余链 6s 后台低频轮转**（`backgroundRefreshTick`，每次只打一个非可见链、带 `If-None-Match`、**只更新缓存不动 DOM**；`window.__optBgRefreshSec=0` 可关）。不要给五条链都上 800ms——后端单次重算约 0.5s，会重现"每请求必重算"的 API 秒级问题。
- **交付前自检入口**：`python -X utf8 scripts/selfcheck_delivery.py`（五组 `code`/`lessons`/`runtime`/`board`/`hygiene`；FAIL 则退出码非 0）。**盘外（含周末）自动降级**：新鲜度/订阅推送类改判"盘后年龄合理"或 SKIP；端口检查区分「**核心端口**（5000 rt_monitor + 15573/15574 QMT 桥 —— 任何时候都该在）」与「**客户端依赖端口**（15580 daqmt / 15581 tdx_opt_live —— 需通达信或大 QMT 客户端在运行，盘外可不在）」。守卫有效性验证 `python -X utf8 scripts/selftest_guard_effective.py`（注入破坏→断言FAIL→字节级还原+哈希校验）；改模板内联 JS 后跑 `python -X utf8 scripts/check_template_js_syntax.py`（`node --check`）。
- **诊断脚本保留期机制（2026-09-11 建立，落实 LESSON-012/013/015/016）**：脚本头部声明 `# 用途: 守卫 | 标定 | 一次性诊断 | 留档` 与 `# 保留至: YYYY-MM-DD`；**只有「一次性诊断」参与保留期**（默认 30 天，`SCRATCH_RETENTION_DAYS` 可改），其余三类一律豁免（豁免集合 = `EXEMPT_KINDS` 唯一权威定义）。**不要按 `mtime` 一刀切清理** —— checkout/编辑器保存会刷新 mtime，"碰过"≠"用过"；且守卫本来就很少改动，按时间删等于拆刹车。**真实使用信号是 `atime`**（本机 `DisableLastAccess=2` 已启用）：placement 写入后 `atime==mtime`，此后被 `python x.py` 执行或被读取就 `atime>mtime`；**会话收尾跑 `python -X utf8 scripts/audit_scratch_retention.py --sweep`** 自动续期（跑过才续，没跑过不续），单个补续仍可用 `--touch scripts/xxx.py`。过期项**默认只报告不删**，先蒸馏方法论进 `LESSONS.md`，再用 `--archive` 移入 `.planning/scratch-archive/`。**第 3 点是用户强烈要求的**：今天的教训（陈旧判定方向写反、退化判据写错分支）都是**复发型**问题，删掉可执行配方等于复发时重写。历史里程碑脚本（`p2_*`/`p3_*`/`p4_*`、`verify_sw_industry_l1_l2.py`、`export_300750_single_quarter_ebit.py`）经用户确认为**手工留档**，已标 `用途: 留档`。批量改标注用 `python -X utf8 scripts/_label_scripts.py`（走 `writeTagged`，定位规则从审计器导入，不重复实现）。
  - ⚠️ **"谁都不许污染 atime"三条铁律（改保留期相关代码时勿破坏）**：① `scan()` 默认**不读文件内容** —— 分类走 `headMeta()` 缓存（`.planning/scratch-meta.json`，**键为文件头内容 sha1，不要加 mtime+size 快路径**，见 LESSON-016）；② 任何读脚本内容的路径（`headMeta()`、自检 `readText()`）读完必须把 atime 原样写回（`os.utime(path, saved)`）；③ 归零只在显式 `resetAfter=True`（报告 / `--sweep`）时发生，且必须用 `time.time()` 而非 `os.stat().st_mtime`（NTFS atime 惰性更新，会拿到陈旧值）。破坏任一条**都不报错、只静默给出假"跑过"**（今天踩了三次：52/52、28/28、自检吃掉证据致误判 31/31；三条齐备后收敛到精确 1/1）。这是观察者效应（LESSON-013）。**无副作用探针用 `Get-Item`**（实测不改变 atime）；`Get-Content`/编辑器预览/grep 类工具会污染。`ATIME_EPS=0.05`（NTFS 精度到毫秒，取 1.0s 会漏判"标注后马上跑一次"）。
  - ⚠️ **源码级守卫必须防自指**（LESSON-015）：断言里的字符串字面量若也出现在被检查的文件里（尤其写在同一份源码中），`in` 检查会**永远为真**。必须把检查范围收窄到**目标函数体**，且每条源码级守卫都要做一次注入验证 —— 否则它可能从未生效过。
- **`flask_socketio.run` 内部已固定 `threaded=True`**，不要再传 `threaded=`（会 `TypeError: multiple values for keyword argument`）。
- `gap_to_peak_pct` 已落库 `stk_qfq_close_break` 并在 fina「首次历史新高/首次850新高」展示；定义：(突破日前一交易日收盘 / prior_max_qfq − 1)×100。
- 首次突破样本内最强可展示因子倾向：`avg_ff_turnover_20/5/60_pct`（越低越好），其次 `pct_chg`（偏低更好）、`gap_to_peak_pct`（贴前高/不太深更好）、`list_age_days`；`leapRatio` 对 post20/60 近零信息，不落库。
- fina 突破组已有浅黄底参考列机制 `FINAA_QFQ_BREAK_REF_SUFFIXES`（含自由市值、前高Age、距前高、5日涨幅、20/60换手）；`list_age_days`（上市至突破日天数）与 `peak_age_days`（前高Age：前高日至突破日间隔）不同，表上目前只展示后者。
- 20日换手前端着色：≤7% 红、≥14% 绿；距前高：≥-2% 红、≤-10% 绿（中间带仅负号绿/浅黄底）。
- QMT 全推宇宙：`沪深A股`（约 5213，不含北交所）+ BJ（约 343）+ `沪深ETF`（约 1663）≈ 6900；债不订。零售 L1 预期约 3 秒快照。期权 `.SHO/.SZO` 不在全推里，仍用 `get_market_data_ex(period='tick')`。
- QMT **策略编辑器 InnerAPI**（内置 Python 3.6，非本仓库 3.12 / `qmt.py`）：首行 `#coding:gbk`；必须 `init`+`handlebar`；第三方库须在券商白名单。立刻下单状态用全局变量，禁止挂 `ContextInfo`（逐 K 线深拷贝/回退；`quickTrade`：K 线结束=`0`，盘中最新 bar=`1`，定时器/回调=`2`；日线+默认0=全天不委托，通常不建议传2）。`ContextInfo.start/end` 仅回测且仅 `init` 生效；回测推荐等比前复权。禁止多线程/多进程与阻塞（全部策略同一线程）。`get_`=客户端内存，`query_`=向服务查询。迅投代码 `代码.交易所`（A 股 SH/SZ/BJ，期权 SHO/SZO；期货 symbol 区分大小写）。官方完整示例：形参常用 `C`；`subscribe_quote` 回调只能一个位置参数；`gmd(subscribe=True)`≤500 且自动订阅无号、只能停策略释放；同品种多周期订阅累加、多策略同品种不累加。立刻下单用 `class a(); A=a()` + 投资备注 `<24` + `waiting_dict` 防超单。交易日切换/断线重连会重启已挂模型；`handlebar` 随主图 tick（股票约3s），非交易时段也可能被推送，09:15前可 return。行情中心=单支订阅，交易中心=全推。股票数据字典：内置 `gmd` 有 `subscribe`，原生 `xtdata.get_market_data_ex` 无该参数、实时须先 `subscribe_quote`；财务表 `ASHAREBALANCESHEET`/`ASHAREINCOME`/`ASHARECASHFLOW`/`CAPITALSTRUCTURE`/`PERSHAREINDEX`，`announce_time` vs `report_time`，`get_raw_financial_data` 不按日填充。官方 FAQ/系统函数/行情函数/变量约定/完整示例/数据字典见 dict.thinktrader.net；细则见 qmt-trader `official-innerapi.md` / `official-code-examples.md` / `official-faq.md` / `official-dictionary-stock.md`。

<!-- gsd-profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- gsd-profile-end -->
