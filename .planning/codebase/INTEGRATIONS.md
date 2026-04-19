# External Integrations

**Analysis Date:** 2026-04-19

## APIs & External Services

**行情数据 - Tushare Pro（核心）:**
- Tushare Pro REST API - 股票、ETF、指数、财务、融资融券等全量历史数据
  - SDK/Client: `tushare` 1.4.24，`ts.pro_api(token)`
  - Auth: 环境变量 `TUSHARE_TOKEN`（必填）
  - 限流策略: 每分钟约 500 次调用上限，`_wait_tushare_api_slot` 全局令牌桶；增量场景 `max_workers=5`，`time.sleep(0.2)`
  - 主要接口: `fina_indicator_vip`、`income`、`balancesheet`、`cashflow`、`fina_mainbz`、`stk_factor_pro`、`index_daily`、`fund_nav`、`margin`、`limit_list_ths` 等
  - 使用文件: `fina_indicator.py`、`backfill_tushare_from_2017.py`、`etf_daily.py`、`main_sync.py`、`tushare_client.py` 等几乎全部同步脚本

**行情数据 - Wind 金融终端:**
- WindPy API - A 股/期货/ETF 分钟级实时行情、指数权重等
  - SDK/Client: `WindPy`（`from WindPy import w; w.start()`），需本地安装 Wind 客户端
  - Auth: Wind 账号登录（无额外 Token），本地 COM 接口
  - 主要接口: `w.wsd()`（历史日线）、`w.wsq()`（实时快照）、`w.edb()`
  - 使用文件: `wind1min.py`、`windrt实时行情.py`、`windrt1min_today.py`、`indexwind全A.py`、`indexwind表获取主要指数wind数据源.py`、`import_missing_dates_windpy.py`、`verify_missing_dates_windpy.py`、`wind实时分钟合并.py` 等

**财报公告 - 巨潮资讯网:**
- 巨潮 HTTP API - 上市公司公告全文抓取
  - SDK/Client: `requests` 直接调用
  - Auth: 无 Token，模拟浏览器请求头（`Referer: https://www.cninfo.com.cn/`）
  - Endpoints:
    - 查询接口: `https://www.cninfo.com.cn/new/hisAnnouncement/query`（POST）
    - 静态文件: `https://static.cninfo.com.cn/` + PDF 路径
  - 使用文件: `cninfo_ann_glm_digest.py`

**AI 分析 - 智谱 GLM:**
- 智谱开放平台 Chat Completion API - 公告 PDF 文本摘要与分析
  - SDK/Client: `requests` 直接调用
  - Auth: 环境变量 `ZHIPU_API_KEY`
  - Endpoint: `https://open.bigmodel.cn/api/paas/v4/chat/completions`（POST）
  - 使用文件: `cninfo_ann_glm_digest.py`

**本地行情文件 - 通达信/mootdx:**
- 通达信本地数据文件读取 - 日线/分钟 K 线数据离线导入
  - SDK/Client: `pytdx` 0.11.7（`TdxHq_API`、`TdxDailyBarReader`）、`mootdx`（`Reader`）
  - Auth: 无（本地文件访问）
  - 数据目录: 通达信安装目录下的 `vipdoc/` 等
  - 使用文件: `V300C0_日线导出_pytdx_mootdx.py`、`通达信数据整合工具.py`

## Data Storage

**Databases:**
- MySQL 8.4.5（主存储，本地）
  - 数据库1: `stock_tushare` - Tushare 来源数据（行情、财务、ETF、融资融券等）
    - 主要表: `stk_factor_pro`、`stk_factor_ma`、`fina_indicator`、`income`、`balancesheet`、`cashflow`、`fina_mainbz`、`etf_daily`、`etf_stk_mins`、`etf_stk_mins_ma`、`etf_rt_min`、`etf_bias_realtime`、`margin_daily`、`limit_list_ths`、`index_daily`、`fund_nav`、`bak_basic` 等
  - 数据库2: `stock_wind` - Wind 来源数据（指数成分、实时行情快照、分钟线）
    - 主要表: `stockstat`、`rt_k`、`wind1min`、`index_stat`、`indexwind` 等
  - Connection: `DB_HOST`（默认 localhost），`DB_PORT`（默认 3306），`DB_USER`（默认 root），`DB_PASSWORD`（默认 123456），`DB_NAME`（双库切换）
  - Client: `pymysql` 1.4.6（主驱动）、`mysql-connector-python`（部分脚本）、`SQLAlchemy` 2.0.47（`create_engine` + `to_sql` 批量写入）
  - 连接池: `database_pool.py` 提供自建连接池（Queue-based，`pool_size=10`，`max_overflow=20`）；`config.json` 中 `pool_recycle=40`

