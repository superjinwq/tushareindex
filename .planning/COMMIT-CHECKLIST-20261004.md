# 提交清单（2026-10-04，第 1–3 轮优化后）——**仅清单，未执行任何 git add/commit**

> 生成方式：`git status --porcelain -uall`、`git diff --numstat`（含忽略空白对照）、导入闭包分析、密钥扫描。
> 当前 HEAD：`b6cbb29 2026-09-23 feat: show call put oi balance`（共 23 个提交），远端 `origin = github.com/superjinwq/tushareindex`。
> 工作区：已修改 48 个文件（全部是实质改动，无纯换行/空白噪声），未跟踪 13104 个文件。

## 0. 先看结论（三件必须先处理的事）

1. **密钥**：全仓只有 **1 个** Tushare token（指纹 `sha1[:8]=6254ec01`），以明文出现在 **83 个未跟踪文件** 里（多为 `os.getenv("TUSHARE_TOKEN", "<明文>")` 的默认值）。
   - **已跟踪文件 0 个、git 历史 0 次** → 目前**从未进过版本库**，暂不需要轮换 token。
   - 下面「应纳入」清单里有 **17 个** 文件含明文 token（见 §3），**提交前必须先把默认值去掉**（改成 `os.environ["TUSHARE_TOKEN"]` 或空默认值并在缺失时报错），否则一推送就泄露。
   - 未发现智谱 key（`32hex.16位` 格式）或 `sk-` 类 key。`jinwq.env`、`config.json` 含 token，**永不提交**。
2. **依赖闭包断裂**：已跟踪代码 import 了 **16 个未跟踪模块**（§2.2）。只提交那 48 个已修改文件，别人（或你在另一台机器）干净 checkout 后 `rt_monitor_app.py` **起不来**。这 16 个必须一起纳入。
3. **`pyproject.toml` / `uv.lock` 未跟踪**：依赖锁定文件应纳入。

## 1. 应纳入的文件

### 1.1 已修改（48 个，全部实质改动）
- **rt_monitor 主体**：`rt_monitor/app_factory.py`、`compat.py`、`core/{config,instance_lock,popup_cache}.py`、`blueprints/{fina,index,market_status,niusan,opt_t_board,sw}_bp.py`、`services/{fina_arturn,fina_arturn_queries,market_status,niusan,opt_t_board}.py`、`monolith.py`、`rt_monitor_app.py`
- **前端**：`static/css/rt_monitor.css`、`static/js/{rt_monitor_fina,opt_wall_510050}.js`、`templates/{rt_monitor,rt_monitor_niusan,rt_monitor_niusan_config,opt_wall_510050}.html`
- **其它脚本**：`opt_wall_510050.py`、`opt_vertical_spread_backtest.py`、`rzye_industry_score_date.py`、`vol_price_screener.py`、`web_dashboard.py`、`大QMT实时行情.py`
- **测试**：`tests/test_daqmt_realtime_quote.py`、`tests/test_opt_wall_510050.py`
- **工具/文档/规则**：`scripts/p2_*.py`、`scripts/p3_*.py`、`scripts/p4_*.py`（只是加了 `用途/保留至` 头）、`AGENTS.md`、`.cursor/rules/rt-monitor-fina-arturn-layout.mdc`、`.planning/STATE.md`、`.planning/codebase/CONVENTIONS.md`、`docs/superpowers/specs/*.md`（2 个）、`vol_price_screener_损坏原因核查.md`、`.gitignore`

### 1.2 未跟踪但必须纳入（依赖闭包，16 个）
| 文件 | 被谁 import |
|---|---|
| `logger_config.py`、`windows_safe_log.py` | `app_factory.py`、`opt_wall_510050.py`、`opt_vertical_spread_backtest.py` |
| `database_pool.py`（⚠ 含本地默认口令 `123456`，见 §3） | `monolith.py`、`fina_arturn_queries.py` |
| `fund_adj.py`（⚠ **含明文 token**） | `monolith.py` |
| `first_qfq_new_high.py` | `services/sw_industry.py` |
| `opt_spread_board_select.py`、`option_iv.py` | `app_factory.py`、`opt_t_board.py`、`opt_iv_board.py` |
| `rt_k_代码分类.py` | `web_dashboard.py` |
| `rt_monitor/core/{market_session,response_compress,small_cache}.py` | `fina_arturn_queries.py`、`monolith.py`、`sw_industry.py` |
| `rt_monitor/services/{amount_extremes,opt_iv_board,opt_iv_windows,opt_sp_plan,sw_industry}.py` | `monolith.py`、`opt_t_board_bp.py`、`index_bp.py`、`app_factory.py` |

