# 需求说明：fina_indicator 扩展 — 股东人数与前十股东

## 目标

在 [fina_indicator.py](d:/cursor/tushareindex/fina_indicator.py) 中：

1. **stk_holdernumber**（[doc 166](https://tushare.pro/document/2?doc_id=166)）：新建 `stock_tushare.stk_holdernumber`，按公告日期区间分页拉取（单次≤3000），季度窗口映射报告期列表（与现有增量/全量季列表一致）。
2. **top10_holders / top10_floatholders**（[doc 61/62](https://tushare.pro/document/2?doc_id=61)）：两表类似逻辑；`ts_code` 必选 → 股票列表循环 + 报告期区间合并以减少调用。
3. **并行**：与现有 fina_indicator + income/balancesheet/cashflow/fina_mainbz 并行，扩展线程池（例如 8 workers）。
4. **模式**：支持历史全量（空表）与财报披露窗口增量（与非空表同一 `_periods_for_table` 规则）。
5. **CLI**：`ALL_SYNC_TABLE_NAMES`、`--truncate-and-full-sync`、`--skip-reference` 类开关；可选 reconcile 扩展。

## 约束

- Tushare 限速与 `fields` 显式传入、分页规则 A。
- MySQL 与项目 camelCase / 日志 / 时间戳约定（jinwq）。

## 非目标（首期）

- 监控页展示股东数据；若需另开里程碑。
