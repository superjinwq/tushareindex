# STATE

**Project:** tushareindex — shareholder reference sync  
**Last updated:** 2026-09-28

## Quick tasks completed

| ID / 目录 | 摘要 | 完成日 |
|-----------|------|--------|
| `quick/261002-grc-test-24-displayed-overseas-symbols-using` | 24个海外标的逐项验证：6个快照可读，18个OT返回ErrorId2；HTTP/原生DLL/Python均测试，3个香港指数观察到报价变化，延迟未证实 | 2026-10-02 |
| `quick/261001-n1m-persist-dedicated-chrome-for-testing-rul` | 个性化与项目指令明确 OpenCLI/Python 使用独立 Chrome for Testing；原文及换行校验通过，全仓检查存在行情基线失败 | 2026-10-01 |
| `quick/261001-ir3-tq-overseas-crude-connectivity-and-code-` | 独立TQ原油探针：CL00W候选无有效报价；SCL8.SHF国内原油主连缓存可读，实时更新未确认 | 2026-10-01 |
| `quick/260928-gm8-159915-oi-opt-chunk-qmt-rpc` | OI压力时非关键 OPT_CHUNK 有界让行；保留串行批量与可回退开关 | 2026-09-28 |
| `quick/20260909-tdx-idx-rt-idx-k` | TQ 写中证2000/北证50→rt_idx_k；盘中与监控放开四列/区间拼 rt | 2026-09-09 |
| `quick/260903-opt-meta-live-gap` | 拉链新增立刻补 optMeta；直播窗窄口 8 张真正接上，T 型能加新行 | 2026-09-03 |
| `quick/260903-heal-give-up` | 同一批 heal 3 轮无效改 poll-only，停止空退空订 | 2026-09-03 |
| `quick/260903-a7y-revision-stop-tick` | 期权：启动校桥接 revision；stop 后核对 tick 句柄；`OPT_HEALTH dead_push` | 2026-09-03 |
| `quick/260829-kt0-opt-t-board-a` | T 型同月标准/A：成交量、持仓量前三与平值分开标记 | 2026-08-29 |
| `quick/20260824-opt-iv-skew` | IV 中轴「斜」=沽−购；页头 588000 警示标明垂直价差；`RT_MONITOR_STATIC_VER` 20260824-3 | 2026-08-24 |
| `quick/20260824-opt-vol-guide` | opt-t-board IV 下、铁律上插入卖方读盘卡（IV/HV/Δ/VRP、IVR、三条路径）；`RT_MONITOR_STATIC_VER` 20260824-2 | 2026-08-24 |
| `quick/20260824-opt-iv-hv-axis` | IV 看板中轴 HV/Δ/HV Per + 弹窗随后 RV/历史 VRP；当月 DTE&lt;10 弱化；`RT_MONITOR_STATIC_VER` 20260824-1 | 2026-08-24 |
| `quick/20260815-sp-correct-open` | Sell Put 更正开仓：未平仓改张数/入场价并同步开仓流水；`RT_MONITOR_STATIC_VER` 20260815-2 | 2026-08-15 |
| `quick/20260807-stockstat-health-check` | 盘中脚本：名册/分市场/compindex 健康检查+漏标自愈；口径 todo 北证50/创业板指 vs index_daily | 2026-08-07 |
| `docs/plans/2026-08-07-cffex-index-opt-t-board` | opt-t-board 中金所股指期权 T型+启发式价差+IV八块；STATIC_VER 20260807-3 | 2026-08-07 |
| `quick/20260727-sw-hlc-popup-align` | 申万收盘价六列弹窗统一申万行业+突破类型；`RT_MONITOR_STATIC_VER` 20260727-1 | 2026-07-27 |
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