（验证：`python -X utf8 %TEMP%\import_closure.py`，按 AST 跟踪所有 import，包括函数内的延迟 import。）

### 1.3 未跟踪、本次三轮新增或改动，建议纳入
- 第 3 轮：`data_health_check.py`、`rt_monitor/CHANGELOG-static-ver.md`、`scripts/p5_extract_amount_extremes.py`、`tests/test_data_health_check.py`、`tests/test_data_health_latest_api.py`、`tests/test_fina_pre_ann_window.py`、`tests/test_static_ver_hash.py`、`opt_mins.py`（⚠ 含 token）、`.planning/COMMIT-CHECKLIST-20261004.md`、`.planning/MONOLITH-SPLIT-ROADMAP.md`
- 第 2 轮：`scripts/restore_archived_scripts.py`；改用 `windows_safe_log` 的 26 个根目录脚本（其中 15 个含 token，见 §3）
- 工程：`pyproject.toml`、`uv.lock`
- 守卫体系：`scripts/selfcheck_delivery.py`、`scripts/selftest_guard_effective.py`、`scripts/audit_scratch_retention.py`、`.planning/LESSONS.md`、`tests/` 下其余 `test_*.py`（约 60 个）
- `rt_monitor/` 下其余未跟踪模块（共 44 个，§1.2 只列出了被 import 链直接引用的部分；其余是 blueprint/service，app_factory 按名注册，也应一并纳入）

## 2. `.gitignore` 建议（**未应用**，只改了一处已有注释行的乱码，见 §5）

```gitignore
# 日志与轮转（根目录 155 个 *.log + 若干 *.log.2026-xx-xx / *.log.N）
*.log
*.log.*
# Python 缓存
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
# 运行时锁/进度/缓存
.rt_monitor_app.lock
*_progress.json
calculation_progress.json
*.pkl
scripts.db
.planning/scratch-meta.json
# 输出与临时目录
output/
out/
_tmp_*/
_tmp_*.json
tmp_*.json
*.out
local_stock_data/
breakout_signals/
features02/
# 模型产物
*.pth
*.h5
*.keras
# 密钥与本机配置
jinwq.env
*.env
config.json
cursor_settings.json
# 个人清单（已有）
fina_interest_marks.json*
opt_iv_perc_anchors.json*
# 第三方 AI 工具镜像目录（每个约 142 个文件，是同一份 skills 被不同工具复制的副本）
.mux/ .pi/ .mcpjam/ .neovate/ .openhands/ .pochi/ .vibe/ .zencoder/ .windsurf/ .qwen/ .qoder/
.trae/ .roo/ .kode/ .crush/ .augment/ .agent/ .continue/ .codebuddy/ .cline/ .commandcode/
.junie/ .kilocode/ .kiro/ .iflow/ .factory/ .adal/ .goose/ skills/
```
> 注：AI 工具目录在 `.gitignore` 里要一行一个（上面为了排版写在一行，落地时请拆开）。

## 3. 含密钥/口令、提交前必须处理的文件

**明文 Tushare token（17 个，都在「应纳入」范围内）**：
`fund_adj.py`（依赖闭包必需）、`opt_mins.py`、`etf宽基盘后数据入库.py`、`fut_basic.py`、`fut_daily.py`、`fut_mapping.py`、`fut_trade_cal.py`、`index_daily.py`、`index_sw_daily.py`、`limit_list_ths.py`、`margin_sum.py`、`opt_basic.py`、`opt_daily.py`、`report_rc.py`、`rt_sw_k.py`、`stk_surv.py`、`tusharert个股实时行情.py`

推荐改法（逐文件，同一模式）：
```python
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()
if not TUSHARE_TOKEN:
    raise SystemExit("缺少环境变量 TUSHARE_TOKEN")
```
复核命令（提交前跑一次，必须无输出）：
```powershell
git diff --cached --name-only | % { rg -l -e '\b[0-9a-f]{56}\b' -- $_ }
```