**File Storage:**
- 本地文件系统（无云存储）
  - `logs/`、`*.log` - 各脚本运行日志（`TimedRotatingFileHandler`，3 天轮转）
  - `etf_rt_min_history_cache.pkl` - ETF 实时分钟历史缓存（pickle）
  - `ma_sr_levels.json` - MA 支撑阻力价位缓存
  - `calculation_progress.json` - 批量计算进度断点续跑
  - `data_cache/` - 部分脚本的临时数据缓存目录
  - 训练好的模型文件: `best_model.h5`、`best_model.keras`、`best_pytorch_model.pth`

**Caching:**
- pickle 文件缓存（`etf_rt_min_history_cache.pkl`）- ETF 实时分钟历史数据
- MySQL 内存表（`innodb_buffer_pool_size=4096M`）- 作为查询缓冲层
- 无 Redis 或 Memcached

## Authentication & Identity

**Auth Provider:**
- 无用户认证系统
- Tushare: 环境变量 Token（`TUSHARE_TOKEN`）
- Wind: 本地 Wind 客户端账号（COM 接口无 Token）
- 智谱 GLM: 环境变量 Key（`ZHIPU_API_KEY`）
- Web 服务无登录/鉴权，直接局域网访问

## Monitoring & Observability

**Error Tracking:**
- 无第三方错误追踪（Sentry 等）

**Logs:**
- 每个脚本对应一个同名 `.log` 文件，放在项目根目录
- 格式: `时间 | 级别 | 线程名 | 正文`
- 轮转: `TimedRotatingFileHandler`，`when='midnight'`，`backupCount=3`
- `logger_config.py` 提供 `get_logger()` 统一初始化（部分脚本自行配置）
- 控制台与文件同步输出同格式日志

## CI/CD & Deployment

**Hosting:**
- 本地 Windows 11 工作站

**CI Pipeline:**
- 无 CI/CD（无 GitHub Actions、Jenkins 等）

**定时调度:**
- 手动执行或 `.bat` 脚本（`run_daily.ps1`、`数据更新（每日）.bat`、`ETF基金（早上）.bat` 等）
- `script_manager.py`（`启动脚本管理器.bat`）- 批量管理多个同步脚本的 GUI 工具

## Environment Configuration

**Required env vars:**
- `TUSHARE_TOKEN` - Tushare Pro API Token（**必填**，缺失时抛出明确 RuntimeError）
- `ZHIPU_API_KEY` - 智谱 GLM API Key（`cninfo_ann_glm_digest.py` 使用时必填）

**Optional env vars:**
- `DB_HOST` - MySQL 主机（默认 localhost）
- `DB_PORT` - MySQL 端口（默认 3306）
- `DB_USER` - MySQL 用户（默认 root）
- `DB_PASSWORD` - MySQL 密码（默认 123456）
- `DB_NAME` - 目标数据库（stock_tushare 或 stock_wind）
- `RT_MONITOR_PORT` - Web 服务端口（默认 5000）
- `RT_MONITOR_AUTO_KILL_PREVIOUS` - 是否清理旧进程（默认 0）
- `RT_MONITOR_API_PREFIX` - 反代路径前缀

**Secrets location:**
- `jinwq.env` - 本地开发默认值（含明文 Token，**不提交至公开仓库**）
- `config.json` - 含 Tushare Token 和数据库密码（**不提交至公开仓库**）
- 部分旧脚本（如 `backfill_tushare_from_2017.py`）将 Token 硬编码为常量 `TUSHARE_TOKEN = "..."` - 应迁移为环境变量

## Webhooks & Callbacks

**Incoming:**
- 无（不接收外部 Webhook）

**Outgoing:**
- 无定义的 Webhook 推送

## Frontend & Web Interface

**Web 监控服务（`rt_monitor_app.py`）:**
- Flask + SocketIO 提供实时行情监控页
- 前端模板: `templates/rt_monitor.html`、`templates/rt_monitor_fina.html` 等
- 客户端 JS: `static/js/rt_monitor.js`、`static/js/dashboard.js`（自定义）
- 图表库（vendor）:
  - `chart.umd.min.js` - Chart.js（K 线/折线图）
  - `chart-stack.umd.js` - Chart.js 堆叠插件
  - `chartjs-plugin-zoom.min.js` - 缩放插件
  - `hammer.min.js` - 手势支持
- CORS: `cors_allowed_origins="*"`（无跨域限制）
- 启用 `TEMPLATES_AUTO_RELOAD`（开发模式热重载）

---

*Integration audit: 2026-04-19*
