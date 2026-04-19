# Requirements — Shareholder reference sync milestone

## v1 Requirements

### Data — stk_holdernumber

- [ ] **SHARE-01**: 在 `stock_tushare` 中存在表 `stk_holdernumber`，结构与 Tushare 输出一致（含审计列），唯一键覆盖 `(ts_code, ann_date, end_date)` 或等价防重复。
- [ ] **SHARE-02**: 可按 **公告日期区间** 分页拉取接口 `stk_holdernumber`，单次上限 3000，直至取完。
- [ ] **SHARE-03**: 同步使用的「季列表」与 `fina_indicator` 一致：**空表全历史季；非空表仅增量窗口**。

### Data — top10

- [ ] **TOP10-01**: 表 `top10_holders`、`top10_floatholders` 自动建表、UPSERT，唯一键至少区分 `(ts_code, end_date, holder_name)`（若样本需含 `ann_date` 则并入）。
- [ ] **TOP10-02**: 从本地 `stock_basic`（或降级 API）取股票列表，对每只股票按 **合并后的报告期区间** 调用接口，减少调用次数。
- [ ] **TOP10-03**: 分页与区间切段：若单票全历史超过上限，按年或子区间拆分且不丢数据。

### Orchestration

- [ ] **ORCH-01**: 阶段1 并行线程包含原五表 + 三新表（目标例如 8 workers），每线程内 API 仍经全局限流；`wait(..., timeout=0.5)` 可中断。
- [ ] **ORCH-02**: `ALL_SYNC_TABLE_NAMES` 含三新表；`--truncate-and-full-sync` 可清空并重拉（与现双确认一致）。
- [ ] **ORCH-03**: 提供 `--skip-reference`（或等价）仅跳过三参考表同步。

### Quality

- [ ] **QUAL-01**: 文件头注释、日志、末尾 `# 代码更新时间` 符合仓库规则。
- [ ] **QUAL-02**: （可选）`stk_holdernumber` 按季条数 reconcile；top10 首期允许文档声明延后。

## v2 / Later

- [ ] rt_monitor 或其它消费者对三表的只读 API/页面。
- [ ] top10 全量条数核对与方案 B 修剪（与现五表 reconcile 对齐）。

## Out of scope

- 非 Tushare 来源的股东数据补全。
- 多进程分布式抢 Tushare（保持单进程全局限流）。

## Traceability

| REQ ID   | ROADMAP Phase |
|----------|----------------|
| SHARE-01..03 | Phase 1 |
| TOP10-01..03 | Phase 2 |
| ORCH-01..03, QUAL-01 | Phase 3 |
| QUAL-02 | Phase 4（可选） |

---
*Generated: 2026-04-19 — GSD new-project (lean research + IDEA doc)*
