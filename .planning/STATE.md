# STATE

**Project:** tushareindex — shareholder reference sync  
**Last updated:** 2026-05-03

## Quick tasks completed

| ID / 目录 | 摘要 | 完成日 |
|-----------|------|--------|
| `quick/20260503-niusan-history-by-scheme-id` | 牛散「+」历史/明细：API 按 `scheme_id` 关联人（非仅 config activeId）；前端主表 `niusanViewSchemeId` + 缓存键；`RT_MONITOR_STATIC_VER` 20260503-21 | 2026-05-03 |
| `quick/20260503-rt-monitor-niusan-expand-btn-fix` | 牛散页「+」展开：`click` 委托内 target 归一化（Text 节点无 `closest`）；`RT_MONITOR_STATIC_VER` 20260503-20 | 2026-05-03 |
| `quick/20260503-fina-stk-holdernumber-columns` | 财报页固定资产右侧「股东人数」hnchg+hn0–hn8（stk_holdernumber）；缓存 sb_act_7；RT_MONITOR_STATIC_VER 20260503-19 | 2026-05-03 |
| `quick/20260503-fina-free-float-ratio` | 财报页「自由流通比」列（前三持股右侧，紧凑 loc-col；free_mv/total_mv×100）；缓存 sb_act_6；RT_MONITOR_STATIC_VER 20260503-18 | 2026-05-03 |
| `quick/20260503-fina-top3-holders-ent-type` | 财报页前三持股+企业性质（top10_holders / act_ent_type）、明细弹窗 localStorage；RT_MONITOR_STATIC_VER 20260503-16 | 2026-05-03 |
| `quick/20260501-sw-ycf-yoy` | 申万页经营现金流 ycf* 同比增速（口径对齐营收 y*）、批量键含同比、缓存 x5、VER 83 | 2026-05-01 |
| `quick/20260430-sw-extend-five-metrics` | 申万页 nine 季五组扩展指标、缓存键 x1、`swColumnMeta`、`RT_MONITOR_STATIC_VER` 77 | 2026-04-30 |
| `quick/260426-v8e-v0-8-income` | 财报表：营业利润同比右侧 `v0..v8`（利润增速−收入增速百分点）前后端与 CSS、`RT_MONITOR_STATIC_VER` 20260426-18 | 2026-04-26 |
| `quick/260427-fa-frozen-name-modal` | 财报表冻结列「股票名称」与 f0 同开固定资产速览；`#fina-arturn-tbody-scroll` 单委托；`RT_MONITOR_STATIC_VER` 20260426-20 | 2026-04-27 |
| `quick/260427-m-gpm-high-qoq` | 财报表单季度毛利率新增 `m_high` 新高与 `m_qoq` 环比连增（0..8）；前后端筛选排序与表头同步；`RT_MONITOR_STATIC_VER` 20260427-21 | 2026-04-27 |
| `quick/260427-m-gpm-fix-display-align` | 修复单季度毛利率新增列：`m_high/m_qoq` 的 0 值显示与表头分组竖线错位；`RT_MONITOR_STATIC_VER` 20260427-22 | 2026-04-27 |
| `quick/260427-nt-high-qoq-columns` | 财报表单季度净利率/ROE(扣非)新增新高与环比两列（n/t 组），整数显示并修正表头分组边界；`RT_MONITOR_STATIC_VER` 20260427-23 | 2026-04-27 |
| `quick/260428-sw-revenue-yoy-cohort` | 申万行业页：单季营收同比增速（相对上年同季），披露期仅本季已披露且上年同季可拆单季的股票入池；`RT_MONITOR_STATIC_VER` 20260428-34 | 2026-04-28 |

## Current phase

- **Active:** Phase 1 — `stk_holdernumber` 设计与落地（见 ROADMAP）
- **Next action:** `/gsd-discuss-phase 1` 或 `/gsd-plan-phase 1`（实现细节与接口探针）

## Blockers

- 无（待 Phase 1 联调：无 `ts_code` 是否全市场拉取）

## Notes

- GSD `new-project` 以棕地方式初始化；代码库映射见 `.planning/codebase/`。
- `workflow._auto_chain_active`: true（config）
