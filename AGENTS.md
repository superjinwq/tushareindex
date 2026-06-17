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
- **rt_monitor 改代码后若浏览器/API 仍像旧版**：先排除「同一监听端口多个 `python … rt_monitor_app.py`」——核对响应头 `X-RT-Monitor-Build` 与仓库 `RT_MONITOR_STATIC_VER`、`netstat -ano` 是否多个 PID 同端口 LISTEN，再查业务逻辑。清单：`docs/solutions/workflow-issues/rt-monitor-multi-instance-debug-checklist-20260426.md`。
## Key files
- `fina_indicator.py`：财务五表 + 全局限流、并行 `ThreadPoolExecutor`。
- `rt_monitor_app.py`：薄入口；业务在 `rt_monitor/` 包（`monolith` + 按域 `blueprints`/`services`）；P1 margin/market_status，P2 niusan/opt + sw/index bp，**P3 main_bp（首页 + 19 API）**；fina 仍 monolith（P4）。
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



<!-- gsd-profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- gsd-profile-end -->
