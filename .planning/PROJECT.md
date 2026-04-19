# tushareindex — 股东参考表同步（GSD 增量里程碑）

## What This Is

本仓库为 **A 股/ETF 的 Tushare → MySQL 数据工程** 与 **Flask 行情监控** 的棕地项目。本里程碑在现有 [fina_indicator.py](d:/cursor/tushareindex/fina_indicator.py) 中 **新增股东人数、前十大股东、前十大流通股东** 三表同步，与当前财务五表 **同进程、同增量季窗口、并行线程池** 对齐。

## Core Value

**在不限爆 Tushare 的前提下，将三类股东参考数据可靠落库 `stock_tushare`，并与财务同步脚本共享同一套全量/增量与危险清表语义。**

## Requirements

### Validated

- ✓ 财务五表（`fina_indicator` + income / balancesheet / cashflow / `fina_mainbz`）VIP 按季全市场同步 — 现有 `fina_indicator.py`
- ✓ MySQL `stock_tushare` / `stock_wind` 双库、Tushare 全局限流与分页 — 见 `.planning/codebase/CONVENTIONS.md`
- ✓ 代码库映射 — `.planning/codebase/*.md`（2026-04-19）

### Active

- [ ] 新建并同步 `stk_holdernumber`（公告区间 + 分页 3000）
- [ ] 新建并同步 `top10_holders`、`top10_floatholders`（按票 + 报告期区间，UPSERT）
- [ ] 阶段1 并行池纳入三表（如 8 线程），保留可中断 `wait(timeout=0.5)` 语义
- [ ] `ALL_SYNC_TABLE_NAMES`、TRUNCATE 危险操作、CLI `--skip-reference`（或等价）与文档头注释更新
- [ ]（可选）条数 reconcile：至少 `stk_holdernumber` 按 `end_date` 对齐季

### Out of Scope

- rt_monitor 页面展示股东表 — 非本里程碑必须
- 替代 Wind / 巨潮 数据源 — 仍只用 Tushare 接口

## Context

- 需求摘要：[@.planning/IDEA-shareholder-sync.md](d:/cursor/tushareindex/.planning/IDEA-shareholder-sync.md)
- 技术映射：[@.planning/codebase/ARCHITECTURE.md](d:/cursor/tushareindex/.planning/codebase/ARCHITECTURE.md)
- Tushare 文档：股东人数、前十大股东、前十大流通股东（积分与必选参数差异见 CONCERNS）

## Constraints

- **API**：Tushare 500 次/分、`top10_*` 2000 积分起；`stk_holdernumber` 单次 3000 行。
- **工程**：单文件尽量扩展 `fina_indicator.py`，遵循 jinwq camelCase 与日志约定。
- **时间**：`top10` 全量按票循环可能极耗时，需日志进度与可中断。

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| 棕地扩展 `fina_indicator.py` 而非新建三脚本 | 与用户要求一致、共享季窗口与线程池 | — Pending |
| 股东人数不按 VIP `period`，而按公告 `start_date`/`end_date` | 接口无 period 全市场语义 | — Pending |
| top10 按 ts_code + 合并报告期区间 | 减少 API 次数 | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-19 after GSD new-project (brownfield + IDEA-shareholder-sync)*