**本地默认口令 `123456`**：在 44 处出现（`database_pool.py`、`index_daily.py`、`scripts/_probe_*` 等）。jinwq.mdc 把它定为「本地开发参考默认值」，风险低；但仓库若会公开，建议同样改成只读环境变量。**需用户决定。**

**永不提交**：`jinwq.env`、`config.json`（含 token）、`mysql_*.cnf`（本机调参）。

## 4. 需用户决定的文件

| 文件/目录 | 现状 | 问题 |
|---|---|---|
| `.cursor/`（636）、`.claude/`（584）、`.agents/`（378） | 未跟踪；含项目规则 `jinwq.mdc`、项目 skills，也有大量第三方 skill | 只纳入 `.cursor/rules/` 和本项目自写的 skill（mytushare、myetfdate、myuiux、tushare-to-mysql、growth-stock-local）？还是全部忽略？ |
| `niusan_holder_config.json` | 未跟踪，牛散页面的方案配置 | 是个人持仓方案（应忽略），还是默认配置（应纳入）？ |
| `docs/`（104）、`.planning/`（107）、`growth_stock_local/`（97） | 未跟踪 | 文档/规划/子项目，是否进库 |
| 根目录 `*.md`（187）、`*.pdf`（158）、`*.csv`（190）、`*.xlsx`（13） | 未跟踪 | 研究笔记和数据导出，建议默认不进库，由你挑选 |
| 根目录其余约 500 个 `*.py` | 未跟踪 | 各类同步脚本；进库前同样要跑 §3 的 token 检查 |
| `自动化特征工程结果/`、`大涨前特征数据/`、`特征定义/` | 未跟踪 | 数据产物，建议忽略 |

## 5. 本次顺手修正（已做）
- `.gitignore` 第 8 行注释是第 2 轮用 PowerShell 追加时写坏的（UTF-8 中文被截断成 `?`，规则本身仍生效）；已按字节改写为 UTF-8：`# 冗余脚本归档（仅本机，可用 scripts/restore_archived_scripts.py 还原）`。`git check-ignore` 确认 `.planning/scratch-archive/` 仍被忽略。

## 6. 拆分提交建议

上次提交是 09-23；这 48 个已修改文件里叠加了 09-23 以来**多轮**工作（例如 `opt_t_board.py` +5252 行），**按轮次拆分只能用 `git add -p` 按代码块挑**，代价高、易错。建议**按领域/文件组拆**：

1. `chore: 完善 .gitignore（日志/缓存/输出/AI 工具目录/密钥/个人清单）`
2. `build: 纳入 pyproject.toml、uv.lock 与公共基础模块`（`logger_config.py`、`windows_safe_log.py`、`database_pool.py`、`rt_monitor/core/*`）
3. `feat(rt_monitor): 期权 T 板 / IV / 价差方案`（`opt_t_board*`、`opt_iv_*`、`opt_sp_plan`、`opt_spread_board_select.py`、`option_iv.py`、相关模板/JS/测试）
4. `feat(rt_monitor): 财报筛选页与申万行业`（`fina_*`、`sw_*`、`first_qfq_new_high.py`、`rt_monitor_fina.js`）
5. `perf(fina): 公告日前收盘价查询加 60 天窗口，冷建 40–53s→9–13s`（若第 4 步已含 `fina_arturn_queries.py`，并入第 4 步并在信息里注明）
6. `refactor(rt_monitor): 静态资源版本号改为内容哈希；历史注释迁入 CHANGELOG`
7. `refactor(monolith): P5.1 迁出成交额极值/均值查询到 services/amount_extremes`
8. `feat: data_health_check 数据新鲜度体检 + 市场状态页展示`
9. `refactor(scripts): 同步脚本统一 windows_safe_log，去除硬编码 token`（**必须在 §3 去 token 之后**）
10. `docs: AGENTS.md / 规则 / LESSONS / 规划文档`

每步提交前：`python -m pytest tests/ -q` + `python -X utf8 scripts/selfcheck_delivery.py` + §3 的 token 复核命令。
