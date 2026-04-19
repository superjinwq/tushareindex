# Architecture

**Analysis Date:** 2026-04-19

## Pattern

- **脚本型数据管线**：根目录大量独立 `.py` 脚本负责 Tushare → MySQL 同步（全量/增量、分页、限速、`REPLACE`/UPSERT）。
- **单文件 Web 服务**：`rt_monitor_app.py` 为行情监控主入口（Flask + 可选 SocketIO），读 `stock_tushare` / `stock_wind`，不写行情业务表。
- **数据库双库**：`stock_tushare`（Tushare 落地）、`stock_wind`（Wind/实时等）；脚本内 `DB_CONFIG` / 环境变量切换。

## Layers（逻辑分层）

1. **数据源**：Tushare `pro_api`、 occasional Wind / yfinance（按脚本）。
2. **同步层**：`fina_indicator.py`（财务五表并行）、各类 `*_入库.py`、`*同步*.py`。
3. **存储层**：MySQL 8.4，SQLAlchemy / `pymysql` + 批量 `executemany`。
4. **展示层**：`templates/` + `static/`，`rt_monitor_app.py` 提供 REST/页面。

## Data flow（财务线示例）

- `fina_indicator_vip` 等按 `period` 拉全市场 → 去重 → `fina_indicator` / `income` 等表。
- 股东三表扩展（计划）：`stk_holdernumber` 按公告区间；`top10_*` 按 `ts_code` 循环 + 报告期区间。

## Entry points

- 财务同步：`python fina_indicator.py`（多线程阶段1 + 可选 reconcile）。
- 监控 Web：`python rt_monitor_app.py`（端口 `RT_MONITOR_PORT`）。

## Key files

- `fina_indicator.py`：财务五表 + 全局限流、并行 `ThreadPoolExecutor`。
- `rt_monitor_app.py`：大盘/ETF/涨跌停等 API 与页面（体量极大，职责只读展示）。
- `.cursor/rules/jinwq.mdc`：项目 Python/MySQL 约定（camelCase、日志、日期口径）。
