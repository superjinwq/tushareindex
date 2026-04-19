# Research Summary — Shareholder / Top10 data sync (Tushare → MySQL)

**Date:** 2026-04-19  
**Sources:** `.planning/codebase/STACK.md`, `INTEGRATIONS.md`, `CONCERNS.md`, Tushare 接口说明（股东人数 doc166；前十 doc61/62）。

## Stack（与本里程碑相关）

- Python 3.12、pandas、tushare、`pro.query` 分页、`SQLAlchemy`/批量写入 MySQL。
- 限速：全局限流 ≈500 次/分钟；线程并行主要用于「各表独立同步」，API 仍串行令牌桶。

## Table stakes（必须做对）

- 分页直至 `< limit`，防止静默截断。
- `fields` 显式列出文档全部输出列。
- NaN → None；日期统一 `date`；唯一键与 UPSERT 一致。
- `stk_holdernumber` **page size 3000**（非 5000）。

## Domain-specific

- **stk_holdernumber**：无 `period` 全市场 VIP；用 **公告起止** 过滤；可选不传 `ts_code` 需联调验证，否则按票降级。
- **top10_***：`ts_code` **必选**；用 **报告期 start/end** 覆盖多季以减少每票调用次数；全量可能极慢。
- **积分**：top10 接口需较高积分档位。

## Pitfalls

- 年报披露跨年：**仅用日历季映射公告窗可能漏披露**，增量需 lookback 或与披露日历对齐（见 fina 脚本 `suggest_anchor_and_also_periods`）。
- 超大文件：`fina_indicator.py` 继续增大时保持函数拆分与单一职责。

## Recommendation

优先实现 **stk_holdernumber**（全市场区间若可用）→ 再 **top10 双表**（按票循环 + 区间）；并行线程与 `ALL_SYNC_TABLE_NAMES` 一并交付。
